"""Manual admission, durable dispatch and recovery against real PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import (
    API,
    NOTEBOOKS,
    create_batch,
    metadata,
    notebook_bytes,
    token,
)
from sqlalchemy import func, select, update

from pr_batch.executor import ExecutorClient, ExecutorError
from pr_batch.models import (
    BatchRun,
    RunCommand,
    RunStepBinding,
    utcnow,
)
from pr_batch.worker import Worker


class FakeExecutor:
    """Contract-shaped remote service; the client still performs HTTP calls."""

    def __init__(self):
        self.receipts = {}
        self.payloads = []
        self.requests = []
        self.lose_response = False
        self.http_error = None
        self.state = "RUNNING"
        self.version = 2
        self.projection = "SUCCEEDED"
        self.artifact_ready = True
        self.artifact_id = str(uuid4())
        self.attempt_id = str(uuid4())
        self.owner_override = None
        self.bad_step = False
        self.bad_result = False

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        if self.http_error:
            return httpx2.Response(
                self.http_error, text="private remote error"
            )
        if request.method == "POST":
            payload = json.loads(request.content)
            self.payloads.append(payload)
            key = payload["idempotency_key"]
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
        payload = next(p for p in self.payloads if p["idempotency_key"] == key)
        lifecycle = {
            "started_at": "2026-10-12T01:00:00Z",
            "finished_at": (
                "2026-10-12T01:01:00Z"
                if self.state in {"SUCCEEDED", "FAILED"}
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
                        **payload["context"],
                        "user_id": (
                            self.owner_override
                            or payload["context"]["user_id"]
                        ),
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


@pytest.fixture
def execution(client):
    client.app.state.settings.executor_base_url = "http://executor.test"
    fake = FakeExecutor()
    remote = ExecutorClient(
        "http://executor.test", transport=httpx2.MockTransport(fake.handle)
    )
    sessions = client.app.state.registration.sessions
    worker = Worker(sessions, remote)
    yield fake, worker
    remote.close()


def run_request(client, batch_id, key="run-1", owner="alice"):
    return client.post(
        f"{API}/{batch_id}/runs",
        headers={
            **token(owner),
            "Idempotency-Key": key,
        },
    )


def run_get(client, run, owner="alice"):
    return client.get(
        f"{API}/{run['batch_id']}/runs/{run['id']}", headers=token(owner)
    )


def due(worker, run_id):
    with worker.sessions.begin() as session:
        session.execute(
            update(RunCommand)
            .where(RunCommand.run_id == UUID(run_id))
            .values(available_at=utcnow() - timedelta(seconds=1))
        )


def tick(worker, run):
    due(worker, run["id"])
    assert worker.tick()


def test_manual_requires_executor_configuration(client):
    batch = create_batch(client).json()
    response = run_request(client, batch["id"])
    assert response.status_code == 503
    assert response.json()["code"] == "EXECUTOR_NOT_CONFIGURED"


def test_durable_acceptance_replay_and_frozen_settings(client, execution):
    fake, worker = execution
    batch = create_batch(client).json()
    first = run_request(client, batch["id"])
    assert first.status_code == 202, first.text
    run = first.json()
    assert run["dispatch_status"] == "PENDING"
    assert run["execution_id"] is None
    assert fake.requests == []
    assert run_request(client, batch["id"]).json() == run
    uploaded = client.post(
        f"{NOTEBOOKS}/{batch['notebook_id']}/versions",
        headers={**token(), "Idempotency-Key": "new-version"},
        files={"file": ("v2.ipynb", notebook_bytes("x = 42"))},
    ).json()
    changed = client.patch(
        f"{API}/{batch['id']}",
        headers={**token(), "Idempotency-Key": "config", "If-Match": '"1"'},
        json={
            "notebook_version_id": uploaded["id"],
            "kernel_profile": "analytics",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["config_number"] == 2
    tick(worker, run)
    payload = fake.payloads[0]
    assert payload["runtime"] == {"type": "JUPYTER", "profile": "python3"}
    assert payload["context"] == {"user_id": "alice", "task_id": run["id"]}
    assert (
        payload["metadata"]["notebook_version_id"]
        == (run["notebook_version_id"])
    )
    assert payload["metadata"]["notebook_id"] == batch["notebook_id"]
    assert payload["lifecycle"] == {"operation_mode": "SINGLE"}
    assert payload["trigger"]["type"] == "BATCH"
    assert run_get(client, run).json()["config_number"] == 1
    assert run_request(client, batch["id"]).json() == run


def test_cell_mapping_and_metadata_only_result(client, execution):
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
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, run)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "SUCCEEDED"
    assert detail["result_status"] == "READY"
    assert detail["report_status"] == "NOT_REQUESTED"
    assert detail["artifacts"][0]["artifact_id"] == fake.artifact_id
    assert [s["original_index"] for s in detail["steps"]] == [0, 2]
    assert detail["steps"][0]["result_ref"]["attempt_id"] == fake.attempt_id
    assert "DO NOT STORE OUTPUT" not in json.dumps(detail)
    with worker.sessions() as session:
        assert session.get(RunCommand, UUID(run["id"])).phase == "DONE"


def test_unknown_submit_is_recovered_without_new_execution(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.lose_response = True
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["tracking_error"] == "EXECUTOR_UNREACHABLE"
    assert detail["execution_status"] is None
    restarted = Worker(worker.sessions, worker.executor)
    tick(restarted, run)
    assert len(fake.receipts) == 1
    assert fake.payloads[0] == fake.payloads[1]
    assert run_get(client, run).json()["tracking_error"] is None


def test_expired_worker_cannot_overwrite_recovered_response(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    old = worker.claim()
    assert old is not None
    receipt = worker.perform(old)
    with worker.sessions.begin() as session:
        session.get(RunCommand, old.run_id).lease_until = utcnow() - timedelta(
            seconds=1
        )
    restarted = Worker(worker.sessions, worker.executor)
    newer = restarted.claim()
    assert newer is not None and newer.token != old.token
    assert worker.apply(old, receipt) is False
    worker.fail(old, "STALE_WORKER_ERROR")
    assert restarted.apply(newer, restarted.perform(newer))
    assert len(fake.receipts) == 1
    assert run_get(client, run).json()["tracking_error"] is None


def test_active_lease_is_not_claimed_twice(client, execution):
    _, worker = execution
    run_request(client, create_batch(client).json()["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: worker.claim(), range(4)))
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
    assert {r.json()["dispatch_status"] for r in responses} == {"SKIPPED"}
    assert {r.json()["skip_reason"] for r in responses} == {"OVERLAP"}
    with worker.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(RunCommand)) == 1
        )


@pytest.mark.parametrize("policy,maximum", [("QUEUE", 1), ("PARALLEL", 2)])
def test_capacity_queue_and_manual_overflow(
    client, execution, policy, maximum
):
    _, worker = execution
    config = metadata(
        policy={
            "overlap": policy,
            "max_concurrent": maximum,
            "max_pending": 1,
        }
    )
    batch = create_batch(client, config=config).json()
    runs = [
        run_request(client, batch["id"], f"r{n}") for n in range(maximum + 2)
    ]
    assert [r.json()["dispatch_status"] for r in runs] == (
        ["PENDING"] * maximum + ["WAITING_CAPACITY", "SKIPPED"]
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


def test_queue_stays_blocked_on_unknown_submission(client, execution):
    fake, worker = execution
    config = metadata(policy={"overlap": "QUEUE"})
    batch = create_batch(client, config=config).json()
    first = run_request(client, batch["id"]).json()
    second = run_request(client, batch["id"], "second").json()
    fake.lose_response = True
    tick(worker, first)
    due(worker, second["id"])
    assert worker.tick() is False
    assert (
        run_get(client, second).json()["dispatch_status"] == "WAITING_CAPACITY"
    )
    tick(worker, first)
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, first)
    # Code completion frees capacity even before collecting the report/result.
    tick(worker, second)
    assert run_get(client, second).json()["dispatch_status"] == "SUBMITTED"
    assert len(fake.receipts) == 2


def test_poll_failure_is_not_code_failure_and_stale_state_is_rejected(
    client,
    execution,
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    tick(worker, run)
    fake.http_error = 503
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "RUNNING"
    assert detail["tracking_error"] == "EXECUTOR_HTTP_503"
    observed = detail["observed_at"]
    fake.http_error = None
    fake.version, fake.state = 1, "QUEUED"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "RUNNING"
    assert detail["observed_at"] == observed
    assert detail["tracking_error"] == "EXECUTOR_CONTRACT_MISMATCH"


def test_failed_execution_never_calls_retry(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.state, fake.version = "FAILED", 3
    tick(worker, run)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["failure_type"] == "USER_CODE_ERROR"
    assert detail["report_status"] == "NOT_APPLICABLE"
    assert "private source" not in json.dumps(detail)
    assert [method for method, _ in fake.requests].count("POST") == 1
    assert all(not path.endswith("/retry") for _, path in fake.requests)


@pytest.mark.parametrize(
    "projection,expected",
    [
        ("PENDING", "PENDING"),
        ("FAILED", "UNAVAILABLE"),
    ],
)
def test_terminal_execution_without_ready_notebook(
    client,
    execution,
    projection,
    expected,
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.state, fake.version = "SUCCEEDED", 3
    fake.projection, fake.artifact_ready = projection, False
    tick(worker, run)
    tick(worker, run)
    assert run_get(client, run).json()["result_status"] == expected
    if projection == "PENDING":
        fake.projection, fake.artifact_ready = "SUCCEEDED", True
        tick(worker, run)
        tick(worker, run)
        assert run_get(client, run).json()["result_status"] == "READY"


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
    assert detail["tracking_error"] == "EXECUTOR_CONTRACT_MISMATCH"
    assert detail["steps"][0]["result_ref"] is None
    assert detail["artifacts"] == []


def test_foreign_executor_context_is_not_applied(client, execution):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.owner_override = "bob"
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["execution_status"] == "QUEUED"
    assert detail["tracking_error"] == "EXECUTOR_CONTRACT_MISMATCH"


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
    assert (
        client.get(
            url, params={"cursor": str(uuid4())}, headers=token()
        ).status_code
        == 422
    )
    other = create_batch(client, key="other").json()
    assert (
        client.get(
            f"{API}/{other['id']}/runs/{runs[0]['id']}", headers=token()
        ).status_code
        == 404
    )


def test_acceptance_rolls_back_with_command_failure(client, execution):
    _, worker = execution
    batch = create_batch(client).json()
    from sqlalchemy import event

    def reject_command(*args):
        raise RuntimeError("simulated command persistence failure")

    event.listen(RunCommand, "before_insert", reject_command)
    try:
        with pytest.raises(RuntimeError):
            run_request(client, batch["id"])
    finally:
        event.remove(RunCommand, "before_insert", reject_command)
    with worker.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BatchRun)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RunStepBinding))
            == 0
        )
    assert run_request(client, batch["id"]).status_code == 202


@pytest.mark.parametrize("status", [301, 401, 409, 422, 429, 503])
def test_http_errors_do_not_release_unknown_submission(
    client, execution, status
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    fake.http_error = status
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["dispatch_status"] == "PENDING"
    assert detail["execution_status"] is None
    assert detail["tracking_error"] == f"EXECUTOR_HTTP_{status}"
    assert "private remote" not in json.dumps(detail)


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
                client.submit({})
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


def test_fifo_promotion_serializes_concurrent_workers(client, execution):
    fake, worker = execution
    config = metadata(policy={"overlap": "QUEUE"})
    batch = create_batch(client, config=config).json()
    runs = [run_request(client, batch["id"], f"r{n}").json() for n in range(4)]
    tick(worker, runs[0])
    fake.state, fake.version = "SUCCEEDED", 3
    tick(worker, runs[0])
    for run in runs[1:]:
        due(worker, run["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: worker.claim(), range(4)))
    promoted = [work for work in claims if work is not None]
    assert len(promoted) == 1
    assert str(promoted[0].run_id) == runs[1]["id"]
    assert worker.apply(promoted[0], worker.perform(promoted[0]))
    assert (
        run_get(client, runs[2]).json()["dispatch_status"]
        == "WAITING_CAPACITY"
    )


@pytest.mark.parametrize(
    "projection,expected",
    [
        ("PENDING", "PENDING"),
        ("FAILED", "UNAVAILABLE"),
    ],
)
def test_partial_notebook_is_not_mistaken_for_ready_result(
    client,
    execution,
    projection,
    expected,
):
    fake, worker = execution
    run = run_request(client, create_batch(client).json()["id"]).json()
    tick(worker, run)
    fake.state, fake.version = "FAILED", 3
    fake.projection = projection
    tick(worker, run)
    tick(worker, run)
    detail = run_get(client, run).json()
    assert detail["result_status"] == expected
    assert detail["artifacts"][0]["artifact_id"] == fake.artifact_id
