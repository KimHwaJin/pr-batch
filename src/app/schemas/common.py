"""Shared request/response building blocks."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
)

Name = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]

KernelName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


# Executor reserves these; its event Stream names use them.
RESERVED_TENANT_IDS = frozenset({"dlq", "work"})


def not_reserved(value: str) -> str:
    if value in RESERVED_TENANT_IDS:
        raise ValueError(f"tenant_id '{value}' is reserved.")
    return value


# Same rule as the Executor's submit `tenant_id`: the system that receives
# the execution's events (Stream executor.events.{tenant_id}).
TenantId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"),
    AfterValidator(not_reserved),
]


# Opaque caller-chosen user IDs (UUIDs, employee numbers); lower-cased.
UserId = Annotated[
    str,
    BeforeValidator(lambda v: v.lower() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Audited(BaseModel):
    """Standard audit fields present on every resource response, as in the
    Executor: the user (X-User-Id) and the API key's client name; both
    null for background work (scheduler, dispatcher)."""

    created_at: datetime
    created_by: str | None
    created_by_client: str | None
    updated_at: datetime
    updated_by: str | None
    updated_by_client: str | None


def audit_fields(record: object) -> dict[str, Any]:
    return {name: getattr(record, name) for name in Audited.model_fields}
