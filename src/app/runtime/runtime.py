"""In-process background runtime started by the API process.

The deployment allows one container per pod and one Deployment per repo,
so each replica runs the HTTP API, the scheduler loop and a pool of task
workers. Replicas are symmetric; PostgreSQL leases coordinate them.
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.executor import ExecutorClient
from app.runtime.dispatcher import Dispatcher
from app.runtime.retention import Retention
from app.runtime.scheduler import Scheduler
from app.tasks.handlers import Clients

logger = logging.getLogger(__name__)


def executor_client(settings: Settings) -> ExecutorClient:
    assert settings.executor_base_url is not None
    key = settings.executor_api_key
    return ExecutorClient(
        settings.executor_base_url,
        api_key=key.get_secret_value() if key else None,
    )


def build_clients(settings: Settings) -> Clients:
    return Clients(
        executor=executor_client(settings),
        sync_initial_seconds=settings.sync_initial_seconds,
        sync_max_seconds=settings.sync_max_seconds,
    )


class BackgroundRuntime:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        clients: Clients,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.clients = clients
        self.scheduler = Scheduler(sessions)
        self.dispatcher = Dispatcher(
            sessions,
            clients,
            lease_seconds=settings.task_lease_seconds,
            give_up_seconds=settings.task_give_up_seconds,
        )
        self.retention = Retention(
            sessions,
            idempotency_days=settings.idempotency_retention_days,
            task_days=settings.task_retention_days,
        )
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.heartbeats: dict[str, float] = {}

    def start(self) -> None:
        loops: list[
            tuple[str, Callable[[], bool], Callable[[], datetime | None]]
        ]
        loops = [
            ("scheduler", self.scheduler.tick, self.scheduler.next_due),
            ("retention", self.retention.tick, self.retention.next_due),
        ]
        loops += [
            (f"tasks-{n}", self.dispatcher.tick, self.dispatcher.next_due)
            for n in range(self.settings.task_concurrency)
        ]
        for name, tick, next_due in loops:
            thread = threading.Thread(
                target=self.loop, args=(name, tick, next_due), name=name
            )
            thread.daemon = True
            self.heartbeats[name] = time.monotonic()
            thread.start()
            self.threads.append(thread)
        logger.info("Background runtime started with %d loops.", len(loops))

    def loop(
        self,
        name: str,
        tick: Callable[[], bool],
        next_due: Callable[[], datetime | None],
    ) -> None:
        while not self.stopping.is_set():
            self.heartbeats[name] = time.monotonic()
            try:
                if tick():
                    continue
                wait = self.idle_wait(next_due)
            except SQLAlchemyError:
                # Committed state stays consistent; leases recover the rest.
                logger.exception("%s: database unavailable; retrying.", name)
                wait = self.settings.idle_max_seconds
            except Exception:
                logger.exception("%s: unexpected error; retrying.", name)
                wait = self.settings.idle_max_seconds
            self.stopping.wait(wait)

    def idle_wait(self, next_due: Callable[[], datetime | None]) -> float:
        """Sleep until the next due item, bounded so new work is seen."""
        limit = self.settings.idle_max_seconds
        due = next_due()
        if due is None:
            return limit
        with self.sessions() as session:
            now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        return max(0.05, min(limit, (due - now).total_seconds()))

    def stop(self) -> None:
        """Stop claiming; let in-flight calls finish within the grace."""
        self.stopping.set()
        deadline = time.monotonic() + self.settings.shutdown_grace_seconds
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.clients.executor.close()
        logger.info("Background runtime stopped.")

    def healthy(self, max_age_seconds: float = 120) -> bool:
        now = time.monotonic()
        return all(
            thread.is_alive()
            and now - self.heartbeats.get(thread.name, 0) < max_age_seconds
            for thread in self.threads
        )
