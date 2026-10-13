"""Durable run admission (manual and scheduled) and owner-scoped history."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from src.core.exceptions import ServiceError
from src.models import (
    Batch,
    BatchConfig,
    BatchRun,
    NotebookVersion,
    RunCommand,
    RunStepBinding,
)
from src.schemas.run import RunDetail, RunPage, RunView, StepView
from src.services.common import current_config, owned_batch
from src.services.idempotency import Idempotency, MutationResult

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def occupied(session: Session, batch_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(BatchRun)
            .where(
                BatchRun.batch_id == batch_id,
                BatchRun.dispatch_status.in_(("PENDING", "SUBMITTED")),
                or_(
                    BatchRun.execution_status.is_(None),
                    BatchRun.execution_status.not_in(TERMINAL),
                ),
            )
        )
        or 0
    )


def waiting(session: Session, batch_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(BatchRun)
            .where(
                BatchRun.batch_id == batch_id,
                BatchRun.dispatch_status == "WAITING_CAPACITY",
            )
        )
        or 0
    )


def admit_run(
    session: Session,
    batch: Batch,
    config: BatchConfig,
    scheduled_for: datetime | None = None,
) -> tuple[int, BatchRun]:
    """Caller must hold the batch row lock; never performs remote I/O."""
    batch_id, owner = batch.id, batch.owner_id
    version = session.get(NotebookVersion, config.notebook_version_id)
    assert version is not None
    policy = config.policy
    active = occupied(session, batch_id)
    pending = waiting(session, batch_id)
    dispatch = "PENDING"
    reason = None
    status = 202
    if policy["overlap"] == "SKIP" and active:
        dispatch, reason = "SKIPPED", "OVERLAP"
    elif active >= policy["max_concurrent"] or pending:
        if pending >= policy["max_pending"]:
            dispatch, reason = "SKIPPED", "SKIPPED_CAPACITY"
            status = 409
        else:
            dispatch = "WAITING_CAPACITY"
    run = BatchRun(
        id=uuid4(),
        created_at=session.scalar(select(func.clock_timestamp())),
        batch_id=batch_id,
        scheduled_for=scheduled_for,
        trigger="SCHEDULED" if scheduled_for else "MANUAL",
        owner_id=owner,
        notebook_id=version.notebook_id,
        notebook_version_id=version.id,
        config_number=config.number,
        kernel_profile=config.kernel_profile,
        snapshot={
            "notebook_id": str(version.notebook_id),
            "notebook_version_id": str(version.id),
            "sha256": version.sha256,
            "code_hash": version.code_hash,
            "schedule": config.schedule,
            "policy": policy,
        },
        dispatch_status=dispatch,
        skip_reason=reason,
        report_status=("NOT_APPLICABLE" if reason else "NOT_REQUESTED"),
        result_status="NOT_APPLICABLE" if reason else "PENDING",
    )
    session.add(run)
    session.flush()
    for cell in version.cell_map:
        session.add(
            RunStepBinding(
                run_id=run.id,
                sequence=cell["sequence"],
                original_index=cell["original_index"],
                source_sha256=cell["sha256"],
            )
        )
    if dispatch != "SKIPPED":
        payload = {
            "idempotency_key": f"batch-run:{run.id}:submit",
            "lifecycle": {"operation_mode": "SINGLE"},
            "trigger": {
                "type": "BATCH",
                "actor": {"type": "BATCH", "id": str(batch_id)},
            },
            "runtime": {
                "type": "JUPYTER",
                "profile": config.kernel_profile,
            },
            "context": {"user_id": owner, "task_id": str(run.id)},
            "operation": {
                "spec": {
                    "schema_version": "1.0",
                    "steps": [
                        {
                            "sequence": cell["sequence"],
                            "payload": {
                                "type": "PYTHON_EXECUTE",
                                "source": {
                                    "type": "PATH",
                                    "path": cell["path"],
                                    "sha256": cell["sha256"],
                                },
                            },
                        }
                        for cell in version.cell_map
                    ],
                }
            },
            "metadata": {
                "batch_id": str(batch_id),
                "notebook_id": str(version.notebook_id),
                "notebook_version_id": str(version.id),
            },
        }
        session.add(RunCommand(run_id=run.id, payload=payload))
    session.flush()
    return status, run


class RunService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.idempotency = Idempotency(sessions)

    def create(self, owner: str, batch_id: UUID, key: str) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            config = current_config(session, batch)
            status, run = admit_run(session, batch, config)
            return status, RunView.model_validate(run).model_dump(mode="json")

        return self.idempotency.run(owner, f"run:{batch_id}", key, {}, action)

    def get(self, owner: str, batch_id: UUID, run_id: UUID) -> RunDetail:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            run = session.scalar(
                select(BatchRun).where(
                    BatchRun.id == run_id,
                    BatchRun.batch_id == batch_id,
                    BatchRun.owner_id == owner,
                )
            )
            if run is None:
                raise ServiceError(404, "NOT_FOUND", "Run not found.")
            steps = session.scalars(
                select(RunStepBinding)
                .where(RunStepBinding.run_id == run_id)
                .order_by(RunStepBinding.sequence)
            )
            return RunDetail(
                **RunView.model_validate(run).model_dump(),
                steps=[StepView.model_validate(step) for step in steps],
            )

    def list(
        self, owner: str, batch_id: UUID, limit: int, cursor: UUID | None
    ) -> RunPage:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(BatchRun).where(
                BatchRun.batch_id == batch_id, BatchRun.owner_id == owner
            )
            if cursor:
                last = session.scalar(query.where(BatchRun.id == cursor))
                if last is None:
                    raise ServiceError(
                        422, "INVALID_CURSOR", "Invalid cursor."
                    )
                query = query.where(
                    tuple_(BatchRun.created_at, BatchRun.id)
                    < tuple_(last.created_at, last.id)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        BatchRun.created_at.desc(), BatchRun.id.desc()
                    ).limit(limit + 1)
                )
            )
            items = [RunView.model_validate(row) for row in rows[:limit]]
            return RunPage(
                items=items,
                next_cursor=(items[-1].id if len(rows) > limit else None),
            )
