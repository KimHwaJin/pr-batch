"""Run lifecycle: the single user-visible status and its transitions."""

from enum import StrEnum


class RunStatus(StrEnum):
    WAITING = "WAITING"  # accepted; waiting for a concurrency slot
    QUEUED = "QUEUED"  # slot reserved; Executor submission pending
    RUNNING = "RUNNING"  # submitted; code runs in the Executor
    COLLECTING = "COLLECTING"  # code finished; collecting cell results
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


# Overlap is judged by code execution only; collecting never blocks.
OCCUPYING = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})
CANCELLABLE = frozenset(
    {RunStatus.WAITING, RunStatus.QUEUED, RunStatus.RUNNING}
)
FINAL = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.SKIPPED,
    }
)

TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.WAITING: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.COLLECTING}),
    RunStatus.COLLECTING: frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
}

# Executor terminal states that end code execution.
EXECUTOR_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class InvalidTransition(Exception):
    pass


def check_transition(current: str, target: RunStatus) -> None:
    if target not in TRANSITIONS.get(RunStatus(current), frozenset()):
        raise InvalidTransition(f"{current} -> {target}")
