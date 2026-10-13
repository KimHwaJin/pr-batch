"""Input, auth, and storage checks do not require a database."""

import json
import time
from uuid import uuid4

import pytest
from conftest import API, NOTEBOOKS, notebook_bytes, token
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.core.exceptions import ServiceError
from src.domain.notebook_parser import parse_notebook
from src.main import create_app
from src.schemas.schedule import Policy, Schedule
from src.storage.notebook_storage import NotebookStorage


@pytest.mark.parametrize("raw", [b"no-json", b"[]", b"{}", b"\xff"])
def test_invalid_notebook(raw, settings):
    with pytest.raises(ServiceError) as exc:
        parse_notebook(raw, "x.ipynb", settings)
    assert exc.value.status == 422


def test_code_only_blank_cells_and_source_lines(settings):
    doc = json.loads(notebook_bytes())
    doc["cells"][0]["source"] = ["x=1\n", "x"]
    doc["cells"].insert(
        0,
        {
            "id": "empty",
            "cell_type": "code",
            "metadata": {},
            "source": "  ",
            "outputs": [],
            "execution_count": None,
        },
    )
    notebook = parse_notebook(json.dumps(doc).encode(), "x.ipynb", settings)
    assert notebook.cells[0].original_index == 1
    assert notebook.cells[0].source == "x=1\nx"
    doc["cells"][0] = {
        "id": "markdown",
        "cell_type": "markdown",
        "metadata": {},
        "source": "skipped",
    }
    notebook = parse_notebook(json.dumps(doc).encode(), "x.ipynb", settings)
    assert [c.original_index for c in notebook.cells] == [1]
    assert notebook.cell_list[0] == {
        "index": 0,
        "type": "markdown",
        "executed": False,
    }


def test_code_cell_limit_ignores_skipped_cells(settings):
    doc = json.loads(notebook_bytes())
    markdown = {"cell_type": "markdown", "metadata": {}, "source": "m"}
    doc["cells"] += [{**markdown, "id": f"m{n}"} for n in range(5)]
    limited = settings.model_copy(update={"max_code_cells": 1})
    raw = json.dumps(doc).encode()
    assert len(parse_notebook(raw, "x.ipynb", limited).cells) == 1
    doc["cells"].append({**doc["cells"][0], "id": "second"})
    with pytest.raises(ServiceError, match="Cell limit"):
        parse_notebook(json.dumps(doc).encode(), "x.ipynb", limited)


def test_display_filename_is_sanitized(settings):
    raw = notebook_bytes()
    assert parse_notebook(raw, "../a/b\\c.ipynb", settings).filename == (
        "c.ipynb"
    )


def test_limits_empty_and_invalid_unicode(settings):
    for raw, filename in [
        (notebook_bytes(" "), "x.ipynb"),
        (notebook_bytes(), "x.py"),
    ]:
        with pytest.raises(ServiceError):
            parse_notebook(raw, filename, settings)
    tiny = settings.model_copy(update={"upload_max_bytes": 10})
    with pytest.raises(ServiceError) as exc:
        parse_notebook(notebook_bytes(), "x.ipynb", tiny)
    assert exc.value.status == 413
    raw = notebook_bytes().replace(b"print('hello')", b"\\ud800")
    with pytest.raises(ServiceError):
        parse_notebook(raw, "x.ipynb", settings)


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "ONCE", "at": "2026-10-12T08:00:00+09:00"},
        {"kind": "INTERVAL", "anchor": "2026-10-12T08:00:00Z", "seconds": 60},
        {
            "kind": "CALENDAR",
            "frequency": "WEEKLY",
            "at": "08:00",
            "weekdays": [0, 1, 2, 3, 4],
        },
        {
            "kind": "CALENDAR",
            "frequency": "MONTHLY",
            "at": "08:00",
            "last_day": True,
        },
        {"kind": "CRON", "expression": "*/10 8-17 * * 1-5"},
    ],
)
def test_supported_schedules(definition):
    config = Schedule(definition=definition)
    assert config.timezone == "Asia/Seoul"


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "ONCE", "at": "2026-10-12T08:00:00"},
        {"kind": "INTERVAL", "anchor": "2026-10-12T08:00:00Z", "seconds": 0},
        {"kind": "CALENDAR", "frequency": "WEEKLY", "at": "08:00"},
        {
            "kind": "CALENDAR",
            "frequency": "DAILY",
            "at": "08:00",
            "weekdays": [1],
        },
        {"kind": "CRON", "expression": "* * * * * *"},
        {"kind": "CRON", "expression": "0 0 L * *"},
        {"kind": "CRON", "expression": "0 0 31 2 *"},
        {"kind": "CRON", "expression": "90 * * * *"},
    ],
)
def test_invalid_schedules(definition):
    with pytest.raises(ValidationError):
        Schedule(definition=definition)


def test_policy_defaults_and_parallel_rule():
    assert Policy().overlap == "SKIP"
    assert Policy(overlap="PARALLEL", max_concurrent=3).max_concurrent == 3
    with pytest.raises(ValidationError):
        Policy(overlap="QUEUE", max_concurrent=2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": 1},
        {"aud": "other"},
        {"iss": "other"},
        {"iat": int(time.time()) + 1000},
        {"exp": int(time.time()) + 10000},
        {"sub": ""},
    ],
)
def test_invalid_gateway_identity(settings, overrides):
    with TestClient(create_app(settings)) as client:
        response = client.get(API, headers=token(**overrides))
    assert response.status_code == 401


def test_owner_header_is_not_authentication(settings):
    with TestClient(create_app(settings)) as client:
        assert (
            client.get(API, headers={"X-User-Id": "alice"}).status_code == 401
        )
        assert client.get(API, headers=token(scope="other")).status_code == 403
        assert (
            client.post(
                NOTEBOOKS,
                headers=token(),
                content=b"x" * (settings.upload_max_bytes + 65537),
            ).status_code
            == 413
        )


def test_storage_escape_and_symlink(settings, tmp_path):
    storage = NotebookStorage(settings.storage_root)
    with pytest.raises(ServiceError):
        storage.safe_path("../escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.storage_root / "link").symlink_to(outside)
    with pytest.raises(ServiceError):
        storage.safe_path("link/file")
    notebook = parse_notebook(notebook_bytes(), "x.ipynb", settings)
    path, _ = storage.save("../../alice", uuid4(), uuid4(), notebook)
    assert "alice" not in path
    assert (
        storage.read(path, notebook.sha256, len(notebook.raw)) == notebook.raw
    )


def test_missing_storage_root_is_not_created(settings, tmp_path):
    root = tmp_path / "unmounted"
    storage = NotebookStorage(root)
    notebook = parse_notebook(notebook_bytes(), "x.ipynb", settings)
    with pytest.raises(ServiceError, match="Mount storage"):
        storage.save("alice", uuid4(), uuid4(), notebook)
    assert not root.exists()


def test_chunked_request_size_limit(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            NOTEBOOKS,
            headers=token(),
            content=iter([b"x" * 65536] * 162),
        )
    assert response.status_code == 413
