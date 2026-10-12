"""Validate uploaded notebooks without executing or rewriting their source."""

import hashlib
import json
from dataclasses import dataclass

import nbformat
from jsonschema import ValidationError

from pr_batch.errors import ServiceError
from pr_batch.settings import Settings


@dataclass(frozen=True)
class CodeCell:
    original_index: int
    source: str


@dataclass(frozen=True)
class Notebook:
    raw: bytes
    sha256: str
    code_hash: str
    cells: tuple[CodeCell, ...]


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
        before = json.dumps(document, ensure_ascii=False).encode("utf-8")
        # Do not repair missing IDs or strip invalid metadata implicitly.
        nbformat.validate(document)
        if json.dumps(document, ensure_ascii=False).encode("utf-8") != before:
            raise ValueError("Notebook requires explicit normalization.")
        node = nbformat.from_dict(document)
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
    if len(node.cells) > settings.max_code_cells:
        raise ServiceError(422, "TOO_MANY_CELLS", "Cell limit exceeded.")
    for index, cell in enumerate(node.cells):
        if cell.cell_type != "code":
            raise ServiceError(
                422, "NON_CODE_CELL", "Only code cells are supported."
            )
        source = cell.source
        if isinstance(source, list):
            source = "".join(source)
        if len(source.encode("utf-8")) > settings.cell_max_bytes:
            raise ServiceError(422, "CELL_TOO_LARGE", "Cell is too large.")
        if source.strip():
            cells.append(CodeCell(index, source))
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
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(code).hexdigest(),
        tuple(cells),
    )
