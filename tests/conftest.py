"""Tests use an explicitly supplied, disposable PostgreSQL database."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from pr_batch.app import create_app
from pr_batch.settings import Settings

TEST_SECRET = "local-test-only-" * 4
API = "/internal/v1/batches"


def token(owner: str = "alice", **overrides: object) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "sub": owner,
        "iss": "prj_re",
        "aud": "pr-batch",
        "iat": now,
        "exp": now + 120,
        "scope": "batch:manage",
    }
    claims.update(overrides)
    return {"Authorization": f"Bearer {jwt.encode(claims, TEST_SECRET)}"}


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
    result = {
        "name": "생산라인 분석",
        "kernel_profile": "python3",
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
        auth_secret=SecretStr(TEST_SECRET),
        allowed_kernel_profiles=["python3", "analytics"],
    )


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("BATCH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BATCH_TEST_DATABASE_URL for PostgreSQL integration.")
    database = make_url(url).database
    if not database or not database.endswith("_test"):
        pytest.fail("Integration database name must end with _test.")
    previous = os.environ.get("BATCH_DATABASE_URL")
    os.environ["BATCH_DATABASE_URL"] = url
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield url
    finally:
        if previous is None:
            os.environ.pop("BATCH_DATABASE_URL", None)
        else:
            os.environ["BATCH_DATABASE_URL"] = previous


@pytest.fixture
def client(database_url: str, settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE batch.run_commands, batch.run_step_bindings, "
                "batch.batch_runs, batch.schedule_revisions, "
                "batch.notebook_versions, "
                "batch.idempotency_records, batch.batches"
            )
        )
    engine.dispose()
    configured = settings.model_copy(
        update={"database_url": SecretStr(database_url)}
    )
    with TestClient(create_app(configured)) as result:
        yield result


def create_batch(
    client: TestClient,
    key: str = "create-1",
    owner: str = "alice",
    raw: bytes | None = None,
    config: dict | None = None,
):
    return client.post(
        API,
        headers={**token(owner), "Idempotency-Key": key},
        data={"metadata": json.dumps(config or metadata())},
        files={
            "file": (
                "analysis.ipynb",
                raw or notebook_bytes(),
                "application/x-ipynb+json",
            )
        },
    )
