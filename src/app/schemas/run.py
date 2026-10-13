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


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    notebook_id: UUID
    notebook_version_id: UUID
    config_number: int
    kernel_profile: str
    snapshot: dict[str, Any]
    trigger: str
    scheduled_for: datetime | None
    dispatch_status: str
    execution_id: UUID | None
    operation_id: UUID | None
    execution_status: str | None
    observed_version: int | None
    report_status: str
    skip_reason: str | None
    failure_type: str | None
    tracking_error: str | None
    notebook_status: str | None
    result_status: str
    artifacts: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    retry_info: dict[str, Any] | None
    created_at: datetime
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    observed_at: datetime | None


class RunDetail(RunView):
    steps: list[StepView]


class RunPage(BaseModel):
    items: list[RunView]
    next_cursor: UUID | None
