"""Notebook upload, version and download endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from src.api.deps import (
    Key,
    Limit,
    Notebooks,
    Owner,
    Revision,
    SettingsDep,
    mutation_response,
    uploaded_notebook,
)
from src.schemas.common import Name
from src.schemas.notebook import (
    NotebookPage,
    NotebookView,
    RenameNotebook,
    VersionDetail,
    VersionPage,
    VersionView,
)

router = APIRouter(prefix="/notebooks", tags=["notebooks"])


@router.post("", response_model=NotebookView, status_code=201)
def create_notebook(
    request: Request,
    owner: Owner,
    key: Key,
    settings: SettingsDep,
    service: Notebooks,
    file: Annotated[UploadFile, File()],
    name: Annotated[Name | None, Form()] = None,
) -> JSONResponse:
    parsed = uploaded_notebook(settings, file)
    return mutation_response(
        service.create_notebook(owner, key, name, parsed), request.url.path
    )


@router.get("", response_model=NotebookPage)
def list_notebooks(
    owner: Owner,
    service: Notebooks,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> NotebookPage:
    return service.list_notebooks(owner, limit, cursor)


@router.get("/{notebook_id}", response_model=NotebookView)
def get_notebook(
    response: Response, owner: Owner, service: Notebooks, notebook_id: UUID
) -> NotebookView:
    notebook = service.get_notebook(owner, notebook_id)
    response.headers["ETag"] = f'"{notebook.revision}"'
    return notebook


@router.patch("/{notebook_id}", response_model=NotebookView)
def rename_notebook(
    owner: Owner,
    service: Notebooks,
    notebook_id: UUID,
    key: Key,
    expected: Revision,
    data: RenameNotebook,
) -> JSONResponse:
    return mutation_response(
        service.rename_notebook(owner, notebook_id, key, expected, data.name)
    )


@router.post(
    "/{notebook_id}/versions", response_model=VersionView, status_code=201
)
def add_version(
    request: Request,
    owner: Owner,
    settings: SettingsDep,
    service: Notebooks,
    notebook_id: UUID,
    key: Key,
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    parsed = uploaded_notebook(settings, file)
    return mutation_response(
        service.add_version(owner, notebook_id, key, parsed), request.url.path
    )


@router.get("/{notebook_id}/versions", response_model=VersionPage)
def list_versions(
    owner: Owner,
    service: Notebooks,
    notebook_id: UUID,
    limit: Limit = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> VersionPage:
    return service.versions(owner, notebook_id, limit, cursor)


@router.get(
    "/{notebook_id}/versions/{version_id}", response_model=VersionDetail
)
def get_version(
    owner: Owner, service: Notebooks, notebook_id: UUID, version_id: UUID
) -> VersionDetail:
    return service.version(owner, notebook_id, version_id)


@router.get("/{notebook_id}/versions/{version_id}/content")
def download_version(
    owner: Owner, service: Notebooks, notebook_id: UUID, version_id: UUID
) -> Response:
    data = service.download(owner, notebook_id, version_id)
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
