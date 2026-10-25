"""Startup schema upgrade (the env script serializes replicas)."""

import logging

from alembic import command
from alembic.config import Config

from app.core.config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)


def upgrade_to_head(settings: Settings) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = (
        settings.database_url.get_secret_value()
    )
    logger.info("Applying database migrations.")
    command.upgrade(config, "head")
