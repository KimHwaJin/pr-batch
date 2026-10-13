"""Real PostgreSQL schedule admission, activation and recovery regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import (
    API,
    SCRIPTS,
    create_batch,
    metadata,
    notebook_bytes,
    token,
)
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from test_runs import FakeExecutor

from app.db.base import SCHEDULER, utcnow
from app.integrations.executor import ExecutorClient
from app.models import Batch, BatchRun, Task
from app.runtime.dispatcher import Dispatcher
from app.runtime.scheduler import Scheduler
from app.services.common import current_config
from app.services.run_service import admit_run
from app.tasks.handlers import Clients


@pytest.fixture
def scheduling(client):
    client.app.state.settings.executor_base_url = "http://executor.test"
    return Scheduler(client.app.state.sessions)


RENAMED = {"misfire_policy": "misfire", "overlap_policy": "overlap"}


def config(**options):
    """Interval batch body; window options go to schedule, others policy."""
    window = {
        k: options.pop(k) for k in ("start_at", "end_at") if k in options
    }
    policy = {"overlap": "QUEUE"}
    policy.update({RENAMED.get(k, k): v for k, v in options.items()})
    return metadata(
        schedule={
            "definition": {
                "kind": "INTERVAL",
                "anchor": utcnow().isoformat(),
                "seconds": 60,
            },
            **window,
        },
        policy=policy,
    )


def activation(
    client, batch_id, enabled=True, expected=1, key=None, owner="alice"
):
    return client.post(
        f"{API}/{batch_id}/{'enable' if enabled else 'disable'}",
        headers={
            **token(owner),
            "Idempotency-Key": key or str(uuid4()),
            "If-Match": f'"{expected}"',
        },
    )


def enabled_batch(client, **options):
    batch = create_batch(client, config=config(**options)).json()
    response = activation(client, batch["id"])
    assert response.status_code == 200, response.text
    return response.json()


def first_at(batch):
    return datetime.fromisoformat(batch["next_run_at"])


def get_batch(client, batch):
    return client.get(f"{API}/{batch['id']}", headers=token()).json()


def runs(client, batch):
    return client.get(
        f"{API}/{batch['id']}/runs", headers=token(), params={"limit": 100}
    ).json()["items"][::-1]


def skipped(client, batch):
    return client.get(
        f"{API}/{batch['id']}/skipped-windows", headers=token()
    ).json()["items"]


def test_activation_etag_replay_noop_and_owner(client, scheduling):
    batch = create_batch(client, config=config()).json()
    assert batch["enabled"] is False
    assert batch["next_run_at"] is None
    assert len(batch["preview_runs"]) == 5
    assert activation(client, batch["id"], owner="bob").status_code == 404
    response = activation(client, batch["id"], key="enable")
    assert response.status_code == 200
    enabled = response.json()
    assert response.headers["etag"] == '"2"'
    assert enabled["enabled"] is True
    assert enabled["next_run_at"] is not None
    assert first_at(enabled) > utcnow()
    assert activation(client, batch["id"], key="enable").json() == enabled
    assert activation(client, batch["id"], expected=1).status_code == 409
    assert activation(client, batch["id"], expected=2).json()["revision"] == 2
    assert (
        activation(client, batch["id"], False, 1, "enable").status_code == 409
    )
    disabled = activation(client, batch["id"], False, 2).json()
    assert disabled["next_run_at"] is None
    assert disabled["revision"] == 3
    assert scheduling.tick(now=first_at(enabled)) is False


def test_elapsed_once_and_expired_window_cannot_enable(client, scheduling):
    for index, schedule in enumerate(
        [
            {
                "definition": {
                    "kind": "ONCE",
                    "at": (utcnow() - timedelta(days=1)).isoformat(),
                }
            },
            {
                **config()["schedule"],
                "end_at": (utcnow() - timedelta(seconds=1)).isoformat(),
            },
        ]
    ):
        batch = create_batch(
            client, key=f"batch{index}", config=metadata(schedule=schedule)
        ).json()
        response = activation(client, batch["id"])
        assert response.status_code == 422
        assert response.json()["code"] == "NO_FUTURE_OCCURRENCE"
        assert get_batch(client, batch)["enabled"] is False


def test_once_exhaustion_and_duplicate_scheduler_ticks(client, scheduling):
    at = utcnow() + timedelta(minutes=5)
    batch = create_batch(
        client,
        config=metadata(
            schedule={"definition": {"kind": "ONCE", "at": at.isoformat()}}
        ),
    ).json()
    enabled = activation(client, batch["id"]).json()
    assert scheduling.tick(now=at - timedelta(microseconds=1)) is False
    assert scheduling.tick(now=at) is True
    assert scheduling.tick(now=at) is False
    current = get_batch(client, enabled)
    assert current["next_run_at"] is None
    assert current["enabled"] is True
    assert current["revision"] == 2
    history = runs(client, batch)
    assert len(history) == 1
    assert history[0]["trigger"] == "SCHEDULED"
    assert datetime.fromisoformat(history[0]["scheduled_for"]) == at


@pytest.mark.parametrize(
    "policy,offsets,omitted",
    [
        ("SKIP", [10], 10),
        ("LATEST", [9, 10], 9),
        ("CATCH_UP", [7, 8, 9, 10], 7),
    ],
)
def test_misfire_policies_preserve_exact_history(
    client,
    scheduling,
    policy,
    offsets,
    omitted,
):
    batch = enabled_batch(client, misfire_policy=policy, catch_up_max_runs=3)
    first = first_at(batch)
    now = first + timedelta(minutes=10, seconds=30)
    assert scheduling.tick(now=now)
    history = runs(client, batch)
    assert [
        datetime.fromisoformat(run["scheduled_for"]) for run in history
    ] == [first + timedelta(minutes=n) for n in offsets]
    windows = skipped(client, batch)
    assert len(windows) == 1
    assert windows[0]["occurrences"] == omitted
    assert windows[0]["reason"] == f"MISFIRE_{policy}"
    assert first_at(get_batch(client, batch)) == first + timedelta(minutes=11)
    assert Scheduler(scheduling.sessions).tick(now=now) is False


def test_catch_up_age_and_exact_grace_boundary(client, scheduling):
    batch = enabled_batch(
        client,
        misfire_policy="CATCH_UP",
        catch_up_max_runs=10,
        catch_up_max_age_seconds=120,
    )
    first = first_at(batch)
    assert scheduling.tick(now=first + timedelta(minutes=10))
    history = runs(client, batch)
    # Minute 8 is recovered by age, minute 9 is exactly on the grace boundary.
    assert [datetime.fromisoformat(r["scheduled_for"]) for r in history] == [
        first + timedelta(minutes=n) for n in [8, 9, 10]
    ]
    assert skipped(client, batch)[0]["occurrences"] == 8


def test_many_missed_occurrences_are_one_window(client, scheduling):
    batch = enabled_batch(client, misfire_policy="SKIP")
    first = first_at(batch)
    now = first + timedelta(days=365, seconds=30)
    assert scheduling.tick(now=now)
    assert len(runs(client, batch)) == 1
    assert skipped(client, batch)[0]["occurrences"] == 365 * 24 * 60


def test_timely_admission_is_bounded_and_resumes(client, scheduling):
    batch = enabled_batch(client, misfire_grace_seconds=86400, max_pending=300)
    first = first_at(batch)
    now = first + timedelta(minutes=249)
    assert scheduling.tick(now=now)
    assert first_at(get_batch(client, batch)) == first + timedelta(minutes=100)
    assert scheduling.tick(now=now)
    assert first_at(get_batch(client, batch)) == first + timedelta(minutes=200)
    assert scheduling.tick(now=now)
    assert first_at(get_batch(client, batch)) == first + timedelta(minutes=250)
    assert not scheduling.tick(now=now)
    with scheduling.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(BatchRun)) == 250
        )


@pytest.mark.parametrize(
    "policy,maximum,expected",
    [
        ("SKIP", 1, ["QUEUED", "SKIPPED", "SKIPPED", "SKIPPED"]),
        ("QUEUE", 1, ["QUEUED", "WAITING", "SKIPPED", "SKIPPED"]),
        ("PARALLEL", 2, ["QUEUED", "QUEUED", "WAITING", "SKIPPED"]),
    ],
)
def test_scheduled_admission_uses_existing_overlap_policy(
    client,
    scheduling,
    policy,
    maximum,
    expected,
):
    batch = enabled_batch(
        client,
        overlap_policy=policy,
        max_concurrent=maximum,
        max_pending=1,
        misfire_grace_seconds=600,
    )
    assert scheduling.tick(now=first_at(batch) + timedelta(minutes=3))
    history = runs(client, batch)
    assert [r["status"] for r in history] == expected
    assert history[-1]["skip_reason"] == (
        "OVERLAP" if policy == "SKIP" else "SKIPPED_CAPACITY"
    )


def test_multiple_scheduler_replicas_admit_once(client, scheduling):
    batch = enabled_batch(client)
    now = first_at(batch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: Scheduler(scheduling.sessions).tick(now=now),
                range(4),
            )
        )
    assert results.count(True) == 1
    assert len(runs(client, batch)) == 1


def test_admission_and_pointer_roll_back_together(client, scheduling):
    batch = enabled_batch(client, misfire_policy="LATEST")
    now = first_at(batch) + timedelta(minutes=10)

    def fail(*args):
        raise RuntimeError("simulated process failure before commit")

    event.listen(Task, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            scheduling.tick(now=now)
    finally:
        event.remove(Task, "before_insert", fail)
    assert get_batch(client, batch)["next_run_at"] == batch["next_run_at"]
    assert runs(client, batch) == []
    assert skipped(client, batch) == []
    assert Scheduler(scheduling.sessions).tick(now=now)
    assert len(runs(client, batch)) == 3


def test_occurrence_unique_constraint_protects_against_duplicate_admission(
    client,
    scheduling,
):
    batch = enabled_batch(client)
    now = first_at(batch)
    scheduling.tick(now=now)
    with pytest.raises(IntegrityError), scheduling.sessions.begin() as session:
        row = session.get(Batch, UUID(batch["id"]))
        admit_run(session, row, current_config(session, row), SCHEDULER, now)
    assert len(runs(client, batch)) == 1


def test_disable_preserves_accepted_runs_and_resume_starts_in_future(
    client,
    scheduling,
):
    batch = enabled_batch(client, misfire_policy="CATCH_UP")
    original_next = first_at(batch)
    with scheduling.sessions.begin() as session:
        session.get(Batch, UUID(batch["id"])).next_run_at = (
            original_next - timedelta(hours=1)
        )
    disabled = activation(client, batch["id"], False, 2).json()
    assert disabled["enabled"] is False
    assert skipped(client, batch)[0]["reason"] == "DISABLED"
    assert runs(client, batch) == []
    resumed = activation(client, batch["id"], True, 3).json()
    assert first_at(resumed) > utcnow()
    assert first_at(resumed) == original_next
    assert scheduling.tick(now=first_at(resumed))
    history = runs(client, batch)
    response = activation(client, batch["id"], False, 4)
    assert response.status_code == 200
    assert runs(client, batch) == history


def test_active_schedule_change_preserves_old_run_snapshot(client, scheduling):
    batch = enabled_batch(client)
    scheduling.tick(now=first_at(batch))
    old_run = runs(client, batch)[0]
    version = client.post(
        f"{SCRIPTS}/{batch['script_id']}/versions",
        headers={**token(), "Idempotency-Key": "v2"},
        files={"file": ("v2.ipynb", notebook_bytes("x = 42"))},
    ).json()
    replacement = {
        "script_version_number": version["version_number"],
        "runtime": {"type": "JUPYTER", "profile": "analytics"},
        "schedule": config()["schedule"],
    }
    response = client.patch(
        f"{API}/{batch['id']}",
        headers={**token(), "Idempotency-Key": "replace", "If-Match": '"2"'},
        json=replacement,
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["enabled"] is True
    assert changed["config_number"] == 2
    assert first_at(changed) > utcnow()
    assert runs(client, batch)[0] == old_run
    assert scheduling.tick(now=first_at(changed))
    latest = runs(client, batch)[-1]
    assert latest["script_version_id"] == version["id"]
    assert latest["runtime"]["profile"] == "analytics"
    assert latest["config_number"] == 2


def test_replaced_overdue_schedule_is_audited(client, scheduling):
    batch = enabled_batch(client)
    with scheduling.sessions.begin() as session:
        row = session.get(Batch, UUID(batch["id"]))
        row.next_run_at = first_at(batch) - timedelta(hours=1)
    response = client.patch(
        f"{API}/{batch['id']}",
        headers={**token(), "Idempotency-Key": "replace", "If-Match": '"2"'},
        json={"schedule": config()["schedule"]},
    )
    assert response.status_code == 200
    assert skipped(client, batch)[0]["reason"] == "SCHEDULE_REPLACED"
    assert skipped(client, batch)[0]["config_number"] == 1
    assert runs(client, batch) == []


def test_skipped_windows_are_owner_scoped_and_paginated(client, scheduling):
    batch = enabled_batch(client)
    scheduling.tick(now=first_at(batch) + timedelta(days=1))
    scheduling.tick(now=first_at(batch) + timedelta(days=2))
    url = f"{API}/{batch['id']}/skipped-windows"
    assert client.get(url, headers=token("bob")).status_code == 404
    first = client.get(url, headers=token(), params={"limit": 1}).json()
    second = client.get(
        url,
        headers=token(),
        params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()
    assert len(first["items"]) == len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert (
        client.get(
            url, headers=token(), params={"cursor": str(uuid4())}
        ).status_code
        == 422
    )


def test_scheduler_to_executor_worker_contract(client, scheduling):
    batch = enabled_batch(client)
    assert scheduling.tick(now=first_at(batch))
    fake = FakeExecutor()
    executor = ExecutorClient(
        "http://executor.test", transport=httpx2.MockTransport(fake.handle)
    )
    try:
        worker = Dispatcher(
            scheduling.sessions, Clients(executor, None, 5, 60)
        )
        assert worker.tick()
        history = runs(client, batch)
        assert history[0]["status"] == "RUNNING"
        assert fake.payloads[0]["trigger"]["type"] == "BATCH"
        assert fake.payloads[0]["context"]["task_id"] == history[0]["id"]
        assert fake.payloads[0]["runtime"]["profile"] == "python3"
    finally:
        executor.close()


def test_schedule_is_optional_but_required_to_enable(client, scheduling):
    batch = create_batch(
        client, key="manual-only", config=metadata(schedule=None)
    ).json()
    assert batch["schedule"] is None
    assert batch["preview_runs"] == []
    response = activation(client, batch["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "SCHEDULE_REQUIRED"
    enabled = enabled_batch(client)
    removed = client.patch(
        f"{API}/{enabled['id']}",
        headers={**token(), "Idempotency-Key": "drop", "If-Match": '"2"'},
        json={"schedule": None},
    )
    assert removed.status_code == 409
    assert removed.json()["code"] == "SCHEDULE_REQUIRED"
    disabled = activation(client, enabled["id"], False, 2).json()
    removed = client.patch(
        f"{API}/{enabled['id']}",
        headers={
            **token(),
            "Idempotency-Key": "drop-2",
            "If-Match": f'"{disabled["revision"]}"',
        },
        json={"schedule": None},
    )
    assert removed.status_code == 200
    assert removed.json()["schedule"] is None


def test_scheduled_runs_are_created_by_the_system(client, scheduling):
    batch = enabled_batch(client)
    assert batch["created_by_type"] == "USER"
    assert batch["updated_by"] == "alice"
    assert scheduling.tick(now=first_at(batch))
    run = runs(client, batch)[0]
    assert (run["created_by_type"], run["created_by"]) == (
        "SYSTEM",
        "scheduler",
    )
    # Moving next_run_at is scheduler bookkeeping, not a batch update.
    assert get_batch(client, batch)["updated_at"] == batch["updated_at"]
    window = client.post(
        f"{API}/{batch['id']}/disable",
        headers={
            **token(),
            "Idempotency-Key": "disable",
            "If-Match": f'"{batch["revision"]}"',
        },
    ).json()
    assert (window["updated_by_type"], window["updated_by"]) == (
        "USER",
        "alice",
    )
