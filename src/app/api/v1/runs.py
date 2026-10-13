"""Run admission, cancellation, report retry, history and result relay."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import (
    Key,
    Limit,
    Owner,
    Runs,
    SettingsDep,
    mutation_response,
    require_executor,
)
from app.schemas.run import ManualRun, RunDetail, RunPage, RunView

router = APIRouter(prefix="/batches/{batch_id}/runs", tags=["runs"])


@router.post("", response_model=RunView, status_code=202)
def create_run(
    owner: Owner,
    settings: SettingsDep,
    service: Runs,
    batch_id: UUID,
    key: Key,
    data: ManualRun | None = None,
) -> JSONResponse:
    require_executor(settings)
    return mutation_response(service.create(owner, batch_id, key))


@router.get("", response_model=RunPage)
def list_runs(
    owner: Owner,
    service: Runs,
    batch_id: UUID,
    limit: Limit = 50,
    cursor: UUID | None = None,
) -> RunPage:
    return service.list(owner, batch_id, limit, cursor)


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    owner: Owner, service: Runs, batch_id: UUID, run_id: UUID
) -> RunDetail:
    return service.get(owner, batch_id, run_id)


@router.post("/{run_id}/cancel", response_model=RunView, status_code=202)
def cancel_run(
    owner: Owner, service: Runs, batch_id: UUID, run_id: UUID, key: Key
) -> JSONResponse:
    return mutation_response(service.cancel(owner, batch_id, run_id, key))


@router.post(
    "/{run_id}/report-retries", response_model=RunView, status_code=202
)
def retry_report(
    owner: Owner, service: Runs, batch_id: UUID, run_id: UUID, key: Key
) -> JSONResponse:
    return mutation_response(
        service.retry_report(owner, batch_id, run_id, key)
    )


def relay(
    request: Request,
    owner: str,
    service: Runs,
    settings: SettingsDep,
    batch_id: UUID,
    run_id: UUID,
    kind: Literal["NOTEBOOK", "REPORT"],
    filename: str,
) -> StreamingResponse:
    require_executor(settings)
    media_type, chunks = service.content(
        owner, batch_id, run_id, kind, request.app.state.executor
    )
    return StreamingResponse(
        chunks,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{run_id}/notebook/content")
def notebook_content(
    request: Request,
    owner: Owner,
    service: Runs,
    settings: SettingsDep,
    batch_id: UUID,
    run_id: UUID,
) -> StreamingResponse:
    return relay(
        request,
        owner,
        service,
        settings,
        batch_id,
        run_id,
        "NOTEBOOK",
        f"{run_id}.ipynb",
    )


@router.get("/{run_id}/report/content")
def report_content(
    request: Request,
    owner: Owner,
    service: Runs,
    settings: SettingsDep,
    batch_id: UUID,
    run_id: UUID,
) -> StreamingResponse:
    return relay(
        request,
        owner,
        service,
        settings,
        batch_id,
        run_id,
        "REPORT",
        f"{run_id}-report.md",
    )
