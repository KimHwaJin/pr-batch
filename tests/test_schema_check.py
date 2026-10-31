"""Readiness across schema revisions: rollouts and rollbacks."""

from pathlib import Path

from sqlalchemy import create_engine, text

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
