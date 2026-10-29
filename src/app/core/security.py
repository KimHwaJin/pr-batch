"""Caller and user identity from an API key and identity headers.

pr-batch keeps only SHA-256 hashes of caller keys (auth_clients), so its
repository and image cannot be used to call it. The caller vouches for
the user it names in X-User-Id; pr-batch trusts it and scopes every
resource to that user.
"""

import re
from dataclasses import dataclass
from typing import cast

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.schemas.common import TenantId

# Opaque user IDs (UUIDs, employee numbers, ...), lower-cased; the same
# rule in pr-agentic-service and the Executor, whose paths they name.
USER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROJECT_ID = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
API_KEY_MAX_LENGTH = 512
# Tenant of batches registered in header mode without X-Tenant-Id.
HEADER_MODE_TENANT = "dev"
TENANT = TypeAdapter(TenantId)


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
class Identity:
    caller: Caller
    user_id: str
    project_id: str | None
    # X-Tenant-Id when sent (already checked against the caller).
    requested_tenant: str | None

    @property
    def tenant(self) -> str:
        return self.requested_tenant or self.caller.tenant


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


def current_caller(request: Request) -> Caller:
    settings = cast(Settings, request.app.state.settings)
    key = request.headers.get("X-Api-Key")
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


def admin_caller(request: Request) -> Caller:
    caller = current_caller(request)
    if not caller.admin:
        raise ServiceError(403, "FORBIDDEN", "This key is not an admin key.")
    return caller


def requested_tenant(request: Request, caller: Caller) -> str | None:
    value = request.headers.get("X-Tenant-Id")
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


def current_identity(request: Request) -> Identity:
    caller = current_caller(request)
    user = request.headers.get("X-User-Id")
    if user is None:
        raise invalid_identity("X-User-Id is required.")
    project = request.headers.get("X-Project-Id")
    if project is not None and (
        not PROJECT_ID.fullmatch(project) or project == "unscoped"
    ):
        raise invalid_identity(
            "X-Project-Id: 1-255 characters of [A-Za-z0-9._-]."
        )
    # X-Session-Id is ignored: batches are not session-scoped.
    return Identity(
        caller=caller,
        user_id=normalize_user_id(user),
        project_id=project,
        requested_tenant=requested_tenant(request, caller),
    )


def current_owner(request: Request) -> str:
    return current_identity(request).user_id
