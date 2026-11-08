# pr-batch API — Coding Agent Reference

> This file is written for coding agents (Claude Code, Cursor, Copilot, etc.) that generate client code for pr-batch.
> Read it top to bottom before writing code. Exact request/response schemas are in `openapi.json` (same folder);
> when this file and `openapi.json` disagree on a schema, `openapi.json` wins. Behavior rules below are not in
> `openapi.json` and MUST be followed.
>
> Human-readable Korean docs: `00-common.md` … `04-runs.md` in the same folder.
> Source of truth: pr-batch `src/app/api/v1/*.py`, `src/app/schemas/*.py` (tenant-wide reads added 2026-11-08).

## 1. What pr-batch is

pr-batch stores Jupyter notebooks and runs them on a schedule or on demand through the Executor service.
The Executor does the actual execution; pr-batch keeps scripts, schedules, admits runs and tracks them.

```
Script (notebook) 1──* Version (immutable bytes, numbered 1,2,3…)
Batch  ──pins──▶ one Version + runtime + schedule + policy      (created DISABLED)
Batch  1──* Run (one execution; 1:1 with an Executor execution)
Batch  1──* SkippedWindow (scheduled occurrences that were not run)
```

Every resource is owned by **(tenant, user)**. tenant comes from the API key; user is the `X-User-Id` header.

## 2. Connection

| Item | Value |
| --- | --- |
| Base URL (cluster) | `http://pr-batch:8000` — ask the batch team per environment (dev/stg/prd) |
| Base URL (local compose) | `http://127.0.0.1:8100` |
| API prefix | `/api/v1` (all 24 public endpoints) |
| Local-only key | `bat_local_agent_0123456789abcdef0123456789ab` (local compose only, relay + admin, tenant `dtest-agent-service`) |
| Content type | `application/json`, except script uploads (`multipart/form-data`) |
| Times | Responses are UTC ISO 8601. Send timezone-aware datetimes. Schedule timezone is always `Asia/Seoul` |
| IDs | UUID strings |

Read the key and base URL from configuration/environment variables. Never hard-code a real key.

## 3. Headers — MUST / MUST NOT

| Header | Rule |
| --- | --- |
| `X-Api-Key` | MUST be sent on every `/api/v1` request, in every environment. Missing/unknown → 401 `UNAUTHENTICATED` |
| `X-User-Id` | MUST be sent on every write (POST/PATCH/DELETE) except the two admin endpoints. Optional on reads (GET): sent → that user's resources only; omitted → every resource of the tenant (see §3.1). Pattern after lower-casing: `^[a-z0-9][a-z0-9._-]{0,63}$` (SSO employee id). Missing on a write / invalid → 400 `INVALID_IDENTITY` |
| `Idempotency-Key` | MUST be sent on every POST/PATCH/DELETE except the two admin endpoints. 1–255 printable ASCII chars, no spaces (`^[!-~]+$`). Use `uuid4()`. Missing → 422 |
| `If-Match` | MUST be sent on: `PATCH /scripts/{id}`, `DELETE /scripts/{id}`, `PATCH /batches/{id}`, `DELETE /batches/{id}`, `POST /batches/{id}/enable`, `POST /batches/{id}/disable`. Value is the `ETag` header **including double quotes**, e.g. `"3"` |
| `X-Tenant-Id` | Send ONLY with a relay key. If you created a resource with `X-Tenant-Id`, you MUST send the same value on every later call for it, or you get 404. Non-relay key + this header → 403 |
| `X-Project-Id` | Optional; stored only on `POST /batches`; ignored elsewhere |

- MUST NOT put `tenant_id` (or any unknown field) in a JSON body → 422 `VALIDATION_ERROR`. All bodies reject unknown fields.
- MUST NOT forward `X-User-Id` from a browser or upstream caller unverified; build it from the user your service authenticated.

### 3.1 Read scope (same rule as the Executor)

| Read request | Returns |
| --- | --- |
| with `X-User-Id` | only that user's resources |
| without `X-User-Id` | every user's resources in the request tenant |
| list + query `user_id` | the tenant's resources of that user. With `X-User-Id` too, it MUST be the same value, else 403 `FORBIDDEN` |

- `user_id` is accepted on `GET /scripts` and `GET /batches`.
- Never crosses tenants. Responses carry `owner_id` (scripts, batches, runs).
- Writes always act as `X-User-Id` and only on that user's resources; a resource found by a tenant-wide read can be
  changed only with its owner's `X-User-Id`.
- If you are writing code for an end user, always send `X-User-Id` (end users must see only their own resources).
  Omit it only for an operator/"master" view that your gateway has authorized.

## 4. Idempotency and retries — MUST

1. Generate **one** `Idempotency-Key` per logical operation, before the first attempt.
2. On timeout, connection error, or 503, retry with the **same key and the same body**. The server replays the first
   response (same status code) without doing the work twice.
3. MUST NOT reuse a key for a different body → 409 `IDEMPOTENCY_CONFLICT`.
4. A replayed response reflects the state at the first call. GET the resource for the current state.
5. Keys are kept 7 days.

Retry policy for generated code:

| Response | Retry? |
| --- | --- |
| Network error / timeout | Yes, same key, exponential backoff |
| 503 (`DATABASE_UNAVAILABLE`, `STORAGE_UNAVAILABLE`, `INCONSISTENT_STATE`) | Yes, same key, backoff |
| 503 `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` | No (server misconfiguration; surface to operator) |
| 409 `REVISION_CONFLICT` | Re-GET the resource, then retry with the new `ETag` and a **new** Idempotency-Key |
| 400, 401, 403, 404, 413, 422, 500, other 409 | No |

## 5. Optimistic locking (If-Match)

- `GET /scripts/{id}` and `GET /batches/{id}` return header `ETag: "<revision>"` (same as body `revision`).
- Every write response also returns the new `ETag`. Use it for the next write instead of re-GETting.
- Stale value → 409 `REVISION_CONFLICT` (`details[0].context` has `expected` and `current`).
- Missing quotes (e.g. `If-Match: 3`) → 422 `INVALID_REVISION`. Missing header → 422 `VALIDATION_ERROR`.
- Uploading a new script version (201) increments the script's `revision`.

## 6. Pagination

List endpoints take `limit` (1–100, default 50) and `cursor`. Response: `{"items": [...], "next_cursor": str | null}`.
Loop until `next_cursor` is `null`, passing it back verbatim as `cursor`. MUST NOT parse or build cursors (→ 422 `INVALID_CURSOR`).

## 7. Errors

Every error has one shape:

```json
{"error": {"code": "REVISION_CONFLICT", "message": "Reload the resource.",
           "details": [{"location": ["header", "If-Match"], "type": "revision_conflict", "context": {"expected": 1, "current": 2}}]}}
```

- Branch on `error.code`, never on `message` or HTTP status alone.
- **Exception:** `POST /batches/{id}/runs` can return **409 with a RunView body** (`status: "SKIPPED"`,
  `skip_reason: "SKIPPED_CAPACITY"`), not an error body. Check for `"error"` in the body before parsing.
- 404 `NOT_FOUND` also means "belongs to another tenant/user" or "deleted". Existence is never revealed.

## 8. Endpoints (24)

`*` = needs `Idempotency-Key`; `M` = needs `If-Match`; paths are under `/api/v1`. Every `*` route needs `X-User-Id`; GET routes take it optionally (§3.1).

| # | Method | Path | Body | Success | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `/runtimes` | — | 200 `{"items":[{"type":"JUPYTER","profile":str}]}` | No `X-User-Id` |
| 2 | POST* | `/scripts` | multipart: `file` (.ipynb), `type`=`NOTEBOOK`, `name`? | 201 ScriptView | |
| 3 | GET | `/scripts` | `?limit&cursor&user_id` | 200 ScriptPage | |
| 4 | GET | `/scripts/{script_id}` | — | 200 ScriptView + ETag | |
| 5 | PATCH*M | `/scripts/{script_id}` | `{"name": str}` | 200 ScriptView | |
| 6 | DELETE*M | `/scripts/{script_id}` | — | 204 | 409 `SCRIPT_IN_USE`, `SCRIPT_HAS_ACTIVE_RUNS` |
| 7 | POST* | `/scripts/{script_id}/versions` | multipart: `file` | 201 VersionView, or 200 if identical bytes exist | |
| 8 | GET | `/scripts/{script_id}/versions` | `?limit&cursor` | 200 VersionPage | |
| 9 | GET | `/scripts/{script_id}/versions/{version_id}` | — | 200 VersionDetail | |
| 10 | GET | `/scripts/{script_id}/versions/{version_id}/content` | — | 200 raw `.ipynb` bytes | |
| 11 | POST* | `/batches` | CreateBatch | 201 BatchView (`enabled:false`) | |
| 12 | GET | `/batches` | `?limit&cursor&script_id&user_id` | 200 BatchPage | |
| 13 | GET | `/batches/{batch_id}` | — | 200 BatchView + ETag | |
| 14 | PATCH*M | `/batches/{batch_id}` | UpdateBatch (partial) | 200 BatchView | |
| 15 | DELETE*M | `/batches/{batch_id}` | — | 204 | 409 `BATCH_HAS_ACTIVE_RUNS` |
| 16 | POST*M | `/batches/{batch_id}/enable` | — | 200 BatchView | |
| 17 | POST*M | `/batches/{batch_id}/disable` | — | 200 BatchView | |
| 18 | GET | `/batches/{batch_id}/skipped-windows` | `?limit&cursor` | 200 SkippedPage | |
| 19 | POST | `/batches:disable-by-owner` | `{"owner_id": str, "reason": "USER_DEACTIVATED"}` | 200 `{"disabled": int}` | admin key only; ops use |
| 20 | POST | `/batches:disable-by-tenant` | `{"reason": "TENANT_REVOKED"}` | 200 `{"disabled": int}` | admin key only; `X-Tenant-Id` required; ops use |
| 21 | POST* | `/batches/{batch_id}/runs` | none or `{}` | 202 RunView (409 RunView when capacity full) | |
| 22 | GET | `/batches/{batch_id}/runs` | `?limit&cursor` | 200 RunPage | |
| 23 | GET | `/batches/{batch_id}/runs/{run_id}` | — | 200 RunDetail | Poll ≥ 5 s |
| 24 | POST* | `/batches/{batch_id}/runs/{run_id}/cancel` | — | 202 RunView | 409 `NOT_CANCELLABLE` |

Note the colon in #19/#20: `/batches:disable-by-owner` (no slash).

## 9. Key schemas (abridged — see `openapi.json`)

```jsonc
// CreateBatch
{
  "name": "string 1-255",                       // required
  "script_id": "uuid",                          // required
  "script_version_number": 2,                   // optional; omitted = latest at creation
  "runtime": {"type": "JUPYTER", "profile": "default"},  // required; must equal an item of GET /runtimes
  "schedule": {                                  // optional; null/omitted = manual-only (cannot enable)
    "definition": {"kind": "CRON", "expression": "0 8 * * MON-FRI"},
    "timezone": "Asia/Seoul",                    // optional, only allowed value
    "start_at": null, "end_at": null             // optional, tz-aware; start inclusive, end exclusive
  },
  "policy": {                                    // optional; default overlap SKIP, misfire SKIP
    "overlap": {"mode": "QUEUE", "max_queued": 100},
    "misfire": {"mode": "LATEST", "grace_seconds": 300}
  }
}
```

Schedule `definition` variants (discriminator `kind`):

```jsonc
{"kind": "ONCE", "at": "2026-12-01T09:00:00+09:00"}
{"kind": "INTERVAL", "seconds": 1800, "anchor": null}               // 60..31536000
{"kind": "CALENDAR", "frequency": "DAILY", "at": "08:00:00"}         // at = local time, no tz
{"kind": "CALENDAR", "frequency": "WEEKLY", "at": "08:00:00", "weekdays": ["MON", "FRI"]}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "month_days": [1, 15]}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "last_day": true}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "nth_weekdays": [{"weekday": "MON", "nth": 1}]}  // nth 1..5 or -1
{"kind": "CRON", "expression": "*/10 9-17 * * MON-FRI"}             // 5 fields or @hourly/@daily/@weekly/@monthly/@yearly; no seconds, no L/W/#
```

Policy variants — send ONLY the fields the chosen `mode` uses (others → 422):

| Section | mode | Fields (default) |
| --- | --- | --- |
| `overlap` | `SKIP` | — |
| | `QUEUE` | `max_queued` (100, 1..10000) |
| | `PARALLEL` | `max_concurrent` (required, 1..100), `max_queued` (100) |
| | `REPLACE` | — (cancels the running run, then runs the new one) |
| `misfire` | `SKIP` | `grace_seconds` (300, 1..86400) |
| | `LATEST` | `grace_seconds` |
| | `CATCH_UP` | `grace_seconds`, `max_runs` (10, 1..1000), `max_age_seconds` (86400, 60..31536000) |

UpdateBatch (PATCH) rules: send only fields to change; `{}` → 422; `name`/`script_id`/`runtime`/`policy` cannot be `null`;
`script_version_number: null` → re-pin latest; `schedule: null` → remove schedule (409 `SCHEDULE_REQUIRED` if enabled);
`policy` is merged per section, a different `mode` resets that section to the new mode's defaults (bad merge → 422 `INVALID_POLICY`).

BatchView fields to use for UI/logic: `owner_id`, `enabled`, `can_enable`, `can_disable`, `enable_blocked_reason`, `disabled_reason`
(`OWNER` | `USER_DEACTIVATED` | `TENANT_REVOKED` | null), `next_run_at`, `preview_runs` (≤ 5), `revision`, `config_number`.

RunView key fields: `id`, `owner_id`, `status`, `skip_reason`, `failure_type`, `cancel_requested`, `trigger` (`MANUAL`|`SCHEDULED`),
`scheduled_for`, `execution_id`, `notebook_status`, `result.notebook_artifact_id`, `completed_at`.

## 10. State machines

Batch:

```
created ──▶ disabled ──enable──▶ enabled ──disable──▶ disabled (disabled_reason=OWNER)
                 ▲                   │
                 │                   ├─ disable-by-owner  ▶ disabled (USER_DEACTIVATED)  manual run → 409 OWNER_DEACTIVATED
                 │                   └─ disable-by-tenant ▶ disabled (TENANT_REVOKED)    manual run → 409 TENANT_REVOKED
                 └── enable clears disabled_reason (the only way back)
```

- Enabling requires a schedule with a future occurrence, a configured Executor, and an available runtime.
  Check `can_enable` / `enable_blocked_reason` first instead of calling and catching.
- Enabling an already enabled batch (or disabling a disabled one) returns 200 and changes nothing.
- Enable schedules from now on; past occurrences while disabled are never back-filled.
- Manual runs work on disabled batches and on batches without a schedule (except the two deactivation reasons above).

Run:

```
WAITING ─▶ QUEUED ─▶ RUNNING ─▶ COLLECTING ─▶ COMPLETED | FAILED
   └──────────┴─────────┴─▶ CANCELLED (cancel allowed only in WAITING/QUEUED/RUNNING)
SKIPPED (policy decided not to run; final at creation)
```

Final statuses: `COMPLETED`, `FAILED`, `CANCELLED`, `SKIPPED`. Stop polling on these.
Cancel of QUEUED/RUNNING returns 202 with `cancel_requested: true` and the **same** status; poll until `CANCELLED`.

## 11. Recipes (Python, httpx)

```python
"""Minimal pr-batch client. Copy and adapt; keep the rules in comments."""

import os
import time
import uuid

import httpx

BATCH_URL = os.environ["BATCH_URL"]  # e.g. http://pr-batch:8000
BATCH_API_KEY = os.environ["BATCH_API_KEY"]  # never hard-code
FINAL = {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED"}
RETRYABLE_503 = {
    "DATABASE_UNAVAILABLE",
    "STORAGE_UNAVAILABLE",
    "INCONSISTENT_STATE",
}


class BatchError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.details = status, code, details


def client(user_id: str, tenant_id: str | None = None) -> httpx.Client:
    headers = {"X-Api-Key": BATCH_API_KEY, "X-User-Id": user_id}
    # Relay keys only; keep sending it for resources created with it.
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    return httpx.Client(
        base_url=f"{BATCH_URL}/api/v1", headers=headers, timeout=30
    )


def call(
    api: httpx.Client,
    method: str,
    path: str,
    *,
    write: bool = False,
    if_match: str | None = None,
    attempts: int = 4,
    **kwargs,
) -> httpx.Response:
    """One logical operation. Writes reuse ONE Idempotency-Key across retries."""
    headers = dict(kwargs.pop("headers", {}))
    if write:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    if if_match:
        headers["If-Match"] = if_match  # the ETag value, quotes included
    for attempt in range(attempts):
        try:
            response = api.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
            continue
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith(
                "application/json"
            )
            else None
        )
        error = body.get("error") if isinstance(body, dict) else None
        if (
            error
            and response.status_code == 503
            and error["code"] in RETRYABLE_503
            and attempt < attempts - 1
        ):
            time.sleep(2**attempt)
            continue
        if error:
            raise BatchError(
                response.status_code,
                error["code"],
                error["message"],
                error["details"],
            )
        return (
            response  # includes 409 RunView from POST /runs (no "error" key)
        )
    raise RuntimeError("unreachable")


def upload_notebook(
    api: httpx.Client, path: str, name: str | None = None
) -> dict:
    with open(path, "rb") as f:
        data = {"type": "NOTEBOOK", **({"name": name} if name else {})}
        files = {
            "file": (os.path.basename(path), f, "application/x-ipynb+json")
        }
        return call(
            api, "POST", "/scripts", write=True, data=data, files=files
        ).json()


def create_batch(
    api: httpx.Client, script_id: str, name: str, cron: str | None = None
) -> dict:
    runtime = call(api, "GET", "/runtimes").json()["items"][
        0
    ]  # pick an item verbatim
    body = {"name": name, "script_id": script_id, "runtime": runtime}
    if cron:
        body["schedule"] = {"definition": {"kind": "CRON", "expression": cron}}
    return call(
        api, "POST", "/batches", write=True, json=body
    ).json()  # enabled: false


def enable_batch(api: httpx.Client, batch_id: str) -> dict:
    current = call(api, "GET", f"/batches/{batch_id}")
    batch = current.json()
    if batch["enabled"]:
        return batch
    if not batch["can_enable"]:
        raise RuntimeError(f"cannot enable: {batch['enable_blocked_reason']}")
    return call(
        api,
        "POST",
        f"/batches/{batch_id}/enable",
        write=True,
        if_match=current.headers["ETag"],
    ).json()


def run_and_wait(
    api: httpx.Client, batch_id: str, poll_seconds: float = 5
) -> dict:
    run = call(api, "POST", f"/batches/{batch_id}/runs", write=True).json()
    while run["status"] not in FINAL:
        time.sleep(max(poll_seconds, 5))
        run = call(api, "GET", f"/batches/{batch_id}/runs/{run['id']}").json()
    return run  # COMPLETED → run["result"]["notebook_artifact_id"] (download from the Executor)


def rename_batch(api: httpx.Client, batch_id: str, name: str) -> dict:
    for _ in range(3):
        current = call(api, "GET", f"/batches/{batch_id}")
        try:
            return call(
                api,
                "PATCH",
                f"/batches/{batch_id}",
                write=True,
                if_match=current.headers["ETag"],
                json={"name": name},
            ).json()
        except BatchError as e:
            if e.code != "REVISION_CONFLICT":
                raise
    raise RuntimeError("batch keeps changing")


def iterate(api: httpx.Client, path: str, **params):
    cursor = None
    while True:
        page = call(
            api,
            "GET",
            path,
            params={**params, **({"cursor": cursor} if cursor else {})},
        ).json()
        yield from page["items"]
        if not (cursor := page["next_cursor"]):
            return


# Typical flow
with client("e12345") as api:
    script = upload_notebook(api, "daily.ipynb", "Daily line report")
    batch = create_batch(
        api, script["id"], "Weekdays 08:00", cron="0 8 * * MON-FRI"
    )
    result = run_and_wait(api, batch["id"])  # one manual run first
    if result["status"] == "COMPLETED":
        enable_batch(api, batch["id"])  # then turn the schedule on
```

Delete flow: a script cannot be deleted while a non-deleted batch uses it (409 `SCRIPT_IN_USE`); a batch cannot be
deleted while it has `WAITING`/`QUEUED`/`RUNNING`/`COLLECTING` runs (409 `BATCH_HAS_ACTIVE_RUNS`). Order: cancel or wait
for runs → delete batches → delete script. Each delete needs a fresh GET for its `ETag`.

## 12. Result notebook and events

- pr-batch does NOT serve result files. When a run is `COMPLETED`, download
  `result.notebook_artifact_id` from the **Executor**: `GET {EXECUTOR_URL}/api/v1/artifacts/{artifact_id}/content`
  with the calling service's own Executor key and the same `X-User-Id` (and `X-Tenant-Id` if the batch tenant is not the key default).
- Executor events for batch runs go to Redis stream `executor.events.<batch tenant>`, with `execution.metadata`
  containing `batch_id`, `script_id`, `script_version_id`, `run_id`. Treat an event only as a hint: re-read
  `GET /batches/{batch_id}/runs/{run_id}` to confirm. Ignore events with unknown `run_id`.
- Only read `notebook_status` after the run is final.

## 13. Notebook upload limits

| Rule | Default | Error |
| --- | --- | --- |
| Filename ends with `.ipynb`; valid nbformat v4 JSON | — | 422 `INVALID_NOTEBOOK` |
| File size ≤ 10 MiB | 10 MiB | 413 `UPLOAD_TOO_LARGE` / `REQUEST_TOO_LARGE` |
| Total cells ≤ 1000 (incl. markdown/raw) | 1000 | 422 `TOO_MANY_CELLS` |
| Non-empty code cells ≤ 100 (Executor limit) | 100 | 422 `TOO_MANY_CELLS` |
| Code cell source ≤ 1 MiB | 1 MiB | 422 `CELL_TOO_LARGE` |
| At least one non-empty code cell | — | 422 `EMPTY_NOTEBOOK` |
| Language (if declared) is Python | — | 422 `UNSUPPORTED_LANGUAGE` |

Each non-empty code cell becomes one Executor step, in order. Markdown/raw/blank cells are kept but not executed.
Uploading identical bytes as a new version returns the existing version with 200 (no new version).

## 14. Common mistakes — checklist for generated code

- [ ] `X-Api-Key` sent on every call (also in local/dev).
- [ ] `X-User-Id` sent on every write and on end-user reads; omitted only for an authorized tenant-wide view; never taken from an unverified upstream header.
- [ ] New `Idempotency-Key` per operation, **same** key across retries of that operation.
- [ ] `If-Match` value copied from `ETag` **with quotes**; refreshed after every write.
- [ ] No `tenant_id`, `enabled`, `id` or other unknown fields in JSON bodies.
- [ ] Relay callers keep sending the same `X-Tenant-Id` for resources they created with it (else 404).
- [ ] `POST /runs` 409 parsed as RunView (`SKIPPED_CAPACITY`), not as an error.
- [ ] New batch is disabled; call `enable` explicitly, and only when `can_enable` is true.
- [ ] A new script version does not update existing batches; PATCH `script_version_number` (or `null` for latest).
- [ ] Runtime object copied verbatim from `GET /runtimes`.
- [ ] Policy section contains only fields of its `mode`.
- [ ] Polling interval ≥ 5 s; stop on `COMPLETED`/`FAILED`/`CANCELLED`/`SKIPPED`.
- [ ] Branch on `error.code`, not on `message`.
- [ ] Tenant-wide list filters use query `user_id`; never send a `user_id` different from `X-User-Id`.
- [ ] Result notebook downloaded from the Executor, not pr-batch.
- [ ] Admin endpoints (`:disable-by-owner`, `:disable-by-tenant`) are not used by end-user features.
