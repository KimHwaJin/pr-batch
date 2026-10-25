"""Runtime selection mirrors the Executor's `runtime` object.

A batch names `{type, profile}`; the Executor's BATCH pool decides which
profiles exist. Script types declare which runtime types can run them.
"""

from typing import Literal

from app.domain.scripts import ScriptType

RuntimeType = Literal["JUPYTER"]
BATCH_POOL = "BATCH"

SCRIPT_RUNTIMES: dict[ScriptType, frozenset[str]] = {
    ScriptType.NOTEBOOK: frozenset({"JUPYTER"}),
}
