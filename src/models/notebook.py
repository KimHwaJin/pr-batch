"""Uploaded notebooks and their immutable versions."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, utcnow


class Notebook(Base):
    __tablename__ = "notebooks"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="notebook_revision_positive"),
        Index("ix_notebooks_owner_created", "owner_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(default=1)
    latest_version_number: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class NotebookVersion(Base):
    __tablename__ = "notebook_versions"
    __table_args__ = (
        UniqueConstraint("notebook_id", "number", name="uq_version_number"),
        UniqueConstraint("notebook_id", "sha256", name="uq_version_content"),
        UniqueConstraint("notebook_id", "id", name="uq_version_notebook_id"),
        CheckConstraint("number >= 1", name="version_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    notebook_id: Mapped[UUID] = mapped_column(ForeignKey("batch.notebooks.id"))
    number: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    original_path: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int]
    # Executable code cells submitted to the Executor, in order.
    cell_map: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # Every original cell with its type and whether it is executed.
    cell_list: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
