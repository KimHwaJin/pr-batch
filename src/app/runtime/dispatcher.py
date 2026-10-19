"""Claims due tasks and drives them: prepare → remote call → apply."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import DISPATCHER
from app.db.session import database_now
from app.integrations.executor import ExecutorError
from app.models import Batch, BatchRun
from app.tasks.handlers import HANDLERS, Clients
from app.tasks.queue import (
    ClaimedTask,
    backoff,
    claim,
    complete,
    locked_lease,
    next_due,
)

logger = logging.getLogger(__name__)
RemoteError = ExecutorError


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


class Dispatcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clients: Clients,
        *,
        lease_seconds: int = 90,
    ) -> None:
        self.sessions = sessions
        self.clients = clients
        self.lease_seconds = lease_seconds

    def tick(self) -> bool:
        """Process at most one task; False when nothing was due."""
        with self.sessions.begin() as session:
            claimed = claim(session, self.lease_seconds)
            if claimed is None:
                return False
            context = self.prepare(session, claimed)
        if context is None:
            return True
        handler = HANDLERS[claimed.kind]
        try:
            response = handler.call(self.clients, context)
        except RemoteError as exc:
            self.fail(claimed, str(exc))
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
                handler.apply(
                    session, task, run, context, response, now, self.clients
                )
                run.last_error = None
                if progress(run) != before:
                    run.stamp_updated(DISPATCHER, now)
        except RemoteError as exc:  # contract violations found while applying
            self.fail(claimed, str(exc))
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

    def fail(self, claimed: ClaimedTask, code: str) -> None:
        logger.warning("Task %s %s failed: %s", claimed.kind, claimed.id, code)
        with self.sessions.begin() as session:
            task = locked_lease(session, claimed)
            if task is None:
                return
            now = database_now(session)
            backoff(task, now, code)
            if claimed.run_id is not None:
                run = session.get(BatchRun, claimed.run_id)
                if run is not None and run.last_error != code:
                    run.last_error = code
                    run.stamp_updated(DISPATCHER, now)

    def next_due(self) -> datetime | None:
        with self.sessions() as session:
            return next_due(session)
