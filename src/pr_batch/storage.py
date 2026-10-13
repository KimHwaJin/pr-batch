"""Immutable originals and cell files on an operator-owned shared volume."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pr_batch.errors import ServiceError
from pr_batch.notebooks import Notebook


class NotebookStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def safe_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ServiceError(503, "STORAGE_UNAVAILABLE", "Invalid storage.")
        candidate = self.root
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ServiceError(
                    503, "STORAGE_UNAVAILABLE", "Invalid storage."
                )
        if not candidate.resolve().is_relative_to(self.root):
            raise ServiceError(503, "STORAGE_UNAVAILABLE", "Invalid storage.")
        return candidate

    def save(
        self, owner: str, notebook_id: UUID, version: UUID, notebook: Notebook
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self.root.is_dir():
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Mount storage before registering."
            )
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        directory = (
            f"batch-input/users/{owner_hash}/notebooks/{notebook_id}"
            f"/versions/{version}"
        )
        destination = self.safe_path(directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".upload-", dir=destination.parent)
        )
        cells: list[dict[str, Any]] = []
        try:
            (temporary / "cells").mkdir(mode=0o750)
            self.write_file(temporary / "original.ipynb", notebook.raw)
            for sequence, cell in enumerate(notebook.cells):
                name = f"cells/{sequence:06d}.py"
                data = cell.source.encode("utf-8")
                self.write_file(temporary / name, data)
                cells.append(
                    {
                        "original_index": cell.original_index,
                        "sequence": sequence,
                        "path": f"{directory}/{name}",
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            temporary.chmod(0o750)
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return f"{directory}/original.ipynb", cells

    @staticmethod
    def write_file(path: Path, data: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o640)

    def read(self, relative: str, sha256: str, size: int) -> bytes:
        try:
            with self.safe_path(relative).open("rb") as handle:
                data = handle.read(size + 1)
        except OSError as exc:
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Notebook storage unavailable."
            ) from exc
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ServiceError(
                503, "STORAGE_INTEGRITY_ERROR", "Notebook integrity mismatch."
            )
        return data
