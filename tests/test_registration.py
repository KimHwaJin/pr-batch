"""Real PostgreSQL API and concurrency regressions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conftest import API, create_batch, metadata, notebook_bytes, token
from sqlalchemy import create_engine, select, text

from pr_batch.models import NotebookVersion


def test_registration_round_trip_and_version_pin(client, settings):
    original = notebook_bytes("%matplotlib inline\nx = 3\nx", "old output")
    response = create_batch(client, raw=original)
    assert response.status_code == 201, response.text
    batch = response.json()
    assert batch["enabled"] is False
    assert response.headers["etag"] == '"1"'
    first_version = batch["selected_schedule"]["version_id"]
    batch_url = f"{API}/{batch['id']}"
    original_url = f"{batch_url}/versions/{first_version}/content"
    assert client.get(original_url, headers=token()).content == original
    paths = list(settings.storage_root.rglob("*.py"))
    assert len(paths) == 1
    assert paths[0].read_text() == "%matplotlib inline\nx = 3\nx"
    assert "old output" not in paths[0].read_text()

    version_response = client.post(
        f"{batch_url}/versions",
        headers={**token(), "Idempotency-Key": "v2"},
        files={"file": ("new-name.ipynb", notebook_bytes("x = 4"))},
    )
    assert version_response.status_code == 201, version_response.text
    v2 = version_response.json()
    assert v2["number"] == 2
    current = client.get(batch_url, headers=token()).json()
    assert current["latest_version_number"] == 2
    assert current["selected_schedule"]["version_id"] == first_version
    update = client.put(
        f"{batch_url}/schedule",
        headers={**token(), "Idempotency-Key": "select-v2", "If-Match": '"2"'},
        json={
            "version_id": v2["id"],
            "kernel_profile": "analytics",
            "schedule": metadata()["schedule"],
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["selected_schedule"]["version_id"] == v2["id"]
    assert update.json()["revision"] == 3
    assert client.get(original_url, headers=token()).content == original


def test_duplicate_upload_and_distinct_same_code(client):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}/versions"
    same = client.post(
        url,
        headers={**token(), "Idempotency-Key": "same"},
        files={"file": ("renamed.ipynb", notebook_bytes())},
    )
    assert same.status_code == 200
    changed_output = client.post(
        url,
        headers={**token(), "Idempotency-Key": "changed-output"},
        files={"file": ("analysis.ipynb", notebook_bytes(output="new"))},
    )
    assert changed_output.status_code == 201
    assert changed_output.json()["code_hash"] == same.json()["code_hash"]
    assert changed_output.json()["sha256"] != same.json()["sha256"]


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


def test_cross_user_access_and_version_selection(client):
    alice = create_batch(client).json()
    bob = create_batch(client, owner="bob").json()
    url = f"{API}/{alice['id']}"
    assert client.get(url, headers=token("bob")).status_code == 404
    assert (
        client.get(f"{url}/versions", headers=token("bob")).status_code == 404
    )
    version_id = alice["selected_schedule"]["version_id"]
    assert (
        client.get(
            f"{url}/versions/{version_id}/content", headers=token("bob")
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{url}/versions",
            headers={**token("bob"), "Idempotency-Key": "forbidden"},
            files={"file": ("x.ipynb", notebook_bytes("x=2"))},
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"{url}/schedule",
            headers={
                **token(),
                "Idempotency-Key": "cross-version",
                "If-Match": '"1"',
            },
            json={
                "version_id": bob["selected_schedule"]["version_id"],
                "kernel_profile": "python3",
                "schedule": metadata()["schedule"],
            },
        ).status_code
        == 404
    )


def test_concurrent_identical_create_is_one_resource(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: create_batch(client), range(4)))
    assert [r.status_code for r in responses] == [201] * 4
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(client.get(API, headers=token()).json()["items"]) == 1


def test_concurrent_versions_have_unique_numbers(client):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}/versions"

    def upload(index):
        return client.post(
            url,
            headers={**token(), "Idempotency-Key": f"upload-{index}"},
            files={"file": ("x.ipynb", notebook_bytes(f"x = {index}"))},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(upload, range(4)))
    assert [r.status_code for r in responses] == [201] * 4
    assert sorted(r.json()["number"] for r in responses) == [2, 3, 4, 5]
    page = client.get(url, params={"limit": 2}, headers=token()).json()
    assert [v["number"] for v in page["items"]] == [5, 4]
    second = client.get(
        url,
        params={"limit": 2, "cursor": page["next_cursor"]},
        headers=token(),
    ).json()
    assert [v["number"] for v in second["items"]] == [3, 2]


def test_same_version_with_different_concurrent_keys(client):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}/versions"

    def upload(index):
        return client.post(
            url,
            headers={**token(), "Idempotency-Key": f"duplicate-{index}"},
            files={"file": ("x.ipynb", notebook_bytes("x = 42"))},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(upload, range(4)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(client.get(url, headers=token()).json()["items"]) == 2


def test_registration_validation_and_readiness(client):
    assert client.get("/readyz").status_code == 200
    assert (
        create_batch(
            client, config=metadata(kernel_profile="not-registered")
        ).status_code
        == 422
    )
    assert (
        create_batch(
            client, config=metadata(owner_id="someone-else")
        ).status_code
        == 422
    )
    assert (
        create_batch(client, config=metadata(enabled=True)).status_code == 422
    )
    assert client.get(API, headers=token()).json()["items"] == []


def test_stale_update_and_exact_replay(client):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}"
    headers = {**token(), "Idempotency-Key": "rename", "If-Match": '"1"'}
    first = client.patch(url, headers=headers, json={"name": "renamed"})
    assert first.status_code == 200
    replay = client.patch(url, headers=headers, json={"name": "renamed"})
    assert replay.json() == first.json()
    stale = client.patch(
        url,
        headers={**headers, "Idempotency-Key": "stale"},
        json={"name": "stale"},
    )
    assert stale.status_code == 409
    assert client.get(url, headers=token()).json()["name"] == "renamed"


def test_pagination_and_invalid_cursor(client):
    for n in range(3):
        create_batch(client, key=f"batch-{n}")
    first = client.get(API, params={"limit": 2}, headers=token()).json()
    second = client.get(
        API,
        params={"limit": 2, "cursor": first["next_cursor"]},
        headers=token(),
    ).json()
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert (
        client.get(API, params={"cursor": "bad"}, headers=token()).status_code
        == 422
    )


def test_database_rollback_after_storage_failure(client, monkeypatch):
    storage = client.app.state.registration.storage

    def fail(*args, **kwargs):
        raise OSError("private path must not leak")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "save", fail)
        response = create_batch(client)
        assert response.status_code == 503
        assert "private path" not in response.text
        assert client.get(API, headers=token()).json()["items"] == []
    assert create_batch(client).status_code == 201


def test_original_integrity_and_activation_requires_configuration(
    client, settings
):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}"
    version = batch["selected_schedule"]["version_id"]
    original = next(settings.storage_root.rglob("original.ipynb"))
    original.write_bytes(b"corrupted")
    response = client.get(f"{url}/versions/{version}/content", headers=token())
    assert response.status_code == 503
    assert response.json()["code"] == "STORAGE_INTEGRITY_ERROR"
    assert (
        client.post(
            f"{url}/enable",
            headers={
                **token(),
                "Idempotency-Key": "enable",
                "If-Match": '"1"',
            },
        ).status_code
        == 503
    )


def test_immutable_schedule_history_and_cell_mapping(client, database_url):
    batch = create_batch(client).json()
    url = f"{API}/{batch['id']}/schedule"
    response = client.put(
        url,
        headers={**token(), "Idempotency-Key": "schedule", "If-Match": '"1"'},
        json={
            "version_id": batch["selected_schedule"]["version_id"],
            "kernel_profile": "python3",
            "schedule": {
                "definition": {
                    "kind": "INTERVAL",
                    "seconds": 600,
                    "anchor": "2026-10-12T00:00:00+09:00",
                }
            },
        },
    )
    assert response.status_code == 200
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM batch.schedule_revisions")
            )
            == 2
        )
        mapping = connection.scalar(select(NotebookVersion.cell_map))
        assert mapping is not None
        assert mapping[0]["sequence"] == mapping[0]["original_index"] == 0
        assert Path(mapping[0]["path"]).suffix == ".py"
    engine.dispose()
