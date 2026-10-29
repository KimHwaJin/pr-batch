"""Caller and user identity from an API key and identity headers.

pr-batch keeps only SHA-256 hashes of caller keys (auth_clients), so its
repository and image cannot be used to call it. The caller vouches for
the user it names in X-User-Id; pr-batch trusts it and scopes every
resource to that user within the request's tenant, as the Executor does.
"""

import re
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyHeader
from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.schemas.common import TenantId

# Opaque user IDs (UUIDs, employee numbers, ...), lower-cased; the same
# rule in pr-agentic-service and the Executor, whose paths they name.
USER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
API_KEY_MAX_LENGTH = 512
# Tenant of batches registered in header mode without X-Tenant-Id.
HEADER_MODE_TENANT = "dev"
TENANT = TypeAdapter(TenantId)

# Declared (not read from request.headers) so OpenAPI and Swagger show
# them. Optional, so a missing header is our 400/401, not FastAPI's 422.
API_KEY_HEADER = APIKeyHeader(
    name="X-Api-Key",
    auto_error=False,
    description="Calling service key. Optional only with AUTH_MODE=header.",
)
UserHeader = Annotated[
    str | None,
    Header(
        alias="X-User-Id",
        description="Required. User the caller acts for.",
    ),
]
ProjectHeader = Annotated[
    str | None,
    Header(alias="X-Project-Id", description="Optional project scope."),
]
TenantHeader = Annotated[
    str | None,
    Header(
        alias="X-Tenant-Id",
        description="Optional tenant; relay keys and allowed tenants only.",
    ),
]


@dataclass(frozen=True)
class Caller:
    # auth_clients name; None in header mode without a key.
    client: str | None
    tenant: str
    relay: bool
    # None: any tenant (header mode without a key).
    allowed_tenants: frozenset[str] | None
    admin: bool


@dataclass(frozen=True)
class Principal:
    """Who owns a resource: a user within a tenant (as in the Executor)."""

    tenant: str
    user_id: str


@dataclass(frozen=True)
class Identity:
    caller: Caller
    user_id: str
    project_id: str | None
    # X-Tenant-Id when sent (already checked against the caller).
    requested_tenant: str | None

    @property
    def tenant(self) -> str:
        return self.requested_tenant or self.caller.tenant

    @property
    def owner(self) -> Principal:
        return Principal(self.tenant, self.user_id)


def unauthenticated() -> ServiceError:
    return ServiceError(401, "UNAUTHENTICATED", "Valid X-Api-Key required.")


def invalid_identity(message: str) -> ServiceError:
    return ServiceError(400, "INVALID_IDENTITY", message)


def normalize_user_id(value: str) -> str:
    """Stored and compared lower-case, so 'A123' and 'a123' are one user."""
    normalized = value.lower()
    if not USER_ID.fullmatch(normalized):
        raise invalid_identity(
            "User ID: 1-64 characters of [a-z0-9._-], starting with [a-z0-9]."
        )
    return normalized


def current_caller(
    request: Request, key: Annotated[str | None, Security(API_KEY_HEADER)]
) -> Caller:
    settings = cast(Settings, request.app.state.settings)
    if key is None:
        if settings.auth_mode == "header":
            return Caller(None, HEADER_MODE_TENANT, True, None, True)
        raise unauthenticated()
    if not key or len(key) > API_KEY_MAX_LENGTH:
        raise unauthenticated()
    client = settings.match_client(key)
    if client is None:
        raise unauthenticated()
    return Caller(
        client=client.name,
        tenant=client.tenant,
        relay=client.relay,
        allowed_tenants=frozenset(client.allowed_tenants),
        admin=client.admin,
    )


def admin_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    if not caller.admin:
        raise ServiceError(403, "FORBIDDEN", "This key is not an admin key.")
    return caller


def requested_tenant(value: str | None, caller: Caller) -> str | None:
    if value is None:
        return None
    try:
        tenant = TENANT.validate_python(value)
    except ValidationError as exc:
        raise invalid_identity("X-Tenant-Id: not a valid tenant.") from exc
    allowed = caller.allowed_tenants
    if not caller.relay or (allowed is not None and tenant not in allowed):
        raise ServiceError(
            403, "FORBIDDEN", "This key may not choose that tenant."
        )
    return tenant


def current_identity(
    caller: Annotated[Caller, Depends(current_caller)],
    user: UserHeader = None,
    project: ProjectHeader = None,
    tenant: TenantHeader = None,
) -> Identity:
    if user is None:
        raise invalid_identity("X-User-Id is required.")
    if project is not None and (
        not PROJECT_ID.fullmatch(project) or project == "unscoped"
    ):
        raise invalid_identity(
            "X-Project-Id: 1-255 characters of [A-Za-z0-9._-], "
            "starting with [A-Za-z0-9]."
        )
    # X-Session-Id is ignored: batches are not session-scoped.
    return Identity(
        caller=caller,
        user_id=normalize_user_id(user),
        project_id=project,
        requested_tenant=requested_tenant(tenant, caller),
    )


def current_owner(
    identity: Annotated[Identity, Depends(current_identity)],
) -> Principal:
    return identity.owner


def admin_tenant(
    caller: Annotated[Caller, Depends(admin_caller)],
    tenant: TenantHeader = None,
) -> str:
    """The tenant an admin operation applies to, chosen like any other."""
    return requested_tenant(tenant, caller) or caller.tenant
