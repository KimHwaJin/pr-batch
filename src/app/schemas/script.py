"""Script upload, version and cell contracts."""

from datetime import datetime
from uuid import UUID

from app.domain.scripts import ScriptType
from app.schemas.common import Contract, Name


class RenameScript(Contract):
    name: Name


class CellCounts(Contract):
    total: int
    executable: int
    skipped: int


class CellEntry(Contract):
    index: int
    type: str
    executed: bool
    sequence: int | None = None


class VersionView(Contract):
    id: UUID
    script_id: UUID
    number: int
    filename: str
    sha256: str
    code_hash: str
    size_bytes: int
    cells: CellCounts
    created_at: datetime


class VersionDetail(VersionView):
    cell_list: list[CellEntry]


class ScriptView(Contract):
    id: UUID
    name: str
    type: ScriptType
    revision: int
    latest_version: VersionView
    created_at: datetime
    updated_at: datetime


class ScriptPage(Contract):
    items: list[ScriptView]
    next_cursor: str | None


class VersionPage(Contract):
    items: list[VersionView]
    next_cursor: int | None
