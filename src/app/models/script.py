"""Uploaded scripts (notebooks, later Python files) and their versions."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, SoftDeleteMixin


class Script(SoftDeleteMixin, Base):
    __tablename__ = "scripts"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="script_revision_positive"),
        CheckConstraint("type IN ('NOTEBOOK')", name="script_type"),
        Index("ix_scripts_owner_created", "owner_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    # Fixed at creation; every version must parse as this type.
    type: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(default=1)
    latest_version_number: Mapped[int] = mapped_column(default=1)


class ScriptVersion(AuditMixin, Base):
    __tablename__ = "script_versions"
    __table_args__ = (
        UniqueConstraint(
            "script_id", "version_number", name="uq_version_number"
        ),
        UniqueConstraint("script_id", "sha256", name="uq_version_content"),
        UniqueConstraint("script_id", "id", name="uq_version_script_id"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    script_id: Mapped[UUID] = mapped_column(ForeignKey("batch.scripts.id"))
    version_number: Mapped[int]  # v1, v2, ... within the script
    filename: Mapped[str] = mapped_column(String(255))
    original_path: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
    # Executable code cells submitted to the Executor, in order.
    cell_map: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # Every original cell with its type and whether it is executed.
    cell_list: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
