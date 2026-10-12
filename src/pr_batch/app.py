"""Application composition and bounded request ingestion."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pr_batch.api import router
from pr_batch.errors import ServiceError
from pr_batch.service import RegistrationService
from pr_batch.settings import Settings
from pr_batch.storage import NotebookStorage


class RequestSizeLimit:
    """Bound chunked and multipart bodies before the form parser runs."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                response = JSONResponse(
                    {
                        "code": "REQUEST_TOO_LARGE",
                        "message": "Body too large.",
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body)}

        await self.app(scope, buffered_receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=10,
        connect_args={"connect_timeout": 5},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Notebook Batch Service", version="0.1.0", lifespan=lifespan
    )
    app.state.settings = settings
    app.state.registration = RegistrationService(
        sessionmaker(engine, expire_on_commit=False),
        NotebookStorage(settings.storage_root),
    )
    app.add_middleware(
        RequestSizeLimit, max_bytes=settings.upload_max_bytes + 64 * 1024
    )

    @app.exception_handler(ServiceError)
    async def service_error(
        request: Request, exc: ServiceError
    ) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            {"code": exc.code, "message": exc.message},
            status_code=exc.status,
            headers=headers,
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
                    {"loc": error["loc"], "type": error["type"]}
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

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readiness() -> dict[str, str]:
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM batch.alembic_version")
            )
        if revision != "0002_execution":
            raise ServiceError(503, "MIGRATION_REQUIRED", "Apply migrations.")
        if not settings.storage_root.is_dir():
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Mount storage first."
            )
        return {"status": "ready"}

    app.include_router(router)
    return app
