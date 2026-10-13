"""Run history, Executor step bindings and durable worker commands."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class BatchRun(Base):
    __tablename__ = "batch_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "config_number"],
            ["batch.batch_configs.batch_id", "batch.batch_configs.number"],
        ),
        ForeignKeyConstraint(
            ["notebook_id", "notebook_version_id"],
            [
                "batch.notebook_versions.notebook_id",
                "batch.notebook_versions.id",
            ],
        ),
        CheckConstraint(
            "dispatch_status IN ('PENDING', 'WAITING_CAPACITY', "
            "'SUBMITTED', 'SKIPPED')",
            name="run_dispatch_status",
        ),
        Index("ix_runs_batch_created", "batch_id", "created_at", "id"),
        Index("ix_runs_capacity", "batch_id", "dispatch_status"),
        UniqueConstraint(
            "batch_id",
            "config_number",
            "scheduled_for",
            name="uq_run_occurrence",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("batch.batches.id"))
    owner_id: Mapped[str] = mapped_column(String(255))
    notebook_id: Mapped[UUID]
    notebook_version_id: Mapped[UUID]
    config_number: Mapped[int]
    kernel_profile: Mapped[str] = mapped_column(String(128))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    trigger: Mapped[str] = mapped_column(String(16), default="MANUAL")
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    dispatch_status: Mapped[str] = mapped_column(String(32))
    execution_id: Mapped[UUID | None] = mapped_column(unique=True)
    operation_id: Mapped[UUID | None]
    execution_status: Mapped[str | None] = mapped_column(String(32))
    observed_version: Mapped[int | None]
    report_status: Mapped[str] = mapped_column(
        String(32), default="NOT_REQUESTED"
    )
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    failure_type: Mapped[str | None] = mapped_column(String(128))
    tracking_error: Mapped[str | None] = mapped_column(String(64))
    notebook_status: Mapped[str | None] = mapped_column(String(32))
    result_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list
    )
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    retry_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class RunStepBinding(Base):
    __tablename__ = "run_step_bindings"
    __table_args__ = (UniqueConstraint("run_id", "step_id"),)

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("batch.batch_runs.id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(primary_key=True)
    original_index: Mapped[int]
    source_sha256: Mapped[str] = mapped_column(String(64))
    step_id: Mapped[UUID | None]
    status: Mapped[str | None] = mapped_column(String(32))
    result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RunCommand(Base):
    """Durable submit/poll/result job; token fences expired workers."""

    __tablename__ = "run_commands"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('SUBMIT', 'POLL', 'RESULT', 'DONE')",
            name="command_phase",
        ),
        Index("ix_commands_due", "available_at", "run_id"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("batch.batch_runs.id"), primary_key=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    phase: Mapped[str] = mapped_column(String(16), default="SUBMIT")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    failures: Mapped[int] = mapped_column(default=0)
