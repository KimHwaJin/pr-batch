"""Migrations for our schema only.

The API runs them at startup (no Jobs or init containers are available);
an advisory lock lets exactly one replica migrate while others wait.
"""

import os

import yaml
from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url

from app.core.config import config_files
from app.models import Base

MIGRATION_LOCK = 7_301_202_610  # arbitrary, stable advisory lock key


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


# Kept for audit by 0002 with no model; autogenerate must not drop it.
UNMODELED_TABLES = {"run_reports_archive"}


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "batch"
    if type_ == "table":
        return name not in UNMODELED_TABLES
    return True


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
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS batch"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="batch",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
