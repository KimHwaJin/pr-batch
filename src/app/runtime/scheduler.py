"""Schedule admission: turns due occurrences into runs, exactly once.

Every replica runs this loop; `FOR UPDATE SKIP LOCKED` on the batch row
guarantees one admission per occurrence without leader election.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import SCHEDULER
from app.db.session import database_now
from app.domain.schedule_clock import EPSILON, ScheduleClock
from app.models import Batch
from app.schemas.schedule import CatchUpMisfire
from app.services.common import (
    current_config,
    policy_of,
    reset_schedule_failures,
    schedule_of,
)
from app.services.run_service import admit_run
from app.services.schedule_history import record_skipped

logger = logging.getLogger(__name__)

# Recorded for occurrences given up after repeated admission failures.
SCHEDULER_ERROR = "SCHEDULER_ERROR"


class Scheduler:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        retry_seconds: int = 300,
        retry_limit: int = 3,
    ) -> None:
        self.sessions = sessions
        self.retry_seconds = retry_seconds
        self.retry_limit = retry_limit

    def tick(self, *, now: datetime | None = None) -> bool:
        """Handle one batch atomically; explicit time is for tests.

        A batch whose admission raises is set aside until its retry time
        (see record_failure), so the loop moves on to the other batches;
        the error is re-raised for the runtime loop to log.
        """
        failed: UUID | None = None
        try:
            with self.sessions.begin() as session:
                now = now or database_now(session)
                batch = session.scalar(
                    select(Batch)
                    .where(
                        Batch.enabled.is_(True),
                        Batch.next_run_at <= now,
                        or_(
                            Batch.schedule_retry_at.is_(None),
                            Batch.schedule_retry_at <= now,
                        ),
                    )
                    .order_by(Batch.next_run_at, Batch.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if batch is None:
                    return False
                failed = batch.id
                self.admit_due(session, batch, now)
                reset_schedule_failures(batch)
                return True
        except (OperationalError, InterfaceError):
            # The database is unreachable: not this batch's fault.
            raise
        except Exception:
            if failed is not None and now is not None:
                self.record_failure(failed, now)
            raise

    def record_failure(self, batch_id: UUID, now: datetime) -> None:
        """Retry the batch later; after retry_limit retries skip the due
        occurrences and move on to the next one."""
        with self.sessions.begin() as session:
            batch = session.scalar(
                select(Batch)
                .where(
                    Batch.id == batch_id,
                    Batch.enabled.is_(True),
                    Batch.next_run_at.is_not(None),
                )
                .with_for_update(skip_locked=True)
            )
            if batch is None:
                return
            batch.schedule_failures += 1
            if batch.schedule_failures <= self.retry_limit:
                batch.schedule_retry_at = now + timedelta(
                    seconds=self.retry_seconds
                )
                logger.warning(
                    "Schedule admission failed batch_id=%s attempt=%d "
                    "retry_in_seconds=%d",
                    batch_id,
                    batch.schedule_failures,
                    self.retry_seconds,
                )
                return
            logger.error(
                "Schedule admission given up batch_id=%s attempts=%d "
                "action=skip_due_occurrences",
                batch_id,
                batch.schedule_failures,
            )
            self.skip_due(session, batch, now)

    def skip_due(self, session: Session, batch: Batch, now: datetime) -> None:
        cursor = batch.next_run_at
        assert cursor is not None
        following = None
        try:
            # A savepoint: the batch may be broken in a way that makes
            # this fail too; it must not undo the pointer change below.
            with session.begin_nested():
                config = current_config(session, batch)
                schedule = schedule_of(config)
                if schedule is not None:
                    following = ScheduleClock(schedule).find(now)
                    record_skipped(
                        session,
                        batch,
                        config,
                        cursor,
                        now + EPSILON,
                        SCHEDULER_ERROR,
                        now,
                        SCHEDULER,
                    )
        except Exception:
            # No next occurrence can be computed: stop scheduling the
            # batch (as for a missing schedule) rather than spin on it.
            logger.exception(
                "Schedule stopped batch_id=%s "
                "reason=next_occurrence_unavailable",
                batch.id,
            )
            following = None
        batch.next_run_at = following
        reset_schedule_failures(batch)

    def admit_due(self, session: Session, batch: Batch, now: datetime) -> None:
        config = current_config(session, batch)
        schedule = schedule_of(config)
        if schedule is None:
            # Enabling and editing reject this; stop rather than spin.
            batch.next_run_at = None
            return
        policy = policy_of(config)
        clock = ScheduleClock(schedule)
        cursor = batch.next_run_at
        assert cursor is not None
        cutoff = now - timedelta(seconds=policy.misfire.grace_seconds)
        due: list[datetime] = []
        if cursor < cutoff:
            recovered = []
            count = 0
            lower = cursor
            if policy.misfire.mode == "LATEST":
                count = 1
            elif isinstance(policy.misfire, CatchUpMisfire):
                count = policy.misfire.max_runs
                lower = max(
                    lower,
                    now - timedelta(seconds=policy.misfire.max_age_seconds),
                )
            previous = clock.find(cutoff, reverse=True)
            for _ in range(count):
                if previous is None or previous < lower:
                    break
                recovered.append(previous)
                previous = clock.find(previous, reverse=True)
            recovered.reverse()
            until = recovered[0] if recovered else cutoff
            record_skipped(
                session,
                batch,
                config,
                cursor,
                until,
                f"MISFIRE_{policy.misfire.mode}",
                now,
                SCHEDULER,
            )
            due.extend(recovered)
            cursor = clock.find(cutoff, inclusive=True)
        # Bound timely admissions too: a long grace period is not an
        # excuse for unbounded row creation in a single transaction.
        for _ in range(100):
            if cursor is None or cursor > now:
                break
            due.append(cursor)
            cursor = clock.find(cursor)
        if policy.overlap.mode == "REPLACE" and len(due) > 1:
            # Each admission would cancel the previous one: run only
            # the latest and record the rest as skipped.
            record_skipped(
                session,
                batch,
                config,
                due[0],
                due[-1],
                "REPLACED_BY_LATEST",
                now,
                SCHEDULER,
            )
            due = due[-1:]
        for scheduled_for in due:
            admit_run(session, batch, config, SCHEDULER, scheduled_for)
        batch.next_run_at = cursor

    def next_due(self) -> datetime | None:
        with self.sessions() as session:
            # GREATEST ignores a NULL retry time.
            return session.scalar(
                select(
                    func.min(
                        func.greatest(
                            Batch.next_run_at, Batch.schedule_retry_at
                        )
                    )
                ).where(Batch.enabled.is_(True))
            )
