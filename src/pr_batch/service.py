"""Transactional registration, immutable versions, and owner-scoped reads."""

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from pr_batch.errors import ServiceError
from pr_batch.models import (
    Batch,
    IdempotencyRecord,
    NotebookVersion,
    ScheduleRevision,
    utcnow,
)
from pr_batch.notebooks import Notebook
from pr_batch.schemas import (
    BatchPage,
    BatchView,
    CreateBatch,
    ScheduleConfig,
    ScheduleView,
    UpdateSchedule,
    VersionPage,
    VersionView,
)
from pr_batch.storage import NotebookStorage

MutationResult = tuple[int, dict[str, Any]]


def version_view(version: NotebookVersion) -> VersionView:
    return VersionView(
        id=version.id,
        number=version.number,
        sha256=version.sha256,
        code_hash=version.code_hash,
        size_bytes=version.size_bytes,
        code_cells=version.code_cells,
        created_at=version.created_at,
    )


def batch_view(batch: Batch, schedule: ScheduleRevision) -> BatchView:
    return BatchView(
        id=batch.id,
        name=batch.name,
        revision=batch.revision,
        latest_version_number=batch.latest_version_number,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        selected_schedule=ScheduleView(
            number=schedule.number,
            version_id=schedule.version_id,
            kernel_profile=schedule.kernel_profile,
            config=ScheduleConfig.model_validate(schedule.config),
            created_at=schedule.created_at,
        ),
    )


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


def current_schedule(session: Session, batch: Batch) -> ScheduleRevision:
    result = session.get(ScheduleRevision, (batch.id, batch.schedule_number))
    if result is None:
        raise ServiceError(503, "INCONSISTENT_STATE", "Schedule unavailable.")
    return result


def check_revision(batch: Batch, expected: int) -> None:
    if batch.revision != expected:
        raise ServiceError(409, "REVISION_CONFLICT", "Reload the batch first.")


class RegistrationService:
    def __init__(
        self, sessions: sessionmaker[Session], storage: NotebookStorage
    ) -> None:
        self.sessions = sessions
        self.storage = storage

    def mutate(
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

    def save_version(
        self, session: Session, batch: Batch, notebook: Notebook, number: int
    ) -> NotebookVersion:
        version_id = uuid4()
        try:
            path, cell_map = self.storage.save(
                batch.owner_id, batch.id, version_id, notebook
            )
        except OSError as exc:
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Notebook storage unavailable."
            ) from exc
        version = NotebookVersion(
            id=version_id,
            batch_id=batch.id,
            number=number,
            original_path=path,
            sha256=notebook.sha256,
            code_hash=notebook.code_hash,
            size_bytes=len(notebook.raw),
            code_cells=len(notebook.cells),
            cell_map=cell_map,
        )
        session.add(version)
        session.flush()
        return version

    def create(
        self, owner: str, key: str, data: CreateBatch, notebook: Notebook
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = Batch(id=uuid4(), owner_id=owner, name=data.name)
            session.add(batch)
            session.flush()
            version = self.save_version(session, batch, notebook, 1)
            schedule = ScheduleRevision(
                batch_id=batch.id,
                number=1,
                version_id=version.id,
                kernel_profile=data.kernel_profile,
                config=data.schedule.model_dump(mode="json"),
            )
            session.add(schedule)
            session.flush()
            return 201, batch_view(batch, schedule).model_dump(mode="json")

        return self.mutate(
            owner,
            "create",
            key,
            {
                "metadata": data.model_dump(mode="json"),
                "notebook_sha256": notebook.sha256,
            },
            action,
        )

    def add_version(
        self, owner: str, batch_id: UUID, key: str, notebook: Notebook
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            existing = session.scalar(
                select(NotebookVersion).where(
                    NotebookVersion.batch_id == batch_id,
                    NotebookVersion.sha256 == notebook.sha256,
                )
            )
            if existing:
                return 200, version_view(existing).model_dump(mode="json")
            number = batch.latest_version_number + 1
            version = self.save_version(session, batch, notebook, number)
            batch.latest_version_number = number
            batch.revision += 1
            batch.updated_at = utcnow()
            return 201, version_view(version).model_dump(mode="json")

        return self.mutate(
            owner,
            f"version:{batch_id}",
            key,
            {"sha256": notebook.sha256},
            action,
        )

    def set_schedule(
        self,
        owner: str,
        batch_id: UUID,
        key: str,
        expected: int,
        data: UpdateSchedule,
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch, expected)
            version = session.scalar(
                select(NotebookVersion).where(
                    NotebookVersion.id == data.version_id,
                    NotebookVersion.batch_id == batch.id,
                )
            )
            if version is None:
                raise ServiceError(404, "NOT_FOUND", "Version not found.")
            batch.schedule_number += 1
            batch.revision += 1
            batch.updated_at = utcnow()
            schedule = ScheduleRevision(
                batch_id=batch.id,
                number=batch.schedule_number,
                version_id=version.id,
                kernel_profile=data.kernel_profile,
                config=data.schedule.model_dump(mode="json"),
            )
            session.add(schedule)
            session.flush()
            return 200, batch_view(batch, schedule).model_dump(mode="json")

        return self.mutate(
            owner,
            f"schedule:{batch_id}",
            key,
            {
                "expected_revision": expected,
                "data": data.model_dump(mode="json"),
            },
            action,
        )

    def rename(
        self, owner: str, batch_id: UUID, key: str, expected: int, name: str
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch, expected)
            batch.name = name
            batch.revision += 1
            batch.updated_at = utcnow()
            return 200, batch_view(
                batch, current_schedule(session, batch)
            ).model_dump(mode="json")

        return self.mutate(
            owner,
            f"rename:{batch_id}",
            key,
            {"expected_revision": expected, "name": name},
            action,
        )

    def get(self, owner: str, batch_id: UUID) -> BatchView:
        with self.sessions() as session:
            batch = owned_batch(session, owner, batch_id)
            return batch_view(batch, current_schedule(session, batch))

    def list_batches(
        self, owner: str, limit: int, cursor: str | None
    ) -> BatchPage:
        query = (
            select(Batch, ScheduleRevision)
            .join(
                ScheduleRevision,
                and_(
                    ScheduleRevision.batch_id == Batch.id,
                    ScheduleRevision.number == Batch.schedule_number,
                ),
            )
            .where(Batch.owner_id == owner)
        )
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor))
                timestamp = datetime.fromisoformat(decoded[0])
                identifier = UUID(decoded[1])
                if timestamp.tzinfo is None:
                    raise ValueError("Timezone required.")
            except (
                ValueError,
                TypeError,
                IndexError,
                KeyError,
                binascii.Error,
            ) as exc:
                raise ServiceError(
                    422, "INVALID_CURSOR", "Invalid cursor."
                ) from exc
            query = query.where(
                tuple_(Batch.created_at, Batch.id)
                < tuple_(timestamp, identifier)
            )
        query = query.order_by(Batch.created_at.desc(), Batch.id.desc())
        with self.sessions() as session:
            rows = session.execute(query.limit(limit + 1)).all()
            items = [batch_view(b, s) for b, s in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [last.created_at.isoformat(), str(last.id)]
                ).encode()
            ).decode()
        return BatchPage(items=items, next_cursor=next_cursor)

    def versions(
        self, owner: str, batch_id: UUID, limit: int, cursor: int | None
    ) -> VersionPage:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(NotebookVersion).where(
                NotebookVersion.batch_id == batch_id
            )
            if cursor is not None:
                query = query.where(NotebookVersion.number < cursor)
            rows = list(
                session.scalars(
                    query.order_by(NotebookVersion.number.desc()).limit(
                        limit + 1
                    )
                )
            )
            items = [version_view(v) for v in rows[:limit]]
            return VersionPage(
                items=items,
                next_cursor=items[-1].number if len(rows) > limit else None,
            )

    def download(self, owner: str, batch_id: UUID, version_id: UUID) -> bytes:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            version = session.scalar(
                select(NotebookVersion).where(
                    NotebookVersion.id == version_id,
                    NotebookVersion.batch_id == batch_id,
                )
            )
            if version is None:
                raise ServiceError(404, "NOT_FOUND", "Version not found.")
            return self.storage.read(
                version.original_path, version.sha256, version.size_bytes
            )
