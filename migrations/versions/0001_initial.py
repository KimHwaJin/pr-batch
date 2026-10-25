"""Initial schema: scripts, batches, configs, runs and tasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
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
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config_number", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_type", sa.String(length=16), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint(
            "deleted_at IS NULL OR NOT enabled", name="deleted_is_disabled"
        ),
        sa.CheckConstraint(
            "enabled OR next_run_at IS NULL", name="disabled_has_no_next_run"
        ),
        sa.CheckConstraint("revision >= 1", name="batch_revision_positive"),
        sa.PrimaryKeyConstraint("id"),
        schema="batch",
    )
    op.create_index(
        "ix_batches_due",
        "batches",
        ["enabled", "next_run_at"],
        unique=False,
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
    op.create_index(
        "ix_idempotency_created",
        "idempotency_records",
        ["created_at"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "scripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("latest_version_number", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_type", sa.String(length=16), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint("type IN ('NOTEBOOK')", name="script_type"),
        sa.CheckConstraint("revision >= 1", name="script_revision_positive"),
        sa.PrimaryKeyConstraint("id"),
        schema="batch",
    )
    op.create_index(
        "ix_scripts_owner_created",
        "scripts",
        ["owner_id", "created_at", "id"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "script_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("original_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "cell_map", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "cell_list",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint(
            "version_number >= 1", name="version_number_positive"
        ),
        sa.ForeignKeyConstraint(
            ["script_id"],
            ["batch.scripts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id", "id", name="uq_version_script_id"),
        sa.UniqueConstraint("script_id", "sha256", name="uq_version_content"),
        sa.UniqueConstraint(
            "script_id", "version_number", name="uq_version_number"
        ),
        schema="batch",
    )
    op.create_table(
        "batch_configs",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "runtime", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint("number >= 1", name="config_number_positive"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.ForeignKeyConstraint(
            ["script_id", "script_version_id"],
            ["batch.script_versions.script_id", "batch.script_versions.id"],
            name="fk_config_version_in_script",
        ),
        sa.PrimaryKeyConstraint("batch_id", "number"),
        schema="batch",
    )
    op.create_index(
        "ix_configs_script",
        "batch_configs",
        ["script_id"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "batch_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("config_number", sa.Integer(), nullable=False),
        sa.Column(
            "runtime", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("cancel_source", sa.String(length=16), nullable=True),
        sa.Column("cancel_reason", sa.String(length=255), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("execution_status", sa.String(length=32), nullable=True),
        sa.Column("observed_version", sa.Integer(), nullable=True),
        sa.Column("failure_type", sa.String(length=128), nullable=True),
        sa.Column("notebook_status", sa.String(length=32), nullable=True),
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
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint(
            "cancel_source IS NULL OR cancel_source IN ('USER', 'POLICY')",
            name="run_cancel_source",
        ),
        sa.CheckConstraint(
            "status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', "
            "'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')",
            name="run_status",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "config_number"],
            ["batch.batch_configs.batch_id", "batch.batch_configs.number"],
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.batches.id"],
        ),
        sa.ForeignKeyConstraint(
            ["script_id", "script_version_id"],
            ["batch.script_versions.script_id", "batch.script_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "config_number",
            "scheduled_for",
            name="uq_run_occurrence",
        ),
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
        "ix_runs_batch_status",
        "batch_runs",
        ["batch_id", "status"],
        unique=False,
        schema="batch",
    )
    op.create_table(
        "skipped_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("config_number", sa.Integer(), nullable=False),
        sa.Column("from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_type", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_type", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.CheckConstraint("from_at < until_at", name="skipped_window_order"),
        sa.CheckConstraint("occurrences > 0", name="skipped_count_positive"),
        sa.ForeignKeyConstraint(
            ["batch_id", "config_number"],
            ["batch.batch_configs.batch_id", "batch.batch_configs.number"],
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
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DONE')", name="task_status"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["batch.batch_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="batch",
    )
    op.create_index(
        "ix_tasks_done_created",
        "tasks",
        ["created_at"],
        unique=False,
        schema="batch",
        postgresql_where=sa.text("status = 'DONE'"),
    )
    op.create_index(
        "ix_tasks_due",
        "tasks",
        ["run_at"],
        unique=False,
        schema="batch",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_tasks_run", "tasks", ["run_id"], unique=False, schema="batch"
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_run", table_name="tasks", schema="batch")
    op.drop_index(
        "ix_tasks_due",
        table_name="tasks",
        schema="batch",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_index(
        "ix_tasks_done_created",
        table_name="tasks",
        schema="batch",
        postgresql_where=sa.text("status = 'DONE'"),
    )
    op.drop_table("tasks", schema="batch")
    op.drop_table("run_step_bindings", schema="batch")
    op.drop_index(
        "ix_skipped_batch_created",
        table_name="skipped_windows",
        schema="batch",
    )
    op.drop_table("skipped_windows", schema="batch")
    op.drop_index(
        "ix_runs_batch_status", table_name="batch_runs", schema="batch"
    )
    op.drop_index(
        "ix_runs_batch_created", table_name="batch_runs", schema="batch"
    )
    op.drop_table("batch_runs", schema="batch")
    op.drop_index(
        "ix_configs_script", table_name="batch_configs", schema="batch"
    )
    op.drop_table("batch_configs", schema="batch")
    op.drop_table("script_versions", schema="batch")
    op.drop_index(
        "ix_scripts_owner_created", table_name="scripts", schema="batch"
    )
    op.drop_table("scripts", schema="batch")
    op.drop_index(
        "ix_idempotency_created",
        table_name="idempotency_records",
        schema="batch",
    )
    op.drop_table("idempotency_records", schema="batch")
    op.drop_index(
        "ix_batches_owner_created", table_name="batches", schema="batch"
    )
    op.drop_index("ix_batches_due", table_name="batches", schema="batch")
    op.drop_table("batches", schema="batch")
