"""Claims due tasks and drives them: prepare → remote call → apply."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import DISPATCHER
from app.db.session import database_now
from app.integrations.executor import ExecutorError
from app.models import Batch, BatchRun
from app.tasks.handlers import (
    ABANDONED,
    HANDLERS,
    UNCERTAIN,
    Clients,
    abandon,
)
from app.tasks.queue import (
    ClaimedTask,
    TaskKind,
    backoff,
    claim,
    complete,
    failing_for,
    locked_lease,
    next_due,
)

logger = logging.getLogger(__name__)


def progress(run: BatchRun) -> tuple[object, ...]:
    """User-visible facts; polls that observe nothing new are not updates.

    (Compared by value because autoflush clears ORM change tracking.)
    """
    return (
        run.status,
        run.execution_status,
        run.observed_version,
        run.notebook_status,
        run.failure_type,
        run.last_error,
        run.completed_at,
        len(run.artifacts or ()),
    )


def lock_run(session: Session, run_id) -> BatchRun | None:
    """Lock order everywhere: batch row, then run row."""
    batch_id = session.scalar(
        select(BatchRun.batch_id).where(BatchRun.id == run_id)
    )
    if batch_id is None:
        return None
    session.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    return session.scalar(
        select(BatchRun)
        .where(BatchRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def unexpected(
    claimed: ClaimedTask, stage: str, exc: Exception
) -> ExecutorError:
    """A bug or unforeseen error: retried with backoff and the give-up
    limit like an outage, instead of on every lease expiry forever."""
    logger.error(
        "Task %s %s: unexpected error in %s.",
        claimed.kind,
        claimed.id,
        stage,
        exc_info=exc,
    )
    # No status: a submission is never taken as refused, so it is
    # retried as uncertain under the same idempotency key.
    return ExecutorError(f"UNEXPECTED_ERROR: {type(exc).__name__}")


class Dispatcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clients: Clients,
        *,
        lease_seconds: int = 90,
        give_up_seconds: int = 86400,
    ) -> None:
        self.sessions = sessions
        self.clients = clients
        self.lease_seconds = lease_seconds
        self.give_up = timedelta(seconds=give_up_seconds)

    def tick(self) -> bool:
        """Process at most one task; False when nothing was due."""
        broken = None
        with self.sessions.begin() as session:
            claimed = claim(session, self.lease_seconds)
            if claimed is None:
                return False
            try:
                # A savepoint keeps the claim when prepare itself breaks.
                with session.begin_nested():
                    context = self.prepare(session, claimed)
            except SQLAlchemyError:
                raise
            except Exception as exc:
                broken = unexpected(claimed, "prepare", exc)
        if broken is not None:
            self.fail(claimed, broken)
            return True
        if context is None:
            return True
        handler = HANDLERS[claimed.kind]
        try:
            response = handler.call(self.clients, context)
        except ExecutorError as exc:
            self.fail(claimed, exc)
            return True
        except Exception as exc:
            self.fail(claimed, unexpected(claimed, "call", exc))
            return True
        try:
            with self.sessions.begin() as session:
                task = locked_lease(session, claimed)
                if task is None:
                    return True  # lease lost; another pod owns it now
                run = lock_run(session, claimed.run_id)
                assert run is not None
                now = database_now(session)
                before = progress(run)
                run.last_error = None  # apply may record a new one
                handler.apply(
                    session, task, run, context, response, now, self.clients
                )
                if progress(run) != before:
                    run.stamp_updated(DISPATCHER, now)
        except ExecutorError as exc:  # contract violations found in apply
            self.fail(claimed, exc)
        except SQLAlchemyError:
            raise  # an outage, not this task's fault: the lease recovers it
        except Exception as exc:
            self.fail(claimed, unexpected(claimed, "apply", exc))
        return True

    def prepare(self, session: Session, claimed: ClaimedTask):
        handler = HANDLERS[claimed.kind]
        task = locked_lease(session, claimed)
        assert task is not None
        run = lock_run(session, claimed.run_id)
        context = None
        if run is not None:
            before = progress(run)
            context = handler.prepare(session, run, claimed)
            if progress(run) != before:
                run.stamp_updated(DISPATCHER, database_now(session))
        if context is None:
            complete(task)  # stale or no longer needed
        return context

    def fail(self, claimed: ClaimedTask, error: ExecutorError) -> None:
        code = str(error)
        # Key or tenant settings, not an outage: make it stand out.
        log = logger.error if error.auth_rejected else logger.warning
        log("Task %s %s failed: %s", claimed.kind, claimed.id, code)
        with self.sessions.begin() as session:
            task = locked_lease(session, claimed)
            if task is None:
                return
            run = lock_run(session, claimed.run_id)
            now = database_now(session)
            before = progress(run) if run is not None else None
            if claimed.payload.get(ABANDONED):
                # Best effort for a run already given up on: no retries.
                complete(task)
            elif failing_for(task, now) >= self.give_up:
                # Retrying the same call has not worked for this long:
                # stop tracking the run so it frees its slot.
                logger.error(
                    "Task %s %s abandoned: %s", claimed.kind, claimed.id, code
                )
                complete(task)
                if (
                    run is not None
                    and claimed.kind != TaskKind.CANCEL_EXECUTION
                ):
                    abandon(session, run, code, now)
            else:
                if (
                    claimed.kind == TaskKind.SUBMIT_EXECUTION
                    and not error.submit_rejected
                ):
                    task.payload = {**task.payload, UNCERTAIN: True}
                backoff(task, now, code)
                if run is not None:
                    run.last_error = code
            if run is not None and progress(run) != before:
                run.stamp_updated(DISPATCHER, now)

    def next_due(self) -> datetime | None:
        with self.sessions() as session:
            return next_due(session)
