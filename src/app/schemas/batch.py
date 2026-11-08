"""Batch registration, partial update and view contracts."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.runtimes import RuntimeType
from app.schemas.common import (
    Audited,
    Contract,
    KernelName,
    Name,
    UserId,
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


class UpdateBatch(Contract):
    """Only fields present in the request change; schedule=null removes."""

    name: Name | None = None
    script_id: UUID | None = None
    # null (or script_id alone): re-pin to the latest version.
    script_version_number: int | None = Field(default=None, ge=1)
    runtime: RuntimeSpec | None = None
    schedule: Schedule | None = None
    policy: PolicyPatch | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in ("name", "script_id", "runtime", "policy"):
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
    # Both fixed at registration, from the caller's key and headers.
    tenant_id: str
    project_id: str | None
    # The user who registered it; tenant-wide reads show every user's.
    user_id: str
    enabled: bool
    # Whether POST .../enable would turn the batch on now (false while
    # enabled: enabling then changes nothing) and .../disable would turn
    # it off. Enabling can still be refused if time passes (the last
    # occurrence goes by) or the batch changes before the call.
    can_enable: bool
    can_disable: bool
    # Error code enabling would return now, while disabled and blocked:
    # EXECUTOR_NOT_CONFIGURED, SCHEDULE_REQUIRED, RUNTIMES_NOT_CONFIGURED,
    # RUNTIME_NOT_SUPPORTED, UNKNOWN_RUNTIME or NO_FUTURE_OCCURRENCE.
    enable_blocked_reason: str | None
    # OWNER | USER_DEACTIVATED | TENANT_REVOKED; null when enabled or
    # never enabled.
    disabled_reason: str | None
    disabled_at: datetime | None
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


class DisableByOwner(Contract):
    owner_id: UserId
    reason: Literal["USER_DEACTIVATED"]


class DisableByTenant(Contract):
    reason: Literal["TENANT_REVOKED"]


class DisabledCount(Contract):
    disabled: int
