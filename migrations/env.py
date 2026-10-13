"""Explicit migrations for our schema only; never migrate at app startup."""

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url

from app.models import Base

load_dotenv()
url = os.environ["BATCH_DATABASE_URL"]
if make_url(url).drivername != "postgresql+psycopg":
    raise ValueError("Migrations require postgresql+psycopg.")


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "batch"
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
