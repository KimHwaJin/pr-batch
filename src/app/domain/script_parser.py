"""Dispatch an uploaded file to the parser for its declared script type."""

from collections.abc import Callable

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.domain.notebook_parser import parse_notebook
from app.domain.scripts import ParsedScript, ScriptType

Parser = Callable[[bytes, str, Settings], ParsedScript]
PARSERS: dict[ScriptType, Parser] = {ScriptType.NOTEBOOK: parse_notebook}


def parse_script(
    script_type: ScriptType, raw: bytes, filename: str, settings: Settings
) -> ParsedScript:
    parser = PARSERS.get(script_type)
    if parser is None:
        raise ServiceError(
            422,
            "UNSUPPORTED_SCRIPT_TYPE",
            f"{script_type} scripts are not supported yet.",
        )
    return parser(raw, filename, settings)
