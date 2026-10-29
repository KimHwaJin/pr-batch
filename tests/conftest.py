"""Tests use an explicitly supplied, disposable PostgreSQL database."""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import AuthClient, Settings
from app.main import create_app
from app.schemas.batch import RuntimeSpec
from app.services.runtime_catalog import RuntimeCatalog

# The calling service's key; Settings keeps only its SHA-256.
CALLER_KEY = "bat_test_0123456789abcdef0123456789abcdef"
PLAIN_KEY = "bat_test_plain_0123456789abcdef0123456789"
CALLER = AuthClient(
    name="pr-agentic-service",
    key_sha256=[hashlib.sha256(CALLER_KEY.encode()).hexdigest()],
    tenant="pr-agentic-service",
    relay=True,
    allowed_tenants=["pr-agentic-service", "agent3"],
    admin=True,
)
# A non-relay, non-admin caller.
PLAIN = AuthClient(
    name="plain-service",
    key_sha256=[hashlib.sha256(PLAIN_KEY.encode()).hexdigest()],
    tenant="plain-service",
)
API = "/api/v1/batches"
SCRIPTS = "/api/v1/scripts"


def user_id(name: str) -> str:
    """Stable UUID for a readable test user name (any opaque ID works)."""
    try:
        return str(UUID(name))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, f"user:{name}"))


ALICE, BOB = user_id("alice"), user_id("bob")


def token(
    owner: str = "alice", key: str = CALLER_KEY, **headers: str
) -> dict[str, str]:
    """Caller key + identity headers; extra keyword names map to X-...

    token(project="p1", tenant="agent3") adds X-Project-Id/X-Tenant-Id.
    """
    result = {"X-Api-Key": key, "X-User-Id": user_id(owner)}
    for name, value in headers.items():
        result[f"X-{name.title()}-Id"] = value
    return result


def notebook_bytes(
    source: str = "print('hello')", output: str = "old"
) -> bytes:
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {
                    "id": "cell-0",
                    "cell_type": "code",
                    "metadata": {},
                    "source": source,
                    "execution_count": 7,
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": output,
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    ).encode()


def metadata(**updates: object) -> dict:
    """Batch registration body; create_batch fills script_id."""
    result = {
        "name": "생산라인 분석",
        "runtime": {"type": "JUPYTER", "profile": "python3"},
        "schedule": {
            "definition": {"kind": "CRON", "expression": "0 8 * * *"}
        },
    }
    result.update(updates)
    return result


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr(
            "postgresql+psycopg://unused:unused@127.0.0.1:1/unit_test"
        ),
        storage_root=tmp_path,
        auth_clients=[CALLER, PLAIN],
        db_auto_migrate=False,
        background_enabled=False,
    )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("BATCH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BATCH_TEST_DATABASE_URL for PostgreSQL integration.")
    database = make_url(url).database
    if not database or not database.endswith("_test"):
        pytest.fail("Integration database name must end with _test.")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def client(database_url: str, settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE batch.skipped_windows, batch.tasks, "
                "batch.run_step_bindings, "
                "batch.batch_runs, batch.batch_configs, "
                "batch.batches, batch.script_versions, batch.scripts, "
                "batch.idempotency_records"
            )
        )
    engine.dispose()
    configured = settings.model_copy(
        update={"database_url": SecretStr(database_url)}
    )
    app = create_app(configured)
    app.state.runtime_catalog = RuntimeCatalog(
        [
            RuntimeSpec(type="JUPYTER", profile="python3"),
            RuntimeSpec(type="JUPYTER", profile="analytics"),
        ]
    )
    with TestClient(app) as result:
        yield result


def upload_script(
    client: TestClient,
    key: str = "script-1",
    owner: str = "alice",
    raw: bytes | None = None,
    name: str | None = None,
    filename: str = "analysis.ipynb",
    script_type: str = "NOTEBOOK",
    **identity: str,
):
    return client.post(
        SCRIPTS,
        headers={**token(owner, **identity), "Idempotency-Key": key},
        data={"type": script_type, **({"name": name} if name else {})},
        files={
            "file": (
                filename,
                raw or notebook_bytes(),
                "application/x-ipynb+json",
            )
        },
    )


def create_batch(
    client: TestClient,
    key: str = "create-1",
    owner: str = "alice",
    raw: bytes | None = None,
    config: dict | None = None,
    **identity: str,
):
    """Upload a notebook (same key) and register a batch for it.

    Keyword identity (project=..., tenant=...) goes into the headers of
    both; an identity error from the upload is returned as is.
    """
    body = dict(config or metadata())
    if "script_id" not in body:
        notebook = upload_script(
            client, f"notebook:{key}", owner, raw, **identity
        )
        if notebook.status_code in (400, 403):
            return notebook
        assert notebook.status_code == 201, notebook.text
        body["script_id"] = notebook.json()["id"]
    return client.post(
        API,
        headers={**token(owner, **identity), "Idempotency-Key": key},
        json=body,
    )
