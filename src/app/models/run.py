"""Run history, Executor step bindings and the task queue."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, utcnow


class BatchRun(AuditMixin, Base):
    __tablename__ = "batch_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "config_number"],
            ["batch.batch_configs.batch_id", "batch.batch_configs.number"],
        ),
        ForeignKeyConstraint(
            ["script_id", "script_version_id"],
            [
                "batch.script_versions.script_id",
                "batch.script_versions.id",
            ],
        ),
        CheckConstraint(
            "status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', "
            "'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')",
            name="run_status",
        ),
        CheckConstraint(
            "cancel_source IS NULL OR cancel_source IN ('USER', 'POLICY')",
            name="run_cancel_source",
        ),
        Index("ix_runs_batch_created", "batch_id", "created_at", "id"),
        Index("ix_runs_batch_status", "batch_id", "status"),
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
    script_id: Mapped[UUID]
    script_version_id: Mapped[UUID]
    config_number: Mapped[int]
    # Executor runtime object, e.g. {"type": "JUPYTER", "profile": "x"}.
    runtime: Mapped[dict[str, Any]] = mapped_column(JSONB)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    trigger: Mapped[str] = mapped_column(String(16), default="MANUAL")
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    status: Mapped[str] = mapped_column(String(32))
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # Who asked first (USER: the owner, POLICY: overlap REPLACE) and why;
    # the cancel task sends them to the Executor as actor and reason.
    cancel_source: Mapped[str | None] = mapped_column(String(16))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    # Executor-side facts, kept verbatim for diagnosis.
    execution_id: Mapped[UUID | None] = mapped_column(unique=True)
    operation_id: Mapped[UUID | None]
    execution_status: Mapped[str | None] = mapped_column(String(32))
    observed_version: Mapped[int | None]
    failure_type: Mapped[str | None] = mapped_column(String(128))
    notebook_status: Mapped[str | None] = mapped_column(String(32))
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list
    )
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    retry_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Latest communication/contract error while progressing the run.
    last_error: Mapped[str | None] = mapped_column(String(64))
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
    completed_at: Mapped[datetime | None] = mapped_column(
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


class Task(Base):
    """One outbound call, enqueued in the transaction that requires it.

    The run status is the source of truth; handlers re-check it and finish
    as no-ops when a task is stale. Leases with tokens fence expired pods.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'DONE')", name="task_status"),
        Index(
            "ix_tasks_due",
            "run_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_tasks_run", "run_id"),
        Index(
            "ix_tasks_done_created",
            "created_at",
            postgresql_where=text("status = 'DONE'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("batch.batch_runs.id")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(String(64))
    # First failure since the last successful call; the dispatcher gives
    # up on the run once failures have lasted task_give_up_seconds.
    failing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
