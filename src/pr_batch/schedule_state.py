"""Schedule pointer changes and compressed omission history under row locks."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pr_batch.models import Batch, ScheduleRevision, SkippedWindow
from pr_batch.schedule_clock import EPSILON, ScheduleClock
from pr_batch.schemas import ScheduleConfig


def database_now(session: Session) -> datetime:
    result = session.scalar(select(func.clock_timestamp()))
    assert isinstance(result, datetime)
    return result


def record_skipped(
    session: Session,
    batch: Batch,
    schedule: ScheduleRevision,
    start: datetime,
    until: datetime,
    reason: str,
    now: datetime,
) -> None:
    clock = ScheduleClock(ScheduleConfig.model_validate(schedule.config))
    count = clock.count(start, until - EPSILON)
    if count:
        session.add(
            SkippedWindow(
                batch_id=batch.id,
                schedule_number=schedule.number,
                from_at=start,
                until_at=until,
                occurrences=count,
                reason=reason,
                created_at=now,
            )
        )
