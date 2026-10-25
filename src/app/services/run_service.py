"""Run admission (manual and scheduled), cancellation and owner-scoped
history. Nothing here calls a remote service; outbound calls
are enqueued as tasks in the same transaction as the state change."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.db.base import DISPATCHER, Actor
from app.db.session import database_now
from app.domain.run_state import CANCELLABLE, OCCUPYING, RunStatus
from app.models import (
    Batch,
    BatchConfig,
    BatchRun,
    RunStepBinding,
    ScriptVersion,
)
from app.schemas.run import (
    ResultRefs,
    RunDetail,
    RunPage,
    RunView,
    StepView,
)
from app.schemas.schedule import concurrency
from app.services.common import (
    before_cursor,
    current_config,
    encode_cursor,
    owned_batch,
)
from app.services.idempotency import Idempotency, MutationResult
from app.tasks.queue import TaskKind, enqueue


def count_status(
    session: Session, batch_id: UUID, statuses: frozenset[RunStatus]
) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(BatchRun)
            .where(
                BatchRun.batch_id == batch_id,
                BatchRun.status.in_([str(s) for s in statuses]),
            )
        )
        or 0
    )


USER_CANCEL_REASON = "Cancelled by the batch owner."
REPLACE_CANCEL_REASON = "Replaced by a newer run (overlap policy REPLACE)."


def request_cancel(run: BatchRun, source: str, reason: str) -> None:
    """Record the first cancel request; the Executor keeps the first too."""
    if run.cancel_source is None:
        run.cancel_source, run.cancel_reason = source, reason


def queue_submission(session: Session, run: BatchRun) -> None:
    run.status = RunStatus.QUEUED
    enqueue(session, TaskKind.SUBMIT_EXECUTION, run.id)


def replace_running(session: Session, batch_id: UUID, actor: Actor) -> None:
    """REPLACE overlap: cancel waiting runs and ask running ones to stop.

    Occupying runs keep their slot until the Executor confirms the end,
    so the new run waits for them.
    """
    now = database_now(session)
    runs = session.scalars(
        select(BatchRun)
        .where(
            BatchRun.batch_id == batch_id,
            BatchRun.status.in_(
                [RunStatus.WAITING, RunStatus.QUEUED, RunStatus.RUNNING]
            ),
        )
        .with_for_update()
    )
    for run in runs:
        # Policy-driven even when a manual run triggered the overlap.
        request_cancel(run, "POLICY", REPLACE_CANCEL_REASON)
        if run.status == RunStatus.WAITING:
            run.status = RunStatus.CANCELLED
            run.completed_at = now
        elif not run.cancel_requested:
            run.cancel_requested = True
            if run.status == RunStatus.RUNNING:
                enqueue(session, TaskKind.CANCEL_EXECUTION, run.id)
        run.stamp_updated(actor, now)


def admit_run(
    session: Session,
    batch: Batch,
    config: BatchConfig,
    actor: Actor,
    scheduled_for: datetime | None = None,
) -> tuple[int, BatchRun]:
    """Caller must hold the batch row lock; never performs remote I/O."""
    version = session.get(ScriptVersion, config.script_version_id)
    assert version is not None
    policy = config.policy
    active = count_status(session, batch.id, OCCUPYING)
    pending = count_status(session, batch.id, frozenset({RunStatus.WAITING}))
    target, reason, http_status = RunStatus.QUEUED, None, 202
    mode, max_concurrent, max_queued = concurrency(policy)
    if mode == "REPLACE":
        replace_running(session, batch.id, actor)
        pending = 0
    if mode == "SKIP" and active:
        target, reason = RunStatus.SKIPPED, "OVERLAP"
    elif active >= max_concurrent or pending:
        if pending >= max_queued:
            target, reason = RunStatus.SKIPPED, "SKIPPED_CAPACITY"
            http_status = 409
        else:
            target = RunStatus.WAITING
    run = BatchRun(
        id=uuid4(),
        batch_id=batch.id,
        scheduled_for=scheduled_for,
        trigger="SCHEDULED" if scheduled_for else "MANUAL",
        owner_id=batch.owner_id,
        script_id=version.script_id,
        script_version_id=version.id,
        config_number=config.number,
        runtime=config.runtime,
        snapshot={
            "script_id": str(version.script_id),
            "script_version_id": str(version.id),
            "sha256": version.sha256,
            "code_hash": version.code_hash,
            "schedule": config.schedule,
            "policy": policy,
            # Fixed per run: the Executor idempotency fingerprint includes
            # it, so a resubmission must send the same value.
            "tenant_id": config.tenant_id,
        },
        status=target,
        skip_reason=reason,
        artifacts=[],
        attempts=[],
    )
    run.stamp_created(actor, session.scalar(select(func.clock_timestamp())))
    if target == RunStatus.SKIPPED:
        run.completed_at = run.created_at
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
    if target == RunStatus.QUEUED:
        enqueue(session, TaskKind.SUBMIT_EXECUTION, run.id)
    session.flush()
    return http_status, run


def promote_waiting(
    session: Session, batch_id: UUID, actor: Actor = DISPATCHER
) -> None:
    """Fill freed concurrency slots in admission order.

    Caller must hold the batch row lock, which also serializes admission.
    """
    while True:
        oldest = session.scalar(
            select(BatchRun)
            .where(
                BatchRun.batch_id == batch_id,
                BatchRun.status == RunStatus.WAITING,
            )
            .order_by(BatchRun.created_at, BatchRun.id)
            .limit(1)
        )
        if oldest is None:
            return
        _, maximum, _ = concurrency(oldest.snapshot["policy"])
        if count_status(session, batch_id, OCCUPYING) >= maximum:
            return
        queue_submission(session, oldest)
        oldest.stamp_updated(actor, database_now(session))
        session.flush()


def submission_request(session: Session, run: BatchRun) -> dict[str, Any]:
    version = session.get(ScriptVersion, run.script_version_id)
    assert version is not None
    request: dict[str, Any] = {
        "idempotency_key": f"batch-run:{run.id}:submit",
        "lifecycle": {"operation_mode": "SINGLE"},
        "trigger": {
            "type": "BATCH",
            # The owner runs the batch; metadata.batch_id names the batch.
            "actor": {"type": "USER", "id": run.owner_id},
        },
        "runtime": dict(run.runtime),
        # Executor scope only; metadata.run_id names the run.
        "context": {"user_id": run.owner_id},
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
            "batch_id": str(run.batch_id),
            "script_id": str(run.script_id),
            "script_version_id": str(run.script_version_id),
            "run_id": str(run.id),
        },
    }
    if tenant_id := run.snapshot.get("tenant_id"):
        request["tenant_id"] = tenant_id
    return request


def run_view(run: BatchRun) -> RunView:
    view = RunView.model_validate(run)
    view.result = ResultRefs(notebook_artifact_id=result_notebook(run))
    return view


class RunService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.idempotency = Idempotency(sessions)

    def owned_run(
        self,
        session: Session,
        owner: str,
        batch_id: UUID,
        run_id: UUID,
        *,
        lock: bool = False,
    ) -> BatchRun:
        query = select(BatchRun).where(
            BatchRun.id == run_id,
            BatchRun.batch_id == batch_id,
            BatchRun.owner_id == owner,
        )
        if lock:
            query = query.with_for_update()
        run = session.scalar(query)
        if run is None:
            raise ServiceError(404, "NOT_FOUND", "Run not found.")
        return run

    def create(self, owner: str, batch_id: UUID, key: str) -> MutationResult:
        def action(session: Session) -> MutationResult:
            batch = owned_batch(session, owner, batch_id, lock=True)
            config = current_config(session, batch)
            status, run = admit_run(session, batch, config, Actor.user(owner))
            return status, run_view(run).model_dump(mode="json")

        return self.idempotency.run(owner, f"run:{batch_id}", key, {}, action)

    def cancel(
        self, owner: str, batch_id: UUID, run_id: UUID, key: str
    ) -> MutationResult:
        def action(session: Session) -> MutationResult:
            # Lock order everywhere: batch row, then run row.
            owned_batch(session, owner, batch_id, lock=True)
            run = self.owned_run(session, owner, batch_id, run_id, lock=True)
            if run.status not in CANCELLABLE:
                raise ServiceError(
                    409, "NOT_CANCELLABLE", "Run can no longer be cancelled."
                )
            now = database_now(session)
            run.stamp_updated(Actor.user(owner), now)
            request_cancel(run, "USER", USER_CANCEL_REASON)
            if run.status == RunStatus.WAITING:
                run.status = RunStatus.CANCELLED
                run.completed_at = now
            elif not run.cancel_requested:
                # QUEUED: the submit task skips or follows up with a cancel.
                run.cancel_requested = True
                if run.status == RunStatus.RUNNING:
                    enqueue(session, TaskKind.CANCEL_EXECUTION, run.id)
            return 202, run_view(run).model_dump(mode="json")

        return self.idempotency.run(
            owner, f"run:cancel:{run_id}", key, {}, action
        )

    def get(self, owner: str, batch_id: UUID, run_id: UUID) -> RunDetail:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            run = self.owned_run(session, owner, batch_id, run_id)
            steps = session.scalars(
                select(RunStepBinding)
                .where(RunStepBinding.run_id == run_id)
                .order_by(RunStepBinding.sequence)
            )
            return RunDetail(
                **run_view(run).model_dump(),
                steps=[StepView.model_validate(step) for step in steps],
            )

    def list(
        self, owner: str, batch_id: UUID, limit: int, cursor: str | None
    ) -> RunPage:
        with self.sessions() as session:
            owned_batch(session, owner, batch_id)
            query = select(BatchRun).where(
                BatchRun.batch_id == batch_id, BatchRun.owner_id == owner
            )
            if cursor:
                query = query.where(
                    before_cursor(BatchRun.created_at, BatchRun.id, cursor)
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        BatchRun.created_at.desc(), BatchRun.id.desc()
                    ).limit(limit + 1)
                )
            )
            items = [run_view(row) for row in rows[:limit]]
            return RunPage(
                items=items,
                next_cursor=(
                    encode_cursor(items[-1].created_at, items[-1].id)
                    if len(rows) > limit
                    else None
                ),
            )


def result_notebook(run: BatchRun) -> str | None:
    """The result notebook; reports live in pr-agentic-service.

    pr-batch does not relay downloads: prj_re checks ownership through this
    service, then streams the artifact from the Executor download API.
    """
    for artifact in run.artifacts:
        if (
            artifact.get("type") == "NOTEBOOK"
            and artifact.get("status") == "AVAILABLE"
        ):
            return artifact.get("artifact_id")
    return None
