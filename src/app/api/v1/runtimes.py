"""BATCH runtimes the Executor currently offers (registration choices)."""

from fastapi import APIRouter

from app.api.deps import Catalog, Owner
from app.schemas.batch import RuntimeList

router = APIRouter(prefix="/runtimes", tags=["runtimes"])


@router.get("", response_model=RuntimeList)
def list_runtimes(owner: Owner, catalog: Catalog) -> RuntimeList:
    return RuntimeList(items=catalog.available())
