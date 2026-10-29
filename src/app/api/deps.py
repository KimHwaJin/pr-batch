"""Request dependencies: identity, write headers and service wiring."""

import re
from typing import Annotated, cast

from fastapi import Depends, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.core.security import (
    Caller,
    Identity,
    admin_caller,
    current_caller,
    current_identity,
    current_owner,
)
from app.domain.script_parser import parse_script
from app.domain.scripts import ParsedScript, ScriptType
from app.services.batch_service import BatchService
from app.services.idempotency import MutationResult
from app.services.run_service import RunService
from app.services.runtime_catalog import RuntimeCatalog
from app.services.script_service import ScriptService

Owner = Annotated[str, Depends(current_owner)]
Ident = Annotated[Identity, Depends(current_identity)]
KeyCaller = Annotated[Caller, Depends(current_caller)]
AdminCaller = Annotated[Caller, Depends(admin_caller)]
Key = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[!-~]+$",
    ),
]
Limit = Annotated[int, Query(ge=1, le=100)]
# The previous page's `next_cursor`, sent back verbatim.
Cursor = Annotated[str | None, Query(max_length=1024)]


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_script_service(request: Request) -> ScriptService:
    state = request.app.state
    return ScriptService(state.sessions, state.storage)


def get_batch_service(request: Request) -> BatchService:
    state = request.app.state
    return BatchService(state.sessions, state.runtime_catalog)


def get_runtime_catalog(request: Request) -> RuntimeCatalog:
    return cast(RuntimeCatalog, request.app.state.runtime_catalog)


def get_run_service(request: Request) -> RunService:
    return RunService(request.app.state.sessions)


SettingsDep = Annotated[Settings, Depends(get_settings)]
Scripts = Annotated[ScriptService, Depends(get_script_service)]
Batches = Annotated[BatchService, Depends(get_batch_service)]
Runs = Annotated[RunService, Depends(get_run_service)]
Catalog = Annotated[RuntimeCatalog, Depends(get_runtime_catalog)]


def expected_revision(
    value: Annotated[str, Header(alias="If-Match", max_length=32)],
) -> int:
    if not re.fullmatch(r'"[1-9][0-9]*"', value):
        raise ServiceError(422, "INVALID_REVISION", 'Use If-Match: "1".')
    return int(value[1:-1])


Revision = Annotated[int, Depends(expected_revision)]


def require_executor(settings: Settings) -> None:
    if settings.executor_base_url is None:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )


def uploaded_script(
    settings: Settings, script_type: ScriptType, file: UploadFile
) -> ParsedScript:
    try:
        raw = file.file.read(settings.upload_max_bytes + 1)
        return parse_script(script_type, raw, file.filename or "", settings)
    finally:
        file.file.close()


def mutation_response(
    result: MutationResult, collection: str | None = None
) -> JSONResponse:
    """Replay-safe JSON response with ETag and, on create, Location."""
    status, payload = result
    headers = {}
    if "revision" in payload:
        headers["ETag"] = f'"{payload["revision"]}"'
    if status == 201 and collection and "id" in payload:
        headers["Location"] = f"{collection}/{payload['id']}"
    return JSONResponse(payload, status_code=status, headers=headers)
