"""Internal notebook and batch HTTP API. No worker is started here."""

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
    Name,
    NotebookPage,
    NotebookView,
    RenameNotebook,
    UpdateBatch,
    VersionDetail,
    VersionPage,
    VersionView,
)
from pr_batch.service import MutationResult, RegistrationService
from pr_batch.settings import Settings

notebooks = APIRouter(prefix="/api/v1/notebooks", tags=["notebooks"])
batches = APIRouter(prefix="/api/v1/batches", tags=["batches"])
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


def check_kernel(request: Request, kernel: str | None) -> None:
    allowed = configuration(request).allowed_kernel_profiles
    if kernel is not None and kernel not in allowed:
        raise ServiceError(422, "UNKNOWN_KERNEL", "Kernel is not allowed.")


def require_executor(request: Request) -> None:
    if configuration(request).executor_base_url is None:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )


def uploaded_notebook(request: Request, file: UploadFile) -> Notebook:
    settings = configuration(request)
    try:
        raw = file.file.read(settings.upload_max_bytes + 1)
        return parse_notebook(raw, file.filename or "", settings)
    finally:
        file.file.close()


def mutation_response(
    result: MutationResult, location: str | None = None
) -> JSONResponse:
    status, payload = result
    headers = {}
    if "revision" in payload:
        headers["ETag"] = f'"{payload["revision"]}"'
    if status == 201 and location and "id" in payload:
        headers["Location"] = f"{location}/{payload['id']}"
    return JSONResponse(payload, status_code=status, headers=headers)


def expected_revision(
    value: Annotated[str, Header(alias="If-Match", max_length=32)],
) -> int:
    if not re.fullmatch(r'"[1-9][0-9]*"', value):
        raise ServiceError(422, "INVALID_REVISION", 'Use If-Match: "1".')
    return int(value[1:-1])


Revision = Annotated[int, Depends(expected_revision)]


# Notebooks


@notebooks.post("", response_model=NotebookView, status_code=201)
def create_notebook(
    request: Request,
    owner: Owner,
    key: Key,
    file: Annotated[UploadFile, File()],
    name: Annotated[Name | None, Form()] = None,
) -> JSONResponse:
    parsed = uploaded_notebook(request, file)
    return mutation_response(
        registration(request).create_notebook(owner, key, name, parsed),
        notebooks.prefix,
    )


@notebooks.get("", response_model=NotebookPage)
def list_notebooks(
    request: Request,
    owner: Owner,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> NotebookPage:
    return registration(request).list_notebooks(owner, limit, cursor)


@notebooks.get("/{notebook_id}", response_model=NotebookView)
def get_notebook(
    request: Request, response: Response, owner: Owner, notebook_id: UUID
) -> NotebookView:
    notebook = registration(request).get_notebook(owner, notebook_id)
    response.headers["ETag"] = f'"{notebook.revision}"'
    return notebook


@notebooks.patch("/{notebook_id}", response_model=NotebookView)
def rename_notebook(
    request: Request,
    owner: Owner,
    notebook_id: UUID,
    key: Key,
    expected: Revision,
    data: RenameNotebook,
) -> JSONResponse:
    return mutation_response(
        registration(request).rename_notebook(
            owner, notebook_id, key, expected, data.name
        )
    )


@notebooks.post(
    "/{notebook_id}/versions", response_model=VersionView, status_code=201
)
def add_version(
    request: Request,
    owner: Owner,
    notebook_id: UUID,
    key: Key,
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    parsed = uploaded_notebook(request, file)
    return mutation_response(
        registration(request).add_version(owner, notebook_id, key, parsed),
        f"{notebooks.prefix}/{notebook_id}/versions",
    )


@notebooks.get("/{notebook_id}/versions", response_model=VersionPage)
def list_versions(
    request: Request,
    owner: Owner,
    notebook_id: UUID,
    limit: Limit = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> VersionPage:
    return registration(request).versions(owner, notebook_id, limit, cursor)


@notebooks.get(
    "/{notebook_id}/versions/{version_id}", response_model=VersionDetail
)
def get_version(
    request: Request, owner: Owner, notebook_id: UUID, version_id: UUID
) -> VersionDetail:
    return registration(request).version(owner, notebook_id, version_id)


@notebooks.get("/{notebook_id}/versions/{version_id}/content")
def download_version(
    request: Request,
    owner: Owner,
    notebook_id: UUID,
    version_id: UUID,
) -> Response:
    data = registration(request).download(owner, notebook_id, version_id)
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


# Batches


@batches.post("", response_model=BatchView, status_code=201)
def create_batch(
    request: Request, owner: Owner, key: Key, data: CreateBatch
) -> JSONResponse:
    check_kernel(request, data.kernel_profile)
    return mutation_response(
        registration(request).create_batch(owner, key, data),
        batches.prefix,
    )


@batches.get("", response_model=BatchPage)
def list_batches(
    request: Request,
    owner: Owner,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    notebook_id: UUID | None = None,
) -> BatchPage:
    return registration(request).list_batches(
        owner, limit, cursor, notebook_id
    )


@batches.get("/{batch_id}", response_model=BatchView)
def get_batch(
    request: Request, response: Response, owner: Owner, batch_id: UUID
) -> BatchView:
    batch = registration(request).get_batch(owner, batch_id)
    response.headers["ETag"] = f'"{batch.revision}"'
    return batch


@batches.patch("/{batch_id}", response_model=BatchView)
def update_batch(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
    data: UpdateBatch,
) -> JSONResponse:
    check_kernel(request, data.kernel_profile)
    return mutation_response(
        registration(request).update_batch(
            owner, batch_id, key, expected, data
        )
    )


@batches.post("/{batch_id}/enable", response_model=BatchView)
def enable_batch(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    require_executor(request)
    return mutation_response(
        SchedulingService(registration(request)).set_enabled(
            owner,
            batch_id,
            key,
            expected,
            True,
            configuration(request).allowed_kernel_profiles,
        )
    )


@batches.post("/{batch_id}/disable", response_model=BatchView)
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


@batches.post("/{batch_id}/runs", response_model=RunView, status_code=202)
def create_run(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    key: Key,
    data: ManualRun | None = None,
) -> JSONResponse:
    require_executor(request)
    return mutation_response(
        RunService(registration(request)).create(owner, batch_id, key)
    )


@batches.get("/{batch_id}/runs", response_model=RunPage)
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


@batches.get("/{batch_id}/runs/{run_id}", response_model=RunDetail)
def get_run(
    request: Request,
    owner: Owner,
    batch_id: UUID,
    run_id: UUID,
) -> RunDetail:
    return RunService(registration(request)).get(owner, batch_id, run_id)


@batches.get("/{batch_id}/skipped-windows", response_model=SkippedPage)
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
