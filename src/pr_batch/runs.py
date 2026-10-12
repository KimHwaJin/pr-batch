"""Durable manual execution admission and owner-scoped history."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from pr_batch.errors import ServiceError
from pr_batch.models import (
    BatchRun,
    NotebookVersion,
    RunCommand,
    RunStepBinding,
)
from pr_batch.service import (
    MutationResult,
    RegistrationService,
    current_schedule,
    owned_batch,
)

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


class StepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sequence: int
    original_index: int
    source_sha256: str
    step_id: UUID | None
    status: str | None
    result_ref: dict[str, Any] | None


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    version_id: UUID
    schedule_number: int
    kernel_profile: str
    snapshot: dict[str, Any]
    trigger: str
    dispatch_status: str
    execution_id: UUID | None
    operation_id: UUID | None
    execution_status: str | None
    observed_version: int | None
    report_status: str
    skip_reason: str | None
    failure_type: str | None
    tracking_error: str | None
    notebook_status: str | None
    result_status: str
    artifacts: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    retry_info: dict[str, Any] | None
    created_at: datetime
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    observed_at: datetime | None


class RunDetail(RunView):
    steps: list[StepView]


class RunPage(BaseModel):
    items: list[RunView]
    next_cursor: UUID | None


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


class RunService:
    def __init__(self, registration: RegistrationService) -> None:
        self.registration = registration
        self.sessions = registration.sessions

    def create(self, owner: str, batch_id: UUID, key: str) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            schedule = current_schedule(session, batch)
            version = session.get(NotebookVersion, schedule.version_id)
            assert version is not None
            config = schedule.config
            active = occupied(session, batch_id)
            pending = waiting(session, batch_id)
            dispatch = "PENDING"
            reason = None
            status = 202
            if config["overlap_policy"] == "SKIP" and active:
                dispatch, reason = "SKIPPED", "OVERLAP"
            elif active >= config["max_concurrent"] or pending:
                if pending >= config["max_pending"]:
                    dispatch, reason = "SKIPPED", "SKIPPED_CAPACITY"
                    status = 409
                else:
                    dispatch = "WAITING_CAPACITY"
            run = BatchRun(
                id=uuid4(),
                created_at=session.scalar(select(func.clock_timestamp())),
                batch_id=batch_id,
                owner_id=owner,
                version_id=version.id,
                schedule_number=schedule.number,
                kernel_profile=schedule.kernel_profile,
                snapshot={
                    "schedule": config,
                    "sha256": version.sha256,
                    "code_hash": version.code_hash,
                },
                dispatch_status=dispatch,
                skip_reason=reason,
                report_status=(
                    "NOT_APPLICABLE" if reason else "NOT_REQUESTED"
                ),
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
                        "profile": schedule.kernel_profile,
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
                        "notebook_version_id": str(version.id),
                    },
                }
                session.add(RunCommand(run_id=run.id, payload=payload))
            session.flush()
            return status, RunView.model_validate(run).model_dump(mode="json")

        return self.registration.mutate(
            owner, f"run:{batch_id}", key, {}, action
        )

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
