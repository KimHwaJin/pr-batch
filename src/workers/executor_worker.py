"""Restartable PostgreSQL dispatcher/reconciler for Executor runs."""

import argparse
import logging
import random
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import Settings
from src.db.session import create_db_engine, create_sessions, database_now
from src.integrations.executor import (
    Detail,
    ExecutorClient,
    ExecutorError,
    Receipt,
    Result,
    require,
)
from src.models import Batch, BatchRun, RunCommand, RunStepBinding
from src.services.run_service import TERMINAL, occupied

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Work:
    run_id: UUID
    token: UUID
    phase: str
    payload: dict[str, Any]
    execution_id: UUID | None


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        executor: ExecutorClient,
        *,
        poll_seconds: int = 5,
    ) -> None:
        self.sessions = sessions
        self.executor = executor
        self.poll_seconds = poll_seconds

    def claim(self) -> Work | None:
        with self.sessions.begin() as session:
            now = database_now(session)
            command = session.scalar(
                select(RunCommand)
                .where(
                    RunCommand.phase != "DONE",
                    RunCommand.available_at <= now,
                    or_(
                        RunCommand.lease_until.is_(None),
                        RunCommand.lease_until <= now,
                    ),
                )
                .order_by(RunCommand.available_at, RunCommand.run_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if command is None:
                return None
            run = session.get(BatchRun, command.run_id)
            assert run is not None
            if run.dispatch_status == "WAITING_CAPACITY":
                # Admission and promotion serialize on the same batch row.
                session.scalar(
                    select(Batch)
                    .where(Batch.id == run.batch_id)
                    .with_for_update()
                )
                earlier = session.scalar(
                    select(BatchRun.id)
                    .where(
                        BatchRun.batch_id == run.batch_id,
                        BatchRun.dispatch_status == "WAITING_CAPACITY",
                        tuple_(BatchRun.created_at, BatchRun.id)
                        < tuple_(run.created_at, run.id),
                    )
                    .limit(1)
                )
                maximum = run.snapshot["policy"]["max_concurrent"]
                if earlier or occupied(session, run.batch_id) >= maximum:
                    command.available_at = now + timedelta(
                        seconds=self.poll_seconds
                    )
                    return None
                run.dispatch_status = "PENDING"
            token = uuid4()
            command.lease_token = token
            command.lease_until = now + timedelta(seconds=90)
            return Work(
                run.id,
                token,
                command.phase,
                command.payload,
                run.execution_id,
            )

    def perform(self, work: Work) -> Receipt | Detail | Result:
        if work.phase == "SUBMIT":
            return self.executor.submit(work.payload)
        assert work.execution_id is not None
        if work.phase == "POLL":
            return self.executor.detail(work.execution_id)
        return self.executor.result(work.execution_id)

    def apply(self, work: Work, response: Receipt | Detail | Result) -> bool:
        with self.sessions.begin() as session:
            command = session.scalar(
                select(RunCommand)
                .where(RunCommand.run_id == work.run_id)
                .with_for_update()
            )
            assert command is not None
            now = database_now(session)
            if (
                command.lease_token != work.token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            run = session.get(BatchRun, work.run_id)
            assert run is not None
            bindings = list(
                session.scalars(
                    select(RunStepBinding)
                    .where(RunStepBinding.run_id == run.id)
                    .order_by(RunStepBinding.sequence)
                )
            )
            if isinstance(response, Receipt):
                self.apply_receipt(run, command, bindings, response, now)
            elif isinstance(response, Detail):
                self.apply_detail(run, command, response, now)
            else:
                self.apply_result(run, command, bindings, response)
            command.failures = 0
            command.lease_token = None
            command.lease_until = None
            command.available_at = now + timedelta(seconds=self.poll_seconds)
            run.tracking_error = None
            return True

    @staticmethod
    def apply_receipt(
        run: BatchRun,
        command: RunCommand,
        bindings: list[RunStepBinding],
        response: Receipt,
        now: datetime,
    ) -> None:
        steps = response.operation.steps
        require(len(steps) == len(bindings))
        by_sequence = {step.sequence: step.step_id for step in steps}
        require(set(by_sequence) == {b.sequence for b in bindings})
        require(len({step.step_id for step in steps}) == len(steps))
        for binding in bindings:
            binding.step_id = by_sequence[binding.sequence]
        run.execution_id = response.execution_id
        run.operation_id = response.operation.operation_id
        run.dispatch_status = "SUBMITTED"
        run.execution_status = response.state.status
        run.observed_version = response.state.version
        run.submitted_at = now
        run.observed_at = now
        command.phase = "POLL"

    @staticmethod
    def apply_detail(
        run: BatchRun,
        command: RunCommand,
        response: Detail,
        now: datetime,
    ) -> None:
        require(response.execution_id == run.execution_id)
        require(response.context.user_id == run.owner_id)
        require(response.context.task_id == str(run.id))
        require(response.state.version >= (run.observed_version or 0))
        if response.state.version == run.observed_version:
            require(response.state.status == run.execution_status)
        # Out-of-band retries must not silently turn historical terminal runs
        # into active runs; Batch does not expose Executor's retry endpoint.
        if run.execution_status in TERMINAL:
            require(response.state.status == run.execution_status)
        run.execution_status = response.state.status
        run.observed_version = response.state.version
        run.observed_at = now
        run.started_at = response.lifecycle.started_at
        run.finished_at = response.lifecycle.finished_at
        run.failure_type = response.failure.type if response.failure else None
        run.retry_info = response.retry.model_dump(mode="json")
        run.notebook_status = response.workspace.notebook_projection.status
        if response.state.status in TERMINAL:
            command.phase = "RESULT"
            if response.state.status != "SUCCEEDED":
                run.report_status = "NOT_APPLICABLE"

    @staticmethod
    def apply_result(
        run: BatchRun,
        command: RunCommand,
        bindings: list[RunStepBinding],
        response: Result,
    ) -> None:
        require(response.execution.execution_id == run.execution_id)
        require(
            response.execution.state.version >= (run.observed_version or 0)
        )
        require(response.execution.state.status == run.execution_status)
        require(len(response.operations) == 1)
        operation = response.operations[0]
        require(operation.operation_id == run.operation_id)
        require(len(operation.steps) == len(bindings))
        by_sequence = {step.sequence: step for step in operation.steps}
        require(set(by_sequence) == {b.sequence for b in bindings})
        for binding in bindings:
            step = by_sequence[binding.sequence]
            require(step.step_id == binding.step_id)
            ref = step.result.result_ref
            if ref:
                require(ref.execution_id == run.execution_id)
                require(ref.step_id == binding.step_id)
            binding.status = step.result.status
            binding.result_ref = ref.model_dump(mode="json") if ref else None
        for artifact in response.artifacts:
            require(artifact.produced_by.execution_id == run.execution_id)
        for attempt in response.attempts:
            require(attempt.execution_id == run.execution_id)
        run.artifacts = [a.model_dump(mode="json") for a in response.artifacts]
        run.attempts = [a.model_dump(mode="json") for a in response.attempts]
        has_notebook = any(
            a.type == "NOTEBOOK" and a.status == "AVAILABLE"
            for a in response.artifacts
        )
        if has_notebook and run.notebook_status == "SUCCEEDED":
            run.result_status = "READY"
            command.phase = "DONE"
        elif run.notebook_status == "FAILED":
            run.result_status = "UNAVAILABLE"
            command.phase = "DONE"
        else:
            # Projection may finish after the execution; continue observing.
            command.phase = "POLL"

    def fail(self, work: Work, code: str) -> None:
        with self.sessions.begin() as session:
            command = session.scalar(
                select(RunCommand)
                .where(RunCommand.run_id == work.run_id)
                .with_for_update()
            )
            assert command is not None
            now = database_now(session)
            if (
                command.lease_token != work.token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return
            command.failures += 1
            delay = min(300, 2 ** min(command.failures, 9))
            command.available_at = now + timedelta(
                seconds=delay + random.uniform(0, delay / 4)
            )
            command.lease_token = None
            command.lease_until = None
            run = session.get(BatchRun, work.run_id)
            assert run is not None
            run.tracking_error = code

    def tick(self) -> bool:
        work = self.claim()
        if work is None:
            return False
        try:
            response = self.perform(work)
            self.apply(work, response)
        except ExecutorError as exc:
            self.fail(work, str(exc))
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if settings.executor_base_url is None:
        parser.error("Set BATCH_EXECUTOR_BASE_URL before starting the worker.")
    engine = create_db_engine(settings)
    client = ExecutorClient(settings.executor_base_url)
    worker = Worker(
        create_sessions(engine),
        client,
        poll_seconds=settings.executor_poll_seconds,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    try:
        while not stop.is_set():
            try:
                busy = worker.tick()
            except SQLAlchemyError:
                # A failed commit leaves the leased command recoverable.
                logger.error("Worker database unavailable; retrying.")
                busy = False
            if args.once:
                break
            if not busy:
                stop.wait(1)
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
