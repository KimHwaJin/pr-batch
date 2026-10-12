"""Create notebook registration tables"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_registration"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("latest_version_number", sa.Integer(), nullable=False),
        sa.Column("schedule_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("NOT enabled", name="registration_only_disabled"),
        sa.CheckConstraint("revision >= 1", name="batch_revision_positive"),
        sa.PrimaryKeyConstraint("id"),
        schema="batch",
    )
    op.create_index(
        "ix_batches_owner_created",
        "batches",
        ["owner_id", "created_at", "id"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "response", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "operation", "key"),
        schema="batch",
    )
    op.create_table(
        "notebook_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("original_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("code_cells", sa.Integer(), nullable=False),
        sa.Column(
            "cell_map", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("number >= 1", name="version_number_positive"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "id", name="uq_version_batch_id"),
        sa.UniqueConstraint("batch_id", "number", name="uq_version_number"),
        sa.UniqueConstraint("batch_id", "sha256", name="uq_version_content"),
        schema="batch",
    )
    op.create_table(
        "schedule_revisions",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("kernel_profile", sa.String(length=128), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("number >= 1", name="schedule_number_positive"),
        sa.ForeignKeyConstraint(
            ["batch_id", "version_id"],
            ["batch.notebook_versions.batch_id", "batch.notebook_versions.id"],
            name="fk_schedule_version_in_batch",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.PrimaryKeyConstraint("batch_id", "number"),
        schema="batch",
    )


def downgrade() -> None:
    op.drop_table("schedule_revisions", schema="batch")
    op.drop_table("notebook_versions", schema="batch")
    op.drop_table("idempotency_records", schema="batch")
    op.drop_index(
        "ix_batches_owner_created", table_name="batches", schema="batch"
    )
    op.drop_table("batches", schema="batch")
