"""Errors safe to return at the API boundary and their handlers."""

import logging
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)
# Connection-level failures: retrying later can succeed.
UNAVAILABLE = (OperationalError, DisconnectionError, PoolTimeout)


def database_detail(exc: SQLAlchemyError) -> dict[str, Any]:
    """Schema-level facts about a DB error: no SQL text or bound values.

    error_id ties the response to its log line; sqlstate and the object
    names come from PostgreSQL's diagnostics when the driver has them.
    """
    detail: dict[str, Any] = {
        "error_id": uuid4().hex,
        "type": type(exc).__name__,
    }
    original = exc.orig if isinstance(exc, DBAPIError) else None
    if original is not None:
        detail["driver_error"] = type(original).__name__
        if sqlstate := getattr(original, "sqlstate", None):
            detail["sqlstate"] = sqlstate
        diag = getattr(original, "diag", None)
        for field in ("table_name", "column_name", "constraint_name"):
            if value := getattr(diag, field, None):
                detail[field.removesuffix("_name")] = value
    return detail


def detail(
    location: list[str | int], type_: str, **context: Any
) -> dict[str, Any]:
    """One details[] entry: which value, why, and the facts behind it.

    context carries limits and counts only, never caller-provided values.
    """
    return {"location": location, "type": type_, "context": context}


def error_body(
    code: str, message: str, details: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The one error shape, shared with the Executor API."""
    return {
        "error": {"code": code, "message": message, "details": details or []}
    }


def error_response(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status)


# Pydantic context entries that are plain facts (limits, patterns).
SAFE_CONTEXT = (str, int, float, bool)
HTTP_CODES = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
}


class ServiceError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error(
        request: Request, exc: ServiceError
    ) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Unknown routes and methods; keeps their headers (e.g. Allow).
        response = error_response(
            exc.status_code,
            HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}"),
            str(exc.detail),
        )
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Do not echo uploaded code, credentials, or caller-provided values:
        # pydantic's "input" is dropped, and context keeps plain facts only.
        details = [
            {
                **detail(
                    list(error["loc"]),
                    error["type"],
                    **{
                        key: value
                        for key, value in (error.get("ctx") or {}).items()
                        if key != "error" and isinstance(value, SAFE_CONTEXT)
                    },
                ),
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return error_response(
            422, "VALIDATION_ERROR", "Request validation failed.", details
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        facts = database_detail(exc)
        unavailable = isinstance(exc, UNAVAILABLE) or (
            isinstance(exc, DBAPIError) and exc.connection_invalidated
        )
        logger.error(
            "Database error on %s %s: %s",
            request.method,
            request.url.path,
            " ".join(f"{k}={v}" for k, v in facts.items()),
            exc_info=exc,
        )
        details = [detail(["database"], "database_error", **facts)]
        if unavailable:
            return error_response(
                503,
                "DATABASE_UNAVAILABLE",
                "Database unavailable; retry later.",
                details,
            )
        return error_response(
            500,
            "INTERNAL_ERROR",
            "Database error; retrying will not help.",
            details,
        )
