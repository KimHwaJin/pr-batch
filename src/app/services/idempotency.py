"""Exactly-once responses for retried writes, keyed per owner."""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.models import IdempotencyRecord

MutationResult = tuple[int, dict[str, Any]]


def fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def stored_result(
    record: IdempotencyRecord, request_hash: str
) -> MutationResult | None:
    if record.request_hash != request_hash:
        raise ServiceError(
            409, "IDEMPOTENCY_CONFLICT", "Key used for another request."
        )
    if record.response is None:
        return None
    assert record.status_code is not None
    return record.status_code, record.response


class Idempotency:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def replay(
        self, owner: str, operation: str, key: str, payload: object
    ) -> MutationResult | None:
        """The stored first response, if any, without starting a write.

        Lets callers skip remote lookups that only a first attempt needs,
        so a retried request is answered even while the Executor is down.
        """
        with self.sessions() as session:
            record = session.get(IdempotencyRecord, (owner, operation, key))
            if record is None:
                return None
            return stored_result(record, fingerprint(payload))

    def run(
        self,
        owner: str,
        operation: str,
        key: str,
        payload: object,
        action: Callable[[Session], MutationResult],
    ) -> MutationResult:
        request_hash = fingerprint(payload)
        with self.sessions.begin() as session:
            # The unique insert waits for a concurrent identical request's
            # commit. Placeholder and response commit together or roll back.
            session.execute(
                insert(IdempotencyRecord)
                .values(
                    owner_id=owner,
                    operation=operation,
                    key=key,
                    request_hash=request_hash,
                )
                .on_conflict_do_nothing()
            )
            record = session.scalar(
                select(IdempotencyRecord)
                .where(
                    IdempotencyRecord.owner_id == owner,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
                .with_for_update()
            )
            assert record is not None
            if (stored := stored_result(record, request_hash)) is not None:
                return stored
            status, response = action(session)
            record.response = response
            record.status_code = status
        return status, response
