"""Durable schedule admission process; Executor dispatch is separate."""

import argparse
import logging
import signal
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import create_db_engine, create_sessions, database_now
from app.domain.schedule_clock import ScheduleClock
from app.models import Batch
from app.services.common import current_config, policy_of, schedule_of
from app.services.run_service import admit_run
from app.services.schedule_history import record_skipped

logger = logging.getLogger(__name__)


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
            cutoff = now - timedelta(seconds=policy.misfire_grace_seconds)
            if cursor < cutoff:
                recovered = []
                count = 0
                lower = cursor
                if policy.misfire == "LATEST":
                    count = 1
                elif policy.misfire == "CATCH_UP":
                    count = policy.catch_up_max_runs
                    lower = max(
                        lower,
                        now
                        - timedelta(seconds=policy.catch_up_max_age_seconds),
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
                    f"MISFIRE_{policy.misfire}",
                    now,
                )
                for scheduled_for in recovered:
                    admit_run(session, batch, config, scheduled_for)
                cursor = clock.find(cutoff, inclusive=True)
            # Bound timely admissions too: a long grace period is not an
            # excuse for unbounded row creation in a single transaction.
            for _ in range(100):
                if cursor is None or cursor > now:
                    break
                admit_run(session, batch, config, cursor)
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
    engine = create_db_engine(settings)
    scheduler = Scheduler(create_sessions(engine))
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
