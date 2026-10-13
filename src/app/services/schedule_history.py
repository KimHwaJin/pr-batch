"""Compressed history of schedule occurrences that were not run."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.schedule_clock import EPSILON, ScheduleClock
from app.models import Batch, BatchConfig, SkippedWindow
from app.schemas.schedule import Schedule


def record_skipped(
    session: Session,
    batch: Batch,
    config: BatchConfig,
    start: datetime,
    until: datetime,
    reason: str,
    now: datetime,
) -> None:
    if config.schedule is None:
        return
    clock = ScheduleClock(Schedule.model_validate(config.schedule))
    count = clock.count(start, until - EPSILON)
    if count:
        session.add(
            SkippedWindow(
                batch_id=batch.id,
                config_number=config.number,
                from_at=start,
                until_at=until,
                occurrences=count,
                reason=reason,
                created_at=now,
            )
        )
