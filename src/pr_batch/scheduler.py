"""Durable schedule admission. Executor dispatch remains a separate worker."""

import argparse
import logging
import signal
import threading
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from pr_batch.errors import ServiceError
from pr_batch.models import Batch, SkippedWindow
from pr_batch.runs import admit_run
from pr_batch.schedule_clock import EPSILON, ScheduleClock
from pr_batch.schedule_state import database_now, record_skipped
from pr_batch.schemas import ScheduleConfig
from pr_batch.service import (
    MutationResult,
    RegistrationService,
    batch_view,
    check_revision,
    current_schedule,
    owned_batch,
)
from pr_batch.settings import Settings

logger = logging.getLogger(__name__)


class SkippedView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    schedule_number: int
    from_at: datetime
    until_at: datetime
    occurrences: int
    reason: str
    created_at: datetime


class SkippedPage(BaseModel):
    items: list[SkippedView]
    next_cursor: UUID | None


class SchedulingService:
    def __init__(self, registration: RegistrationService) -> None:
        self.registration = registration

    def set_enabled(
        self,
        owner: str,
        batch_id: UUID,
        key: str,
        expected: int,
        enabled: bool,
        allowed_kernels: list[str],
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch, expected)
            schedule = current_schedule(session, batch)
            if batch.enabled != enabled:
                now = database_now(session)
                if enabled:
                    if schedule.kernel_profile not in allowed_kernels:
                        raise ServiceError(
                            422, "UNKNOWN_KERNEL", "Kernel is not allowed."
                        )
                    clock = ScheduleClock(
                        ScheduleConfig.model_validate(schedule.config)
                    )
                    following = clock.find(now)
                    if following is None:
                        raise ServiceError(
                            422,
                            "NO_FUTURE_OCCURRENCE",
                            "Choose a schedule with a future occurrence.",
                        )
                    batch.next_run_at = following
                else:
                    if batch.next_run_at and batch.next_run_at <= now:
                        record_skipped(
                            session,
                            batch,
                            schedule,
                            batch.next_run_at,
                            now + EPSILON,
                            "DISABLED",
                            now,
                        )
                    batch.next_run_at = None
                batch.enabled = enabled
                batch.revision += 1
                batch.updated_at = now
            return 200, batch_view(batch, schedule).model_dump(mode="json")

        return self.registration.mutate(
            owner,
            f"activation:{batch_id}",
            key,
            {"enabled": enabled, "expected_revision": expected},
            action,
        )

    def skipped(
        self,
        owner: str,
        batch_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> SkippedPage:
        with self.registration.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(SkippedWindow).where(
                SkippedWindow.batch_id == batch_id
            )
            if cursor:
                last = session.scalar(query.where(SkippedWindow.id == cursor))
                if last is None:
                    raise ServiceError(
                        422, "INVALID_CURSOR", "Invalid cursor."
                    )
                query = query.where(
                    tuple_(SkippedWindow.created_at, SkippedWindow.id)
                    < tuple_(last.created_at, last.id)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        SkippedWindow.created_at.desc(),
                        SkippedWindow.id.desc(),
                    ).limit(limit + 1)
                )
            )
            items = [SkippedView.model_validate(row) for row in rows[:limit]]
            return SkippedPage(
                items=items,
                next_cursor=(items[-1].id if len(rows) > limit else None),
            )


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
            schedule = current_schedule(session, batch)
            config = ScheduleConfig.model_validate(schedule.config)
            clock = ScheduleClock(config)
            cursor = batch.next_run_at
            assert cursor is not None
            cutoff = now - timedelta(seconds=config.misfire_grace_seconds)
            if cursor < cutoff:
                recovered = []
                count = 0
                lower = cursor
                if config.misfire_policy == "LATEST":
                    count = 1
                elif config.misfire_policy == "CATCH_UP":
                    count = config.catch_up_max_runs
                    lower = max(
                        lower,
                        now
                        - timedelta(seconds=config.catch_up_max_age_seconds),
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
                    schedule,
                    cursor,
                    until,
                    f"MISFIRE_{config.misfire_policy}",
                    now,
                )
                for scheduled_for in recovered:
                    admit_run(session, batch, schedule, scheduled_for)
                cursor = clock.find(cutoff, inclusive=True)
            # Bound timely admissions too: a long grace period is not an
            # excuse for unbounded row creation in a single transaction.
            for _ in range(100):
                if cursor is None or cursor > now:
                    break
                admit_run(session, batch, schedule, cursor)
                cursor = clock.find(cursor)
            batch.next_run_at = cursor
            return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if settings.executor_base_url is None:
        parser.error(
            "Set BATCH_EXECUTOR_BASE_URL before starting the scheduler."
        )
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=10,
        connect_args={"connect_timeout": 5},
    )
    scheduler = Scheduler(sessionmaker(engine, expire_on_commit=False))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    try:
        while not stop.is_set():
            try:
                busy = scheduler.tick()
            except SQLAlchemyError:
                if args.once:
                    raise
                logger.error("Scheduler database unavailable; retrying.")
                busy = False
            if args.once:
                break
            if not busy:
                stop.wait(settings.scheduler_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
