"""Declarative base and the audit columns every exposed record carries."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema="batch")


@dataclass(frozen=True)
class Actor:
    """Who performed a change: a signed-in user or the service itself."""

    type: Literal["USER", "SYSTEM"]
    id: str

    @classmethod
    def user(cls, user_id: str) -> "Actor":
        return cls("USER", user_id)


SCHEDULER = Actor("SYSTEM", "scheduler")  # admits scheduled runs, skips
DISPATCHER = Actor("SYSTEM", "dispatcher")  # progresses runs via tasks


class AuditMixin:
    """created/updated at and by (+ actor type) on every exposed record.

    `updated_*` reflects the last change to the record's own content;
    scheduler bookkeeping such as moving `next_run_at` is not an update.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    created_by_type: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_by_type: Mapped[str] = mapped_column(String(16))
    updated_by: Mapped[str] = mapped_column(String(255))

    def stamp_created(self, actor: Actor, at: datetime | None = None) -> None:
        at = at or utcnow()
        self.created_at = at
        self.created_by_type, self.created_by = actor.type, actor.id
        self.stamp_updated(actor, at)

    def stamp_updated(self, actor: Actor, at: datetime | None = None) -> None:
        self.updated_at = at or utcnow()
        self.updated_by_type, self.updated_by = actor.type, actor.id
