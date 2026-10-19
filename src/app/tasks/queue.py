"""Enqueue, claim and settle tasks with PostgreSQL leases."""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.session import database_now
from app.models import Task


class TaskKind(StrEnum):
    SUBMIT_EXECUTION = "SUBMIT_EXECUTION"
    SYNC_EXECUTION = "SYNC_EXECUTION"
    COLLECT_RESULT = "COLLECT_RESULT"
    CANCEL_EXECUTION = "CANCEL_EXECUTION"


@dataclass(frozen=True)
class ClaimedTask:
    id: UUID
    kind: TaskKind
    run_id: UUID | None
    payload: dict[str, Any]
    token: UUID
    attempts: int


def enqueue(
    session: Session,
    kind: TaskKind,
    run_id: UUID | None,
    payload: dict[str, Any] | None = None,
    *,
    delay_seconds: float = 0,
) -> None:
    """Add a task in the caller's transaction (commits with the state)."""
    run_at = database_now(session) + timedelta(seconds=delay_seconds)
    session.add(
        Task(
            kind=kind,
            run_id=run_id,
            payload=payload or {},
            run_at=run_at,
            status="PENDING",
            attempts=0,
        )
    )


def claim(session: Session, lease_seconds: int) -> ClaimedTask | None:
    now = database_now(session)
    task = session.scalar(
        select(Task)
        .where(
            Task.status == "PENDING",
            Task.run_at <= now,
            or_(Task.lease_until.is_(None), Task.lease_until <= now),
        )
        .order_by(Task.run_at, Task.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        return None
    token = uuid4()
    task.lease_token = token
    task.lease_until = now + timedelta(seconds=lease_seconds)
    task.attempts += 1
    return ClaimedTask(
        task.id,
        TaskKind(task.kind),
        task.run_id,
        dict(task.payload),
        token,
        task.attempts,
    )


def locked_lease(session: Session, claimed: ClaimedTask) -> Task | None:
    """The task row if this worker still holds a live lease, else None."""
    task = session.scalar(
        select(Task).where(Task.id == claimed.id).with_for_update()
    )
    now = database_now(session)
    if (
        task is None
        or task.status != "PENDING"
        or task.lease_token != claimed.token
        or task.lease_until is None
        or task.lease_until <= now
    ):
        return None
    return task


def complete(task: Task) -> None:
    task.status = "DONE"
    task.lease_token = None
    task.lease_until = None
    task.last_error = None


def reschedule(
    task: Task,
    at: datetime,
    payload: dict[str, Any] | None = None,
    *,
    reset_attempts: bool = True,
) -> None:
    task.run_at = at
    task.lease_token = None
    task.lease_until = None
    task.last_error = None
    if reset_attempts:
        task.attempts = 0
    if payload is not None:
        task.payload = payload


def backoff(task: Task, now: datetime, error: str) -> None:
    """Retry transport/contract failures with capped exponential backoff."""
    delay = min(300, 2 ** min(task.attempts, 9))
    reschedule(
        task,
        now + timedelta(seconds=delay + random.uniform(0, delay / 4)),
        reset_attempts=False,
    )
    task.last_error = error


def next_due(session: Session) -> datetime | None:
    return session.scalar(
        select(func.min(Task.run_at)).where(Task.status == "PENDING")
    )
