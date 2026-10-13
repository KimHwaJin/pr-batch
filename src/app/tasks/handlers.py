"""Task handlers: prepare (in DB) → one remote call → apply (in DB).

Each handler re-checks the run status before acting, so duplicated or
stale tasks finish without side effects. Status transitions and the tasks
they require are written in the same transaction.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import database_now
from app.domain.run_state import EXECUTOR_TERMINAL, RunStatus
from app.integrations.executor import (
    Detail,
    ExecutorClient,
    Receipt,
    Result,
    require,
)
from app.integrations.report import ReportClient, ReportJob
from app.models import BatchRun, RunReport, RunStepBinding, Task
from app.services.run_service import (
    latest_report,
    promote_waiting,
    start_report,
    submission_request,
)
from app.tasks.queue import (
    ClaimedTask,
    TaskKind,
    complete,
    enqueue,
    reschedule,
)


@dataclass
class Clients:
    executor: ExecutorClient
    report: ReportClient | None
    sync_initial_seconds: int
    sync_max_seconds: int


class Handler(Protocol):
    def prepare(
        self, session: Session, run: BatchRun, task: ClaimedTask
    ) -> Any | None: ...

    def call(self, clients: Clients, context: Any) -> Any: ...

    def apply(
        self,
        session: Session,
        task: Task,
        run: BatchRun,
        context: Any,
        response: Any,
        now: datetime,
        clients: Clients,
    ) -> None: ...


def bindings(session: Session, run: BatchRun) -> list[RunStepBinding]:
    return list(
        session.scalars(
            select(RunStepBinding)
            .where(RunStepBinding.run_id == run.id)
            .order_by(RunStepBinding.sequence)
        )
    )


def finish(run: BatchRun, status: RunStatus, now: datetime) -> None:
    run.status = status
    run.completed_at = now


def next_interval(task: Task, clients: Clients) -> tuple[int, dict[str, Any]]:
    current = int(task.payload.get("interval", clients.sync_initial_seconds))
    following = min(current * 2, clients.sync_max_seconds)
    return current, {**task.payload, "interval": following}


def poll_again(task: Task, now: datetime, clients: Clients) -> None:
    wait, payload = next_interval(task, clients)
    reschedule(task, now + timedelta(seconds=wait), payload)


class SubmitExecution:
    def prepare(self, session, run, task):
        if run.status != RunStatus.QUEUED or run.execution_id is not None:
            return None
        if run.cancel_requested:
            # Cancelled before anything reached the Executor.
            finish(run, RunStatus.CANCELLED, database_now(session))
            promote_waiting(session, run.batch_id)
            return None
        return submission_request(session, run)

    def call(self, clients, context):
        return clients.executor.submit(context)

    def apply(self, session, task, run, context, response, now, clients):
        assert isinstance(response, Receipt)
        complete(task)
        if run.status != RunStatus.QUEUED:
            return
        steps = response.operation.steps
        rows = bindings(session, run)
        by_sequence = {step.sequence: step.step_id for step in steps}
        require(len(steps) == len(rows))
        require(set(by_sequence) == {b.sequence for b in rows})
        require(len({step.step_id for step in steps}) == len(steps))
        for row in rows:
            row.step_id = by_sequence[row.sequence]
        run.execution_id = response.execution_id
        run.operation_id = response.operation.operation_id
        run.execution_status = response.state.status
        run.observed_version = response.state.version
        run.submitted_at = now
        run.observed_at = now
        run.status = RunStatus.RUNNING
        enqueue(
            session,
            TaskKind.SYNC_EXECUTION,
            run.id,
            delay_seconds=clients.sync_initial_seconds,
        )
        if run.cancel_requested:
            enqueue(session, TaskKind.CANCEL_EXECUTION, run.id)


class SyncExecution:
    def prepare(self, session, run, task):
        if run.status != RunStatus.RUNNING or run.execution_id is None:
            return None
        return run.execution_id

    def call(self, clients, context):
        return clients.executor.detail(context)

    def apply(self, session, task, run, context, response, now, clients):
        assert isinstance(response, Detail)
        if run.status != RunStatus.RUNNING:
            complete(task)
            return
        require(response.execution_id == run.execution_id)
        require(response.context.user_id == run.owner_id)
        require(response.context.task_id == str(run.id))
        require(response.state.version >= (run.observed_version or 0))
        if response.state.version == run.observed_version:
            require(response.state.status == run.execution_status)
        run.execution_status = response.state.status
        run.observed_version = response.state.version
        run.observed_at = now
        run.started_at = response.lifecycle.started_at
        run.finished_at = response.lifecycle.finished_at
        run.failure_type = response.failure.type if response.failure else None
        run.retry_info = response.retry.model_dump(mode="json")
        run.notebook_status = response.workspace.notebook_projection.status
        if response.state.status not in EXECUTOR_TERMINAL:
            poll_again(task, now, clients)
            return
        complete(task)
        # Code execution ended: free the slot before collecting results.
        run.status = RunStatus.COLLECTING
        promote_waiting(session, run.batch_id)
        enqueue(session, TaskKind.COLLECT_RESULT, run.id)


class CollectResult:
    def prepare(self, session, run, task):
        if run.status != RunStatus.COLLECTING or run.execution_id is None:
            return None
        return run.execution_id

    def call(self, clients, context):
        # The notebook projection is only reported by the detail endpoint.
        return clients.executor.detail(context), clients.executor.result(
            context
        )

    def apply(self, session, task, run, context, response, now, clients):
        detail, response = response
        assert isinstance(detail, Detail) and isinstance(response, Result)
        if run.status != RunStatus.COLLECTING:
            complete(task)
            return
        require(detail.execution_id == run.execution_id)
        require(detail.state.status == run.execution_status)
        run.notebook_status = detail.workspace.notebook_projection.status
        rows = bindings(session, run)
        require(response.execution.execution_id == run.execution_id)
        require(
            response.execution.state.version >= (run.observed_version or 0)
        )
        require(response.execution.state.status == run.execution_status)
        require(len(response.operations) == 1)
        operation = response.operations[0]
        require(operation.operation_id == run.operation_id)
        require(len(operation.steps) == len(rows))
        by_sequence = {step.sequence: step for step in operation.steps}
        require(set(by_sequence) == {b.sequence for b in rows})
        for row in rows:
            step = by_sequence[row.sequence]
            require(step.step_id == row.step_id)
            ref = step.result.result_ref
            if ref:
                require(ref.execution_id == run.execution_id)
                require(ref.step_id == row.step_id)
            row.status = step.result.status
            row.result_ref = ref.model_dump(mode="json") if ref else None
        for artifact in response.artifacts:
            require(artifact.produced_by.execution_id == run.execution_id)
        for attempt in response.attempts:
            require(attempt.execution_id == run.execution_id)
        run.artifacts = [a.model_dump(mode="json") for a in response.artifacts]
        run.attempts = [a.model_dump(mode="json") for a in response.attempts]
        outcome = run.execution_status
        if outcome == "CANCELLED":
            complete(task)
            finish(run, RunStatus.CANCELLED, now)
            return
        if outcome != "SUCCEEDED":
            complete(task)
            finish(run, RunStatus.FAILED, now)
            return
        notebook_ready = any(
            a.type == "NOTEBOOK" and a.status == "AVAILABLE"
            for a in response.artifacts
        )
        if run.notebook_status == "SUCCEEDED" and notebook_ready:
            complete(task)
            if clients.report is None:
                finish(run, RunStatus.COMPLETED, now)
                return
            start_report(session, run)
            return
        if run.notebook_status == "FAILED":
            # Code succeeded but no result notebook exists to report on.
            complete(task)
            run.report_generation += 1
            session.add(
                RunReport(
                    run_id=run.id,
                    generation=run.report_generation,
                    status="FAILED",
                    error_code="RESULT_NOTEBOOK_UNAVAILABLE",
                    artifacts=[],
                    created_at=now,
                    updated_at=now,
                )
            )
            finish(run, RunStatus.REPORT_FAILED, now)
            return
        # The notebook projection can finish after the execution.
        poll_again(task, now, clients)


class CancelExecution:
    def prepare(self, session, run, task):
        if (
            run.status != RunStatus.RUNNING
            or run.execution_id is None
            or not run.cancel_requested
        ):
            return None
        return (
            run.execution_id,
            f"batch-run:{run.id}:cancel",
            str(run.batch_id),
        )

    def call(self, clients, context):
        execution_id, key, actor = context
        return clients.executor.cancel(execution_id, key, actor)

    def apply(self, session, task, run, context, response, now, clients):
        # SYNC_EXECUTION observes the resulting CANCELLED state.
        complete(task)


def report_payload(
    session: Session, run: BatchRun, generation: int
) -> dict[str, Any]:
    notebook = next(
        (
            a["artifact_id"]
            for a in run.artifacts
            if a.get("type") == "NOTEBOOK" and a.get("status") == "AVAILABLE"
        ),
        None,
    )
    return {
        "idempotency_key": f"batch-run:{run.id}:report:{generation}",
        "batch_run_id": str(run.id),
        "generation": generation,
        "owner_id": run.owner_id,
        "execution_id": str(run.execution_id),
        "notebook_artifact_id": notebook,
        "steps": [
            {
                "sequence": row.sequence,
                "original_index": row.original_index,
                "step_id": str(row.step_id) if row.step_id else None,
                "result_ref": row.result_ref,
            }
            for row in bindings(session, run)
        ],
    }


def apply_report_job(
    run: BatchRun, report: RunReport, job: ReportJob, now: datetime
) -> bool:
    """Record a job snapshot; True once the report reached a final state."""
    report.job_id = job.report_job_id
    report.updated_at = now
    if job.status == "SUCCEEDED":
        report.status = "SUCCEEDED"
        report.artifacts = [a.model_dump() for a in job.artifacts]
        finish(run, RunStatus.COMPLETED, now)
        return True
    if job.status == "FAILED":
        report.status = "FAILED"
        report.error_code = job.error_code or "REPORT_FAILED"
        finish(run, RunStatus.REPORT_FAILED, now)
        return True
    report.status = "RUNNING"
    return False


class RequestReport:
    def prepare(self, session, run, task):
        generation = task.payload.get("generation")
        report = latest_report(session, run)
        if (
            run.status != RunStatus.REPORTING
            or report is None
            or report.generation != generation
            or report.job_id is not None
        ):
            return None
        return run.owner_id, report_payload(session, run, generation)

    def call(self, clients, context):
        assert clients.report is not None
        owner, payload = context
        return clients.report.request_report(owner, payload)

    def apply(self, session, task, run, context, response, now, clients):
        complete(task)
        report = latest_report(session, run)
        if (
            run.status != RunStatus.REPORTING
            or report is None
            or report.generation != context[1]["generation"]
        ):
            return
        if not apply_report_job(run, report, response, now):
            enqueue(
                session,
                TaskKind.SYNC_REPORT,
                run.id,
                {"generation": report.generation},
                delay_seconds=clients.sync_initial_seconds,
            )


class SyncReport:
    def prepare(self, session, run, task):
        report = latest_report(session, run)
        if (
            run.status != RunStatus.REPORTING
            or report is None
            or report.generation != task.payload.get("generation")
            or report.job_id is None
        ):
            return None
        return run.owner_id, report.job_id, report.generation

    def call(self, clients, context):
        assert clients.report is not None
        owner, job_id, _ = context
        return clients.report.report(owner, job_id)

    def apply(self, session, task, run, context, response, now, clients):
        report = latest_report(session, run)
        if (
            run.status != RunStatus.REPORTING
            or report is None
            or report.generation != context[2]
        ):
            complete(task)
            return
        if apply_report_job(run, report, response, now):
            complete(task)
        else:
            poll_again(task, now, clients)


HANDLERS: dict[TaskKind, Handler] = {
    TaskKind.SUBMIT_EXECUTION: SubmitExecution(),
    TaskKind.SYNC_EXECUTION: SyncExecution(),
    TaskKind.COLLECT_RESULT: CollectResult(),
    TaskKind.CANCEL_EXECUTION: CancelExecution(),
    TaskKind.REQUEST_REPORT: RequestReport(),
    TaskKind.SYNC_REPORT: SyncReport(),
}
