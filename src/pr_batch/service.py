"""Transactional notebook/batch registration and owner-scoped reads."""

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from pr_batch.errors import ServiceError
from pr_batch.models import (
    Batch,
    BatchConfig,
    IdempotencyRecord,
    Notebook,
    NotebookVersion,
    utcnow,
)
from pr_batch.notebooks import Notebook as ParsedNotebook
from pr_batch.schedule_clock import EPSILON, ScheduleClock
from pr_batch.schedule_state import database_now, record_skipped
from pr_batch.schemas import (
    BatchPage,
    BatchView,
    CellCounts,
    CellEntry,
    CreateBatch,
    NotebookPage,
    NotebookView,
    Policy,
    Schedule,
    UpdateBatch,
    VersionDetail,
    VersionPage,
    VersionView,
)
from pr_batch.storage import NotebookStorage

MutationResult = tuple[int, dict[str, Any]]


def version_view(version: NotebookVersion) -> VersionView:
    executable = len(version.cell_map)
    return VersionView(
        id=version.id,
        notebook_id=version.notebook_id,
        number=version.number,
        filename=version.filename,
        sha256=version.sha256,
        code_hash=version.code_hash,
        size_bytes=version.size_bytes,
        cells=CellCounts(
            total=len(version.cell_list),
            executable=executable,
            skipped=len(version.cell_list) - executable,
        ),
        created_at=version.created_at,
    )


def version_detail(version: NotebookVersion) -> VersionDetail:
    return VersionDetail(
        **version_view(version).model_dump(),
        cell_list=[CellEntry(**cell) for cell in version.cell_list],
    )


def notebook_view(notebook: Notebook, latest: NotebookVersion) -> NotebookView:
    return NotebookView(
        id=notebook.id,
        name=notebook.name,
        revision=notebook.revision,
        latest_version=version_view(latest),
        created_at=notebook.created_at,
        updated_at=notebook.updated_at,
    )


def schedule_of(config: BatchConfig) -> Schedule | None:
    if config.schedule is None:
        return None
    return Schedule.model_validate(config.schedule)


def policy_of(config: BatchConfig) -> Policy:
    return Policy.model_validate(config.policy)


def batch_view(
    batch: Batch, config: BatchConfig, version: NotebookVersion
) -> BatchView:
    schedule = schedule_of(config)
    return BatchView(
        id=batch.id,
        name=batch.name,
        notebook_id=config.notebook_id,
        notebook_version_id=config.notebook_version_id,
        notebook_version_number=version.number,
        kernel_profile=config.kernel_profile,
        schedule=schedule,
        policy=policy_of(config),
        enabled=batch.enabled,
        next_run_at=batch.next_run_at,
        preview_runs=(
            ScheduleClock(schedule).preview(utcnow()) if schedule else []
        ),
        config_number=batch.config_number,
        revision=batch.revision,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def owned_notebook(
    session: Session, owner: str, notebook_id: UUID, *, lock: bool = False
) -> Notebook:
    query = select(Notebook).where(
        Notebook.id == notebook_id, Notebook.owner_id == owner
    )
    if lock:
        query = query.with_for_update()
    result = session.scalar(query)
    if result is None:
        raise ServiceError(404, "NOT_FOUND", "Notebook not found.")
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


def notebook_version(
    session: Session, notebook_id: UUID, number_or_id: int | UUID
) -> NotebookVersion:
    column = (
        NotebookVersion.number
        if isinstance(number_or_id, int)
        else NotebookVersion.id
    )
    result = session.scalar(
        select(NotebookVersion).where(
            NotebookVersion.notebook_id == notebook_id,
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


def config_version(session: Session, config: BatchConfig) -> NotebookVersion:
    result = session.get(NotebookVersion, config.notebook_version_id)
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


def merged_policy(current: Policy, patch: dict[str, Any]) -> Policy:
    try:
        return Policy.model_validate(current.model_dump() | patch)
    except ValidationError as exc:
        raise ServiceError(
            422, "INVALID_POLICY", "Only PARALLEL allows max_concurrent > 1."
        ) from exc


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

    # Notebooks

    def save_version(
        self,
        session: Session,
        notebook: Notebook,
        parsed: ParsedNotebook,
        number: int,
    ) -> NotebookVersion:
        version_id = uuid4()
        try:
            path, cell_map = self.storage.save(
                notebook.owner_id, notebook.id, version_id, parsed
            )
        except OSError as exc:
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Notebook storage unavailable."
            ) from exc
        version = NotebookVersion(
            id=version_id,
            notebook_id=notebook.id,
            number=number,
            filename=parsed.filename,
            original_path=path,
            sha256=parsed.sha256,
            code_hash=parsed.code_hash,
            size_bytes=len(parsed.raw),
            cell_map=cell_map,
            cell_list=list(parsed.cell_list),
        )
        session.add(version)
        session.flush()
        return version

    def create_notebook(
        self, owner: str, key: str, name: str | None, parsed: ParsedNotebook
    ) -> MutationResult:
        display = name or parsed.filename.removesuffix(".ipynb") or "notebook"

        def action(session: Session) -> MutationResult:
            notebook = Notebook(id=uuid4(), owner_id=owner, name=display)
            session.add(notebook)
            session.flush()
            version = self.save_version(session, notebook, parsed, 1)
            return 201, notebook_view(notebook, version).model_dump(
                mode="json"
            )

        return self.mutate(
            owner,
            "notebook:create",
            key,
            {"name": name, "sha256": parsed.sha256},
            action,
        )

    def add_version(
        self, owner: str, notebook_id: UUID, key: str, parsed: ParsedNotebook
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            notebook = owned_notebook(session, owner, notebook_id, lock=True)
            existing = session.scalar(
                select(NotebookVersion).where(
                    NotebookVersion.notebook_id == notebook_id,
                    NotebookVersion.sha256 == parsed.sha256,
                )
            )
            if existing:
                return 200, version_view(existing).model_dump(mode="json")
            number = notebook.latest_version_number + 1
            version = self.save_version(session, notebook, parsed, number)
            notebook.latest_version_number = number
            notebook.revision += 1
            notebook.updated_at = utcnow()
            return 201, version_view(version).model_dump(mode="json")

        return self.mutate(
            owner,
            f"notebook:version:{notebook_id}",
            key,
            {"sha256": parsed.sha256},
            action,
        )

    def rename_notebook(
        self,
        owner: str,
        notebook_id: UUID,
        key: str,
        expected: int,
        name: str,
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            notebook = owned_notebook(session, owner, notebook_id, lock=True)
            check_revision(notebook.revision, expected)
            notebook.name = name
            notebook.revision += 1
            notebook.updated_at = utcnow()
            latest = notebook_version(
                session, notebook.id, notebook.latest_version_number
            )
            return 200, notebook_view(notebook, latest).model_dump(mode="json")

        return self.mutate(
            owner,
            f"notebook:rename:{notebook_id}",
            key,
            {"expected_revision": expected, "name": name},
            action,
        )

    def get_notebook(self, owner: str, notebook_id: UUID) -> NotebookView:
        with self.sessions() as session:
            notebook = owned_notebook(session, owner, notebook_id)
            latest = notebook_version(
                session, notebook.id, notebook.latest_version_number
            )
            return notebook_view(notebook, latest)

    def list_notebooks(
        self, owner: str, limit: int, cursor: str | None
    ) -> NotebookPage:
        query = (
            select(Notebook, NotebookVersion)
            .join(
                NotebookVersion,
                and_(
                    NotebookVersion.notebook_id == Notebook.id,
                    NotebookVersion.number == Notebook.latest_version_number,
                ),
            )
            .where(Notebook.owner_id == owner)
        )
        if cursor:
            query = query.where(
                before_cursor(Notebook.created_at, Notebook.id, cursor)
            )
        query = query.order_by(Notebook.created_at.desc(), Notebook.id.desc())
        with self.sessions() as session:
            rows = session.execute(query.limit(limit + 1)).all()
            items = [notebook_view(n, v) for n, v in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].id)
        return NotebookPage(items=items, next_cursor=next_cursor)

    def versions(
        self, owner: str, notebook_id: UUID, limit: int, cursor: int | None
    ) -> VersionPage:
        with self.sessions() as session:
            owned_notebook(session, owner, notebook_id)
            query = select(NotebookVersion).where(
                NotebookVersion.notebook_id == notebook_id
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

    def version(
        self, owner: str, notebook_id: UUID, version_id: UUID
    ) -> VersionDetail:
        with self.sessions() as session:
            owned_notebook(session, owner, notebook_id)
            return version_detail(
                notebook_version(session, notebook_id, version_id)
            )

    def download(
        self, owner: str, notebook_id: UUID, version_id: UUID
    ) -> bytes:
        with self.sessions() as session:
            owned_notebook(session, owner, notebook_id)
            version = notebook_version(session, notebook_id, version_id)
            return self.storage.read(
                version.original_path, version.sha256, version.size_bytes
            )

    # Batches

    def resolve_version(
        self,
        session: Session,
        owner: str,
        notebook_id: UUID,
        version_id: UUID | None,
    ) -> NotebookVersion:
        """Pin an explicit version, or the notebook's latest one."""
        notebook = owned_notebook(session, owner, notebook_id)
        if version_id is None:
            return notebook_version(
                session, notebook.id, notebook.latest_version_number
            )
        return notebook_version(session, notebook.id, version_id)

    def create_batch(
        self, owner: str, key: str, data: CreateBatch
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            version = self.resolve_version(
                session, owner, data.notebook_id, data.notebook_version_id
            )
            batch = Batch(id=uuid4(), owner_id=owner, name=data.name)
            session.add(batch)
            session.flush()
            config = BatchConfig(
                batch_id=batch.id,
                number=1,
                notebook_id=version.notebook_id,
                notebook_version_id=version.id,
                kernel_profile=data.kernel_profile,
                schedule=(
                    data.schedule.model_dump(mode="json")
                    if data.schedule
                    else None
                ),
                policy=data.policy.model_dump(mode="json"),
            )
            session.add(config)
            session.flush()
            return 201, batch_view(batch, config, version).model_dump(
                mode="json"
            )

        return self.mutate(
            owner,
            "batch:create",
            key,
            data.model_dump(mode="json"),
            action,
        )

    def update_batch(
        self,
        owner: str,
        batch_id: UUID,
        key: str,
        expected: int,
        data: UpdateBatch,
    ) -> MutationResult:
        fields = data.model_fields_set

        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            check_revision(batch.revision, expected)
            config = current_config(session, batch)
            now = database_now(session)
            if "name" in fields and data.name is not None:
                batch.name = data.name
            if fields - {"name"}:
                config = self.next_config(session, owner, batch, config, data)
            batch.revision += 1
            batch.updated_at = now
            return 200, batch_view(
                batch, config, config_version(session, config)
            ).model_dump(mode="json")

        return self.mutate(
            owner,
            f"batch:update:{batch_id}",
            key,
            {
                "expected_revision": expected,
                "data": data.model_dump(mode="json", exclude_unset=True),
            },
            action,
        )

    def next_config(
        self,
        session: Session,
        owner: str,
        batch: Batch,
        current: BatchConfig,
        data: UpdateBatch,
    ) -> BatchConfig:
        fields = data.model_fields_set
        notebook_id = data.notebook_id or current.notebook_id
        if "notebook_id" in fields or "notebook_version_id" in fields:
            version = self.resolve_version(
                session, owner, notebook_id, data.notebook_version_id
            )
        else:
            version = config_version(session, current)
        schedule = (
            data.schedule if "schedule" in fields else schedule_of(current)
        )
        policy = policy_of(current)
        if data.policy is not None:
            policy = merged_policy(
                policy, data.policy.model_dump(exclude_none=True)
            )
        now = database_now(session)
        if batch.enabled:
            if schedule is None:
                raise ServiceError(
                    409, "SCHEDULE_REQUIRED", "Disable the batch first."
                )
            if "schedule" in fields:
                following = ScheduleClock(schedule).find(now)
                if following is None:
                    raise ServiceError(
                        422,
                        "NO_FUTURE_OCCURRENCE",
                        "Choose a schedule with a future occurrence.",
                    )
                if batch.next_run_at and batch.next_run_at <= now:
                    record_skipped(
                        session,
                        batch,
                        current,
                        batch.next_run_at,
                        now + EPSILON,
                        "SCHEDULE_REPLACED",
                        now,
                    )
                batch.next_run_at = following
        batch.config_number += 1
        config = BatchConfig(
            batch_id=batch.id,
            number=batch.config_number,
            notebook_id=version.notebook_id,
            notebook_version_id=version.id,
            kernel_profile=data.kernel_profile or current.kernel_profile,
            schedule=schedule.model_dump(mode="json") if schedule else None,
            policy=policy.model_dump(mode="json"),
        )
        session.add(config)
        session.flush()
        return config

    def get_batch(self, owner: str, batch_id: UUID) -> BatchView:
        with self.sessions() as session:
            batch = owned_batch(session, owner, batch_id)
            config = current_config(session, batch)
            return batch_view(batch, config, config_version(session, config))

    def list_batches(
        self,
        owner: str,
        limit: int,
        cursor: str | None,
        notebook_id: UUID | None,
    ) -> BatchPage:
        query = (
            select(Batch, BatchConfig, NotebookVersion)
            .join(
                BatchConfig,
                and_(
                    BatchConfig.batch_id == Batch.id,
                    BatchConfig.number == Batch.config_number,
                ),
            )
            .join(
                NotebookVersion,
                NotebookVersion.id == BatchConfig.notebook_version_id,
            )
            .where(Batch.owner_id == owner)
        )
        if notebook_id is not None:
            query = query.where(BatchConfig.notebook_id == notebook_id)
        if cursor:
            query = query.where(
                before_cursor(Batch.created_at, Batch.id, cursor)
            )
        query = query.order_by(Batch.created_at.desc(), Batch.id.desc())
        with self.sessions() as session:
            rows = session.execute(query.limit(limit + 1)).all()
            items = [batch_view(b, c, v) for b, c, v in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].id)
        return BatchPage(items=items, next_cursor=next_cursor)
