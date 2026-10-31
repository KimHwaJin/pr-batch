"""Owner-scoped (tenant and user) lookups, revisions and keyset cursors."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.exceptions import ServiceError, detail
from app.core.security import Principal
from app.models import Batch, BatchConfig, Script, ScriptVersion
from app.schemas.schedule import Policy, Schedule


def schedule_of(config: BatchConfig) -> Schedule | None:
    if config.schedule is None:
        return None
    return Schedule.model_validate(config.schedule)


def policy_of(config: BatchConfig) -> Policy:
    return Policy.model_validate(config.policy)


def owned_script(
    session: Session,
    owner: Principal,
    script_id: UUID,
    *,
    lock: bool = False,
    share: bool = False,
) -> Script:
    """A live script; `share` blocks its deletion until commit."""
    query = select(Script).where(
        Script.id == script_id,
        Script.tenant_id == owner.tenant,
        Script.owner_id == owner.user_id,
        Script.deleted_at.is_(None),
    )
    if lock or share:
        query = query.with_for_update(read=not lock)
    result = session.scalar(query)
    if result is None:
        raise ServiceError(404, "NOT_FOUND", "Script not found.")
    return result


def owned_batch(
    session: Session, owner: Principal, batch_id: UUID, *, lock: bool = False
) -> Batch:
    query = select(Batch).where(
        Batch.id == batch_id,
        Batch.tenant_id == owner.tenant,
        Batch.owner_id == owner.user_id,
        Batch.deleted_at.is_(None),
    )
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
        raise ServiceError(
            409,
            "REVISION_CONFLICT",
            "Reload the resource.",
            [
                detail(
                    ["header", "If-Match"],
                    "revision_conflict",
                    expected=expected,
                    current=revision,
                )
            ],
        )


# Every list returns an opaque `next_cursor` (AIP-158 page tokens): the
# client sends it back verbatim and never builds or parses one.
CURSOR_ERRORS = (ValueError, TypeError, IndexError, KeyError, binascii.Error)


def opaque(parts: list[object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()


def unwrap(cursor: str) -> list:
    decoded = json.loads(base64.urlsafe_b64decode(cursor))
    if not isinstance(decoded, list):
        raise ValueError("Not a cursor.")
    return decoded


def invalid_cursor() -> ServiceError:
    return ServiceError(422, "INVALID_CURSOR", "Invalid cursor.")


def encode_cursor(created_at: datetime, identifier: UUID) -> str:
    return opaque([created_at.isoformat(), str(identifier)])


def before_cursor(
    created_at: InstrumentedAttribute[datetime],
    identifier: InstrumentedAttribute[UUID],
    cursor: str,
) -> ColumnElement[bool]:
    """Keyset condition for rows older than the opaque cursor."""
    try:
        decoded = unwrap(cursor)
        timestamp = datetime.fromisoformat(decoded[0])
        last = UUID(decoded[1])
        if timestamp.tzinfo is None:
            raise ValueError("Timezone required.")
    except CURSOR_ERRORS as exc:
        raise invalid_cursor() from exc
    return tuple_(created_at, identifier) < tuple_(timestamp, last)


def encode_number_cursor(number: int) -> str:
    return opaque([number])


def decode_number_cursor(cursor: str) -> int:
    try:
        (number,) = unwrap(cursor)
        if type(number) is not int or number < 1:
            raise ValueError("Positive integer required.")
    except CURSOR_ERRORS as exc:
        raise invalid_cursor() from exc
    return number
