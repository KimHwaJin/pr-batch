"""Run admission and the task pipeline against real PostgreSQL."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import (
    ALICE,
    API,
    BOB,
    CALLER_KEY,
    PLAIN_KEY,
    SCRIPTS,
    create_batch,
    metadata,
    notebook_bytes,
    token,
    user_id,
)
from sqlalchemy import event, func, select, update

from app.db.base import utcnow
from app.integrations.executor import ExecutorClient, ExecutorError
from app.models import BatchRun, IdempotencyRecord, RunStepBinding, Task
from app.runtime.dispatcher import Dispatcher
from app.runtime.retention import Retention
from app.runtime.runtime import BackgroundRuntime
from app.services.run_service import (
    identity_headers,
    promote_waiting,
    submission_request,
)
from app.tasks.handlers import HANDLERS, UNCERTAIN, Clients
from app.tasks.queue import TaskKind, claim, locked_lease


class FakeExecutor:
    """Contract-shaped remote service; the client still performs HTTP calls."""

    def __init__(self):
        self.receipts = {}
        self.payloads = []
        self.requests = []
        self.lose_response = False
        self.http_error = None
        self.error_code = None
        self.state = "RUNNING"
        self.version = 2
        self.projection = "SUCCEEDED"
        self.artifact_ready = True
        self.artifact_id = str(uuid4())
        self.attempt_id = str(uuid4())
        self.owner_override = None
        self.bad_step = False
        self.bad_result = False
        self.cancels = []
        self.contents = {}
        self.identities = {}
        self.headers = []

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        self.headers.append(
            {k: v for k, v in request.headers.items() if k.startswith("x-")}
        )
        if self.http_error:
            if self.error_code:
                return httpx2.Response(
                    self.http_error,
                    json={
                        "error": {
                            "code": self.error_code,
                            "message": "private remote error",
                        }
                    },
                )
            return httpx2.Response(
                self.http_error, text="private remote error"
            )
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            execution_id = request.url.path.split("/")[4]
            self.cancels.append(json.loads(request.content))
            if self.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return httpx2.Response(
                    409, json={"error": {"code": "INVALID_STATE_TRANSITION"}}
                )
            self.state, self.version = "CANCELLED", self.version + 1
            return httpx2.Response(
                202,
                json={
                    "execution_id": execution_id,
                    "state": {"status": "CANCEL_REQUESTED", "version": 3},
                },
            )
        if request.url.path.startswith("/api/v1/artifacts/"):
            artifact_id = request.url.path.split("/")[4]
            return httpx2.Response(
                200,
                content=self.contents.get(artifact_id, b"{}"),
                headers={"content-type": "application/x-ipynb+json"},
            )
        if request.method == "POST":
            payload = json.loads(request.content)
            self.payloads.append(payload)
            key = payload["idempotency_key"]
            self.identities.setdefault(key, self.headers[-1])
            if key not in self.receipts:
                self.receipts[key] = {
                    "execution_id": str(uuid4()),
                    "operation": {
                        "operation_id": str(uuid4()),
                        "steps": [
                            {
                                "sequence": step["sequence"],
                                "step_id": str(uuid4()),
                            }
                            for step in payload["operation"]["spec"]["steps"]
                        ],
                    },
                    "state": {"status": "QUEUED", "version": 1},
                }
            if self.lose_response:
                self.lose_response = False
                raise httpx2.ReadTimeout("response lost after remote commit")
            receipt = self.receipts[key]
            if self.bad_step:
                receipt = {
                    **receipt,
                    "operation": {**receipt["operation"], "steps": []},
                }
            return httpx2.Response(201, json=receipt)
        execution_id = request.url.path.split("/")[4]
        key, receipt = next(
            (k, r)
            for k, r in self.receipts.items()
            if r["execution_id"] == execution_id
        )
        identity = self.identities[key]
        context = {"user_id": identity["x-user-id"]}
        if "x-project-id" in identity:
            context["project_id"] = identity["x-project-id"]
        lifecycle = {
            "started_at": "2026-10-12T01:00:00Z",
            "finished_at": (
                "2026-10-12T01:01:00Z"
                if self.state in {"SUCCEEDED", "FAILED", "CANCELLED"}
                else None
            ),
        }
        state = {"status": self.state, "version": self.version}
        if not request.url.path.endswith("/result"):
            return httpx2.Response(
                200,
                json={
                    "execution_id": execution_id,
                    "state": state,
                    "context": {
                        **context,
                        "user_id": self.owner_override or context["user_id"],
                    },
                    "lifecycle": lifecycle,
                    "failure": (
                        {
                            "type": "USER_CODE_ERROR",
                            "message": "private source and output",
                        }
                        if self.state == "FAILED"
                        else None
                    ),
                    "retry": {
                        "strategy": "FULL_RESTART",
                        "count": 0,
                        "from_sequence": None,
                        "retained_runtime_session_until": None,
                    },
                    "workspace": {
                        "notebook_projection": {"status": self.projection}
                    },
                },
            )
        return httpx2.Response(
            200,
            json={
                "execution": {"execution_id": execution_id, "state": state},
                "operations": [
                    {
                        "operation_id": receipt["operation"]["operation_id"],
                        "steps": [
                            {
                                **s,
                                "result": {
                                    "status": self.state,
                                    "output_summary": {
                                        "text": "DO NOT STORE OUTPUT"
                                    },
                                    "result_ref": {
                                        "storage": "SHARED_PV",
                                        "execution_id": (
                                            str(uuid4())
                                            if self.bad_result
                                            else execution_id
                                        ),
                                        "step_id": s["step_id"],
                                        "attempt_id": self.attempt_id,
                                        "fencing_token": 1,
                                        "relative_path": "manifest.json",
                                        "checksum_sha256": "a" * 64,
                                        "size_bytes": 100,
                                        "complete": True,
                                        "representation_count": 1,
                                        "total_size_bytes": 200,
                                    },
                                },
                            }
                            for s in receipt["operation"]["steps"]
                        ],
                    }
                ],
                "attempts": [
                    {
                        "attempt_id": self.attempt_id,
                        "execution_id": execution_id,
                        "attempt_number": 1,
                        "lifecycle": lifecycle,
                    }
                ],
                "artifacts": (
                    [
                        {
                            "artifact_id": self.artifact_id,
                            "type": "NOTEBOOK",
                            "status": "AVAILABLE",
                            "produced_by": {"execution_id": execution_id},
                        }
                    ]
                    if self.artifact_ready
                    else []
                ),
            },
        )


EXECUTOR_KEY = "exe_test_0123456789"


@pytest.fixture
def execution(client):
    client.app.state.settings.executor_base_url = "http://executor.test"
    fake = FakeExecutor()
    remote = ExecutorClient(
        "http://executor.test",
        api_key=EXECUTOR_KEY,
        transport=httpx2.MockTransport(fake.handle),
    )
    worker = Dispatcher(client.app.state.sessions, Clients(remote, 5, 60))
    yield fake, worker
    remote.close()


def run_request(client, batch_id, key="run-1", owner="alice", **identity):
    return client.post(
        f"{API}/{batch_id}/runs",
        headers={**token(owner, **identity), "Idempotency-Key": key},
    )


def run_get(client, run, owner="alice", **identity):
    return client.get(
        f"{API}/{run['batch_id']}/runs/{run['id']}",
        headers=token(owner, **identity),
    )


def command(client, run, action, key):
    return client.post(
        f"{API}/{run['batch_id']}/runs/{run['id']}/{action}",
        headers={**token(), "Idempotency-Key": key},
    )


def due(worker, run_id):
    with worker.sessions.begin() as session:
        session.execute(
            update(Task)
            .where(Task.run_id == UUID(run_id), Task.status == "PENDING")
            # Shift instead of overwrite so due order is preserved.
            .values(run_at=Task.run_at - timedelta(days=1), lease_until=None)
        )


def tick(worker, run):
    due(worker, run["id"])
    assert worker.tick()


def drain(worker, run, limit=20):
    for _ in range(limit):
        due(worker, run["id"])
        if not worker.tick():
            return
    with worker.sessions() as session:
        rows = session.execute(
            select(
                Task.kind, Task.status, Task.last_error, Task.attempts
            ).where(Task.run_id == UUID(run["id"]))
        ).all()
        current = session.get(BatchRun, UUID(run["id"]))
        state = (current.status, current.last_error) if current else None
    raise AssertionError(f"pipeline did not settle: {state} {rows}")


def pending_tasks(worker, run):
    with worker.sessions() as session:
        return list(
            session.scalars(
                select(Task.kind).where(
                    Task.run_id == UUID(run["id"]), Task.status == "PENDING"
                )
            )
        )


def test_manual_requires_executor_configuration(client):
    batch = create_batch(client).json()
    response = run_request(client, batch["id"])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXECUTOR_NOT_CONFIGURED"


def test_enable_requires_executor_but_drafts_do_not(client):
    batch = create_batch(client).json()  # registered without an Executor
    response = client.post(
        f"{API}/{batch['id']}/enable",
        headers={
            **token(),
            "Idempotency-Key": "enable",
            "If-Match": f'"{batch["revision"]}"',
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXECUTOR_NOT_CONFIGURED"
    stored = client.get(f"{API}/{batch['id']}", headers=token()).json()
    assert stored["enabled"] is False


def test_durable_acceptance_replay_and_frozen_settings(client, execution):
    fake, worker = execution
    batch = create_batch(client).json()
    first = run_request(client, batch["id"])
    assert first.status_code == 202, first.text
    run = first.json()
    assert run["status"] == "QUEUED"
    assert run["execution_id"] is None
    assert fake.requests == []
    assert pending_tasks(worker, run) == ["SUBMIT_EXECUTION"]
    assert run_request(client, batch["id"]).json() == run
    uploaded = client.post(
        f"{SCRIPTS}/{batch['script_id']}/versions",
        headers={**token(), "Idempotency-Key": "new-version"},
        files={"file": ("v2.ipynb", notebook_bytes("x = 42"))},
    ).json()
    changed = client.patch(
        f"{API}/{batch['id']}",
        headers={**token(), "Idempotency-Key": "config", "If-Match": '"1"'},
        json={
            "script_version_number": uploaded["version_number"],
            "runtime": {"type": "JUPYTER", "profile": "analytics"},
        },
    )
    assert changed.json()["config_number"] == 2
    tick(worker, run)
    payload = fake.payloads[0]
    assert payload["runtime"] == {"type": "JUPYTER", "profile": "python3"}
    assert "context" not in payload
    assert payload["metadata"]["run_id"] == run["id"]
    assert payload["metadata"]["script_version_id"] == run["script_version_id"]
    assert payload["metadata"]["script_id"] == batch["script_id"]
    assert payload["lifecycle"] == {"operation_mode": "SINGLE"}
    assert payload["trigger"] == {
        "type": "BATCH",
        "actor": {"type": "USER", "id": ALICE},
    }
    assert payload["metadata"]["batch_id"] == batch["id"]
    detail = run_get(client, run).json()
    assert detail["config_number"] == 1
    assert detail["status"] == "RUNNING"
    assert pending_tasks(worker, run) == ["SYNC_EXECUTION"]


def test_pipeline_completes_after_notebook_projection(client, execution):
    fake, worker = execution
    notebook = json.loads(notebook_bytes("x = 1"))
    notebook["cells"].append(
        {**notebook["cells"][0], "id": "empty", "source": " "}
    )
    notebook["cells"].append(
        {**notebook["cells"][0], "id": "second", "source": "print(x)"}
    )
    batch = create_batch(client, raw=json.dumps(notebook).encode()).json()
    run = run_request(client, batch["id"]).json()
    tick(worker, run)
    steps = fake.payloads[0]["operation"]["spec"]["steps"]
    assert [s["sequence"] for s in steps] == [0, 1]
    for step in steps:
        source = step["payload"]["source"]
        assert source["type"] == "PATH"
        assert source["path"].endswith(".py")
        assert len(source["sha256"]) == 64
    tick(worker, run)
    assert run_get(client, run).json()["status"] == "RUNNING"
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, run)
    assert run_get(client, run).json()["status"] == "COLLECTING"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "COMPLETED"
    assert detail["completed_at"] is not None
    assert "report" not in detail
    assert detail["result"] == {"notebook_artifact_id": fake.artifact_id}
    assert detail["artifacts"][0]["artifact_id"] == fake.artifact_id
    assert [s["original_index"] for s in detail["steps"]] == [0, 2]
    assert detail["steps"][0]["result_ref"]["attempt_id"] == fake.attempt_id
    assert "DO NOT STORE OUTPUT" not in json.dumps(detail)
    assert pending_tasks(worker, run) == []


def test_sync_interval_backs_off_to_maximum(client, execution):
    _, worker = execution
    worker.clients.sync_max_seconds = 20
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    waits = []
    for _ in range(4):
        tick(worker, run)
        with worker.sessions() as session:
            task = session.scalar(
                select(Task).where(
                    Task.run_id == UUID(run["id"]), Task.status == "PENDING"
                )
            )
            waits.append(task.payload["interval"])
    assert waits == [10, 20, 20, 20]


def test_unknown_submit_is_recovered_without_new_execution(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.lose_response = True
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["last_error"] == "EXECUTOR_UNREACHABLE"
    assert detail["status"] == "QUEUED"
    restarted = Dispatcher(worker.sessions, worker.clients)
    tick(restarted, run)
    assert len(fake.receipts) == 1
    assert fake.payloads[0] == fake.payloads[1]
    detail = run_get(client, run).json()
    assert detail["last_error"] is None
    assert detail["status"] == "RUNNING"


def test_expired_lease_cannot_overwrite_recovered_task(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    with worker.sessions.begin() as session:
        old = claim(session, 90)
    assert old is not None
    with worker.sessions.begin() as session:
        session.get(Task, old.id).lease_until = utcnow() - timedelta(seconds=1)
    assert worker.tick()
    with worker.sessions.begin() as session:
        assert locked_lease(session, old) is None
    assert len(fake.receipts) == 1
    assert run_get(client, run).json()["status"] == "RUNNING"


def test_active_lease_is_not_claimed_twice(client, execution):
    _, worker = execution
    run_request(client, create_batch(client).json()["id"])

    def try_claim(_):
        with worker.sessions.begin() as session:
            return claim(session, 90)

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(try_claim, range(4)))
    assert sum(work is not None for work in claims) == 1


def test_concurrent_replay_and_skip_admission(client, execution):
    _, worker = execution
    batch = create_batch(client).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: run_request(client, batch["id"]), range(4))
        )
    assert {r.status_code for r in responses} == {202}
    assert len({r.json()["id"] for r in responses}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda n: run_request(client, batch["id"], f"r{n}"), range(4)
            )
        )
    assert {r.json()["status"] for r in responses} == {"SKIPPED"}
    assert {r.json()["skip_reason"] for r in responses} == {"OVERLAP"}
    with worker.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1


@pytest.mark.parametrize("policy,maximum", [("QUEUE", 1), ("PARALLEL", 2)])
def test_capacity_queue_and_manual_overflow(
    client, execution, policy, maximum
):
    _, worker = execution
    overlap = {"mode": policy, "max_queued": 1}
    if policy == "PARALLEL":
        overlap["max_concurrent"] = maximum
    config = metadata(policy={"overlap": overlap})
    batch = create_batch(client, config=config).json()
    runs = [
        run_request(client, batch["id"], f"r{n}") for n in range(maximum + 2)
    ]
    assert [r.json()["status"] for r in runs] == (
        ["QUEUED"] * maximum + ["WAITING", "SKIPPED"]
    )
    assert runs[-1].status_code == 409
    assert runs[-1].json()["skip_reason"] == "SKIPPED_CAPACITY"
    assert (
        run_request(client, batch["id"], f"r{maximum + 1}").status_code == 409
    )
    with worker.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BatchRun)) == (
            maximum + 2
        )


def test_code_completion_frees_slot_before_collection(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "QUEUE"}})
    ).json()
    first = run_request(client, batch["id"]).json()
    second = run_request(client, batch["id"], "second").json()
    assert second["status"] == "WAITING"
    assert pending_tasks(worker, second) == []
    fake.lose_response = True
    tick(worker, first)
    assert run_get(client, second).json()["status"] == "WAITING"
    tick(worker, first)
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, first)
    assert run_get(client, first).json()["status"] == "COLLECTING"
    assert run_get(client, second).json()["status"] == "QUEUED"
    assert pending_tasks(worker, second) == ["SUBMIT_EXECUTION"]
    tick(worker, second)
    assert run_get(client, second).json()["status"] == "RUNNING"
    assert len(fake.receipts) == 2


def test_fifo_promotion_is_serialized(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "QUEUE"}})
    ).json()
    runs = [run_request(client, batch["id"], f"r{n}").json() for n in range(4)]
    tick(worker, runs[0])
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, runs[0])

    def promote(_):
        with worker.sessions.begin() as session:
            from app.models import Batch

            session.scalar(
                select(Batch)
                .where(Batch.id == UUID(batch["id"]))
                .with_for_update()
            )
            promote_waiting(session, UUID(batch["id"]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(promote, range(4)))
    assert [run_get(client, r).json()["status"] for r in runs[1:]] == [
        "QUEUED",
        "WAITING",
        "WAITING",
    ]


def test_poll_failure_is_not_code_failure_and_stale_state_is_rejected(
    client, execution
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    tick(worker, run)
    fake.http_error = 503
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "RUNNING"
    assert detail["execution_status"] == "RUNNING"
    assert detail["last_error"] == "EXECUTOR_HTTP_503"
    observed = detail["observed_at"]
    fake.http_error = None
    fake.version, fake.state = 1, "QUEUED"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "RUNNING"
    assert detail["observed_at"] == observed
    assert detail["last_error"] == "EXECUTOR_CONTRACT_MISMATCH"


def test_failed_execution_never_retries(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.state, fake.version = "FAILED", 3
    drain(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "FAILED"
    assert detail["failure_type"] == "USER_CODE_ERROR"
    assert detail["artifacts"][0]["artifact_id"] == fake.artifact_id
    assert "private source" not in json.dumps(detail)
    assert [method for method, _ in fake.requests].count("POST") == 1
    assert all(not path.endswith("/retry") for _, path in fake.requests)


def test_success_collects_without_waiting_for_notebook(client, execution):
    # The Executor only reports SUCCEEDED with the notebook projected, so
    # one collect finishes the run; a missing notebook is not waited for.
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.state, fake.version = "SUCCEEDED", 3
    fake.projection, fake.artifact_ready = "FAILED", False
    tick(worker, run)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "COMPLETED"
    assert detail["notebook_status"] == "FAILED"
    assert detail["result"] == {"notebook_artifact_id": None}
    assert pending_tasks(worker, run) == []


def test_mismatched_receipt_and_result_roll_back(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.bad_step = True
    tick(worker, run)
    assert run_get(client, run).json()["execution_id"] is None
    fake.bad_step = False
    tick(worker, run)
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, run)
    fake.bad_result = True
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "COLLECTING"
    assert detail["last_error"] == "EXECUTOR_CONTRACT_MISMATCH"
    assert detail["steps"][0]["result_ref"] is None
    assert detail["artifacts"] == []


def test_foreign_executor_context_is_not_applied(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.owner_override = BOB
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "QUEUED"
    assert detail["last_error"] == "EXECUTOR_CONTRACT_MISMATCH"


def test_owner_isolation_and_history_pagination(client, execution):
    batch = create_batch(client).json()
    runs = [run_request(client, batch["id"], f"r{n}").json() for n in range(3)]
    assert run_request(client, batch["id"], owner="bob").status_code == 404
    assert run_get(client, runs[0], owner="bob").status_code == 404
    url = f"{API}/{batch['id']}/runs"
    assert client.get(url, headers=token("bob")).status_code == 404
    first = client.get(url, params={"limit": 2}, headers=token()).json()
    second = client.get(
        url, params={"cursor": first["next_cursor"]}, headers=token()
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert second["next_cursor"] is None
    response = client.get(
        url, params={"cursor": str(uuid4())}, headers=token()
    )
    assert response.status_code == 422
    other = create_batch(client, key="other").json()
    response = client.get(
        f"{API}/{other['id']}/runs/{runs[0]['id']}", headers=token()
    )
    assert response.status_code == 404


def test_acceptance_rolls_back_with_task_failure(client, execution):
    _, worker = execution
    batch = create_batch(client).json()

    def reject(*args):
        raise RuntimeError("simulated task persistence failure")

    event.listen(Task, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError):
            run_request(client, batch["id"])
    finally:
        event.remove(Task, "before_insert", reject)
    with worker.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BatchRun)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RunStepBinding))
            == 0
        )
    assert run_request(client, batch["id"]).status_code == 202


@pytest.mark.parametrize("status", [301, 409, 422, 429, 503])
def test_http_errors_do_not_release_unknown_submission(
    client, execution, status
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.http_error = status
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "QUEUED"
    assert detail["execution_status"] is None
    assert detail["last_error"] == f"EXECUTOR_HTTP_{status}"
    assert "private remote" not in json.dumps(detail)
    assert pending_tasks(worker, run) == ["SUBMIT_EXECUTION"]


def test_client_limits_response_and_validates_contract():
    for content, expected in [
        (b"x" * 101, "EXECUTOR_RESPONSE_LIMIT"),
        (b"{}", "EXECUTOR_INVALID_RESPONSE"),
    ]:
        client = ExecutorClient(
            "http://executor.test",
            response_max_bytes=100,
            transport=httpx2.MockTransport(
                lambda request, body=content: httpx2.Response(
                    200, content=body
                )
            ),
        )
        try:
            with pytest.raises(ExecutorError, match=expected):
                client.submit({}, {"X-User-Id": "alice"})
        finally:
            client.close()


def test_manual_body_rejects_unimplemented_overrides(client, execution):
    batch = create_batch(client).json()
    response = client.post(
        f"{API}/{batch['id']}/runs",
        headers={**token(), "Idempotency-Key": "override"},
        json={"version_id": str(uuid4())},
    )
    assert response.status_code == 422
    assert run_request(client, batch["id"], "override").status_code == 202


def test_cancel_waiting_queued_and_running(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "QUEUE"}})
    ).json()
    queued = run_request(client, batch["id"], "a").json()
    waiting = run_request(client, batch["id"], "b").json()
    cancelled = command(client, waiting, "cancel", "c1")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "CANCELLED"
    flagged = command(client, queued, "cancel", "c2").json()
    assert flagged["status"] == "QUEUED" and flagged["cancel_requested"]
    tick(worker, queued)
    assert run_get(client, queued).json()["status"] == "CANCELLED"
    assert fake.payloads == []
    running = run_request(client, batch["id"], "d").json()
    tick(worker, running)
    assert command(client, running, "cancel", "c3").status_code == 202
    drain(worker, running)
    detail = run_get(client, running).json()
    assert detail["status"] == "CANCELLED"
    assert (detail["cancel_source"], detail["cancel_reason"]) == (
        "USER",
        "Cancelled by the batch owner.",
    )
    assert fake.cancels[0] == {
        "idempotency_key": f"batch-run:{running['id']}:cancel",
        "reason": "Cancelled by the batch owner.",
        "actor": {"type": "USER", "id": ALICE},
    }
    again = command(client, running, "cancel", "c4")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "NOT_CANCELLABLE"


def test_background_runtime_drives_runs_and_stops(client, execution):
    fake, worker = execution
    settings = client.app.state.settings.model_copy(
        update={
            "idle_max_seconds": 0.05,
            "sync_initial_seconds": 1,
            "task_concurrency": 2,
            "shutdown_grace_seconds": 5,
        }
    )
    fake.state, fake.version = "SUCCEEDED", 3
    clients = Clients(worker.clients.executor, 1, 1)
    runtime = BackgroundRuntime(settings, worker.sessions, clients)
    run = run_request(client, create_batch(client).json()["id"]).json()
    runtime.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if run_get(client, run).json()["status"] == "COMPLETED":
                break
            time.sleep(0.1)
        assert run_get(client, run).json()["status"] == "COMPLETED"
        assert runtime.healthy()
    finally:
        runtime.stopping.set()
        for thread in runtime.threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in runtime.threads)


def test_audit_fields_follow_who_changed_the_run(client, execution):
    fake, worker = execution
    batch = create_batch(client).json()
    run = run_request(client, batch["id"]).json()
    assert (run["created_by_type"], run["created_by"]) == ("USER", ALICE)
    assert run["updated_by"] == ALICE
    tick(worker, run)
    detail = run_get(client, run).json()
    assert (detail["updated_by_type"], detail["updated_by"]) == (
        "SYSTEM",
        "dispatcher",
    )
    assert detail["updated_at"] > run["updated_at"]
    assert detail["created_by"] == ALICE
    cancelled = command(client, run, "cancel", "cancel").json()
    assert (cancelled["updated_by_type"], cancelled["updated_by"]) == (
        "USER",
        ALICE,
    )


def test_replace_overlap_cancels_running_and_starts_new(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "REPLACE"}})
    ).json()
    old = run_request(client, batch["id"], "old").json()
    tick(worker, old)
    assert run_get(client, old).json()["status"] == "RUNNING"
    queued = run_request(client, batch["id"], "queued").json()
    assert queued["status"] == "WAITING"
    new = run_request(client, batch["id"], "new").json()
    assert new["status"] == "WAITING"
    assert run_get(client, queued).json()["status"] == "CANCELLED"
    assert run_get(client, old).json()["cancel_requested"] is True
    drain(worker, old)
    assert run_get(client, old).json()["status"] == "CANCELLED"
    reason = "Replaced by a newer run (overlap policy REPLACE)."
    # A manual run triggered the overlap, yet the policy cancels.
    assert fake.cancels == [
        {
            "idempotency_key": f"batch-run:{old['id']}:cancel",
            "reason": reason,
            "actor": {"type": "BATCH", "id": batch["id"]},
        }
    ]
    assert run_get(client, old).json()["cancel_source"] == "POLICY"
    assert run_get(client, queued).json()["cancel_reason"] == reason
    assert run_get(client, new).json()["status"] in {"QUEUED", "RUNNING"}


def test_submission_carries_identity_headers_and_run_id(client, execution):
    fake, worker = execution
    batch = create_batch(client, tenant="agent3", project="proj-1").json()
    run = run_request(client, batch["id"], tenant="agent3").json()
    assert run["snapshot"]["tenant_id"] == "agent3"
    assert run["snapshot"]["project_id"] == "proj-1"
    fake.state = "SUCCEEDED"
    drain(worker, run)
    body = fake.payloads[0]
    assert "tenant_id" not in body and "context" not in body
    assert body["metadata"] == {
        "batch_id": run["batch_id"],
        "script_id": run["script_id"],
        "script_version_id": run["script_version_id"],
        "run_id": run["id"],
    }
    # Submit, detail and result all carry the stored identity.
    assert len(fake.headers) >= 3
    for headers in fake.headers:
        assert headers["x-api-key"] == EXECUTOR_KEY
        assert headers["x-user-id"] == ALICE
        assert headers["x-tenant-id"] == "agent3"
        assert headers["x-project-id"] == "proj-1"
    other = create_batch(client, key="default").json()
    run = run_request(client, other["id"], key="run-2").json()
    tick(worker, run)
    assert fake.headers[-1]["x-tenant-id"] == "pr-agentic-service"
    assert "x-project-id" not in fake.headers[-1]


def test_runs_are_not_visible_from_another_tenant(client, execution):
    fake, worker = execution
    batch = create_batch(client, tenant="agent3").json()
    run = run_request(client, batch["id"], tenant="agent3").json()
    # Same user, the key's default tenant: neither the batch nor its runs.
    assert run_request(client, batch["id"], key="x").status_code == 404
    assert run_get(client, run).status_code == 404
    listed = client.get(f"{API}/{batch['id']}/runs", headers=token())
    assert listed.status_code == 404
    cancel = client.post(
        f"{API}/{batch['id']}/runs/{run['id']}/cancel",
        headers={**token(), "Idempotency-Key": "cancel"},
    )
    assert cancel.status_code == 404
    assert run_get(client, run, tenant="agent3").status_code == 200
    response = client.patch(
        f"{API}/{batch['id']}",
        headers={
            **token(tenant="pr-agentic-service"),
            "Idempotency-Key": "move-tenant",
            "If-Match": f'"{batch["revision"]}"',
        },
        json={"name": "renamed"},
    )
    assert response.status_code == 404
    tick(worker, run)
    assert fake.headers[0]["x-tenant-id"] == "agent3"


def disable_owner(client, owner="alice", key=CALLER_KEY, tenant=None):
    return client.post(
        f"{API}:disable-by-owner",
        headers={
            "X-Api-Key": key,
            **({"X-Tenant-Id": tenant} if tenant else {}),
        },
        json={"owner_id": user_id(owner), "reason": "USER_DEACTIVATED"},
    )


def test_disable_by_owner_stops_schedules_and_waiting_runs(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "QUEUE"}})
    ).json()
    enabled = client.post(
        f"{API}/{batch['id']}/enable",
        headers={
            **token(),
            "Idempotency-Key": "enable",
            "If-Match": f'"{batch["revision"]}"',
        },
    )
    assert enabled.status_code == 200, enabled.text
    running = run_request(client, batch["id"]).json()
    tick(worker, running)
    waiting = run_request(client, batch["id"], key="run-2").json()
    assert run_get(client, waiting).json()["status"] == "WAITING"
    bobs = create_batch(client, key="bob", owner="bob").json()
    response = disable_owner(client)
    assert response.status_code == 200, response.text
    assert response.json() == {"disabled": 1}
    view = client.get(f"{API}/{batch['id']}", headers=token()).json()
    assert view["enabled"] is False
    assert view["disabled_reason"] == "USER_DEACTIVATED"
    assert view["disabled_at"] is not None
    assert view["next_run_at"] is None
    skipped = run_get(client, waiting).json()
    assert skipped["status"] == "SKIPPED"
    assert skipped["skip_reason"] == "OWNER_DEACTIVATED"
    # The submitted execution keeps running.
    assert run_get(client, running).json()["status"] in {"QUEUED", "RUNNING"}
    manual = run_request(client, batch["id"], key="run-3")
    assert manual.status_code == 409
    assert manual.json()["error"]["code"] == "OWNER_DEACTIVATED"
    assert disable_owner(client).json() == {"disabled": 0}
    untouched = client.get(f"{API}/{bobs['id']}", headers=token("bob"))
    assert untouched.json()["disabled_reason"] is None
    # Re-enabling is explicit and clears the reason.
    again = client.post(
        f"{API}/{batch['id']}/enable",
        headers={
            **token(),
            "Idempotency-Key": "re-enable",
            "If-Match": f'"{view["revision"]}"',
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["disabled_reason"] is None
    assert again.json()["disabled_at"] is None


def test_disable_by_owner_applies_to_one_tenant(client, execution):
    batches = {}
    for tenant in ("pr-agentic-service", "agent3"):
        created = create_batch(client, key=tenant, tenant=tenant).json()
        enabled = client.post(
            f"{API}/{created['id']}/enable",
            headers={
                **token(tenant=tenant),
                "Idempotency-Key": f"enable-{tenant}",
                "If-Match": f'"{created["revision"]}"',
            },
        )
        assert enabled.status_code == 200, enabled.text
        batches[tenant] = created["id"]
    assert disable_owner(client, tenant="agent3").json() == {"disabled": 1}
    off = client.get(
        f"{API}/{batches['agent3']}", headers=token(tenant="agent3")
    ).json()
    assert off["disabled_reason"] == "USER_DEACTIVATED"
    # Without X-Tenant-Id: the key's default tenant only.
    on = client.get(f"{API}/{batches['pr-agentic-service']}", headers=token())
    assert on.json()["enabled"] is True
    assert disable_owner(client).json() == {"disabled": 1}
    assert disable_owner(client, tenant="someone-else").status_code == 403


def test_disable_by_owner_needs_an_admin_key(client):
    assert disable_owner(client, key=PLAIN_KEY).status_code == 403
    assert disable_owner(client, key="bat_unknown_key").status_code == 401


def test_owner_disabled_batch_still_runs_manually(client, execution):
    batch = create_batch(client).json()
    enabled = client.post(
        f"{API}/{batch['id']}/enable",
        headers={**token(), "Idempotency-Key": "on", "If-Match": '"1"'},
    ).json()
    disabled = client.post(
        f"{API}/{batch['id']}/disable",
        headers={
            **token(),
            "Idempotency-Key": "off",
            "If-Match": f'"{enabled["revision"]}"',
        },
    ).json()
    assert disabled["disabled_reason"] == "OWNER"
    assert run_request(client, batch["id"]).status_code == 202


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "INVALID_EXECUTION_SPEC"),
        (422, "UNSUPPORTED_RUNTIME_PROFILE"),
        (409, "INVALID_STATE_TRANSITION"),
        (400, "INVALID_IDENTITY"),
        (401, "UNAUTHENTICATED"),
        (403, "TENANT_OVERRIDE_FORBIDDEN"),
    ],
)
def test_rejected_submission_fails_and_frees_the_slot(
    client, execution, status, code
):
    fake, worker = execution
    batch = create_batch(
        client,
        config=metadata(
            policy={"overlap": {"mode": "QUEUE", "max_queued": 1}}
        ),
    ).json()
    first = run_request(client, batch["id"]).json()
    second = run_request(client, batch["id"], key="run-2").json()
    assert run_get(client, second).json()["status"] == "WAITING"
    fake.http_error, fake.error_code = status, code
    tick(worker, first)
    detail = run_get(client, first).json()
    assert detail["status"] == "FAILED"
    assert detail["failure_type"] == "SUBMIT_REJECTED"
    assert detail["last_error"] == f"EXECUTOR_HTTP_{status}:{code}"
    assert detail["completed_at"] is not None
    assert "private remote" not in json.dumps(detail)
    assert pending_tasks(worker, first) == []
    assert run_get(client, second).json()["status"] == "QUEUED"


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "IDEMPOTENCY_CONFLICT"), (409, "PERSISTENCE_CONFLICT")],
)
def test_conflicts_keep_retrying(client, execution, status, code):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.http_error, fake.error_code = status, code
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "QUEUED"
    assert detail["last_error"] == f"EXECUTOR_HTTP_{status}:{code}"
    assert pending_tasks(worker, run) == ["SUBMIT_EXECUTION"]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_refusal_without_error_body_is_final(client, execution, status):
    """A gateway may refuse before the Executor; nothing was created."""
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.http_error = status
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "FAILED"
    assert detail["failure_type"] == "SUBMIT_REJECTED"
    assert detail["last_error"] == f"EXECUTOR_HTTP_{status}"
    assert pending_tasks(worker, run) == []


def test_auth_refusal_after_an_uncertain_attempt_keeps_retrying(
    client, execution
):
    """An earlier attempt may have created the execution."""
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.lose_response = True
    tick(worker, run)
    fake.lose_response = False
    fake.http_error, fake.error_code = 403, "TENANT_OVERRIDE_FORBIDDEN"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "QUEUED"
    assert pending_tasks(worker, run) == ["SUBMIT_EXECUTION"]


def test_rejection_after_an_uncertain_attempt_is_not_final(client, execution):
    """The lost submission may exist; validation runs before the lookup."""
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.lose_response = True
    tick(worker, run)
    fake.http_error, fake.error_code = 422, "INVALID_EXECUTION_SPEC"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["status"] == "QUEUED"
    assert detail["last_error"] == "EXECUTOR_HTTP_422:INVALID_EXECUTION_SPEC"
    assert pending_tasks(worker, run) == ["SUBMIT_EXECUTION"]
    fake.http_error = None
    tick(worker, run)
    assert run_get(client, run).json()["status"] == "RUNNING"
    assert len(fake.receipts) == 1


def test_cancel_after_uncertain_submit_cancels_the_execution(
    client, execution
):
    """The lost submission may exist, so it is resubmitted and cancelled."""
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.lose_response = True
    tick(worker, run)
    flagged = command(client, run, "cancel", "c1").json()
    assert flagged["status"] == "QUEUED" and flagged["cancel_requested"]
    drain(worker, run)
    assert run_get(client, run).json()["status"] == "CANCELLED"
    assert len(fake.receipts) == 1
    assert len(fake.cancels) == 1


def test_cancel_after_a_lost_lease_cancels_the_execution(client, execution):
    """A pod that died mid-submit may have created the execution."""
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    with worker.sessions.begin() as session:
        claimed = claim(session, 90)
        assert claimed is not None
    # The submission reaches the Executor, then the pod dies before apply.
    with worker.sessions() as session:
        current = session.get(BatchRun, UUID(run["id"]))
        request = submission_request(session, current)
        identity = identity_headers(current)
    worker.clients.executor.submit(request, identity)
    with worker.sessions.begin() as session:
        session.get(Task, claimed.id).lease_until = utcnow() - timedelta(
            seconds=1
        )
    assert command(client, run, "cancel", "c1").json()["status"] == "QUEUED"
    drain(worker, run)
    assert run_get(client, run).json()["status"] == "CANCELLED"
    assert len(fake.receipts) == 1
    assert len(fake.cancels) == 1


def test_task_failing_past_the_limit_abandons_the_run(client, execution):
    fake, worker = execution
    batch = create_batch(
        client, config=metadata(policy={"overlap": {"mode": "QUEUE"}})
    ).json()
    first = run_request(client, batch["id"]).json()
    second = run_request(client, batch["id"], "second").json()
    fake.http_error = 503
    tick(worker, first)
    assert run_get(client, first).json()["status"] == "QUEUED"
    with worker.sessions.begin() as session:
        session.execute(
            update(Task)
            .where(Task.run_id == UUID(first["id"]))
            .values(failing_since=Task.failing_since - timedelta(days=2))
        )
    tick(worker, first)
    detail = run_get(client, first).json()
    assert (detail["status"], detail["failure_type"]) == (
        "FAILED",
        "ABANDONED",
    )
    assert detail["last_error"] == "EXECUTOR_HTTP_503"
    assert pending_tasks(worker, first) == []
    assert run_get(client, second).json()["status"] == "QUEUED"


@pytest.mark.parametrize("stage", ["prepare", "call", "apply"])
def test_unexpected_errors_back_off_and_give_up(
    client, execution, monkeypatch, stage
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    handler = HANDLERS[TaskKind.SUBMIT_EXECUTION]

    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(handler, stage, broken)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert (detail["status"], detail["last_error"]) == (
        "QUEUED",
        "UNEXPECTED_ERROR: KeyError",
    )
    with worker.sessions() as session:
        task = session.scalars(
            select(Task).where(Task.run_id == UUID(run["id"]))
        ).one()
        assert task.failing_since is not None  # counts toward give-up
        assert task.run_at > utcnow()  # backed off, not retried at once
        assert task.payload.get(UNCERTAIN) is True
    give_up(worker, run)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert (detail["status"], detail["failure_type"]) == (
        "FAILED",
        "ABANDONED",
    )


def test_unexpected_error_recovers_once_fixed(client, execution, monkeypatch):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    handler = HANDLERS[TaskKind.SUBMIT_EXECUTION]
    original = handler.apply

    def broken(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr(handler, "apply", broken)
    tick(worker, run)
    monkeypatch.setattr(handler, "apply", original)
    tick(worker, run)  # same idempotency key: one execution
    assert run_get(client, run).json()["status"] == "RUNNING"
    assert len(fake.receipts) == 1


def give_up(worker, run):
    with worker.sessions.begin() as session:
        session.execute(
            update(Task)
            .where(Task.run_id == UUID(run["id"]), Task.status == "PENDING")
            .values(failing_since=Task.failing_since - timedelta(days=2))
        )


def test_abandoned_running_execution_is_cancelled_once(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)  # submitted: RUNNING
    fake.http_error = 503
    tick(worker, run)  # poll fails
    give_up(worker, run)
    tick(worker, run)  # abandoned; one cancel queued
    detail = run_get(client, run).json()
    assert (detail["status"], detail["failure_type"]) == (
        "FAILED",
        "ABANDONED",
    )
    assert pending_tasks(worker, run) == ["CANCEL_EXECUTION"]
    fake.http_error = None
    drain(worker, run)
    assert len(fake.cancels) == 1
    assert run_get(client, run).json()["status"] == "FAILED"


def test_abandon_cancel_is_not_retried(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.http_error = 503
    tick(worker, run)
    give_up(worker, run)
    tick(worker, run)
    tick(worker, run)  # the cancel fails too
    assert pending_tasks(worker, run) == []
    assert run_get(client, run).json()["failure_type"] == "ABANDONED"


def test_retention_purges_old_records_only(client, execution):
    fake, worker = execution
    batch = create_batch(client).json()
    run = run_request(client, batch["id"]).json()
    fake.state, fake.version = "SUCCEEDED", 3
    drain(worker, run)
    with worker.sessions.begin() as session:
        session.execute(
            update(Task).values(created_at=Task.created_at - timedelta(31))
        )
        session.execute(
            update(IdempotencyRecord).values(
                created_at=IdempotencyRecord.created_at - timedelta(8)
            )
        )
    fresh = run_request(client, batch["id"], "fresh").json()
    Retention(worker.sessions, idempotency_days=7, task_days=30).tick()
    with worker.sessions() as session:
        tasks = session.scalars(select(Task.run_id)).all()
        keys = session.scalars(select(IdempotencyRecord.key)).all()
    assert set(tasks) == {UUID(fresh["id"])}
    assert keys == ["fresh"]
