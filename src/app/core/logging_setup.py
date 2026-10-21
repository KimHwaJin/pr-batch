"""Logging setup that runs with or without the internal HCP library.

Inside the company network the HCP logger owns logging and reads its own
logger.yml. Outside it, logger.yml at the project root is applied with
the standard library so the same code runs everywhere.
"""

import logging
import logging.config
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT

LOGGER_CONFIG = PROJECT_ROOT / "logger.yml"


def configure_logging(path: Path = LOGGER_CONFIG) -> None:
    try:
        from hcp.framework.logger import (  # ty: ignore[unresolved-import]
            init_logger,
        )
    except ImportError:
        apply_logger_config(path)
    else:
        init_logger()


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
