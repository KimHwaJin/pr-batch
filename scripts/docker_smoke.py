"""Upload a sample notebook script, run it once and wait for the result.

Runs inside the Compose `smoke` service and exits non-zero on any failure.
"""

import json
import os
import sys
import time
import urllib.request
import uuid
from urllib.error import HTTPError

import jwt

from app.core.config import Settings

FINAL = {"COMPLETED", "FAILED", "REPORT_FAILED", "CANCELLED", "SKIPPED"}
NOTEBOOK = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {},
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": "# Smoke"},
    ]
    + [
        {
            "cell_type": "code",
            "id": f"c{index}",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": source,
        }
        for index, source in enumerate(
            [
                "lines = {'A': [98.1, 97.5, 99.0], 'B': [91.2, 85.4]}",
                "for k, v in lines.items():\n    print(k, min(v), max(v))",
                "{k: round(max(v) - min(v), 2) for k, v in lines.items()}",
            ]
        )
    ],
}


def token(settings: Settings) -> str:
    now = int(time.time())
    claims = {
        "sub": "docker-smoke",
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
        "iat": now,
        "exp": now + 300,
        "scope": "batch:manage",
    }
    return jwt.encode(
        claims, settings.auth_secret.get_secret_value(), algorithm="HS256"
    )


def call(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> dict:
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        sys.exit(f"{method} {url} -> {exc.code}: {exc.read()[:500]!r}")


def multipart(fields: dict[str, tuple[str | None, str, bytes]]):
    boundary = uuid.uuid4().hex
    parts = []
    for name, (filename, content_type, data) in fields.items():
        disposition = f'form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        parts.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            f"Content-Type: {content_type}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> None:
    settings = Settings()
    api = os.environ.get("SMOKE_API_URL", "http://127.0.0.1:8000")
    base = f"{api}/api/v1/batches"
    kernel = settings.allowed_kernel_profiles[0]
    nonce = uuid.uuid4().hex
    auth = {"Authorization": f"Bearer {token(settings)}"}
    body, content_type = multipart(
        {
            "name": (None, "text/plain", f"smoke-{nonce[:8]}".encode()),
            "type": (None, "text/plain", b"NOTEBOOK"),
            "file": (
                "smoke.ipynb",
                "application/x-ipynb+json",
                json.dumps(NOTEBOOK).encode(),
            ),
        }
    )
    script = call(
        "POST",
        f"{api}/api/v1/scripts",
        auth
        | {"Content-Type": content_type, "Idempotency-Key": f"nb-{nonce}"},
        body,
    )
    cells = script["latest_version"]["cells"]
    print(f"uploaded script {script['id']} (cells {cells})")
    registration = {
        "name": f"docker-smoke-{nonce[:8]}",
        "script_id": script["id"],
        "kernel_profile": kernel,
        "schedule": {
            "definition": {"kind": "CRON", "expression": "0 8 * * *"}
        },
        "policy": {"overlap": "QUEUE", "max_pending": 10},
    }
    batch = call(
        "POST",
        base,
        auth
        | {
            "Content-Type": "application/json",
            "Idempotency-Key": f"reg-{nonce}",
        },
        json.dumps(registration).encode(),
    )
    print(f"registered batch {batch['id']} (kernel {kernel})")
    run = call(
        "POST",
        f"{base}/{batch['id']}/runs",
        auth | {"Idempotency-Key": f"run-{nonce}"},
    )
    print(f"accepted run {run['id']} ({run['status']})")
    deadline = time.monotonic() + 180
    while True:
        run = call("GET", f"{base}/{batch['id']}/runs/{run['id']}", auth)
        status = run["status"]
        if status in FINAL:
            break
        if time.monotonic() > deadline:
            sys.exit(f"timed out waiting for run (last status {status})")
        time.sleep(2)
    summary = {
        key: run[key]
        for key in (
            "status",
            "execution_id",
            "execution_status",
            "failure_type",
            "last_error",
        )
    }
    print(json.dumps(summary, indent=2))
    notebooks = [a for a in run["artifacts"] if a["type"] == "NOTEBOOK"]
    steps_ok = all(step["status"] == "SUCCEEDED" for step in run["steps"])
    if status != "COMPLETED" or not steps_ok or not notebooks:
        sys.exit("smoke test FAILED")
    print(
        f"smoke test passed; notebook artifact {notebooks[0]['artifact_id']}"
    )


if __name__ == "__main__":
    main()
