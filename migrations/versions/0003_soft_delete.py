"""Users can delete scripts and batches; rows and history are kept.

Deletion only stamps deleted_at/by. A deleted batch is always disabled,
so the scheduler never admits for it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_soft_delete"
down_revision = "0002_drop_reports"
branch_labels = None
depends_on = None

TABLES = ("scripts", "batches")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            schema="batch",
        )
        op.add_column(
            table,
            sa.Column("deleted_by_type", sa.String(length=16), nullable=True),
            schema="batch",
        )
        op.add_column(
            table,
            sa.Column("deleted_by", sa.String(length=255), nullable=True),
            schema="batch",
        )
    op.create_check_constraint(
        "deleted_is_disabled",
        "batches",
        "deleted_at IS NULL OR NOT enabled",
        schema="batch",
    )


def downgrade() -> None:
    # Deleted rows would reappear; refuse rather than resurrect them.
    connection = op.get_bind()
    for table in TABLES:
        deleted = connection.scalar(
            sa.text(
                f"SELECT count(*) FROM batch.{table} "
                "WHERE deleted_at IS NOT NULL"
            )
        )
        if deleted:
            raise RuntimeError(f"{deleted} deleted {table} would reappear.")
    op.drop_constraint("deleted_is_disabled", "batches", schema="batch")
    for table in TABLES:
        for column in ("deleted_by", "deleted_by_type", "deleted_at"):
            op.drop_column(table, column, schema="batch")
