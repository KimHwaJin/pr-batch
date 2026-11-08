"""Record the calling client instead of an actor type, as the Executor does.

Audit columns now hold the user from X-User-Id (``*_by``, null for
background work) and the API key's client name (``*_by_client``).
Existing rows: a USER row keeps its user and gets no client; a SYSTEM row
loses its ``*_by`` (scheduler, dispatcher) and, when it named a caller
(admin operations), keeps that name as ``*_by_client``.

Dropping ``*_by_type`` breaks Pods still on the previous release, so this
release deploys with Recreate (no rolling overlap).
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_audit_client"
down_revision = "0004_tenant_revoked_reason"
branch_labels = None
depends_on = None

SCHEMA = "batch"
AUDITED = (
    "scripts",
    "script_versions",
    "batches",
    "batch_configs",
    "skipped_windows",
    "batch_runs",
)
SOFT_DELETED = ("scripts", "batches")
# Background actors had no caller; anything else SYSTEM was a client name.
BACKGROUND = "('scheduler', 'dispatcher')"


def prefixes(table: str) -> tuple[str, ...]:
    if table in SOFT_DELETED:
        return ("created", "updated", "deleted")
    return ("created", "updated")


def upgrade() -> None:
    for table in AUDITED:
        for prefix in prefixes(table):
            op.add_column(
                table,
                sa.Column(
                    f"{prefix}_by_client", sa.String(length=64), nullable=True
                ),
                schema=SCHEMA,
            )
            op.alter_column(
                table, f"{prefix}_by", nullable=True, schema=SCHEMA
            )
            op.execute(
                f"UPDATE {SCHEMA}.{table} SET "
                f"{prefix}_by_client = CASE WHEN {prefix}_by IN {BACKGROUND} "
                f"THEN NULL ELSE {prefix}_by END, "
                f"{prefix}_by = NULL "
                f"WHERE {prefix}_by_type = 'SYSTEM'"
            )
            op.drop_column(table, f"{prefix}_by_type", schema=SCHEMA)


def downgrade() -> None:
    for table in AUDITED:
        for prefix in prefixes(table):
            op.add_column(
                table,
                sa.Column(
                    f"{prefix}_by_type", sa.String(length=16), nullable=True
                ),
                schema=SCHEMA,
            )
            op.execute(
                f"UPDATE {SCHEMA}.{table} SET "
                f"{prefix}_by_type = CASE WHEN {prefix}_by IS NULL "
                f"THEN 'SYSTEM' ELSE 'USER' END, "
                f"{prefix}_by = COALESCE({prefix}_by, {prefix}_by_client, "
                f"'scheduler') "
                + (
                    f"WHERE {prefix}_at IS NOT NULL"
                    if prefix == "deleted"
                    else ""
                )
            )
            if prefix != "deleted":
                for column in (f"{prefix}_by_type", f"{prefix}_by"):
                    op.alter_column(
                        table, column, nullable=False, schema=SCHEMA
                    )
            op.drop_column(table, f"{prefix}_by_client", schema=SCHEMA)
