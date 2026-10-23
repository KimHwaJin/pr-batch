"""Batches, their immutable run configuration history and skips."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, SoftDeleteMixin


class Batch(SoftDeleteMixin, Base):
    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="batch_revision_positive"),
        CheckConstraint(
            "enabled OR next_run_at IS NULL", name="disabled_has_no_next_run"
        ),
        CheckConstraint(
            "deleted_at IS NULL OR NOT enabled", name="deleted_is_disabled"
        ),
        Index("ix_batches_due", "enabled", "next_run_at"),
        Index("ix_batches_owner_created", "owner_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revision: Mapped[int] = mapped_column(default=1)
    config_number: Mapped[int] = mapped_column(default=1)


class BatchConfig(AuditMixin, Base):
    """Immutable run configuration history; runs point at one revision."""

    __tablename__ = "batch_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["script_id", "script_version_id"],
            [
                "batch.script_versions.script_id",
                "batch.script_versions.id",
            ],
            name="fk_config_version_in_script",
        ),
        CheckConstraint("number >= 1", name="config_number_positive"),
        Index("ix_configs_script", "script_id"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("batch.batches.id"), primary_key=True
    )
    number: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[UUID]
    script_version_id: Mapped[UUID]
    # Executor runtime object, e.g. {"type": "JUPYTER", "profile": "x"}.
    runtime: Mapped[dict[str, Any]] = mapped_column(JSONB)
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Executor tenant (event Stream) for runs of this config; null: shared.
    tenant_id: Mapped[str | None] = mapped_column(String(64))


class SkippedWindow(AuditMixin, Base):
    """Compressed, exact-count history for deliberately unsubmitted slots."""

    __tablename__ = "skipped_windows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "config_number"],
            ["batch.batch_configs.batch_id", "batch.batch_configs.number"],
        ),
        CheckConstraint("occurrences > 0", name="skipped_count_positive"),
        CheckConstraint("from_at < until_at", name="skipped_window_order"),
        Index("ix_skipped_batch_created", "batch_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("batch.batches.id"))
    config_number: Mapped[int]
    from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurrences: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(64))
