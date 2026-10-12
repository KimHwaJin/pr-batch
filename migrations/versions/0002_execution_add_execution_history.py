"""Add durable execution history and worker commands"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_execution"
down_revision = "0001_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_number", sa.Integer(), nullable=False),
        sa.Column("kernel_profile", sa.String(length=128), nullable=False),
        sa.Column(
            "snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("dispatch_status", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("execution_status", sa.String(length=32), nullable=True),
        sa.Column("observed_version", sa.Integer(), nullable=True),
        sa.Column("report_status", sa.String(length=32), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("failure_type", sa.String(length=128), nullable=True),
        sa.Column("tracking_error", sa.String(length=64), nullable=True),
        sa.Column("notebook_status", sa.String(length=32), nullable=True),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.Column(
            "artifacts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "attempts", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "retry_info",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "dispatch_status IN ('PENDING', 'WAITING_CAPACITY', "
            "'SUBMITTED', 'SKIPPED')",
            name="run_dispatch_status",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "schedule_number"],
            [
                "batch.schedule_revisions.batch_id",
                "batch.schedule_revisions.number",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "version_id"],
            ["batch.notebook_versions.batch_id", "batch.notebook_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id"),
        schema="batch",
    )
    op.create_index(
        "ix_runs_batch_created",
        "batch_runs",
        ["batch_id", "created_at", "id"],
        unique=False,
        schema="batch",
    )
    op.create_index(
        "ix_runs_capacity",
        "batch_runs",
        ["batch_id", "dispatch_status"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "run_commands",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "phase IN ('SUBMIT', 'POLL', 'RESULT', 'DONE')",
            name="command_phase",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["batch.batch_runs.id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
        schema="batch",
    )
    op.create_index(
        "ix_commands_due",
        "run_commands",
        ["available_at", "run_id"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "run_step_bindings",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("original_index", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column(
            "result_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["batch.batch_runs.id"],
        ),
        sa.PrimaryKeyConstraint("run_id", "sequence"),
        sa.UniqueConstraint("run_id", "step_id"),
        schema="batch",
    )


def downgrade() -> None:
    op.drop_table("run_step_bindings", schema="batch")
    op.drop_index("ix_commands_due", table_name="run_commands", schema="batch")
    op.drop_table("run_commands", schema="batch")
    op.drop_index("ix_runs_capacity", table_name="batch_runs", schema="batch")
    op.drop_index(
        "ix_runs_batch_created", table_name="batch_runs", schema="batch"
    )
    op.drop_table("batch_runs", schema="batch")
