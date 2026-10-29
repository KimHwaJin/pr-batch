"""Script uploads, immutable versions and owner-scoped (tenant and user)
reads."""

from collections.abc import Callable
from pathlib import PurePath
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.core.security import Principal
from app.db.base import Actor
from app.db.session import database_now
from app.domain.run_state import ACTIVE
from app.domain.scripts import ParsedScript, ScriptType
from app.models import Batch, BatchConfig, BatchRun, Script, ScriptVersion
from app.schemas.common import audit_fields
from app.schemas.script import (
    CellCounts,
    CellEntry,
    ScriptPage,
    ScriptView,
    VersionDetail,
    VersionPage,
    VersionView,
)
from app.services.common import (
    before_cursor,
    check_revision,
    decode_number_cursor,
    encode_cursor,
    encode_number_cursor,
    owned_script,
    script_version,
)
from app.services.idempotency import Idempotency, MutationResult
from app.storage.script_storage import ScriptStorage


def version_view(version: ScriptVersion) -> VersionView:
    executable = len(version.cell_map)
    return VersionView(
        id=version.id,
        script_id=version.script_id,
        version_number=version.version_number,
        filename=version.filename,
        sha256=version.sha256,
        code_hash=version.code_hash,
        size_bytes=version.size_bytes,
        cells=CellCounts(
            total=len(version.cell_list),
            executable=executable,
            skipped=len(version.cell_list) - executable,
        ),
        **audit_fields(version),
    )


def version_detail(version: ScriptVersion) -> VersionDetail:
    return VersionDetail(
        **version_view(version).model_dump(),
        cell_list=[CellEntry(**cell) for cell in version.cell_list],
    )


def script_view(script: Script, latest: ScriptVersion) -> ScriptView:
    return ScriptView(
        id=script.id,
        name=script.name,
        type=ScriptType(script.type),
        revision=script.revision,
        latest_version=version_view(latest),
        **audit_fields(script),
    )


class ScriptService:
    def __init__(
        self, sessions: sessionmaker[Session], storage: ScriptStorage
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.idempotency = Idempotency(sessions)

    def save_version(
        self,
        session: Session,
        script: Script,
        parsed: ParsedScript,
        number: int,
        written: list[str],
    ) -> ScriptVersion:
        version_id = uuid4()
        try:
            directory, path, cell_map = self.storage.save(
                Principal(script.tenant_id, script.owner_id),
                script.id,
                version_id,
                parsed,
            )
        except OSError as exc:
            raise ServiceError(
                503, "STORAGE_UNAVAILABLE", "Script storage unavailable."
            ) from exc
        written.append(directory)
        version = ScriptVersion(
            id=version_id,
            script_id=script.id,
            version_number=number,
            filename=parsed.filename,
            original_path=path,
            sha256=parsed.sha256,
            code_hash=parsed.code_hash,
            size_bytes=len(parsed.raw),
            cell_map=cell_map,
            cell_list=list(parsed.cell_list),
        )
        version.stamp_created(
            Actor.user(script.owner_id), database_now(session)
        )
        session.add(version)
        session.flush()
        return version

    def run_upload(
        self,
        owner: Principal,
        operation: str,
        key: str,
        payload: object,
        action: Callable[[Session, list[str]], MutationResult],
    ) -> MutationResult:
        """Idempotent write whose files are removed if it does not commit."""
        written: list[str] = []
        try:
            return self.idempotency.run(
                owner, operation, key, payload, lambda s: action(s, written)
            )
        except BaseException:
            for directory in written:
                self.storage.discard(directory)
            raise

    def create_script(
        self,
        owner: Principal,
        key: str,
        name: str | None,
        parsed: ParsedScript,
    ) -> MutationResult:
        display = name or PurePath(parsed.filename).stem or "script"

        def action(session: Session, written: list[str]) -> MutationResult:
            script = Script(
                id=uuid4(),
                tenant_id=owner.tenant,
                owner_id=owner.user_id,
                name=display,
                type=parsed.type,
            )
            script.stamp_created(
                Actor.user(owner.user_id), database_now(session)
            )
            session.add(script)
            session.flush()
            version = self.save_version(session, script, parsed, 1, written)
            return 201, script_view(script, version).model_dump(mode="json")

        return self.run_upload(
            owner,
            "script:create",
            key,
            {"name": name, "type": parsed.type, "sha256": parsed.sha256},
            action,
        )

    def add_version(
        self, owner: Principal, script_id: UUID, key: str, parsed: ParsedScript
    ) -> MutationResult:
        """`parsed` was parsed as this script's own type (see the API)."""

        def action(session: Session, written: list[str]) -> MutationResult:
            script = owned_script(session, owner, script_id, lock=True)
            existing = session.scalar(
                select(ScriptVersion).where(
                    ScriptVersion.script_id == script_id,
                    ScriptVersion.sha256 == parsed.sha256,
                )
            )
            if existing:
                return 200, version_view(existing).model_dump(mode="json")
            number = script.latest_version_number + 1
            version = self.save_version(
                session, script, parsed, number, written
            )
            script.latest_version_number = number
            script.revision += 1
            script.stamp_updated(Actor.user(owner.user_id), version.created_at)
            return 201, version_view(version).model_dump(mode="json")

        return self.run_upload(
            owner,
            f"script:version:{script_id}",
            key,
            {"sha256": parsed.sha256},
            action,
        )

    def rename_script(
        self,
        owner: Principal,
        script_id: UUID,
        key: str,
        expected: int,
        name: str,
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            script = owned_script(session, owner, script_id, lock=True)
            check_revision(script.revision, expected)
            script.name = name
            script.revision += 1
            script.stamp_updated(
                Actor.user(owner.user_id), database_now(session)
            )
            latest = script_version(
                session, script.id, script.latest_version_number
            )
            return 200, script_view(script, latest).model_dump(mode="json")

        return self.idempotency.run(
            owner,
            f"script:rename:{script_id}",
            key,
            {"expected_revision": expected, "name": name},
            action,
        )

    def delete_script(
        self, owner: Principal, script_id: UUID, key: str, expected: int
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            # The row lock waits for batches pinning it (they share-lock).
            script = owned_script(session, owner, script_id, lock=True)
            check_revision(script.revision, expected)
            pinned = exists().where(
                Batch.id == BatchConfig.batch_id,
                Batch.config_number == BatchConfig.number,
                Batch.deleted_at.is_(None),
                BatchConfig.script_id == script_id,
            )
            running = exists().where(
                BatchRun.script_id == script_id, BatchRun.status.in_(ACTIVE)
            )
            if session.scalar(select(pinned)):
                raise ServiceError(
                    409,
                    "SCRIPT_IN_USE",
                    "Delete or repoint the batches using this script first.",
                )
            if session.scalar(select(running)):
                raise ServiceError(
                    409,
                    "SCRIPT_HAS_ACTIVE_RUNS",
                    "Wait for or cancel its active runs first.",
                )
            script.revision += 1
            script.stamp_deleted(
                Actor.user(owner.user_id), database_now(session)
            )
            return 204, {}

        return self.idempotency.run(
            owner,
            f"script:delete:{script_id}",
            key,
            {"expected_revision": expected},
            action,
        )

    def get_script(self, owner: Principal, script_id: UUID) -> ScriptView:
        with self.sessions() as session:
            script = owned_script(session, owner, script_id)
            latest = script_version(
                session, script.id, script.latest_version_number
            )
            return script_view(script, latest)

    def list_scripts(
        self, owner: Principal, limit: int, cursor: str | None
    ) -> ScriptPage:
        query = (
            select(Script, ScriptVersion)
            .join(
                ScriptVersion,
                and_(
                    ScriptVersion.script_id == Script.id,
                    ScriptVersion.version_number
                    == Script.latest_version_number,
                ),
            )
            .where(
                Script.tenant_id == owner.tenant,
                Script.owner_id == owner.user_id,
                Script.deleted_at.is_(None),
            )
        )
        if cursor:
            query = query.where(
                before_cursor(Script.created_at, Script.id, cursor)
            )
        query = query.order_by(Script.created_at.desc(), Script.id.desc())
        with self.sessions() as session:
            rows = session.execute(query.limit(limit + 1)).all()
            items = [script_view(n, v) for n, v in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].id)
        return ScriptPage(items=items, next_cursor=next_cursor)

    def versions(
        self, owner: Principal, script_id: UUID, limit: int, cursor: str | None
    ) -> VersionPage:
        with self.sessions() as session:
            owned_script(session, owner, script_id)
            query = select(ScriptVersion).where(
                ScriptVersion.script_id == script_id
            )
            if cursor:
                query = query.where(
                    ScriptVersion.version_number < decode_number_cursor(cursor)
                )
            rows = list(
                session.scalars(
                    query.order_by(ScriptVersion.version_number.desc()).limit(
                        limit + 1
                    )
                )
            )
            items = [version_view(v) for v in rows[:limit]]
            return VersionPage(
                items=items,
                next_cursor=(
                    encode_number_cursor(items[-1].version_number)
                    if len(rows) > limit
                    else None
                ),
            )

    def version(
        self, owner: Principal, script_id: UUID, version_id: UUID
    ) -> VersionDetail:
        with self.sessions() as session:
            owned_script(session, owner, script_id)
            return version_detail(
                script_version(session, script_id, version_id)
            )

    def script_type(self, owner: Principal, script_id: UUID) -> ScriptType:
        with self.sessions() as session:
            return ScriptType(owned_script(session, owner, script_id).type)

    def download(
        self, owner: Principal, script_id: UUID, version_id: UUID
    ) -> tuple[bytes, ScriptType]:
        with self.sessions() as session:
            script = owned_script(session, owner, script_id)
            version = script_version(session, script_id, version_id)
            data = self.storage.read(
                version.original_path, version.sha256, version.size_bytes
            )
            return data, ScriptType(script.type)
