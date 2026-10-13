"""Batch registration, update, activation and skip-history endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import (
    Batches,
    Key,
    Limit,
    Owner,
    Revision,
    SettingsDep,
    check_kernel,
    mutation_response,
    require_executor,
)
from app.schemas.batch import (
    BatchPage,
    BatchView,
    CreateBatch,
    SkippedPage,
    UpdateBatch,
)

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", response_model=BatchView, status_code=201)
def create_batch(
    request: Request,
    owner: Owner,
    key: Key,
    settings: SettingsDep,
    service: Batches,
    data: CreateBatch,
) -> JSONResponse:
    check_kernel(settings, data.kernel_profile)
    return mutation_response(
        service.create_batch(owner, key, data), request.url.path
    )


@router.get("", response_model=BatchPage)
def list_batches(
    owner: Owner,
    service: Batches,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    notebook_id: UUID | None = None,
) -> BatchPage:
    return service.list_batches(owner, limit, cursor, notebook_id)


@router.get("/{batch_id}", response_model=BatchView)
def get_batch(
    response: Response, owner: Owner, service: Batches, batch_id: UUID
) -> BatchView:
    batch = service.get_batch(owner, batch_id)
    response.headers["ETag"] = f'"{batch.revision}"'
    return batch


@router.patch("/{batch_id}", response_model=BatchView)
def update_batch(
    owner: Owner,
    settings: SettingsDep,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
    data: UpdateBatch,
) -> JSONResponse:
    check_kernel(settings, data.kernel_profile)
    return mutation_response(
        service.update_batch(owner, batch_id, key, expected, data)
    )


@router.post("/{batch_id}/enable", response_model=BatchView)
def enable_batch(
    owner: Owner,
    settings: SettingsDep,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    require_executor(settings)
    return mutation_response(
        service.set_enabled(
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
    owner: Owner,
    settings: SettingsDep,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    return mutation_response(
        service.set_enabled(
            owner,
            batch_id,
            key,
            expected,
            False,
            settings.allowed_kernel_profiles,
        )
    )


@router.get("/{batch_id}/skipped-windows", response_model=SkippedPage)
def list_skipped_windows(
    owner: Owner,
    service: Batches,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: UUID | None = None,
) -> SkippedPage:
    return service.skipped(owner, batch_id, limit, cursor)
