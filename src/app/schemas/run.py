"""Run admission and history contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Contract


class ManualRun(Contract):
    """Run the selected immutable configuration without overrides."""


class StepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sequence: int
    original_index: int
    source_sha256: str
    step_id: UUID | None
    status: str | None
    result_ref: dict[str, Any] | None


class ReportView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    generation: int
    status: str
    error_code: str | None
    artifacts: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class ResultRefs(BaseModel):
    """Executor artifact IDs; download via Executor after ownership check."""

    notebook_artifact_id: str | None = None
    report_artifact_id: str | None = None


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    script_id: UUID
    script_version_id: UUID
    config_number: int
    kernel_profile: str
    snapshot: dict[str, Any]
    trigger: str
    scheduled_for: datetime | None
    status: str
    skip_reason: str | None
    cancel_requested: bool
    execution_id: UUID | None
    operation_id: UUID | None
    execution_status: str | None
    observed_version: int | None
    failure_type: str | None
    notebook_status: str | None
    last_error: str | None
    artifacts: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    retry_info: dict[str, Any] | None
    report: ReportView | None = None
    result: ResultRefs = ResultRefs()
    created_at: datetime
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    observed_at: datetime | None
    completed_at: datetime | None


class RunDetail(RunView):
    steps: list[StepView]


class RunPage(BaseModel):
    items: list[RunView]
    next_cursor: UUID | None
