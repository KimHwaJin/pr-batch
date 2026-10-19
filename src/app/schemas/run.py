"""Run admission and history contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import Audited, Contract


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


class ResultRefs(BaseModel):
    """Executor artifact IDs; download via Executor after ownership check."""

    notebook_artifact_id: str | None = None


class RunView(Audited, BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    script_id: UUID
    script_version_id: UUID
    config_number: int
    runtime: dict[str, Any]
    snapshot: dict[str, Any]
    trigger: str
    scheduled_for: datetime | None
    status: str
    skip_reason: str | None
    cancel_requested: bool
    cancel_source: str | None
    cancel_reason: str | None
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
    result: ResultRefs = ResultRefs()
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
