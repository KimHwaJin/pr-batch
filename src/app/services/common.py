"""Owner-scoped lookups, revisions and keyset cursors."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.exceptions import ServiceError
from app.models import Batch, BatchConfig, Script, ScriptVersion
from app.schemas.schedule import Policy, Schedule


def schedule_of(config: BatchConfig) -> Schedule | None:
    if config.schedule is None:
        return None
    return Schedule.model_validate(config.schedule)


def policy_of(config: BatchConfig) -> Policy:
    return Policy.model_validate(config.policy)


def owned_script(
    session: Session, owner: str, script_id: UUID, *, lock: bool = False
) -> Script:
    query = select(Script).where(
        Script.id == script_id, Script.owner_id == owner
    )
    if lock:
        query = query.with_for_update()
    result = session.scalar(query)
    if result is None:
        raise ServiceError(404, "NOT_FOUND", "Script not found.")
    return result


def owned_batch(
    session: Session, owner: str, batch_id: UUID, *, lock: bool = False
) -> Batch:
    query = select(Batch).where(Batch.id == batch_id, Batch.owner_id == owner)
    if lock:
        query = query.with_for_update()
    result = session.scalar(query)
    if result is None:
        raise ServiceError(404, "NOT_FOUND", "Batch not found.")
    return result


def script_version(
    session: Session, script_id: UUID, number_or_id: int | UUID
) -> ScriptVersion:
    column = (
        ScriptVersion.version_number
        if isinstance(number_or_id, int)
        else ScriptVersion.id
    )
    result = session.scalar(
        select(ScriptVersion).where(
            ScriptVersion.script_id == script_id,
            column == number_or_id,
        )
    )
    if result is None:
        raise ServiceError(404, "NOT_FOUND", "Version not found.")
    return result


def current_config(session: Session, batch: Batch) -> BatchConfig:
    result = session.get(BatchConfig, (batch.id, batch.config_number))
    if result is None:
        raise ServiceError(503, "INCONSISTENT_STATE", "Batch config missing.")
    return result


def config_version(session: Session, config: BatchConfig) -> ScriptVersion:
    result = session.get(ScriptVersion, config.script_version_id)
    if result is None:
        raise ServiceError(503, "INCONSISTENT_STATE", "Version missing.")
    return result


def check_revision(revision: int, expected: int) -> None:
    if revision != expected:
        raise ServiceError(409, "REVISION_CONFLICT", "Reload the resource.")


def encode_cursor(created_at: datetime, identifier: UUID) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([created_at.isoformat(), str(identifier)]).encode()
    ).decode()


def before_cursor(
    created_at: InstrumentedAttribute[datetime],
    identifier: InstrumentedAttribute[UUID],
    cursor: str,
) -> ColumnElement[bool]:
    """Keyset condition for rows older than the opaque cursor."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor))
        timestamp = datetime.fromisoformat(decoded[0])
        last = UUID(decoded[1])
        if timestamp.tzinfo is None:
            raise ValueError("Timezone required.")
    except (
        ValueError,
        TypeError,
        IndexError,
        KeyError,
        binascii.Error,
    ) as exc:
        raise ServiceError(422, "INVALID_CURSOR", "Invalid cursor.") from exc
    return tuple_(created_at, identifier) < tuple_(timestamp, last)
