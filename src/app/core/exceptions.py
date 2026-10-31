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


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error(
        request: Request, exc: ServiceError
    ) -> JSONResponse:
        return JSONResponse(
            {"code": exc.code, "message": exc.message},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Do not echo uploaded code, credentials, or caller-provided values.
        return JSONResponse(
            {
                "code": "VALIDATION_ERROR",
                "errors": [
                    {
                        "loc": error["loc"],
                        "type": error["type"],
                        "msg": error["msg"],
                    }
                    for error in exc.errors()
                ],
            },
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        detail = database_detail(exc)
        unavailable = isinstance(exc, UNAVAILABLE) or (
            isinstance(exc, DBAPIError) and exc.connection_invalidated
        )
        logger.error(
            "Database error on %s %s: %s",
            request.method,
            request.url.path,
            " ".join(f"{k}={v}" for k, v in detail.items()),
            exc_info=exc,
        )
        if unavailable:
            return JSONResponse(
                {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Database unavailable; retry later.",
                    "detail": detail,
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "code": "INTERNAL_ERROR",
                "message": "Database error; retrying will not help.",
                "detail": detail,
            },
            status_code=500,
        )
