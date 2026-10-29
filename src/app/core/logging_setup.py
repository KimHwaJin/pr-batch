"""Logging setup that runs with or without the internal HCP library.

Inside the company network the HCP logger owns logging and reads its own
logger.yml. Outside it, logger.yml at the project root is applied with
the standard library so the same code runs everywhere.
"""

import logging
import logging.config
import re
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT

LOGGER_CONFIG = PROJECT_ROOT / "logger.yml"
# API keys issued for pr-batch (bat_...) and the Executor (exe_...).
API_KEY = re.compile(r"\b(?:bat|exe)_[A-Za-z0-9_-]{8,}")


class RedactApiKeys(logging.Filter):
    """Keep API keys out of log output, whoever logs them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if API_KEY.search(message):
            record.msg, record.args = API_KEY.sub("[REDACTED]", message), None
        return True


def configure_logging(path: Path = LOGGER_CONFIG) -> None:
    try:
        from hcp.framework.logger import (  # ty: ignore[unresolved-import]
            init_logger,
        )
    except ImportError:
        apply_logger_config(path)
    else:
        init_logger()
    # Handler filters see records propagated from every logger.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactApiKeys) for f in handler.filters):
            handler.addFilter(RedactApiKeys())


def apply_logger_config(path: Path) -> None:
    if not path.is_file():
        logging.basicConfig(
            level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
        )
        return
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Loggers created at import time (app.*, uvicorn.*) must stay enabled.
    config.setdefault("disable_existing_loggers", False)
    logging.config.dictConfig(config)
