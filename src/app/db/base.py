"""Declarative base and the audit columns every exposed record carries."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema="batch")


@dataclass(frozen=True)
class Actor:
    """Who made a change, as in the Executor: the user from X-User-Id and
    the API key's client name. Background work (scheduler, dispatcher)
    has neither."""

    client: str | None
    user: str | None


# Background work: admitting scheduled runs, progressing runs via tasks.
SYSTEM = Actor(client=None, user=None)
SCHEDULER = DISPATCHER = SYSTEM


class AuditMixin:
    """created/updated at, by (user) and by_client on every exposed record.

    `updated_*` reflects the last change to the record's own content;
    scheduler bookkeeping such as moving `next_run_at` is not an update.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_client: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_by_client: Mapped[str | None] = mapped_column(String(64))

    def stamp_created(self, actor: Actor, at: datetime | None = None) -> None:
        at = at or utcnow()
        self.created_at = at
        self.created_by, self.created_by_client = actor.user, actor.client
        self.stamp_updated(actor, at)

    def stamp_updated(self, actor: Actor, at: datetime | None = None) -> None:
        self.updated_at = at or utcnow()
        self.updated_by, self.updated_by_client = actor.user, actor.client


class SoftDeleteMixin(AuditMixin):
    """User deletion hides a record but keeps it and its history.

    Requirements forbid discarding originals and run history, so delete
    only stamps who and when; owner-scoped lookups treat it as gone.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    deleted_by: Mapped[str | None] = mapped_column(String(255))
    deleted_by_client: Mapped[str | None] = mapped_column(String(64))

    def stamp_deleted(self, actor: Actor, at: datetime) -> None:
        self.deleted_at = at
        self.deleted_by, self.deleted_by_client = actor.user, actor.client
        self.stamp_updated(actor, at)
