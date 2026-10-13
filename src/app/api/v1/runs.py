"""Manual run admission and run history endpoints under a batch."""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
