"""BATCH runtimes this deployment offers, from settings (BATCH_RUNTIMES).

Registration and activation validate against this list. The Executor's
runtime target API is for operators (admin key) only, so the list of
kernels its BATCH pool runs is kept here; add a profile to the settings
when the Executor gains one.
"""

from app.core.exceptions import ServiceError
from app.domain.runtimes import SCRIPT_RUNTIMES
from app.domain.scripts import ScriptType
from app.schemas.batch import RuntimeSpec


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
    def __init__(self, specs: list[RuntimeSpec]) -> None:
        self.specs = list(specs)

    def available(self) -> list[RuntimeSpec]:
        if not self.specs:
            raise ServiceError(
                503,
                "RUNTIMES_NOT_CONFIGURED",
                "Configure BATCH_RUNTIMES first.",
            )
        return list(self.specs)
