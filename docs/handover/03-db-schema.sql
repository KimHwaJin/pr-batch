-- pr-batch batch 스키마 DDL (revision 0005_audit_client 기준). SQLAlchemy 모델에서 생성했고, CI의 alembic check가 모델과 migration 일치를 보장한다.
-- 참고용이다. 실제 스키마는 앱 시작 시 Alembic migration으로 만든다.

CREATE TABLE batch.batches (
	id UUID NOT NULL, 
	tenant_id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	next_run_at TIMESTAMP WITH TIME ZONE, 
	revision INTEGER NOT NULL, 
	schedule_failures INTEGER DEFAULT '0' NOT NULL, 
	schedule_retry_at TIMESTAMP WITH TIME ZONE, 
	config_number INTEGER NOT NULL, 
	project_id VARCHAR(255), 
	client VARCHAR(64), 
	disabled_reason VARCHAR(32), 
	disabled_at TIMESTAMP WITH TIME ZONE, 
	disabled_by VARCHAR(64), 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	deleted_by VARCHAR(255), 
	deleted_by_client VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT batch_revision_positive CHECK (revision >= 1), 
	CONSTRAINT disabled_has_no_next_run CHECK (enabled OR next_run_at IS NULL), 
	CONSTRAINT deleted_is_disabled CHECK (deleted_at IS NULL OR NOT enabled), 
	CONSTRAINT batch_disabled_reason CHECK (disabled_reason IS NULL OR disabled_reason IN ('OWNER', 'USER_DEACTIVATED', 'TENANT_REVOKED')), 
	CONSTRAINT disabled_reason_is_disabled CHECK (disabled_reason IS NULL OR NOT enabled)
);
CREATE INDEX ix_batches_due ON batch.batches (enabled, next_run_at);
CREATE INDEX ix_batches_owner_created ON batch.batches (tenant_id, owner_id, created_at, id);

CREATE TABLE batch.idempotency_records (
	tenant_id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(255) NOT NULL, 
	operation VARCHAR(128) NOT NULL, 
	key VARCHAR(255) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	response JSONB, 
	status_code INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (tenant_id, owner_id, operation, key)
);
CREATE INDEX ix_idempotency_created ON batch.idempotency_records (created_at);

CREATE TABLE batch.scripts (
	id UUID NOT NULL, 
	tenant_id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	type VARCHAR(16) NOT NULL, 
	revision INTEGER NOT NULL, 
	latest_version_number INTEGER NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	deleted_by VARCHAR(255), 
	deleted_by_client VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT script_revision_positive CHECK (revision >= 1), 
	CONSTRAINT script_type CHECK (type IN ('NOTEBOOK'))
);
CREATE INDEX ix_scripts_owner_created ON batch.scripts (tenant_id, owner_id, created_at, id);

CREATE TABLE batch.script_versions (
	id UUID NOT NULL, 
	script_id UUID NOT NULL, 
	version_number INTEGER NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	original_path VARCHAR NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	code_hash VARCHAR(64) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	cell_map JSONB NOT NULL, 
	cell_list JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_version_number UNIQUE (script_id, version_number), 
	CONSTRAINT uq_version_content UNIQUE (script_id, sha256), 
	CONSTRAINT uq_version_script_id UNIQUE (script_id, id), 
	CONSTRAINT version_number_positive CHECK (version_number >= 1), 
	FOREIGN KEY(script_id) REFERENCES batch.scripts (id)
);

CREATE TABLE batch.batch_configs (
	batch_id UUID NOT NULL, 
	number INTEGER NOT NULL, 
	script_id UUID NOT NULL, 
	script_version_id UUID NOT NULL, 
	runtime JSONB NOT NULL, 
	schedule JSONB, 
	policy JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (batch_id, number), 
	CONSTRAINT fk_config_version_in_script FOREIGN KEY(script_id, script_version_id) REFERENCES batch.script_versions (script_id, id), 
	CONSTRAINT config_number_positive CHECK (number >= 1), 
	FOREIGN KEY(batch_id) REFERENCES batch.batches (id)
);
CREATE INDEX ix_configs_script ON batch.batch_configs (script_id);

CREATE TABLE batch.batch_runs (
	id UUID NOT NULL, 
	batch_id UUID NOT NULL, 
	owner_id VARCHAR(255) NOT NULL, 
	script_id UUID NOT NULL, 
	script_version_id UUID NOT NULL, 
	config_number INTEGER NOT NULL, 
	runtime JSONB NOT NULL, 
	snapshot JSONB NOT NULL, 
	trigger VARCHAR(16) NOT NULL, 
	scheduled_for TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(32) NOT NULL, 
	skip_reason VARCHAR(64), 
	cancel_requested BOOLEAN NOT NULL, 
	cancel_source VARCHAR(16), 
	cancel_reason VARCHAR(255), 
	execution_id UUID, 
	operation_id UUID, 
	execution_status VARCHAR(32), 
	observed_version INTEGER, 
	failure_type VARCHAR(128), 
	notebook_status VARCHAR(32), 
	artifacts JSONB NOT NULL, 
	attempts JSONB NOT NULL, 
	retry_info JSONB, 
	last_error VARCHAR(64), 
	submitted_at TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	observed_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(batch_id, config_number) REFERENCES batch.batch_configs (batch_id, number), 
	FOREIGN KEY(script_id, script_version_id) REFERENCES batch.script_versions (script_id, id), 
	CONSTRAINT run_status CHECK (status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', 'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')), 
	CONSTRAINT run_cancel_source CHECK (cancel_source IS NULL OR cancel_source IN ('USER', 'POLICY')), 
	CONSTRAINT uq_run_occurrence UNIQUE (batch_id, config_number, scheduled_for), 
	FOREIGN KEY(batch_id) REFERENCES batch.batches (id), 
	UNIQUE (execution_id)
);
CREATE INDEX ix_runs_batch_created ON batch.batch_runs (batch_id, created_at, id);
CREATE INDEX ix_runs_batch_status ON batch.batch_runs (batch_id, status);
CREATE INDEX ix_runs_script_status ON batch.batch_runs (script_id, status);

CREATE TABLE batch.skipped_windows (
	id UUID NOT NULL, 
	batch_id UUID NOT NULL, 
	config_number INTEGER NOT NULL, 
	from_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	until_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	occurrences BIGINT NOT NULL, 
	reason VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by VARCHAR(255), 
	created_by_client VARCHAR(64), 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_by VARCHAR(255), 
	updated_by_client VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(batch_id, config_number) REFERENCES batch.batch_configs (batch_id, number), 
	CONSTRAINT skipped_count_positive CHECK (occurrences > 0), 
	CONSTRAINT skipped_window_order CHECK (from_at < until_at), 
	FOREIGN KEY(batch_id) REFERENCES batch.batches (id)
);
CREATE INDEX ix_skipped_batch_created ON batch.skipped_windows (batch_id, created_at, id);

CREATE TABLE batch.run_step_bindings (
	run_id UUID NOT NULL, 
	sequence INTEGER NOT NULL, 
	original_index INTEGER NOT NULL, 
	source_sha256 VARCHAR(64) NOT NULL, 
	step_id UUID, 
	status VARCHAR(32), 
	result_ref JSONB, 
	PRIMARY KEY (run_id, sequence), 
	UNIQUE (run_id, step_id), 
	FOREIGN KEY(run_id) REFERENCES batch.batch_runs (id)
);

CREATE TABLE batch.tasks (
	id UUID NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	run_id UUID, 
	payload JSONB NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	run_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	attempts INTEGER NOT NULL, 
	lease_token UUID, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	last_error VARCHAR(64), 
	failing_since TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT task_status CHECK (status IN ('PENDING', 'DONE')), 
	FOREIGN KEY(run_id) REFERENCES batch.batch_runs (id)
);
CREATE INDEX ix_tasks_done_created ON batch.tasks (created_at) WHERE status = 'DONE';
CREATE INDEX ix_tasks_due ON batch.tasks (run_at) WHERE status = 'PENDING';
CREATE INDEX ix_tasks_run ON batch.tasks (run_id);

