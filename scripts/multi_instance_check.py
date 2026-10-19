"""Multi-replica check driven against the API (run in the smoke container).

schedule OWNER COUNT DELAY SLEEP  register COUNT batches firing once,
                                  DELAY seconds from now, each running
                                  a notebook that sleeps SLEEP seconds
verify OWNER TIMEOUT              wait until every run is final, then
                                  require exactly one completed run per
                                  batch with a distinct Executor run
"""

import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from scripts.docker_smoke import call, multipart

from app.core.config import Settings

API = "http://app:8000/api/v1"
FINAL = {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED"}


def auth(owner: str) -> dict[str, str]:
    settings = Settings()
    now = int(time.time())
    claims = {
        "sub": str(uuid.uuid5(uuid.NAMESPACE_URL, f"user:{owner}")),
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
        "iat": now,
        "exp": now + 300,
        "scope": "batch:manage",
    }
    secret = settings.auth_secret.get_secret_value()
    token = jwt.encode(claims, secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def notebook(sleep: int) -> bytes:
    cells = [
        f"import time\ntime.sleep({sleep})",
        "print('done')",
    ]
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {
                    "cell_type": "code",
                    "id": f"c{i}",
                    "metadata": {},
                    "execution_count": None,
                    "outputs": [],
                    "source": source,
                }
                for i, source in enumerate(cells)
            ],
        }
    ).encode()


def schedule(owner: str, count: int, delay: int, sleep: int) -> None:
    nonce = uuid.uuid4().hex
    body, content_type = multipart(
        {
            "type": (None, "text/plain", b"NOTEBOOK"),
            "file": ("sleep.ipynb", "application/json", notebook(sleep)),
        }
    )
    script = call(
        "POST",
        f"{API}/scripts",
        auth(owner) | {"Content-Type": content_type, "Idempotency-Key": nonce},
        body,
    )
    at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
    for index in range(count):
        registration = {
            "name": f"multi-{index}",
            "script_id": script["id"],
            "runtime": {"type": "JUPYTER", "profile": "default"},
            "schedule": {"definition": {"kind": "ONCE", "at": at}},
        }
        batch = call(
            "POST",
            f"{API}/batches",
            auth(owner)
            | {
                "Content-Type": "application/json",
                "Idempotency-Key": f"{nonce}-{index}",
            },
            json.dumps(registration).encode(),
        )
        call(
            "POST",
            f"{API}/batches/{batch['id']}/enable",
            auth(owner)
            | {"Idempotency-Key": f"{nonce}-e{index}", "If-Match": '"1"'},
        )
    print(f"scheduled {count} batches at {at}")


def runs_by_batch(owner: str) -> dict[str, list[dict]]:
    batches = call("GET", f"{API}/batches?limit=100", auth(owner))["items"]
    return {
        b["id"]: call(
            "GET", f"{API}/batches/{b['id']}/runs?limit=100", auth(owner)
        )["items"]
        for b in batches
    }


def verify(owner: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while True:
        runs = runs_by_batch(owner)
        statuses = [r["status"] for items in runs.values() for r in items]
        pending = [s for s in statuses if s not in FINAL]
        if runs and not pending and all(runs.values()):
            break
        if time.monotonic() > deadline:
            counts = {s: statuses.count(s) for s in set(statuses)}
            sys.exit(f"FAILED: not settled after {timeout}s: {counts}")
        time.sleep(3)
    problems = []
    executions = []
    for batch_id, items in runs.items():
        if len(items) != 1:
            problems.append(f"{batch_id}: {len(items)} runs")
        for run in items:
            if run["status"] != "COMPLETED":
                problems.append(f"{run['id']}: {run['status']}")
            executions.append(run["execution_id"])
    if len(set(executions)) != len(executions):
        problems.append("duplicate Executor executions")
    print(
        f"batches={len(runs)} runs={len(executions)} "
        f"distinct_executions={len(set(executions))}"
    )
    if problems:
        sys.exit("FAILED: " + "; ".join(problems[:10]))
    print("multi-instance check passed")


if __name__ == "__main__":
    command, owner, *rest = sys.argv[1:]
    if command == "schedule":
        schedule(owner, *(int(v) for v in rest))
    else:
        verify(owner, int(rest[0]))
