"""Allow disabled_reason TENANT_REVOKED (POST /batches:disable-by-tenant).

Widens a check constraint: the previous release never writes the new
value, so both releases can run against this schema.
"""

from alembic import op

revision = "0004_tenant_revoked_reason"
down_revision = "0003_batch_schedule_retry"
branch_labels = None
depends_on = None

OLD = (
    "disabled_reason IS NULL OR "
    "disabled_reason IN ('OWNER', 'USER_DEACTIVATED')"
)
NEW = (
    "disabled_reason IS NULL OR "
    "disabled_reason IN ('OWNER', 'USER_DEACTIVATED', 'TENANT_REVOKED')"
)


def replace(condition: str) -> None:
    op.drop_constraint(
        "batch_disabled_reason", "batches", schema="batch", type_="check"
    )
    op.create_check_constraint(
        "batch_disabled_reason", "batches", condition, schema="batch"
    )


def upgrade() -> None:
    replace(NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE batch.batches SET disabled_reason = 'OWNER' "
        "WHERE disabled_reason = 'TENANT_REVOKED'"
    )
    replace(OLD)
