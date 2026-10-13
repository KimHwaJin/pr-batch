"""Claims due tasks and drives them: prepare → remote call → apply."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import database_now
from app.integrations.executor import ExecutorError
from app.integrations.report import ReportError
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
RemoteError = (ExecutorError, ReportError)


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
                handler.apply(
                    session,
                    task,
                    run,
                    context,
                    response,
                    database_now(session),
                    self.clients,
                )
                run.last_error = None
        except RemoteError as exc:  # contract violations found while applying
            self.fail(claimed, str(exc))
        return True

    def prepare(self, session: Session, claimed: ClaimedTask):
        handler = HANDLERS[claimed.kind]
        task = locked_lease(session, claimed)
        assert task is not None
        run = lock_run(session, claimed.run_id)
        context = (
            None if run is None else handler.prepare(session, run, claimed)
        )
        if context is None:
            complete(task)  # stale or no longer needed
        return context

    def fail(self, claimed: ClaimedTask, code: str) -> None:
        logger.warning("Task %s %s failed: %s", claimed.kind, claimed.id, code)
        with self.sessions.begin() as session:
            task = locked_lease(session, claimed)
            if task is None:
                return
            backoff(task, database_now(session), code)
            if claimed.run_id is not None:
                run = session.get(BatchRun, claimed.run_id)
                if run is not None:
                    run.last_error = code

    def next_due(self) -> datetime | None:
        with self.sessions() as session:
            return next_due(session)
