"""Reports move to pr-agentic-service; record who cancelled a run.

Existing REPORTING and REPORT_FAILED runs become COMPLETED: both only
exist after the code succeeded. Pending report tasks are closed first,
since the dispatcher no longer knows their kinds. Past report rows are
kept in run_reports_archive instead of being dropped.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_drop_reports"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

OLD_STATUSES = (
    "status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', "
    "'REPORTING', 'COMPLETED', 'FAILED', 'REPORT_FAILED', "
    "'CANCELLED', 'SKIPPED')"
)
NEW_STATUSES = (
    "status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', "
    "'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')"
)


def upgrade() -> None:
    op.execute(
        "UPDATE batch.tasks SET status = 'DONE', lease_token = NULL, "
        "lease_until = NULL "
        "WHERE status = 'PENDING' "
        "AND kind IN ('REQUEST_REPORT', 'SYNC_REPORT')"
    )
    # start_report cleared completed_at; the latest report attempt is the
    # closest record of when the run ended.
    op.execute(
        "UPDATE batch.batch_runs AS r SET status = 'COMPLETED', "
        "completed_at = COALESCE(r.completed_at, ("
        "SELECT max(p.updated_at) FROM batch.run_reports AS p "
        "WHERE p.run_id = r.id), r.finished_at, r.updated_at) "
        "WHERE r.status IN ('REPORTING', 'REPORT_FAILED')"
    )
    op.drop_constraint("run_status", "batch_runs", schema="batch")
    op.create_check_constraint(
        "run_status", "batch_runs", NEW_STATUSES, schema="batch"
    )
    op.drop_column("batch_runs", "report_generation", schema="batch")
    op.add_column(
        "batch_runs",
        sa.Column("cancel_source", sa.String(length=16), nullable=True),
        schema="batch",
    )
    op.add_column(
        "batch_runs",
        sa.Column("cancel_reason", sa.String(length=255), nullable=True),
        schema="batch",
    )
    op.create_check_constraint(
        "run_cancel_source",
        "batch_runs",
        "cancel_source IS NULL OR cancel_source IN ('USER', 'POLICY')",
        schema="batch",
    )
    op.rename_table("run_reports", "run_reports_archive", schema="batch")


def downgrade() -> None:
    # Runs moved to COMPLETED stay COMPLETED; their reports are not resumed.
    op.rename_table("run_reports_archive", "run_reports", schema="batch")
    op.drop_constraint("run_cancel_source", "batch_runs", schema="batch")
    op.drop_column("batch_runs", "cancel_reason", schema="batch")
    op.drop_column("batch_runs", "cancel_source", schema="batch")
    op.add_column(
        "batch_runs",
        sa.Column(
            "report_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="batch",
    )
    op.execute(
        "UPDATE batch.batch_runs AS r SET report_generation = COALESCE(("
        "SELECT max(p.generation) FROM batch.run_reports AS p "
        "WHERE p.run_id = r.id), 0)"
    )
    op.alter_column(
        "batch_runs", "report_generation", server_default=None, schema="batch"
    )
    op.drop_constraint("run_status", "batch_runs", schema="batch")
    op.create_check_constraint(
        "run_status", "batch_runs", OLD_STATUSES, schema="batch"
    )
