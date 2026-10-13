"""Run admission (manual and scheduled), cancellation, report retries and
owner-scoped history. Nothing here calls a remote service; outbound calls
are enqueued as tasks in the same transaction as the state change."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ServiceError
from app.db.base import DISPATCHER, Actor
from app.db.session import database_now
from app.domain.run_state import CANCELLABLE, OCCUPYING, RunStatus
from app.models import (
    Batch,
    BatchConfig,
    BatchRun,
    RunReport,
    RunStepBinding,
    ScriptVersion,
)
from app.schemas.run import (
    ReportView,
    ResultRefs,
    RunDetail,
    RunPage,
    RunView,
    StepView,
)
from app.services.common import current_config, owned_batch
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


def queue_submission(session: Session, run: BatchRun) -> None:
    run.status = RunStatus.QUEUED
    enqueue(session, TaskKind.SUBMIT_EXECUTION, run.id)


def replace_running(session: Session, batch_id: UUID, actor: Actor) -> int:
    """REPLACE overlap: cancel waiting runs and ask running ones to stop.

    Returns how many occupying runs stopped immediately (none today: a
    running run frees its slot only when the Executor confirms the end).
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
        if run.status == RunStatus.WAITING:
            run.status = RunStatus.CANCELLED
            run.completed_at = now
        elif not run.cancel_requested:
            run.cancel_requested = True
            if run.status == RunStatus.RUNNING:
                enqueue(session, TaskKind.CANCEL_EXECUTION, run.id)
        run.stamp_updated(actor, now)
    return 0


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
    overlap = policy["overlap"]
    if overlap["mode"] == "REPLACE":
        active -= replace_running(session, batch.id, actor)
        pending = 0
    if overlap["mode"] == "SKIP" and active:
        target, reason = RunStatus.SKIPPED, "OVERLAP"
    elif active >= overlap["max_concurrent"] or pending:
        if pending >= overlap["max_queued"]:
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
        maximum = oldest.snapshot["policy"]["overlap"]["max_concurrent"]
        if count_status(session, batch_id, OCCUPYING) >= maximum:
            return
        queue_submission(session, oldest)
        oldest.stamp_updated(actor, database_now(session))
        session.flush()


def submission_request(session: Session, run: BatchRun) -> dict[str, Any]:
    version = session.get(ScriptVersion, run.script_version_id)
    assert version is not None
    return {
        "idempotency_key": f"batch-run:{run.id}:submit",
        "lifecycle": {"operation_mode": "SINGLE"},
        "trigger": {
            "type": "BATCH",
            "actor": {"type": "BATCH", "id": str(run.batch_id)},
        },
        "runtime": dict(run.runtime),
        "context": {"user_id": run.owner_id, "task_id": str(run.id)},
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
        },
    }


def latest_report(session: Session, run: BatchRun) -> RunReport | None:
    if not run.report_generation:
        return None
    return session.get(RunReport, (run.id, run.report_generation))


def run_view(session: Session, run: BatchRun) -> RunView:
    report = latest_report(session, run)
    view = RunView.model_validate(run)
    if report is not None:
        view.report = ReportView.model_validate(report)
    view.result = ResultRefs(
        notebook_artifact_id=result_artifact(run, report, "NOTEBOOK"),
        report_artifact_id=result_artifact(run, report, "REPORT"),
    )
    return view


class RunService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        reports_enabled: bool = False,
    ) -> None:
        self.sessions = sessions
        self.idempotency = Idempotency(sessions)
        self.reports_enabled = reports_enabled

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
            return status, run_view(session, run).model_dump(mode="json")

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
            run.stamp_updated(Actor.user(owner), database_now(session))
            if run.status == RunStatus.WAITING:
                run.status = RunStatus.CANCELLED
                run.completed_at = database_now(session)
            elif not run.cancel_requested:
                # QUEUED: the submit task skips or follows up with a cancel.
                run.cancel_requested = True
                if run.status == RunStatus.RUNNING:
                    enqueue(session, TaskKind.CANCEL_EXECUTION, run.id)
            return 202, run_view(session, run).model_dump(mode="json")

        return self.idempotency.run(
            owner, f"run:cancel:{run_id}", key, {}, action
        )

    def retry_report(
        self, owner: str, batch_id: UUID, run_id: UUID, key: str
    ) -> MutationResult:
        if not self.reports_enabled:
            raise ServiceError(
                503, "REPORT_NOT_CONFIGURED", "Configure the report API."
            )

        def action(session: Session) -> MutationResult:
            owned_batch(session, owner, batch_id, lock=True)
            run = self.owned_run(session, owner, batch_id, run_id, lock=True)
            if run.status != RunStatus.REPORT_FAILED:
                raise ServiceError(
                    409, "REPORT_NOT_RETRYABLE", "Only failed reports retry."
                )
            start_report(session, run, Actor.user(owner))
            return 202, run_view(session, run).model_dump(mode="json")

        return self.idempotency.run(
            owner, f"run:report:{run_id}", key, {}, action
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
                **run_view(session, run).model_dump(),
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
            items = [run_view(session, row) for row in rows[:limit]]
            return RunPage(
                items=items,
                next_cursor=(items[-1].id if len(rows) > limit else None),
            )


def result_artifact(
    run: BatchRun, report: RunReport | None, kind: str
) -> str | None:
    """Prefer the report-appended notebook; reports come from the report.

    pr-batch does not relay downloads: prj_re checks ownership through this
    service, then streams the artifact from the Executor download API.
    """
    if report is not None and report.status == "SUCCEEDED":
        for artifact in report.artifacts:
            if artifact.get("type") == kind:
                return artifact.get("artifact_id")
    if kind == "NOTEBOOK":
        for artifact in run.artifacts:
            if (
                artifact.get("type") == "NOTEBOOK"
                and artifact.get("status") == "AVAILABLE"
            ):
                return artifact.get("artifact_id")
    return None


def start_report(session: Session, run: BatchRun, actor: Actor) -> None:
    """Open a new report generation; code is never executed again."""
    now = database_now(session)
    run.stamp_updated(actor, now)
    run.report_generation += 1
    run.status = RunStatus.REPORTING
    run.completed_at = None
    report = RunReport(
        run_id=run.id,
        generation=run.report_generation,
        status="REQUESTED",
        artifacts=[],
    )
    report.stamp_created(actor, now)
    session.add(report)
    enqueue(
        session,
        TaskKind.REQUEST_REPORT,
        run.id,
        {"generation": run.report_generation},
    )
