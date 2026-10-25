"""Batch registration, partial update and view contracts."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.runtimes import RuntimeType
from app.schemas.common import (
    Audited,
    Contract,
    KernelName,
    Name,
    TenantId,
)
from app.schemas.schedule import Policy, PolicyPatch, Schedule


class RuntimeSpec(Contract):
    """Same shape as the Executor's execution `runtime` object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: RuntimeType = "JUPYTER"
    profile: KernelName


class RuntimeList(Contract):
    items: list[RuntimeSpec]


class CreateBatch(Contract):
    name: Name
    script_id: UUID
    # v1, v2, ...; omitted: pin the script's latest version at this time.
    script_version_number: int | None = Field(default=None, ge=1)
    runtime: RuntimeSpec
    schedule: Schedule | None = None
    policy: Policy = Field(default_factory=Policy)
    # Receiver of this batch's Executor events; null: shared Stream.
    tenant_id: TenantId | None = None


class UpdateBatch(Contract):
    """Only fields present in the request change; schedule=null removes."""

    name: Name | None = None
    script_id: UUID | None = None
    # null (or script_id alone): re-pin to the latest version.
    script_version_number: int | None = Field(default=None, ge=1)
    runtime: RuntimeSpec | None = None
    schedule: Schedule | None = None
    policy: PolicyPatch | None = None
    # Set once (null -> value); the receiver of events never changes after.
    tenant_id: TenantId | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in ("name", "script_id", "runtime", "policy", "tenant_id"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class BatchView(Audited, Contract):
    id: UUID
    name: str
    script_id: UUID
    script_version_id: UUID
    script_version_number: int
    runtime: RuntimeSpec
    schedule: Schedule | None
    policy: Policy
    tenant_id: str | None
    enabled: bool
    next_run_at: datetime | None
    preview_runs: list[datetime]
    config_number: int
    revision: int


class BatchPage(Contract):
    items: list[BatchView]
    next_cursor: str | None


class SkippedView(Audited, BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    config_number: int
    from_at: datetime
    until_at: datetime
    occurrences: int
    reason: str


class SkippedPage(BaseModel):
    items: list[SkippedView]
    next_cursor: str | None
