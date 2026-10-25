"""Schedule admission: turns due occurrences into runs, exactly once.

Every replica runs this loop; `FOR UPDATE SKIP LOCKED` on the batch row
guarantees one admission per occurrence without leader election.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import SCHEDULER
from app.db.session import database_now
from app.domain.schedule_clock import ScheduleClock
from app.models import Batch
from app.schemas.schedule import CatchUpMisfire
from app.services.common import current_config, policy_of, schedule_of
from app.services.run_service import admit_run
from app.services.schedule_history import record_skipped


class Scheduler:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def tick(self, *, now: datetime | None = None) -> bool:
        """Handle one batch atomically; explicit time is for tests."""
        with self.sessions.begin() as session:
            now = now or database_now(session)
            batch = session.scalar(
                select(Batch)
                .where(
                    Batch.enabled.is_(True),
                    Batch.next_run_at <= now,
                )
                .order_by(Batch.next_run_at, Batch.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if batch is None:
                return False
            config = current_config(session, batch)
            schedule = schedule_of(config)
            if schedule is None:
                # Enabling and editing reject this; stop rather than spin.
                batch.next_run_at = None
                return True
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
                        now
                        - timedelta(seconds=policy.misfire.max_age_seconds),
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
            return True

    def next_due(self) -> datetime | None:
        with self.sessions() as session:
            return session.scalar(
                select(func.min(Batch.next_run_at)).where(
                    Batch.enabled.is_(True)
                )
            )
