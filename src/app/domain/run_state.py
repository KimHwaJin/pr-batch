"""Run lifecycle: the single user-visible status and its groupings."""

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
# Still progressing; their batch and script cannot be deleted yet.
ACTIVE = frozenset(RunStatus) - FINAL

# Executor terminal states that end code execution.
EXECUTOR_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
