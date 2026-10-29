"""Errors safe to return at the API boundary and their handlers."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError


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
        return JSONResponse(
            {
                "code": "DATABASE_UNAVAILABLE",
                "message": "Database unavailable.",
            },
            status_code=503,
        )
