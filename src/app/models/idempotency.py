"""Stored first responses for idempotent writes."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class IdempotencyRecord(Base):
    """Kept for idempotency_retention_days, then purged (keys expire)."""

    __tablename__ = "idempotency_records"
    __table_args__ = (Index("ix_idempotency_created", "created_at"),)

    # Scoped like resources: the same key in another tenant is new.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status_code: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
