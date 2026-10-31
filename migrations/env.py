"""Migrations for our schema only.

The API runs them at startup (no Jobs or init containers are available);
an advisory lock lets exactly one replica migrate while others wait.
"""

import logging
import os

import yaml
from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url

from app.core.config import config_files
from app.models import Base

MIGRATION_LOCK = 7_301_202_610  # arbitrary, stable advisory lock key
log = logging.getLogger("alembic.env")


def newer_than_this_code(connection) -> bool:
    """The stored revision is one this code's migrations do not know."""
    current = MigrationContext.configure(
        connection, opts={"version_table_schema": "batch"}
    ).get_current_revision()
    if current is None:
        return False
    scripts = ScriptDirectory.from_config(context.config)
    return current not in {rev.revision for rev in scripts.walk_revisions()}


def configured_url() -> str:
    """Same precedence as Settings, for the alembic CLI."""
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    for path in config_files():
        if url is not None:
            break
        values = yaml.safe_load(path.read_text()) or {}
        # Keys match Settings fields case-insensitively, as in the app.
        url = {str(k).lower(): v for k, v in values.items()}.get(
            "database_url"
        )
    if url is None:
        raise KeyError("Set DATABASE_URL, config.yml or APP_ENV.")
    return url


url = context.config.attributes.get("database_url") or configured_url()
if make_url(url).drivername != "postgresql+psycopg":
    raise ValueError("Migrations require postgresql+psycopg.")


def include_name(name, type_, parent_names):
    return type_ != "schema" or name == "batch"


if context.is_offline_mode():
    context.configure(
        url=url,
        literal_binds=True,
        target_metadata=Base.metadata,
        version_table_schema="batch",
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.execute("CREATE SCHEMA IF NOT EXISTS batch")
        context.run_migrations()
else:
    # A role named "batch" would make "batch" the default schema through
    # "$user"; reflection then reports it as None and autogenerate/check
    # sees every table as new. Pin the default to public for migrations.
    engine = create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"options": "-c search_path=public"},
    )
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK}
        )
        # CREATE SCHEMA IF NOT EXISTS checks the database CREATE privilege
        # even when the schema exists; a DBA-made schema needs no such grant.
        exists = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = 'batch'")
        ).first()
        if exists is None:
            connection.execute(text("CREATE SCHEMA batch"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="batch",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        if newer_than_this_code(connection):
            # A later release migrated ahead (this is a rollback, or the
            # old pod in a rollout): its schema stays compatible, so serve
            # it as is rather than fail on a revision we do not know.
            log.warning("Database is at a newer revision; not migrating.")
        else:
            with context.begin_transaction():
                context.run_migrations()
    engine.dispose()
