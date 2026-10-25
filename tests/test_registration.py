"""Real PostgreSQL notebook/batch API and concurrency regressions."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import (
    ALICE,
    API,
    BOB,
    SCRIPTS,
    create_batch,
    metadata,
    notebook_bytes,
    token,
    upload_script,
)
from sqlalchemy import create_engine, select, text

from app.domain.script_parser import parse_script
from app.domain.scripts import ScriptType
from app.models import ScriptVersion
from app.services import script_service
from app.services.runtime_catalog import UnconfiguredRuntimeCatalog
from app.services.script_service import ScriptService


def mixed_notebook() -> bytes:
    """Markdown and raw cells plus nbformat 4.5 cells without ids."""
    doc = json.loads(notebook_bytes("%matplotlib inline\nx = 3\nx", "old"))
    code = doc["cells"][0]
    del code["id"]
    doc["cells"] = [
        {"cell_type": "markdown", "metadata": {}, "source": "# 제목"},
        code,
        {"cell_type": "raw", "metadata": {}, "source": "raw"},
        {**code, "source": "  "},
    ]
    return json.dumps(doc, ensure_ascii=False).encode()


def versions_url(notebook):
    return f"{SCRIPTS}/{notebook['id']}/versions"


def add_version(client, notebook, key, raw, owner="alice"):
    return client.post(
        versions_url(notebook),
        headers={**token(owner), "Idempotency-Key": key},
        files={"file": ("new-name.ipynb", raw)},
    )


def patch_batch(client, batch, key, body, expected=None, owner="alice"):
    return client.patch(
        f"{API}/{batch['id']}",
        headers={
            **token(owner),
            "Idempotency-Key": key,
            "If-Match": f'"{expected or batch["revision"]}"',
        },
        json=body,
    )


def test_notebook_upload_keeps_original_and_skips_non_code(client, settings):
    original = mixed_notebook()
    response = upload_script(client, raw=original, filename="daily.ipynb")
    assert response.status_code == 201, response.text
    notebook = response.json()
    assert response.headers["location"] == f"{SCRIPTS}/{notebook['id']}"
    assert notebook["name"] == "daily"
    latest = notebook["latest_version"]
    assert latest["filename"] == "daily.ipynb"
    assert latest["cells"] == {"total": 4, "executable": 1, "skipped": 3}
    detail_url = f"{versions_url(notebook)}/{latest['id']}"
    detail = client.get(detail_url, headers=token()).json()
    assert [c["type"] for c in detail["cell_list"]] == [
        "markdown",
        "code",
        "raw",
        "code",
    ]
    assert [c["executed"] for c in detail["cell_list"]] == [
        False,
        True,
        False,
        False,
    ]
    assert detail["cell_list"][1]["sequence"] == 0
    content = client.get(f"{detail_url}/content", headers=token()).content
    assert content == original
    paths = list(settings.storage_root.rglob("*.py"))
    assert len(paths) == 1
    assert paths[0].read_text() == "%matplotlib inline\nx = 3\nx"
    assert "/scripts/" in paths[0].as_posix()


def test_named_upload_rename_and_versions(client):
    notebook = upload_script(client, name="생산 분석").json()
    assert notebook["name"] == "생산 분석"
    url = f"{SCRIPTS}/{notebook['id']}"
    same = add_version(client, notebook, "same", notebook_bytes())
    assert same.status_code == 200
    changed = add_version(client, notebook, "out", notebook_bytes(output="x"))
    assert changed.status_code == 201
    assert changed.json()["version_number"] == 2
    assert changed.json()["code_hash"] == same.json()["code_hash"]
    assert changed.json()["sha256"] != same.json()["sha256"]
    current = client.get(url, headers=token())
    assert current.headers["etag"] == '"2"'
    assert current.json()["latest_version"]["version_number"] == 2
    renamed = client.patch(
        url,
        headers={**token(), "Idempotency-Key": "rename", "If-Match": '"2"'},
        json={"name": "renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["revision"] == 3
    stale = client.patch(
        url,
        headers={**token(), "Idempotency-Key": "stale", "If-Match": '"2"'},
        json={"name": "stale"},
    )
    assert stale.status_code == 409


def test_batch_pins_version_until_changed(client):
    batch = create_batch(client).json()
    assert batch["enabled"] is False
    assert batch["script_version_number"] == 1
    assert batch["policy"]["overlap"]["mode"] == "SKIP"
    notebook = {"id": batch["script_id"]}
    v2 = add_version(client, notebook, "v2", notebook_bytes("x = 4")).json()
    current = client.get(f"{API}/{batch['id']}", headers=token()).json()
    assert current["script_version_id"] == batch["script_version_id"]
    pinned = patch_batch(client, batch, "pin", {"script_version_number": None})
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["script_version_id"] == v2["id"]
    assert pinned.json()["config_number"] == 2
    back = patch_batch(
        client,
        pinned.json(),
        "back",
        {"script_version_number": 1},
    )
    assert back.json()["script_version_number"] == 1
    explicit = create_batch(
        client,
        key="explicit",
        config=metadata(script_id=batch["script_id"], script_version_number=2),
    )
    assert explicit.status_code == 201
    assert explicit.json()["script_version_number"] == 2


def test_one_notebook_in_many_batches_and_filter(client):
    first = create_batch(client).json()
    second = create_batch(
        client,
        key="second",
        config=metadata(name="other", script_id=first["script_id"]),
    ).json()
    unrelated = create_batch(client, key="unrelated").json()
    assert second["script_id"] == first["script_id"]
    listed = client.get(
        API, headers=token(), params={"script_id": first["script_id"]}
    ).json()["items"]
    assert {b["id"] for b in listed} == {first["id"], second["id"]}
    moved = patch_batch(
        client, unrelated, "move", {"script_id": first["script_id"]}
    )
    assert moved.json()["script_id"] == first["script_id"]


def test_idempotency_replay_conflict_and_owner_scope(client):
    first = create_batch(client)
    replay = create_batch(client)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    conflict = create_batch(client, config=metadata(name="changed"))
    assert conflict.status_code == 409
    other = create_batch(client, owner="bob")
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]
    assert len(client.get(API, headers=token()).json()["items"]) == 1
    assert len(client.get(SCRIPTS, headers=token()).json()["items"]) == 1


def test_cross_user_access(client):
    alice = create_batch(client).json()
    bob = create_batch(client, owner="bob").json()
    notebook_url = f"{SCRIPTS}/{alice['script_id']}"
    version = alice["script_version_id"]
    for url in [
        f"{API}/{alice['id']}",
        notebook_url,
        f"{notebook_url}/versions",
        f"{notebook_url}/versions/{version}",
        f"{notebook_url}/versions/{version}/content",
    ]:
        assert client.get(url, headers=token("bob")).status_code == 404
    forbidden = add_version(
        client,
        {"id": alice["script_id"]},
        "forbidden",
        notebook_bytes("x=2"),
        owner="bob",
    )
    assert forbidden.status_code == 404
    borrowed = create_batch(
        client,
        key="borrow",
        owner="bob",
        config=metadata(script_id=alice["script_id"]),
    )
    assert borrowed.status_code == 404
    foreign_script = patch_batch(
        client, alice, "foreign", {"script_id": bob["script_id"]}
    )
    assert foreign_script.status_code == 404
    missing_version = patch_batch(
        client, alice, "missing", {"script_version_number": 99}
    )
    assert missing_version.status_code == 404


def test_concurrent_identical_create_is_one_resource(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: upload_script(client), range(4)))
    assert [r.status_code for r in responses] == [201] * 4
    assert len({r.json()["id"] for r in responses}) == 1
    script_id = responses[0].json()["id"]
    body = metadata(script_id=script_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: create_batch(client, config=body), range(4))
        )
    assert [r.status_code for r in responses] == [201] * 4
    assert len({r.json()["id"] for r in responses}) == 1


def test_concurrent_versions_have_unique_numbers(client):
    notebook = upload_script(client).json()

    def upload(index):
        return add_version(
            client, notebook, f"upload-{index}", notebook_bytes(f"x={index}")
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(upload, range(4)))
    assert [r.status_code for r in responses] == [201] * 4
    assert sorted(r.json()["version_number"] for r in responses) == [
        2,
        3,
        4,
        5,
    ]
    url = versions_url(notebook)
    page = client.get(url, params={"limit": 2}, headers=token()).json()
    assert [v["version_number"] for v in page["items"]] == [5, 4]
    second = client.get(
        url,
        params={"limit": 2, "cursor": page["next_cursor"]},
        headers=token(),
    ).json()
    assert [v["version_number"] for v in second["items"]] == [3, 2]


def test_same_version_with_different_concurrent_keys(client):
    notebook = upload_script(client).json()

    def upload(index):
        return add_version(
            client, notebook, f"duplicate-{index}", notebook_bytes("x = 42")
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(upload, range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    url = versions_url(notebook)
    assert len(client.get(url, headers=token()).json()["items"]) == 2


def test_batch_validation_reports_fields(client):
    assert client.get("/readyz").status_code == 200
    script_id = upload_script(client).json()["id"]

    def register(key, **updates):
        return create_batch(
            client,
            key=key,
            config=metadata(**{"script_id": script_id, **updates}),
        )

    unknown = register(
        "kernel", runtime={"type": "JUPYTER", "profile": "not-registered"}
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "UNKNOWN_RUNTIME"
    for key, updates in [
        ("owner", {"owner_id": "someone-else"}),
        ("enabled", {"enabled": True}),
    ]:
        response = register(key, **updates)
        assert response.status_code == 422
        assert response.json()["errors"][0]["type"] == "extra_forbidden"
    cron = register(
        "cron",
        schedule={"definition": {"kind": "CRON", "expression": "0 0 L * *"}},
    )
    assert cron.status_code == 422
    error = cron.json()["errors"][0]
    assert error["loc"][:3] == ["body", "schedule", "definition"]
    assert "Cron" in error["msg"]
    policy = register(
        "policy",
        policy={"overlap": {"mode": "SKIP", "max_concurrent": 2}},
    )
    assert policy.status_code == 422
    assert policy.json()["errors"][0]["type"] == "extra_forbidden"
    queue = register(
        "queue",
        policy={"overlap": {"mode": "QUEUE"}, "misfire": {"mode": "LATEST"}},
    )
    assert queue.status_code == 201
    assert queue.json()["policy"] == {
        "overlap": {"mode": "QUEUE", "max_queued": 100},
        "misfire": {"mode": "LATEST", "grace_seconds": 300},
    }
    missing = register("missing", script_id=str(uuid4()))
    assert missing.status_code == 404
    # Only the valid QUEUE/LATEST registration above was stored.
    assert len(client.get(API, headers=token()).json()["items"]) == 1


def test_patch_rules_and_policy_merge(client):
    batch = create_batch(client).json()
    empty = patch_batch(client, batch, "empty", {})
    assert empty.status_code == 422
    nulled = patch_batch(client, batch, "null", {"name": None})
    assert nulled.status_code == 422
    renamed = patch_batch(client, batch, "rename", {"name": "renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["config_number"] == 1
    replay = patch_batch(client, batch, "rename", {"name": "renamed"})
    assert replay.json() == renamed.json()
    stale = patch_batch(client, batch, "stale", {"name": "stale"})
    assert stale.status_code == 409
    current = renamed.json()
    merged = patch_batch(
        client,
        current,
        "policy",
        {"policy": {"overlap": {"mode": "PARALLEL", "max_concurrent": 3}}},
    )
    assert merged.status_code == 200
    assert merged.json()["policy"]["overlap"]["max_queued"] == 100
    assert merged.json()["policy"]["overlap"]["max_concurrent"] == 3
    invalid = patch_batch(
        client,
        merged.json(),
        "bad-policy",
        {"policy": {"overlap": {"max_concurrent": 1, "mode": "QUEUE"}}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_POLICY"
    assert client.get(f"{API}/{batch['id']}", headers=token()).json()[
        "name"
    ] == ("renamed")


def test_pagination_and_invalid_cursor(client):
    for n in range(3):
        create_batch(client, key=f"batch-{n}")
    for url in [API, SCRIPTS]:
        first = client.get(url, params={"limit": 2}, headers=token()).json()
        second = client.get(
            url,
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=token(),
        ).json()
        assert len(first["items"]) == 2
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None
        response = client.get(url, params={"cursor": "bad"}, headers=token())
        assert response.status_code == 422


def test_database_rollback_after_storage_failure(client, monkeypatch):
    storage = client.app.state.storage

    def fail(*args, **kwargs):
        raise OSError("private path must not leak")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "save", fail)
        response = upload_script(client)
        assert response.status_code == 503
        assert "private path" not in response.text
        assert client.get(SCRIPTS, headers=token()).json()["items"] == []
    assert upload_script(client).status_code == 201


def test_original_integrity_and_activation_requires_configuration(
    client, settings
):
    batch = create_batch(client).json()
    notebook_url = f"{SCRIPTS}/{batch['script_id']}"
    version = batch["script_version_id"]
    original = next(settings.storage_root.rglob("original.ipynb"))
    original.write_bytes(b"corrupted")
    response = client.get(
        f"{notebook_url}/versions/{version}/content", headers=token()
    )
    assert response.status_code == 503
    assert response.json()["code"] == "STORAGE_INTEGRITY_ERROR"
    # Production wires this catalog when no Executor is configured.
    client.app.state.runtime_catalog = UnconfiguredRuntimeCatalog()
    assert (
        client.post(
            f"{API}/{batch['id']}/enable",
            headers={
                **token(),
                "Idempotency-Key": "enable",
                "If-Match": '"1"',
            },
        ).status_code
        == 503
    )


def test_immutable_config_history_and_cell_mapping(client, database_url):
    batch = create_batch(client).json()
    response = patch_batch(
        client,
        batch,
        "schedule",
        {
            "schedule": {
                "definition": {
                    "kind": "INTERVAL",
                    "seconds": 600,
                    "anchor": "2026-10-12T00:00:00+09:00",
                }
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["definition"]["kind"] == "INTERVAL"
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM batch.batch_configs"))
            == 2
        )
        mapping = connection.scalar(select(ScriptVersion.cell_map))
        assert mapping is not None
        assert mapping[0]["sequence"] == mapping[0]["original_index"] == 0
        assert Path(mapping[0]["path"]).suffix == ".py"
    engine.dispose()


def test_script_type_is_required_and_fixed(client):
    missing = client.post(
        SCRIPTS,
        headers={**token(), "Idempotency-Key": "no-type"},
        files={"file": ("a.ipynb", notebook_bytes())},
    )
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["loc"] == ["body", "type"]
    python = upload_script(
        client,
        key="python",
        raw=b"print(1)\n",
        filename="job.py",
        script_type="PYTHON",
    )
    assert python.status_code == 422
    assert python.json()["code"] == "UNSUPPORTED_SCRIPT_TYPE"
    unknown = upload_script(client, key="sql", script_type="SQL")
    assert unknown.status_code == 422
    script = upload_script(client).json()
    assert script["type"] == "NOTEBOOK"
    version = script["latest_version"]["id"]
    content = client.get(
        f"{SCRIPTS}/{script['id']}/versions/{version}/content",
        headers=token(),
    )
    assert content.headers["content-type"] == "application/x-ipynb+json"
    assert content.headers["content-disposition"].endswith('.ipynb"')


def test_resources_report_created_and_updated_by(client):
    script = upload_script(client).json()
    for record in (script, script["latest_version"]):
        assert (record["created_by_type"], record["created_by"]) == (
            "USER",
            ALICE,
        )
        assert record["updated_by"] == ALICE
    batch = create_batch(client, owner="bob").json()
    assert batch["created_by"] == batch["updated_by"] == BOB
    renamed = patch_batch(
        client, batch, "rename", {"name": "renamed"}, owner="bob"
    ).json()
    assert renamed["updated_at"] > batch["updated_at"]
    assert renamed["created_at"] == batch["created_at"]


def test_runtime_choices_and_unreachable_catalog(client):
    listed = client.get("/api/v1/runtimes", headers=token())
    assert listed.json()["items"] == [
        {"type": "JUPYTER", "profile": "python3"},
        {"type": "JUPYTER", "profile": "analytics"},
    ]
    batch = create_batch(client).json()
    assert batch["runtime"] == {"type": "JUPYTER", "profile": "python3"}
    client.app.state.runtime_catalog = UnconfiguredRuntimeCatalog()
    rejected = create_batch(client, key="no-executor")
    assert rejected.status_code == 503
    renamed = patch_batch(client, batch, "rename", {"name": "still ok"})
    assert renamed.status_code == 200
    moved = patch_batch(
        client,
        renamed.json(),
        "runtime",
        {"runtime": {"type": "JUPYTER", "profile": "analytics"}},
    )
    assert moved.status_code == 503


@pytest.mark.parametrize(
    "tenant",
    ["dlq", "work", "Team-a", "ab", "a-", "-ab", "1ab", "a_b", "a" * 65],
)
def test_tenant_id_follows_executor_rule(client, tenant):
    response = create_batch(client, config=metadata(tenant_id=tenant))
    assert response.status_code == 422, tenant


def test_tenant_id_is_optional_and_set_once(client):
    plain = create_batch(client).json()
    assert plain["tenant_id"] is None
    batch = create_batch(
        client, key="tenant", config=metadata(tenant_id="pr-agentic")
    ).json()
    assert batch["tenant_id"] == "pr-agentic"
    fetched = client.get(f"{API}/{batch['id']}", headers=token()).json()
    assert fetched["tenant_id"] == "pr-agentic"
    same = patch_batch(client, batch, "same", {"tenant_id": "pr-agentic"})
    assert same.status_code == 200
    # Resending the current value is not a change: no new config/revision.
    assert same.json()["config_number"] == batch["config_number"]
    assert same.json()["revision"] == batch["revision"]
    assert same.json()["updated_at"] == batch["updated_at"]
    changed = patch_batch(
        client, same.json(), "change", {"tenant_id": "other-service"}
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "TENANT_IMMUTABLE"
    cleared = patch_batch(client, same.json(), "clear", {"tenant_id": None})
    assert cleared.status_code == 422
    backfilled = patch_batch(client, plain, "fill", {"tenant_id": "team-a"})
    assert backfilled.status_code == 200, backfilled.text
    assert backfilled.json()["tenant_id"] == "team-a"
    assert backfilled.json()["config_number"] == plain["config_number"] + 1
    renamed = patch_batch(client, backfilled.json(), "rename", {"name": "x"})
    assert renamed.json()["tenant_id"] == "team-a"


def test_replay_is_answered_while_executor_is_down(client):
    created = create_batch(client)
    client.app.state.runtime_catalog = UnconfiguredRuntimeCatalog()
    replay = create_batch(client)
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert create_batch(client, key="new").status_code == 503


def test_upload_files_are_removed_when_the_write_rolls_back(
    client, settings, monkeypatch
):
    state = client.app.state
    service = ScriptService(state.sessions, state.storage)
    parsed = parse_script(
        ScriptType.NOTEBOOK, notebook_bytes(), "a.ipynb", settings
    )

    def broken(*args):
        raise RuntimeError("after the files were written")

    monkeypatch.setattr(script_service, "script_view", broken)
    with pytest.raises(RuntimeError):
        service.create_script(ALICE, "k", None, parsed)
    assert not any(settings.storage_root.rglob("original.ipynb"))
