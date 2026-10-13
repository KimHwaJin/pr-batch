"""Notebook uploads, immutable versions and owner-scoped reads."""

from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.db.base import utcnow
from app.domain.notebook_parser import Notebook as ParsedNotebook
from app.models import Notebook, NotebookVersion
from app.schemas.notebook import (
    CellCounts,
    CellEntry,
    NotebookPage,
    NotebookView,
    VersionDetail,
    VersionPage,
    VersionView,
)
from app.services.common import (
    before_cursor,
    check_revision,
    encode_cursor,
    notebook_version,
    owned_notebook,
)
from app.services.idempotency import Idempotency, MutationResult
from app.storage.notebook_storage import NotebookStorage


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


class NotebookService:
    def __init__(
        self, sessions: sessionmaker[Session], storage: NotebookStorage
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.idempotency = Idempotency(sessions)

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

        return self.idempotency.run(
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

        return self.idempotency.run(
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

        return self.idempotency.run(
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
