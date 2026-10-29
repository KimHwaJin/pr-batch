"""Immutable script originals and cell files on the shared volume."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.core.exceptions import ServiceError
from app.core.security import TENANT, USER_ID, Principal
from app.domain.scripts import ORIGINAL_NAMES, ParsedScript


def tenant_ok(tenant: str) -> bool:
    try:
        TENANT.validate_python(tenant)
    except ValidationError:
        return False
    return True


class ScriptStorage:
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
        self,
        owner: Principal,
        script_id: UUID,
        version: UUID,
        parsed: ParsedScript,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Write the version; returns (directory, original path, cells)."""
        if not self.root.is_dir():
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Mount storage before registering."
            )
        # Checked at authentication; checked again since they form a path.
        if not USER_ID.fullmatch(owner.user_id) or not tenant_ok(owner.tenant):
            raise ServiceError(400, "INVALID_IDENTITY", "Invalid owner.")
        # Same tenants/{tenant}/users/{user} layout as Executor workspaces.
        directory = (
            f"batch/inputs/tenants/{owner.tenant}/users/{owner.user_id}"
            f"/scripts/{script_id}/versions/{version}"
        )
        destination = self.safe_path(directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".upload-", dir=destination.parent)
        )
        cells: list[dict[str, Any]] = []
        try:
            (temporary / "cells").mkdir(mode=0o750)
            original = ORIGINAL_NAMES[parsed.type]
            self.write_file(temporary / original, parsed.raw)
            for sequence, cell in enumerate(parsed.cells):
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
        return directory, f"{directory}/{original}", cells

    def discard(self, directory: str) -> None:
        """Remove a version saved by a write that did not commit."""
        shutil.rmtree(self.safe_path(directory), ignore_errors=True)

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
                503, "STORAGE_UNAVAILABLE", "Script storage unavailable."
            ) from exc
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise ServiceError(
                503, "STORAGE_INTEGRITY_ERROR", "Script integrity mismatch."
            )
        return data
