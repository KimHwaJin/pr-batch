"""Batch registration, partial update and view contracts."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import Contract, KernelName, Name
from app.schemas.schedule import Policy, PolicyPatch, Schedule


class CreateBatch(Contract):
    name: Name
    script_id: UUID
    script_version_id: UUID | None = None
    kernel_profile: KernelName
    schedule: Schedule | None = None
    policy: Policy = Field(default_factory=Policy)


class UpdateBatch(Contract):
    """Only fields present in the request change; schedule=null removes."""

    name: Name | None = None
    script_id: UUID | None = None
    script_version_id: UUID | None = None
    kernel_profile: KernelName | None = None
    schedule: Schedule | None = None
    policy: PolicyPatch | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in ("name", "script_id", "kernel_profile", "policy"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class BatchView(Contract):
    id: UUID
    name: str
    script_id: UUID
    script_version_id: UUID
    script_version_number: int
    kernel_profile: str
    schedule: Schedule | None
    policy: Policy
    enabled: bool
    next_run_at: datetime | None
    preview_runs: list[datetime]
    config_number: int
    revision: int
    created_at: datetime
    updated_at: datetime


class BatchPage(Contract):
    items: list[BatchView]
    next_cursor: str | None


class SkippedView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    config_number: int
    from_at: datetime
    until_at: datetime
    occurrences: int
    reason: str
    created_at: datetime


class SkippedPage(BaseModel):
    items: list[SkippedView]
    next_cursor: UUID | None
