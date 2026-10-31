"""Database errors: logged with detail; outages 503, the rest 500."""

import logging

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

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
    assert body["code"] == "INTERNAL_ERROR"
    detail = body["detail"]
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
    assert response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert response.json()["detail"]["error_id"]
    assert len(caplog.records) == 1
