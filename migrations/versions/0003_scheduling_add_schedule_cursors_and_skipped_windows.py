"""Add schedule cursors and skipped windows"""

import sqlalchemy as sa
from alembic import op

revision = "0003_scheduling"
down_revision = "0002_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skipped_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_number", sa.Integer(), nullable=False),
        sa.Column("from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("from_at < until_at", name="skipped_window_order"),
        sa.CheckConstraint("occurrences > 0", name="skipped_count_positive"),
        sa.ForeignKeyConstraint(
            ["batch_id", "schedule_number"],
            [
                "batch.schedule_revisions.batch_id",
                "batch.schedule_revisions.number",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="batch",
    )
    op.create_index(
        "ix_skipped_batch_created",
        "skipped_windows",
        ["batch_id", "created_at", "id"],
        unique=False,
        schema="batch",
    )
    op.add_column(
        "batch_runs",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        schema="batch",
    )
    op.create_unique_constraint(
        "uq_run_occurrence",
        "batch_runs",
        ["batch_id", "schedule_number", "scheduled_for"],
        schema="batch",
    )
    op.add_column(
        "batches",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        schema="batch",
    )
    op.drop_constraint("registration_only_disabled", "batches", schema="batch")
    op.create_check_constraint(
        "disabled_has_no_next_run",
        "batches",
        "enabled OR next_run_at IS NULL",
        schema="batch",
    )
    op.create_index(
        "ix_batches_due",
        "batches",
        ["enabled", "next_run_at"],
        unique=False,
        schema="batch",
    )


def downgrade() -> None:
    # Restoring the previous release also disables all schedules.
    op.execute("UPDATE batch.batches SET enabled = false, next_run_at = NULL")
    op.drop_constraint("disabled_has_no_next_run", "batches", schema="batch")
    op.create_check_constraint(
        "registration_only_disabled", "batches", "NOT enabled", schema="batch"
    )
    op.drop_index("ix_batches_due", table_name="batches", schema="batch")
    op.drop_column("batches", "next_run_at", schema="batch")
    op.drop_constraint(
        "uq_run_occurrence", "batch_runs", schema="batch", type_="unique"
    )
    op.drop_column("batch_runs", "scheduled_for", schema="batch")
    op.drop_index(
        "ix_skipped_batch_created",
        table_name="skipped_windows",
        schema="batch",
    )
    op.drop_table("skipped_windows", schema="batch")
