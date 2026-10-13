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


class Idempotency:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def run(
        self,
        owner: str,
        operation: str,
        key: str,
        payload: object,
        action: Callable[[Session], MutationResult],
    ) -> MutationResult:
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            # The unique insert waits for a concurrent identical request's
            # commit. Placeholder and response commit together or roll back.
            session.execute(
                insert(IdempotencyRecord)
                .values(
                    owner_id=owner,
                    operation=operation,
                    key=key,
                    request_hash=fingerprint,
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
            if record.request_hash != fingerprint:
                raise ServiceError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "Key used for another request.",
                )
            if record.response is not None:
                assert record.status_code is not None
                return record.status_code, record.response
            status, response = action(session)
            record.response = response
            record.status_code = status
        return status, response
