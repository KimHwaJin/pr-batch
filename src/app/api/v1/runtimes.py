"""BATCH runtimes this deployment offers (registration choices)."""

from fastapi import APIRouter

from app.api.deps import Catalog, KeyCaller
from app.schemas.batch import RuntimeList

router = APIRouter(prefix="/runtimes", tags=["runtimes"])


@router.get("", response_model=RuntimeList)
def list_runtimes(caller: KeyCaller, catalog: Catalog) -> RuntimeList:
    return RuntimeList(items=catalog.available())
