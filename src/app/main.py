"""Application factory: one process serves the API and runs the
background runtime (scheduler + task dispatcher) for this replica."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import PROJECT_ROOT, Settings
from app.core.exceptions import ServiceError, register_exception_handlers
from app.core.logging_setup import configure_logging
from app.core.middleware import RequestSizeLimit
from app.db.migrate import upgrade_to_head
from app.db.session import create_db_engine, create_sessions
from app.runtime.runtime import BackgroundRuntime, build_clients
from app.services.runtime_catalog import RuntimeCatalog
from app.storage.script_storage import ScriptStorage

logger = logging.getLogger(__name__)


class SchemaCheck:
    """Whether this code can serve the database's schema revision.

    Ready at this code's head, and also at a revision it has never seen:
    a newer release migrated ahead (rollout or rollback), and migrations
    stay backward compatible (docs/architecture.md). An older known
    revision, or none, still needs `alembic upgrade head`.
    """

    def __init__(self, migrations: Path = PROJECT_ROOT / "migrations"):
        scripts = ScriptDirectory(str(migrations))
        self.head = scripts.get_current_head()
        self.known = {rev.revision for rev in scripts.walk_revisions()}

    def ready(self, revision: str | None) -> bool:
        if revision is None:
            return False
        if revision == self.head:
            return True
        return revision not in self.known


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    engine = create_db_engine(settings)
    sessions = create_sessions(engine)
    schema = SchemaCheck()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if settings.db_auto_migrate:
            await run_in_threadpool(upgrade_to_head, settings)
        runtime = None
        if settings.background_enabled and settings.executor_base_url:
            runtime = BackgroundRuntime(
                settings, sessions, build_clients(settings)
            )
            runtime.start()
        app.state.runtime = runtime
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if runtime is not None:
                await run_in_threadpool(runtime.stop)
            engine.dispose()

    docs = settings.docs_enabled
    app = FastAPI(
        title="Notebook Batch Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.storage = ScriptStorage(settings.storage_root)
    app.state.runtime = None
    app.state.runtime_catalog = RuntimeCatalog(settings.batch_runtimes)
    app.state.ready = False
    app.add_middleware(
        RequestSizeLimit, max_bytes=settings.upload_max_bytes + 64 * 1024
    )
    register_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    def health() -> JSONResponse:
        runtime = app.state.runtime
        if runtime is not None and not runtime.healthy():
            return JSONResponse({"status": "stalled"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    def readiness() -> dict[str, str]:
        if not app.state.ready:
            raise ServiceError(503, "NOT_READY", "Starting or stopping.")
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM batch.alembic_version")
            )
        if not schema.ready(revision):
            raise ServiceError(503, "MIGRATION_REQUIRED", "Apply migrations.")
        if not settings.storage_root.is_dir():
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Mount storage first."
            )
        return {"status": "ready"}

    app.include_router(api_router)
    return app
