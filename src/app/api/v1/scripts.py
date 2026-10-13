"""Script upload, version and download endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.api.deps import (
    Key,
    Limit,
    Owner,
    Revision,
    Scripts,
    SettingsDep,
    mutation_response,
    uploaded_script,
)
from app.domain.scripts import MEDIA_TYPES, ScriptType
from app.schemas.common import Name
from app.schemas.script import (
    RenameScript,
    ScriptPage,
    ScriptView,
    VersionDetail,
    VersionPage,
    VersionView,
)

router = APIRouter(prefix="/scripts", tags=["scripts"])


@router.post("", response_model=ScriptView, status_code=201)
def create_script(
    request: Request,
    owner: Owner,
    key: Key,
    settings: SettingsDep,
    service: Scripts,
    file: Annotated[UploadFile, File()],
    type: Annotated[ScriptType, Form()],
    name: Annotated[Name | None, Form()] = None,
) -> JSONResponse:
    parsed = uploaded_script(settings, type, file)
    return mutation_response(
        service.create_script(owner, key, name, parsed), request.url.path
    )


@router.get("", response_model=ScriptPage)
def list_scripts(
    owner: Owner,
    service: Scripts,
    limit: Limit = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> ScriptPage:
    return service.list_scripts(owner, limit, cursor)


@router.get("/{script_id}", response_model=ScriptView)
def get_script(
    response: Response, owner: Owner, service: Scripts, script_id: UUID
) -> ScriptView:
    script = service.get_script(owner, script_id)
    response.headers["ETag"] = f'"{script.revision}"'
    return script


@router.patch("/{script_id}", response_model=ScriptView)
def rename_script(
    owner: Owner,
    service: Scripts,
    script_id: UUID,
    key: Key,
    expected: Revision,
    data: RenameScript,
) -> JSONResponse:
    return mutation_response(
        service.rename_script(owner, script_id, key, expected, data.name)
    )


@router.post(
    "/{script_id}/versions", response_model=VersionView, status_code=201
)
def add_version(
    request: Request,
    owner: Owner,
    settings: SettingsDep,
    service: Scripts,
    script_id: UUID,
    key: Key,
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    # A version is always parsed as the script's own type.
    parsed = uploaded_script(
        settings, service.script_type(owner, script_id), file
    )
    return mutation_response(
        service.add_version(owner, script_id, key, parsed), request.url.path
    )


@router.get("/{script_id}/versions", response_model=VersionPage)
def list_versions(
    owner: Owner,
    service: Scripts,
    script_id: UUID,
    limit: Limit = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> VersionPage:
    return service.versions(owner, script_id, limit, cursor)


@router.get("/{script_id}/versions/{version_id}", response_model=VersionDetail)
def get_version(
    owner: Owner, service: Scripts, script_id: UUID, version_id: UUID
) -> VersionDetail:
    return service.version(owner, script_id, version_id)


@router.get("/{script_id}/versions/{version_id}/content")
def download_version(
    owner: Owner, service: Scripts, script_id: UUID, version_id: UUID
) -> Response:
    data, script_type = service.download(owner, script_id, version_id)
    extension = ".ipynb" if script_type == ScriptType.NOTEBOOK else ".py"
    return Response(
        data,
        media_type=MEDIA_TYPES[script_type],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{version_id}{extension}"'
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
