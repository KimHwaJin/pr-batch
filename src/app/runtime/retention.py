"""Purges bookkeeping rows nobody reads once they are old enough.

Idempotency keys expire after idempotency_retention_days (a retry after
that is a new request) and settled tasks after task_retention_days. Run
history, scripts and skipped windows are never purged. Every replica runs
this; the deletes are idempotent, so overlapping replicas are harmless.
"""

import time
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, delete, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import database_now
from app.models import IdempotencyRecord, Task

BATCH_SIZE = 1000
PERIOD_SECONDS = 3600


class Retention:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        idempotency_days: int,
        task_days: int,
    ) -> None:
        self.sessions = sessions
        self.idempotency_age = timedelta(days=idempotency_days)
        self.task_age = timedelta(days=task_days)
        self.next_at = 0.0

    def tick(self) -> bool:
        """Delete one batch of expired rows; False when nothing is due."""
        if time.monotonic() < self.next_at:
            return False
        with self.sessions.begin() as session:
            now = database_now(session)
            deleted = purge(
                session,
                IdempotencyRecord,
                IdempotencyRecord.created_at < now - self.idempotency_age,
            ) + purge(
                session,
                Task,
                (Task.status == "DONE")
                & (Task.created_at < now - self.task_age),
            )
        if deleted < BATCH_SIZE:
            self.next_at = time.monotonic() + PERIOD_SECONDS
        return deleted > 0

    def next_due(self) -> datetime | None:
        return None  # the loop's idle bound is often enough to re-check


def purge(session: Session, model, condition) -> int:
    """Delete up to BATCH_SIZE rows matching condition, by primary key."""
    key = tuple(model.__table__.primary_key.columns)
    expired = select(*key).where(condition).limit(BATCH_SIZE)
    statement = delete(model).where(tuple_(*key).in_(expired))
    return cast(CursorResult, session.execute(statement)).rowcount or 0
