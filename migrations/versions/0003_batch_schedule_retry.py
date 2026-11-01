"""Track failed schedule admissions so one batch cannot stall the rest.

Additive: the previous release ignores the new columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_batch_schedule_retry"
down_revision = "0002_runs_script_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batches",
        sa.Column(
            "schedule_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="batch",
    )
    op.add_column(
        "batches",
        sa.Column(
            "schedule_retry_at", sa.DateTime(timezone=True), nullable=True
        ),
        schema="batch",
    )


def downgrade() -> None:
    op.drop_column("batches", "schedule_retry_at", schema="batch")
    op.drop_column("batches", "schedule_failures", schema="batch")
