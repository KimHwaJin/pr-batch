"""All tables; importing this package registers them on Base.metadata."""

from app.db.base import Base
from app.models.batch import Batch, BatchConfig, SkippedWindow
from app.models.idempotency import IdempotencyRecord
from app.models.run import BatchRun, RunReport, RunStepBinding, Task
from app.models.script import Script, ScriptVersion

__all__ = [
    "Base",
    "Batch",
    "BatchConfig",
    "BatchRun",
    "IdempotencyRecord",
    "ScriptVersion",
    "RunReport",
    "RunStepBinding",
    "Script",
    "SkippedWindow",
    "Task",
]
