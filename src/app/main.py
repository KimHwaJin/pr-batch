"""Application factory: wiring, middleware, handlers and health checks."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import Settings
from app.core.exceptions import ServiceError, register_exception_handlers
from app.core.middleware import RequestSizeLimit
from app.db.session import create_db_engine, create_sessions
from app.storage.notebook_storage import NotebookStorage

MIGRATION_HEAD = "0001_baseline"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_db_engine(settings)

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
    app.state.sessions = create_sessions(engine)
    app.state.storage = NotebookStorage(settings.storage_root)
    app.add_middleware(
        RequestSizeLimit, max_bytes=settings.upload_max_bytes + 64 * 1024
    )
    register_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readiness() -> dict[str, str]:
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM batch.alembic_version")
            )
        if revision != MIGRATION_HEAD:
            raise ServiceError(503, "MIGRATION_REQUIRED", "Apply migrations.")
        if not settings.storage_root.is_dir():
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Mount storage first."
            )
        return {"status": "ready"}

    app.include_router(api_router)
    return app
