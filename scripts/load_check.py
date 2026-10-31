"""Load check against a running stack (run in the Compose `load` service).

    python -m scripts.load_check BATCHES SLEEP API_THREADS UPLOADS

1. Registers BATCHES batches that all fire ONCE at the same moment (a
   09:00-style burst), each running a notebook that sleeps SLEEP seconds.
2. While they run, API_THREADS threads list batches and runs, and UPLOADS
   notebook uploads go in parallel, recording latency and 5xx answers.
3. Samples the pr-batch role's PostgreSQL connections every 0.5s.

Prints one JSON summary: pr-batch lags (scheduled -> submitted, Executor
finished -> pr-batch completed), Executor queueing (submitted -> started),
API latency percentiles, error counts and peak DB connections. Deletes
what it created. Settings under test come from the app's environment.
"""

import json
import os
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError

import psycopg
from scripts.docker_smoke import multipart
from scripts.multi_instance_check import notebook

API = os.environ.get("LOAD_API_URL", "http://app:8000") + "/api/v1"
FINAL = {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED"}
OWNER = f"load-{uuid.uuid4().hex[:8]}"
AUTH = {"X-User-Id": OWNER}


def request(method, url, headers=None, body=None, timeout=60):
    """(status, json, seconds); never raises for HTTP errors."""
    started = time.monotonic()
    req = urllib.request.Request(
        url, data=body, method=method, headers=AUTH | (headers or {})
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
            status = response.status
    except HTTPError as exc:
        content, status = exc.read(), exc.code
    except (URLError, TimeoutError):
        return 0, {}, time.monotonic() - started
    elapsed = time.monotonic() - started
    try:
        data = json.loads(content) if content else {}
    except ValueError:
        data = {}
    return status, data, elapsed


def must(result, what):
    status, data, _ = result
    if status >= 300 or status == 0:
        sys.exit(f"{what} -> {status}: {json.dumps(data)[:300]}")
    return data


def upload(sleep):
    body, content_type = multipart(
        {
            "type": (None, "text/plain", b"NOTEBOOK"),
            "file": ("load.ipynb", "application/json", notebook(sleep)),
        }
    )
    return request(
        "POST",
        f"{API}/scripts",
        {"Content-Type": content_type, "Idempotency-Key": uuid.uuid4().hex},
        body,
    )


def register(script_id, at, index):
    registration = {
        "name": f"load-{index}",
        "script_id": script_id,
        "runtime": {"type": "JUPYTER", "profile": "default"},
        "schedule": {"definition": {"kind": "ONCE", "at": at}},
    }
    batch = must(
        request(
            "POST",
            f"{API}/batches",
            {
                "Content-Type": "application/json",
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json.dumps(registration).encode(),
        ),
        "register",
    )
    must(
        request(
            "POST",
            f"{API}/batches/{batch['id']}/enable",
            {"Idempotency-Key": uuid.uuid4().hex, "If-Match": '"1"'},
        ),
        "enable",
    )
    return batch["id"]


class ConnectionSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        url = os.environ["DATABASE_URL"].replace("+psycopg", "")
        self.conninfo = url
        self.stop = threading.Event()
        self.peak = 0
        self.samples = []

    def run(self):
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            while not self.stop.is_set():
                row = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE usename = current_user AND pid <> pg_backend_pid()"
                ).fetchone()
                count = row[0] if row else 0
                self.samples.append(count)
                self.peak = max(self.peak, count)
                self.stop.wait(0.5)


def api_load(stop, batch_ids, record):
    while not stop.is_set():
        record("list", request("GET", f"{API}/batches?limit=50"))
        if batch_ids:
            target = batch_ids[int(time.monotonic() * 1000) % len(batch_ids)]
            record("runs", request("GET", f"{API}/batches/{target}/runs"))


def seconds(a, b):
    if not a or not b:
        return None
    return (
        datetime.fromisoformat(b) - datetime.fromisoformat(a)
    ).total_seconds()


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None

    def at(q):
        return round(values[min(len(values) - 1, int(q * len(values)))], 3)

    return {
        "n": len(values),
        "p50": at(0.5),
        "p95": at(0.95),
        "max": round(values[-1], 3),
        "mean": round(statistics.fmean(values), 3),
    }


def main(batches, sleep, api_threads, uploads):
    sampler = ConnectionSampler()
    sampler.start()
    script = must(upload(sleep), "upload")
    lead = max(20, batches // 5)  # time to register before the burst
    at = (datetime.now(UTC) + timedelta(seconds=lead)).isoformat()
    with ThreadPoolExecutor(8) as pool:
        batch_ids = list(
            pool.map(lambda i: register(script["id"], at, i), range(batches))
        )

    latencies = {"list": [], "runs": [], "upload": []}
    errors = {}
    lock = threading.Lock()

    def record(kind, result):
        status, _, elapsed = result
        with lock:
            latencies[kind].append(elapsed)
            if status == 0 or status >= 500:
                errors[f"{kind}:{status}"] = (
                    errors.get(f"{kind}:{status}", 0) + 1
                )

    fire = datetime.fromisoformat(at)
    time.sleep(max(0, (fire - datetime.now(UTC)).total_seconds()))
    stop = threading.Event()
    workers = [
        threading.Thread(target=api_load, args=(stop, batch_ids, record))
        for _ in range(api_threads)
    ]
    for worker in workers:
        worker.start()
    uploaded = []
    with ThreadPoolExecutor(max(1, min(uploads, 20))) as pool:
        for result in pool.map(lambda _: upload(sleep), range(uploads)):
            record("upload", result)
            if result[0] in (200, 201):
                uploaded.append(result[1])

    deadline = time.monotonic() + sleep * 4 + 120 + batches
    runs = []
    while time.monotonic() < deadline:
        runs = []
        for batch_id in batch_ids:
            status, data, _ = request("GET", f"{API}/batches/{batch_id}/runs")
            runs += data.get("items", []) if status == 200 else []
        if len(runs) >= batches and all(r["status"] in FINAL for r in runs):
            break
        time.sleep(3)
    stop.set()
    for worker in workers:
        worker.join()
    sampler.stop.set()

    statuses = {}
    for run in runs:
        statuses[run["status"]] = statuses.get(run["status"], 0) + 1
    summary = {
        "settings": {
            key: os.environ.get(key)
            for key in (
                "DB_POOL_SIZE",
                "DB_MAX_OVERFLOW",
                "TASK_CONCURRENCY",
                "IDLE_MAX_SECONDS",
                "SYNC_INITIAL_SECONDS",
                "SYNC_MAX_SECONDS",
            )
        },
        "load": {
            "batches": batches,
            "sleep": sleep,
            "api_threads": api_threads,
            "uploads": uploads,
        },
        "runs": statuses,
        "settled": len(runs) >= batches
        and all(r["status"] in FINAL for r in runs),
        "lag_scheduled_to_submitted": percentiles(
            seconds(r["scheduled_for"], r["submitted_at"]) for r in runs
        ),
        "executor_submitted_to_started": percentiles(
            seconds(r["submitted_at"], r["started_at"]) for r in runs
        ),
        "lag_finished_to_completed": percentiles(
            seconds(r["finished_at"], r["completed_at"]) for r in runs
        ),
        "api_seconds": {k: percentiles(v) for k, v in latencies.items()},
        "api_errors": errors,
        "db_connections_peak": sampler.peak,
        "db_connections_mean": round(statistics.fmean(sampler.samples), 2)
        if sampler.samples
        else None,
    }
    print(json.dumps(summary, indent=2))

    for batch_id in batch_ids:
        delete(f"{API}/batches/{batch_id}")
    for created in [script, *uploaded]:
        delete(f"{API}/scripts/{created['id']}")


def delete(url):
    """Soft delete at the current revision (If-Match is required)."""
    status, current, _ = request("GET", url)
    if status == 200:
        request(
            "DELETE",
            url,
            {
                "Idempotency-Key": uuid.uuid4().hex,
                "If-Match": f'"{current["revision"]}"',
            },
        )


if __name__ == "__main__":
    main(*(int(v) for v in sys.argv[1:5]))
