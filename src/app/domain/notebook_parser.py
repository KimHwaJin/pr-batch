"""Validate uploaded notebooks without executing or rewriting their source."""

import copy
import hashlib
import json
import warnings
from dataclasses import dataclass
from typing import Any

import nbformat
from jsonschema import ValidationError
from nbformat.validator import normalize

from app.core.config import Settings
from app.core.exceptions import ServiceError


@dataclass(frozen=True)
class CodeCell:
    original_index: int
    source: str


@dataclass(frozen=True)
class Notebook:
    raw: bytes
    filename: str
    sha256: str
    code_hash: str
    cells: tuple[CodeCell, ...]
    # Every original cell: index, type, executed and execution sequence.
    cell_list: tuple[dict[str, Any], ...]


def reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


def parse_notebook(raw: bytes, filename: str, settings: Settings) -> Notebook:
    if not filename.lower().endswith(".ipynb"):
        raise ServiceError(422, "INVALID_NOTEBOOK", "Upload an .ipynb file.")
    if len(raw) > settings.upload_max_bytes:
        raise ServiceError(413, "UPLOAD_TOO_LARGE", "Notebook is too large.")
    try:
        document = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(document, dict) or document.get("nbformat") != 4:
            raise ValueError("Only notebook format 4 is supported.")
        # Validate a normalized copy (e.g. missing cell ids) so ordinary
        # notebooks are accepted; the stored original bytes stay untouched.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _, normalized = normalize(copy.deepcopy(document))
            nbformat.validate(normalized)
        # Lone surrogates parse as JSON but cannot be stored as UTF-8.
        json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        node = nbformat.from_dict(normalized)
    except (
        ValueError,
        TypeError,
        ValidationError,
        AttributeError,
        RecursionError,
    ) as exc:
        raise ServiceError(
            422, "INVALID_NOTEBOOK", "Invalid nbformat v4 notebook."
        ) from exc
    cells: list[CodeCell] = []
    cell_list: list[dict[str, Any]] = []
    for index, cell in enumerate(node.cells):
        entry: dict[str, Any] = {
            "index": index,
            "type": cell.cell_type,
            "executed": False,
        }
        cell_list.append(entry)
        # Markdown and raw cells are preserved in the original only.
        if cell.cell_type != "code":
            continue
        source = cell.source
        if isinstance(source, list):
            source = "".join(source)
        if len(source.encode("utf-8")) > settings.cell_max_bytes:
            raise ServiceError(422, "CELL_TOO_LARGE", "Cell is too large.")
        if source.strip():
            entry["executed"] = True
            entry["sequence"] = len(cells)
            cells.append(CodeCell(index, source))
    if len(cells) > settings.max_code_cells:
        raise ServiceError(422, "TOO_MANY_CELLS", "Cell limit exceeded.")
    if not cells:
        raise ServiceError(422, "EMPTY_NOTEBOOK", "No executable code cells.")
    language = node.metadata.get("language_info", {}).get("name")
    kernel_language = node.metadata.get("kernelspec", {}).get("language")
    if any(
        value and str(value).lower() != "python"
        for value in (language, kernel_language)
    ):
        raise ServiceError(422, "UNSUPPORTED_LANGUAGE", "Python is required.")
    code = json.dumps(
        [cell.source for cell in cells], ensure_ascii=False
    ).encode()
    return Notebook(
        raw,
        display_filename(filename),
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(code).hexdigest(),
        tuple(cells),
        tuple(cell_list),
    )


def display_filename(filename: str) -> str:
    """Base name for display only; never used to build storage paths."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or "notebook.ipynb"
