"""Script upload, version and cell contracts."""

from uuid import UUID

from app.domain.scripts import ScriptType
from app.schemas.common import Audited, Contract, Name


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


class VersionView(Audited, Contract):
    id: UUID
    script_id: UUID
    version_number: int
    filename: str
    sha256: str
    code_hash: str
    size_bytes: int
    cells: CellCounts


class VersionDetail(VersionView):
    cell_list: list[CellEntry]


class ScriptView(Audited, Contract):
    id: UUID
    name: str
    type: ScriptType
    revision: int
    latest_version: VersionView


class ScriptPage(Contract):
    items: list[ScriptView]
    next_cursor: str | None


class VersionPage(Contract):
    items: list[VersionView]
    next_cursor: int | None
