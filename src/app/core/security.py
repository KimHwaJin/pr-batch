"""Trusted caller identity via short-lived, audience-bound RS256 tokens.

Callers sign with the shared private key; pr-batch holds only the public
key, so nothing in this service can mint a token.
"""

from typing import Annotated, cast
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.exceptions import ServiceError

bearer = HTTPBearer(auto_error=False)


def current_owner(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer)
    ],
) -> str:
    settings = cast(Settings, request.app.state.settings)
    try:
        if credentials is None or len(credentials.credentials) > 4096:
            raise ValueError("Missing token.")
        claims = jwt.decode(
            credentials.credentials,
            settings.auth_public_key,
            algorithms=["RS256"],
            audience=settings.auth_audience,
            issuer=settings.auth_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        # prj_re user IDs are UUIDs; storage paths and ownership use the
        # canonical form, so "ABC…" and "abc…" are the same user.
        owner = str(UUID(claims["sub"]))
        issued, expires = claims["iat"], claims["exp"]
        if (
            type(issued) is not int
            or type(expires) is not int
            or not 0 < expires - issued <= settings.auth_max_lifetime_seconds
        ):
            raise ValueError("Invalid claims.")
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        raise ServiceError(
            401, "UNAUTHENTICATED", "Valid service token required."
        ) from exc
    if claims.get("scope") != "batch:manage":
        raise ServiceError(403, "FORBIDDEN", "Missing batch:manage scope.")
    return owner
