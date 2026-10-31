"""Index batch_runs by script: deletion checks and the version FK.

Run history only grows, so add it before the table is large.
"""

from alembic import op

revision = "0002_runs_script_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_runs_script_status",
        "batch_runs",
        ["script_id", "status"],
        schema="batch",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runs_script_status", table_name="batch_runs", schema="batch"
    )
