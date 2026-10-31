"""Task handlers: prepare (in DB) → one remote call → apply (in DB).

Each handler re-checks the run status before acting, so duplicated or
stale tasks finish without side effects. Status transitions and the tasks
they require are written in the same transaction.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import database_now
from app.domain.run_state import (
    ACTIVE,
    EXECUTOR_TERMINAL,
    OCCUPYING,
    RunStatus,
)
from app.integrations.executor import (
    Detail,
    ExecutorClient,
    ExecutorError,
    Receipt,
    Result,
    require,
)
from app.models import BatchRun, RunStepBinding, Task
from app.services.run_service import (
    identity_headers,
    promote_waiting,
    submission_request,
)
from app.tasks.queue import (
    ClaimedTask,
    TaskKind,
    complete,
    enqueue,
    reschedule,
)

logger = logging.getLogger(__name__)

# Task payload flag: a SUBMIT attempt failed without a definite answer.
UNCERTAIN = "uncertain"
# Task payload flag: one best-effort cancel for a run given up on.
ABANDONED = "abandoned"
ABANDON_CANCEL_REASON = "pr-batch gave up tracking this run."


@dataclass
class Clients:
    executor: ExecutorClient
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


def abandon(
    session: Session, run: BatchRun, error: str, now: datetime
) -> None:
    """Give up on a run whose remote calls kept failing.

    The Executor may still hold the execution; the run is FAILED here so
    its slot frees and its batch and script can be deleted again.
    """
    if run.status not in ACTIVE or run.status == RunStatus.WAITING:
        return
    occupying = run.status in OCCUPYING
    # Code may still be running there; its slot is freed here, so ask the
    # Executor to stop it (once: it has been unreachable for a day).
    still_running = (
        run.execution_id is not None
        and run.execution_status not in EXECUTOR_TERMINAL
    )
    finish(run, RunStatus.FAILED, now)
    run.failure_type = "ABANDONED"
    run.last_error = error
    if occupying:
        promote_waiting(session, run.batch_id)
    if still_running:
        enqueue(session, TaskKind.CANCEL_EXECUTION, run.id, {ABANDONED: True})


def next_interval(task: Task, clients: Clients) -> tuple[int, dict[str, Any]]:
    current = int(task.payload.get("interval", clients.sync_initial_seconds))
    following = min(current * 2, clients.sync_max_seconds)
    return current, {**task.payload, "interval": following}


def poll_again(task: Task, now: datetime, clients: Clients) -> None:
    wait, payload = next_interval(task, clients)
    reschedule(task, now + timedelta(seconds=wait), payload)


@dataclass
class Submission:
    request: dict[str, Any]
    # An earlier attempt may have reached the Executor (lost response,
    # outage): a later rejection then does not prove nothing was created.
    uncertain: bool
    identity: dict[str, str]


@dataclass
class Rejected:
    error: ExecutorError


class SubmitExecution:
    def prepare(self, session, run, task):
        if run.status != RunStatus.QUEUED or run.execution_id is not None:
            return None
        # A failed call sets the flag; a pod that died or lost its lease
        # mid-call never got that far, so any earlier claim counts too.
        uncertain = bool(task.payload.get(UNCERTAIN)) or task.attempts > 1
        if run.cancel_requested and not uncertain:
            # Cancelled before anything reached the Executor.
            finish(run, RunStatus.CANCELLED, database_now(session))
            promote_waiting(session, run.batch_id)
            return None
        # An uncertain earlier attempt may have created the execution:
        # resubmit (same key, so never a second one) and cancel it after.
        return Submission(
            submission_request(session, run), uncertain, identity_headers(run)
        )

    def call(self, clients, context):
        try:
            return clients.executor.submit(context.request, context.identity)
        except ExecutorError as exc:
            if exc.submit_rejected and not context.uncertain:
                return Rejected(exc)
            raise

    def apply(self, session, task, run, context, response, now, clients):
        if isinstance(response, Rejected):
            log = logger.error if response.error.auth_rejected else logger.info
            log("Run %s submission rejected: %s", run.id, response.error)
            complete(task)
            if run.status != RunStatus.QUEUED:
                return
            # Retrying the same body can never succeed; free the slot.
            finish(run, RunStatus.FAILED, now)
            run.failure_type = "SUBMIT_REJECTED"
            run.last_error = str(response.error)
            promote_waiting(session, run.batch_id)
            return
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
        return run.execution_id, identity_headers(run)

    def call(self, clients, context):
        return clients.executor.detail(*context)

    def apply(self, session, task, run, context, response, now, clients):
        assert isinstance(response, Detail)
        if run.status != RunStatus.RUNNING:
            complete(task)
            return
        require(response.execution_id == run.execution_id)
        require(response.context.user_id == run.owner_id)
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
        return run.execution_id, identity_headers(run)

    def call(self, clients, context):
        return clients.executor.result(*context)

    def apply(self, session, task, run, context, response, now, clients):
        assert isinstance(response, Result)
        complete(task)
        if run.status != RunStatus.COLLECTING:
            return
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
        # The Executor reports SUCCEEDED only once the result notebook is
        # projected and registered, so there is nothing left to wait for.
        outcome = {
            "SUCCEEDED": RunStatus.COMPLETED,
            "CANCELLED": RunStatus.CANCELLED,
        }.get(run.execution_status or "", RunStatus.FAILED)
        finish(run, outcome, now)


class CancelExecution:
    def prepare(self, session, run, task):
        if task.payload.get(ABANDONED):
            if run.failure_type != "ABANDONED" or run.execution_id is None:
                return None
            return (
                run.execution_id,
                f"batch-run:{run.id}:abandon-cancel",
                {"type": "BATCH", "id": str(run.batch_id)},
                ABANDON_CANCEL_REASON,
                identity_headers(run),
            )
        if (
            run.status != RunStatus.RUNNING
            or run.execution_id is None
            or not run.cancel_requested
        ):
            return None
        if run.cancel_source == "USER":
            actor = {"type": "USER", "id": run.owner_id}
        else:
            actor = {"type": "BATCH", "id": str(run.batch_id)}
        assert run.cancel_reason is not None  # set with every request
        return (
            run.execution_id,
            f"batch-run:{run.id}:cancel",
            actor,
            run.cancel_reason,
            identity_headers(run),
        )

    def call(self, clients, context):
        return clients.executor.cancel(*context)

    def apply(self, session, task, run, context, response, now, clients):
        # SYNC_EXECUTION observes the resulting CANCELLED state.
        complete(task)


HANDLERS: dict[TaskKind, Handler] = {
    TaskKind.SUBMIT_EXECUTION: SubmitExecution(),
    TaskKind.SYNC_EXECUTION: SyncExecution(),
    TaskKind.COLLECT_RESULT: CollectResult(),
    TaskKind.CANCEL_EXECUTION: CancelExecution(),
}
