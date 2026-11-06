"""Readiness across schema revisions: rollouts and rollbacks."""

from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, make_url, text

from app.db.migrate import upgrade_to_head
from app.main import SchemaCheck

REVISION = """revision = "{rev}"
down_revision = {down}


def upgrade():
    pass
"""


def migrations(tmp_path: Path) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir()
    for rev, down in (("0001_a", None), ("0002_b", '"0001_a"')):
        (versions / f"{rev}.py").write_text(
            REVISION.format(rev=rev, down=down)
        )
    return tmp_path


def test_ready_at_head_or_at_a_newer_unknown_revision(tmp_path):
    check = SchemaCheck(migrations(tmp_path))
    assert check.head == "0002_b"
    assert check.ready("0002_b")
    assert check.ready("0003_from_a_later_release")
    assert not check.ready("0001_a")  # known but older: upgrade first
    assert not check.ready(None)


def test_repository_head_is_ready():
    check = SchemaCheck()
    assert check.ready(check.head)


def test_rolled_back_code_starts_on_a_newer_schema(client, database_url):
    """An older image neither fails to migrate nor stays unready."""
    engine = create_engine(database_url)
    head = SchemaCheck().head
    settings = client.app.state.settings
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE batch.alembic_version SET version_num = 'zz9'")
            )
        upgrade_to_head(settings)
        assert client.get("/readyz").status_code == 200
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE batch.alembic_version SET version_num = :v"),
                {"v": head},
            )
        engine.dispose()


def test_existing_schema_needs_no_database_create_privilege(
    database_url, settings
):
    """A DBA-made schema: the app role cannot CREATE on the database."""
    engine = create_engine(database_url)
    role = "batch_no_create_test"
    with engine.begin() as connection:
        if not connection.execute(
            text(
                "SELECT rolsuper OR rolcreaterole FROM pg_roles"
                " WHERE rolname = current_user"
            )
        ).scalar():
            pytest.skip("The test role cannot create roles.")
        database = connection.execute(text("SELECT current_database()"))
        database = database.scalar()
        connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'x'"))
        connection.execute(
            text(f'REVOKE CREATE ON DATABASE "{database}" FROM PUBLIC')
        )
        connection.execute(text(f"GRANT USAGE ON SCHEMA batch TO {role}"))
        connection.execute(
            text(f"GRANT SELECT ON batch.alembic_version TO {role}")
        )
    url = make_url(database_url).set(username=role, password="x")
    limited = settings.model_copy(
        update={
            "database_url": SecretStr(
                url.render_as_string(hide_password=False)
            )
        }
    )
    try:
        upgrade_to_head(limited)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
            connection.execute(text(f"DROP ROLE {role}"))
        engine.dispose()
