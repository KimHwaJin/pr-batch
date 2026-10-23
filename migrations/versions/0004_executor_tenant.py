"""Batch configs name the Executor tenant that receives run events.

Null keeps the Executor's shared event Stream; existing configs stay null.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_executor_tenant"
down_revision = "0003_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batch_configs",
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        schema="batch",
    )


def downgrade() -> None:
    op.drop_column("batch_configs", "tenant_id", schema="batch")
