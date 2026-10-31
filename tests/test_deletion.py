"""Script and batch deletion against real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import (
    ALICE,
    API,
    SCRIPTS,
    create_batch,
    metadata,
    token,
    upload_script,
)
from sqlalchemy import update
from test_scheduler import activation, config

from app.db.base import utcnow
from app.models import Batch, BatchRun, Script, SkippedWindow
from app.runtime.scheduler import Scheduler


@pytest.fixture
def scheduling(client):
    """Executor configured (runs and activation allowed), no dispatcher."""
    client.app.state.settings.executor_base_url = "http://executor.test"
    return Scheduler(client.app.state.sessions)


def delete(client, url, revision, key=None, owner="alice"):
    return client.delete(
        url,
        headers={
            **token(owner),
            "Idempotency-Key": key or str(uuid4()),
            "If-Match": f'"{revision}"',
        },
    )


def revision(client, url):
    return client.get(url, headers=token()).json()["revision"]


def manual_run(client, batch_id):
    response = client.post(
        f"{API}/{batch_id}/runs",
        headers={**token(), "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    return response.json()


def finish_runs(client):
    with client.app.state.sessions.begin() as session:
        session.execute(
            update(BatchRun).values(status="COMPLETED", completed_at=utcnow())
        )


def test_batch_delete_hides_it_and_stops_its_schedule(client, scheduling):
    batch = create_batch(client, config=config()).json()
    enabled = activation(client, batch["id"]).json()
    url = f"{API}/{batch['id']}"
    assert delete(client, url, 1).status_code == 409  # stale revision
    assert (
        delete(client, url, enabled["revision"], owner="bob").status_code
        == 404
    )

    response = delete(client, url, enabled["revision"], key="del")
    assert response.status_code == 204, response.text
    assert response.content == b""
    # Retried with the same key: same answer, not a 404.
    assert (
        delete(client, url, enabled["revision"], key="del").status_code == 204
    )
    assert delete(client, url, enabled["revision"] + 1).status_code == 404

    assert client.get(url, headers=token()).status_code == 404
    assert client.get(API, headers=token()).json()["items"] == []
    assert client.get(f"{url}/runs", headers=token()).status_code == 404
    assert activation(client, batch["id"], expected=3).status_code == 404

    with client.app.state.sessions() as session:
        row = session.get(Batch, UUID(batch["id"]))
        assert row is not None
        assert row.enabled is False and row.next_run_at is None
        assert row.deleted_at is not None
        assert (row.deleted_by_type, row.deleted_by) == ("USER", ALICE)
    # The scheduler never picks a deleted batch.
    assert scheduling.tick(now=utcnow().replace(year=2100)) is False


def test_overdue_occurrence_is_kept_as_deleted_window(client, scheduling):
    batch = create_batch(client, config=config()).json()
    enabled = activation(client, batch["id"]).json()
    with client.app.state.sessions.begin() as session:
        session.execute(
            update(Batch)
            .where(Batch.id == UUID(batch["id"]))
            .values(next_run_at=utcnow().replace(year=2000))
        )
    url = f"{API}/{batch['id']}"
    assert delete(client, url, enabled["revision"]).status_code == 204
    with client.app.state.sessions() as session:
        reasons = [w.reason for w in session.query(SkippedWindow)]
    assert reasons == ["DELETED"]


def test_batch_with_active_run_cannot_be_deleted(client, scheduling):
    batch = create_batch(client).json()
    run = manual_run(client, batch["id"])
    url = f"{API}/{batch['id']}"
    response = delete(client, url, batch["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BATCH_HAS_ACTIVE_RUNS"
    finish_runs(client)
    assert delete(client, url, batch["revision"]).status_code == 204
    # History stays in the database for audit.
    with client.app.state.sessions() as session:
        assert session.get(BatchRun, UUID(run["id"])) is not None


def test_script_delete_requires_no_live_batch(client, settings):
    batch = create_batch(client, key="b").json()
    script_url = f"{SCRIPTS}/{batch['script_id']}"
    script = client.get(script_url, headers=token()).json()

    response = delete(client, script_url, script["revision"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCRIPT_IN_USE"

    assert delete(client, f"{API}/{batch['id']}", 1).status_code == 204
    response = delete(client, script_url, script["revision"], key="s")
    assert response.status_code == 204, response.text
    assert (
        delete(client, script_url, script["revision"], key="s").status_code
        == 204
    )

    version_id = script["latest_version"]["id"]
    for url in (
        script_url,
        f"{script_url}/versions",
        f"{script_url}/versions/{version_id}",
        f"{script_url}/versions/{version_id}/content",
    ):
        assert client.get(url, headers=token()).status_code == 404
    assert client.get(SCRIPTS, headers=token()).json()["items"] == []
    again = client.post(
        API,
        headers={**token(), "Idempotency-Key": "new"},
        json={**metadata(), "script_id": script["id"]},
    )
    assert again.status_code == 404
    # Originals are retained (no automatic or user-triggered file removal).
    assert list(Path(settings.storage_root).rglob("original.ipynb"))
    with client.app.state.sessions() as session:
        row = session.get(Script, UUID(script["id"]))
        assert row is not None and row.deleted_by == ALICE


def test_script_with_active_run_cannot_be_deleted(client, scheduling):
    batch = create_batch(client, key="b").json()
    old_script = batch["script_id"]
    manual_run(client, batch["id"])
    other = upload_script(client, "other").json()
    moved = client.patch(
        f"{API}/{batch['id']}",
        headers={**token(), "Idempotency-Key": "move", "If-Match": '"1"'},
        json={"script_id": other["id"]},
    )
    assert moved.status_code == 200, moved.text

    url = f"{SCRIPTS}/{old_script}"
    response = delete(client, url, revision(client, url))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCRIPT_HAS_ACTIVE_RUNS"
    finish_runs(client)
    assert delete(client, url, revision(client, url)).status_code == 204


def pin_or_delete(client, attempt):
    """Pin a fresh script in a new batch while deleting it."""
    script = upload_script(client, f"race-{attempt}").json()
    with ThreadPoolExecutor(2) as pool:
        pinned = pool.submit(
            client.post,
            API,
            headers={**token(), "Idempotency-Key": f"pin-{attempt}"},
            json={**metadata(), "script_id": script["id"]},
        )
        removed = pool.submit(
            delete, client, f"{SCRIPTS}/{script['id']}", script["revision"]
        )
        return pinned.result().status_code, removed.result().status_code


def test_script_delete_and_batch_pin_serialize(client):
    for attempt in range(5):
        outcome = pin_or_delete(client, attempt)
        # Never both: a live batch must not point at a deleted script.
        assert outcome in {(201, 409), (404, 204)}, outcome
