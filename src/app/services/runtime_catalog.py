"""Available BATCH runtimes, read from the Executor and cached briefly.

Registration and activation validate against this instead of a static
allowlist, so kernels added to the Executor are usable without redeploying
pr-batch. If the Executor cannot be reached, callers get 503 rather than
storing a runtime that may not exist.
"""

import threading
import time
from typing import get_args

from app.core.exceptions import ServiceError
from app.domain.runtimes import BATCH_POOL, SCRIPT_RUNTIMES, RuntimeType
from app.domain.scripts import ScriptType
from app.integrations.executor import ExecutorClient, ExecutorError
from app.schemas.batch import RuntimeSpec

KNOWN_TYPES = frozenset(get_args(RuntimeType))


def check_runtime(
    runtime: RuntimeSpec,
    script_type: ScriptType,
    available: list[RuntimeSpec],
) -> None:
    """Script type must fit the runtime; the Executor must offer it."""
    if runtime.type not in SCRIPT_RUNTIMES[script_type]:
        raise ServiceError(
            422,
            "RUNTIME_NOT_SUPPORTED",
            f"{script_type} scripts cannot run on {runtime.type}.",
        )
    if runtime not in available:
        raise ServiceError(
            422,
            "UNKNOWN_RUNTIME",
            "The Executor BATCH pool does not offer this runtime.",
        )


class RuntimeCatalog:
    def __init__(self, executor: ExecutorClient, ttl_seconds: float) -> None:
        self.executor = executor
        self.ttl = ttl_seconds
        # Guards the cache only; the Executor is read without holding it.
        self.lock = threading.Lock()
        self.cached: tuple[float, list[RuntimeSpec]] | None = None

    def available(self) -> list[RuntimeSpec]:
        with self.lock:
            cached = self.cached
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        try:
            targets = self.executor.runtime_targets(BATCH_POOL)
        except ExecutorError as exc:
            raise ServiceError(
                503,
                "RUNTIME_CATALOG_UNAVAILABLE",
                "Cannot read available runtimes from the Executor.",
            ) from exc
        specs = sorted(
            {
                (target.runtime.type, profile)
                for target in targets
                if target.runtime.pool == BATCH_POOL
                and target.state.enabled
                and target.runtime.type in KNOWN_TYPES
                for profile in target.runtime.supported_profiles
            }
        )
        result = [
            RuntimeSpec.model_validate({"type": kind, "profile": profile})
            for kind, profile in specs
        ]
        with self.lock:
            self.cached = (time.monotonic(), result)
        return result


class UnconfiguredRuntimeCatalog(RuntimeCatalog):
    """No Executor configured: runtimes cannot be validated."""

    def __init__(self) -> None:
        pass

    def available(self) -> list[RuntimeSpec]:
        raise ServiceError(
            503, "EXECUTOR_NOT_CONFIGURED", "Configure Executor first."
        )
