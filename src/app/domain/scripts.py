"""Script types and the parsed form every type is reduced to.

A script is reduced to ordered code units that become Executor steps.
NOTEBOOK maps each non-empty code cell to one unit. A new type needs a
member here, its file name and media type, a parser registered in
`app.domain.script_parser`, its runtimes and a migration for the CHECK.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ScriptType(StrEnum):
    NOTEBOOK = "NOTEBOOK"


# Stored original file name per type (never the user's file name).
ORIGINAL_NAMES = {
    ScriptType.NOTEBOOK: "original.ipynb",
}
MEDIA_TYPES = {
    ScriptType.NOTEBOOK: "application/x-ipynb+json",
}


@dataclass(frozen=True)
class CodeCell:
    original_index: int
    source: str


@dataclass(frozen=True)
class ParsedScript:
    type: ScriptType
    raw: bytes
    filename: str
    sha256: str
    code_hash: str
    cells: tuple[CodeCell, ...]
    # Every original unit: index, type, executed and execution sequence.
    cell_list: tuple[dict[str, Any], ...]
