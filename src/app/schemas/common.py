"""Shared request/response building blocks."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

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


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


ActorType = Literal["USER", "SYSTEM"]


class Audited(BaseModel):
    """Standard audit fields present on every resource response."""

    created_at: datetime
    created_by_type: ActorType
    created_by: str
    updated_at: datetime
    updated_by_type: ActorType
    updated_by: str


def audit_fields(record: object) -> dict[str, Any]:
    return {name: getattr(record, name) for name in Audited.model_fields}
