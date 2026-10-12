"""PostgreSQL registration tables owned exclusively by this service."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema="batch")


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="batch_revision_positive"),
        CheckConstraint("NOT enabled", name="registration_only_disabled"),
        Index("ix_batches_owner_created", "owner_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)
    latest_version_number: Mapped[int] = mapped_column(default=1)
    schedule_number: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class NotebookVersion(Base):
    __tablename__ = "notebook_versions"
    __table_args__ = (
        UniqueConstraint("batch_id", "number", name="uq_version_number"),
        UniqueConstraint("batch_id", "sha256", name="uq_version_content"),
        UniqueConstraint("batch_id", "id", name="uq_version_batch_id"),
        CheckConstraint("number >= 1", name="version_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("batch.batches.id"))
    number: Mapped[int]
    original_path: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
    code_cells: Mapped[int]
    cell_map: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ScheduleRevision(Base):
    __tablename__ = "schedule_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "version_id"],
            ["batch.notebook_versions.batch_id", "batch.notebook_versions.id"],
            name="fk_schedule_version_in_batch",
        ),
        CheckConstraint("number >= 1", name="schedule_number_positive"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("batch.batches.id"), primary_key=True
    )
    number: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[UUID]
    kernel_profile: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    owner_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status_code: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
