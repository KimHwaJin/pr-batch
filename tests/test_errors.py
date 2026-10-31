"""One error envelope; DB errors logged, outages 503, the rest 500."""

import json
import logging

import psycopg
import pytest
from conftest import notebook_bytes
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.exceptions import ServiceError
from app.domain.notebook_parser import parse_notebook
from app.main import create_app


class Diagnostic:
    table_name = "batch_runs"
    column_name = None
    constraint_name = "uq_run_occurrence"


class UniqueViolation(Exception):
    """Stands in for psycopg's error, whose diag cannot be set."""

    sqlstate = "23505"
    diag = Diagnostic()


def raising(error):
    def route():
        raise error

    return route


@pytest.fixture
def app(settings):
    app = create_app(settings)
    unique = IntegrityError("INSERT ...", {"secret": "v"}, UniqueViolation())
    down = OperationalError("SELECT 1", {}, psycopg.OperationalError())
    for path, error in {
        "/unique": unique,
        "/down": down,
        "/pool": PoolTimeout("QueuePool limit reached"),
    }.items():
        app.add_api_route(path, raising(error))
    return app


def test_integrity_error_is_500_with_schema_detail(app, caplog):
    caplog.set_level(logging.ERROR, "app.core.exceptions")
    response = TestClient(app).get("/unique")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    (entry,) = body["error"]["details"]
    assert (entry["location"], entry["type"]) == (
        ["database"],
        "database_error",
    )
    detail = entry["context"]
    assert detail["type"] == "IntegrityError"
    assert detail["sqlstate"] == "23505"
    assert detail["table"] == "batch_runs"
    assert detail["constraint"] == "uq_run_occurrence"
    assert "secret" not in response.text and "INSERT" not in response.text
    (record,) = caplog.records
    assert detail["error_id"] in record.getMessage()
    assert "GET /unique" in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.parametrize("path", ["/down", "/pool"])
def test_connection_errors_stay_503(app, caplog, path):
    caplog.set_level(logging.ERROR, "app.core.exceptions")
    response = TestClient(app).get(path)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert response.json()["error"]["details"][0]["context"]["error_id"]
    assert len(caplog.records) == 1


def test_unknown_routes_use_the_envelope(app):
    client = TestClient(app)
    missing = client.get("/api/v1/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert missing.json()["error"]["details"] == []
    wrong = client.delete("/healthz")
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in wrong.headers


def test_limit_errors_carry_context(settings):
    doc = json.loads(notebook_bytes())
    markdown = {"cell_type": "markdown", "metadata": {}, "source": "m"}
    doc["cells"] += [{**markdown, "id": f"m{n}"} for n in range(3)]
    limited = settings.model_copy(
        update={"max_code_cells": 1, "max_notebook_cells": 2}
    )
    with pytest.raises(ServiceError) as exc:
        parse_notebook(json.dumps(doc).encode(), "x.ipynb", limited)
    assert exc.value.details == [
        {
            "location": ["body", "file", "cells"],
            "type": "notebook_cell_limit_exceeded",
            "context": {"limit": 2, "requested": 4},
        }
    ]
