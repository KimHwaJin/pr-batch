"""Request dependencies: identity, write headers and service wiring."""

import re
from typing import Annotated, cast

from fastapi import Depends, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.core.security import current_owner
from app.domain.notebook_parser import Notebook, parse_notebook
from app.services.batch_service import BatchService
from app.services.idempotency import MutationResult
from app.services.notebook_service import NotebookService
from app.services.run_service import RunService

Owner = Annotated[str, Depends(current_owner)]
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


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_notebook_service(request: Request) -> NotebookService:
    state = request.app.state
    return NotebookService(state.sessions, state.storage)


def get_batch_service(request: Request) -> BatchService:
    return BatchService(request.app.state.sessions)


def get_run_service(request: Request) -> RunService:
    return RunService(request.app.state.sessions)


SettingsDep = Annotated[Settings, Depends(get_settings)]
Notebooks = Annotated[NotebookService, Depends(get_notebook_service)]
Batches = Annotated[BatchService, Depends(get_batch_service)]
Runs = Annotated[RunService, Depends(get_run_service)]


def expected_revision(
    value: Annotated[str, Header(alias="If-Match", max_length=32)],
) -> int:
    if not re.fullmatch(r'"[1-9][0-9]*"', value):
        raise ServiceError(422, "INVALID_REVISION", 'Use If-Match: "1".')
    return int(value[1:-1])


Revision = Annotated[int, Depends(expected_revision)]


def check_kernel(settings: Settings, kernel: str | None) -> None:
    if kernel is not None and kernel not in settings.allowed_kernel_profiles:
        raise ServiceError(422, "UNKNOWN_KERNEL", "Kernel is not allowed.")


def require_executor(settings: Settings) -> None:
    if settings.executor_base_url is None:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )


def uploaded_notebook(settings: Settings, file: UploadFile) -> Notebook:
    try:
        raw = file.file.read(settings.upload_max_bytes + 1)
        return parse_notebook(raw, file.filename or "", settings)
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
