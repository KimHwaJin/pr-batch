"""Input, auth, and storage checks do not require a database."""

import json
import time
from uuid import uuid4

import httpx2
import pytest
from conftest import API, SCRIPTS, notebook_bytes, token
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.exceptions import ServiceError
from app.domain.notebook_parser import parse_notebook
from app.domain.scripts import ScriptType
from app.integrations.executor import ExecutorClient
from app.main import create_app
from app.schemas.batch import RuntimeSpec
from app.schemas.schedule import ParallelOverlap, Policy, Schedule
from app.services.runtime_catalog import RuntimeCatalog, check_runtime
from app.storage.script_storage import ScriptStorage


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
            "weekdays": ["MON", "TUE", "WED", "THU", "FRI"],
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
            "weekdays": ["TUE"],
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
    assert Policy().overlap.mode == "SKIP"
    assert Policy().misfire.mode == "SKIP"
    parallel = Policy.model_validate(
        {"overlap": {"mode": "PARALLEL", "max_concurrent": 3}}
    )
    assert isinstance(parallel.overlap, ParallelOverlap)
    assert parallel.overlap.max_concurrent == 3
    with pytest.raises(ValidationError):
        Policy.model_validate(
            {"overlap": {"mode": "QUEUE", "max_concurrent": 2}}
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": 1},
        {"aud": "other"},
        {"iss": "other"},
        {"iat": int(time.time()) + 1000},
        {"exp": int(time.time()) + 10000},
        {"sub": ""},
        {"sub": "alice"},
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
                SCRIPTS,
                headers=token(),
                content=b"x" * (settings.upload_max_bytes + 65537),
            ).status_code
            == 413
        )


def test_storage_escape_and_symlink(settings, tmp_path):
    storage = ScriptStorage(settings.storage_root)
    with pytest.raises(ServiceError):
        storage.safe_path("../escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.storage_root / "link").symlink_to(outside)
    with pytest.raises(ServiceError):
        storage.safe_path("link/file")
    notebook = parse_notebook(notebook_bytes(), "x.ipynb", settings)
    with pytest.raises(ValueError):
        storage.save("../../alice", uuid4(), uuid4(), notebook)
    owner = uuid4()
    path, _ = storage.save(str(owner), uuid4(), uuid4(), notebook)
    assert path.startswith(f"batch/inputs/users/{owner}/scripts/")
    assert (
        storage.read(path, notebook.sha256, len(notebook.raw)) == notebook.raw
    )


def test_missing_storage_root_is_not_created(settings, tmp_path):
    root = tmp_path / "unmounted"
    storage = ScriptStorage(root)
    notebook = parse_notebook(notebook_bytes(), "x.ipynb", settings)
    with pytest.raises(ServiceError, match="Mount storage"):
        storage.save(str(uuid4()), uuid4(), uuid4(), notebook)
    assert not root.exists()


def test_chunked_request_size_limit(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            SCRIPTS,
            headers=token(),
            content=iter([b"x" * 65536] * 162),
        )
    assert response.status_code == 413


def test_runtime_catalog_reads_batch_targets_and_caches():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        profiles = ["default", "gpu"] if cursor else ["default"]
        target = {
            "runtime": {
                "type": "JUPYTER",
                "pool": "BATCH",
                "supported_profiles": profiles,
            },
            "state": {"status": "ACTIVE", "enabled": True},
        }
        return httpx2.Response(
            200,
            json={"items": [target], "next_cursor": None if cursor else "p2"},
        )

    executor = ExecutorClient(
        "http://executor.test", transport=httpx2.MockTransport(handle)
    )
    catalog = RuntimeCatalog(executor, ttl_seconds=60)
    assert [r.profile for r in catalog.available()] == ["default", "gpu"]
    assert catalog.available()
    assert len(calls) == 2
    assert "pool=BATCH" in calls[0] and "enabled=true" in calls[0]
    with pytest.raises(ServiceError) as exc:
        check_runtime(
            RuntimeSpec(type="JUPYTER", profile="missing"),
            ScriptType.NOTEBOOK,
            catalog.available(),
        )
    assert exc.value.code == "UNKNOWN_RUNTIME"
    broken = RuntimeCatalog(
        ExecutorClient(
            "http://executor.test",
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(503)
            ),
        ),
        ttl_seconds=60,
    )
    with pytest.raises(ServiceError) as exc:
        broken.available()
    assert exc.value.status == 503
    executor.close()


@pytest.mark.parametrize(
    "expression,numeric",
    [
        ("@daily", "0 0 * * *"),
        ("@hourly", "0 * * * *"),
        ("0 8 * * mon-fri", "0 8 * * 1-5"),
        ("0 0 1 JAN,JUL *", "0 0 1 1,7 *"),
    ],
)
def test_cron_macros_and_names(expression, numeric):
    from app.schemas.schedule import numeric_cron

    assert numeric_cron(expression) == numeric
    assert Schedule(definition={"kind": "CRON", "expression": expression})


def test_monthly_nth_weekday_and_interval_anchor_default():
    from datetime import datetime

    from app.domain.schedule_clock import SEOUL, ScheduleClock

    first_monday = Schedule(
        definition={
            "kind": "CALENDAR",
            "frequency": "MONTHLY",
            "at": "09:00",
            "nth_weekdays": [
                {"weekday": "MON", "nth": 1},
                {"weekday": "FRI", "nth": -1},
            ],
        }
    )
    start = datetime(2026, 11, 1, tzinfo=SEOUL)
    days = [
        d.astimezone(SEOUL).day
        for d in ScheduleClock(first_monday).preview(start, 4)
    ]
    # Nov 2026: first Monday 2nd, last Friday 27th; Dec: 7th, 25th.
    assert days == [2, 27, 7, 25]
    interval = Schedule(
        definition={"kind": "INTERVAL", "seconds": 3600},
        start_at=start,
    )
    following = ScheduleClock(interval).find(start)
    assert following is not None
    assert following.astimezone(SEOUL).hour == 1
