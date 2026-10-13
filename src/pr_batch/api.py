"""Internal registration HTTP API. No scheduling worker is started here."""

import json
import re
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from pr_batch.auth import current_owner
from pr_batch.errors import ServiceError
from pr_batch.notebooks import Notebook, parse_notebook
from pr_batch.runs import RunDetail, RunPage, RunService, RunView
from pr_batch.scheduler import SchedulingService, SkippedPage
from pr_batch.schemas import (
    BatchPage,
    BatchView,
    CreateBatch,
    ManualRun,
    RenameBatch,
    UpdateSchedule,
    VersionPage,
    VersionView,
)
from pr_batch.service import MutationResult, RegistrationService
from pr_batch.settings import Settings

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])
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


def registration(request: Request) -> RegistrationService:
    return cast(RegistrationService, request.app.state.registration)


def configuration(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def check_kernel(request: Request, kernel: str) -> None:
    if kernel not in configuration(request).allowed_kernel_profiles:
        raise ServiceError(422, "UNKNOWN_KERNEL", "Kernel is not allowed.")


def uploaded_notebook(request: Request, file: UploadFile) -> Notebook:
    settings = configuration(request)
    try:
        raw = file.file.read(settings.upload_max_bytes + 1)
        return parse_notebook(raw, file.filename or "", settings)
    finally:
        file.file.close()


def mutation_response(result: MutationResult) -> JSONResponse:
    status, payload = result
    headers = {}
    if "revision" in payload:
        headers["ETag"] = f'"{payload["revision"]}"'
    return JSONResponse(payload, status_code=status, headers=headers)


def expected_revision(
    value: Annotated[str, Header(alias="If-Match", max_length=32)],
) -> int:
    if not re.fullmatch(r'"[1-9][0-9]*"', value):
        raise ServiceError(422, "INVALID_REVISION", 'Use If-Match: "1".')
    return int(value[1:-1])


Revision = Annotated[int, Depends(expected_revision)]


@router.post("", response_model=BatchView, status_code=201)
def create_batch(
    request: Request,
    owner: Owner,
    key: Key,
    file: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form(max_length=16384)],
) -> JSONResponse:
    try:
        data = CreateBatch.model_validate_json(metadata)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ServiceError(
            422, "INVALID_METADATA", "Invalid registration metadata."
        ) from exc
    check_kernel(request, data.kernel_profile)
    notebook = uploaded_notebook(request, file)
    return mutation_response(
        registration(request).create(owner, key, data, notebook)
    )


@router.get("", response_model=BatchPage)
def list_batches(
    request: Request,
    owner: Owner,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> BatchPage:
    return registration(request).list_batches(owner, limit, cursor)


@router.get("/{batch_id}", response_model=BatchView)
def get_batch(
    request: Request, response: Response, owner: Owner, batch_id: UUID
) -> BatchView:
    batch = registration(request).get(owner, batch_id)
    response.headers["ETag"] = f'"{batch.revision}"'
    return batch


@router.patch("/{batch_id}", response_model=BatchView)
def rename_batch(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
    data: RenameBatch,
) -> JSONResponse:
    return mutation_response(
        registration(request).rename(owner, batch_id, key, expected, data.name)
    )


@router.post(
    "/{batch_id}/versions", response_model=VersionView, status_code=201
)
def add_version(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    notebook = uploaded_notebook(request, file)
    return mutation_response(
        registration(request).add_version(owner, batch_id, key, notebook)
    )


@router.get("/{batch_id}/versions", response_model=VersionPage)
def list_versions(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> VersionPage:
    return registration(request).versions(owner, batch_id, limit, cursor)


@router.get("/{batch_id}/versions/{version_id}/content")
def download_version(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    version_id: UUID,
) -> Response:
    data = registration(request).download(owner, batch_id, version_id)
    return Response(
        data,
        media_type="application/x-ipynb+json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{version_id}.ipynb"'
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.put("/{batch_id}/schedule", response_model=BatchView)
def set_schedule(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
    data: UpdateSchedule,
) -> JSONResponse:
    check_kernel(request, data.kernel_profile)
    return mutation_response(
        registration(request).set_schedule(
            owner, batch_id, key, expected, data
        )
    )


@router.post("/{batch_id}/runs", response_model=RunView, status_code=202)
def create_run(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    data: ManualRun | None = None,
) -> JSONResponse:
    if configuration(request).executor_base_url is None:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )
    return mutation_response(
        RunService(registration(request)).create(owner, batch_id, key)
    )


@router.get("/{batch_id}/runs", response_model=RunPage)
def list_runs(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: UUID | None = None,
) -> RunPage:
    return RunService(registration(request)).list(
        owner, batch_id, limit, cursor
    )


@router.get("/{batch_id}/runs/{run_id}", response_model=RunDetail)
def get_run(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    run_id: UUID,
) -> RunDetail:
    return RunService(registration(request)).get(owner, batch_id, run_id)


@router.post("/{batch_id}/enable", response_model=BatchView)
def enable_batch(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    settings = configuration(request)
    if settings.executor_base_url is None:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )
    return mutation_response(
        SchedulingService(registration(request)).set_enabled(
            owner,
            batch_id,
            key,
            expected,
            True,
            settings.allowed_kernel_profiles,
        )
    )


@router.post("/{batch_id}/disable", response_model=BatchView)
def disable_batch(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    return mutation_response(
        SchedulingService(registration(request)).set_enabled(
            owner,
            batch_id,
            key,
            expected,
            False,
            configuration(request).allowed_kernel_profiles,
        )
    )


@router.get("/{batch_id}/skipped-windows", response_model=SkippedPage)
def list_skipped_windows(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: UUID | None = None,
) -> SkippedPage:
    return SchedulingService(registration(request)).skipped(
        owner, batch_id, limit, cursor
    )
