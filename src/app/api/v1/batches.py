"""Batch registration, update, activation, delete and skip history."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import (
    AdminCaller,
    AdminTenant,
    Batches,
    Cursor,
    Ident,
    Key,
    Limit,
    Owner,
    Revision,
    mutation_response,
)
from app.core.security import Principal
from app.schemas.batch import (
    BatchPage,
    BatchView,
    CreateBatch,
    DisableByOwner,
    DisabledCount,
    SkippedPage,
    UpdateBatch,
)

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", response_model=BatchView, status_code=201)
def create_batch(
    request: Request,
    identity: Ident,
    key: Key,
    service: Batches,
    data: CreateBatch,
) -> JSONResponse:
    return mutation_response(
        service.create_batch(identity, key, data), request.url.path
    )


@router.post(":disable-by-owner", response_model=DisabledCount)
def disable_by_owner(
    caller: AdminCaller,
    tenant: AdminTenant,
    service: Batches,
    data: DisableByOwner,
) -> DisabledCount:
    """Called when the owner is deactivated; repeating it is harmless.

    Applies to the caller's tenant (X-Tenant-Id for a relay key).
    """
    return service.disable_by_owner(
        Principal(tenant, data.owner_id), caller.client
    )


@router.get("", response_model=BatchPage)
def list_batches(
    owner: Owner,
    service: Batches,
    limit: Limit = 50,
    cursor: Cursor = None,
    script_id: UUID | None = None,
) -> BatchPage:
    return service.list_batches(owner, limit, cursor, script_id)


@router.get("/{batch_id}", response_model=BatchView)
def get_batch(
    response: Response, owner: Owner, service: Batches, batch_id: UUID
) -> BatchView:
    batch = service.get_batch(owner, batch_id)
    response.headers["ETag"] = f'"{batch.revision}"'
    return batch


@router.patch("/{batch_id}", response_model=BatchView)
def update_batch(
    identity: Ident,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
    data: UpdateBatch,
) -> JSONResponse:
    return mutation_response(
        service.update_batch(identity, batch_id, key, expected, data)
    )


@router.delete("/{batch_id}", status_code=204)
def delete_batch(
    owner: Owner,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> Response:
    status, _ = service.delete_batch(owner, batch_id, key, expected)
    return Response(status_code=status)


@router.post("/{batch_id}/enable", response_model=BatchView)
def enable_batch(
    identity: Ident,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    # Without an Executor the runtime catalog answers 503 itself.
    return mutation_response(
        service.set_enabled(identity, batch_id, key, expected, True)
    )


@router.post("/{batch_id}/disable", response_model=BatchView)
def disable_batch(
    identity: Ident,
    service: Batches,
    batch_id: UUID,
    key: Key,
    expected: Revision,
) -> JSONResponse:
    return mutation_response(
        service.set_enabled(identity, batch_id, key, expected, False)
    )


@router.get("/{batch_id}/skipped-windows", response_model=SkippedPage)
def list_skipped_windows(
    owner: Owner,
    service: Batches,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> SkippedPage:
    return service.skipped(owner, batch_id, limit, cursor)
