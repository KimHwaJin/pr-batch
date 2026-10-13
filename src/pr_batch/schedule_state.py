"""Schedule pointer changes and compressed omission history under row locks."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pr_batch.models import Batch, BatchConfig, SkippedWindow
from pr_batch.schedule_clock import EPSILON, ScheduleClock
from pr_batch.schemas import Schedule


def database_now(session: Session) -> datetime:
    result = session.scalar(select(func.clock_timestamp()))
    assert isinstance(result, datetime)
    return result


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
