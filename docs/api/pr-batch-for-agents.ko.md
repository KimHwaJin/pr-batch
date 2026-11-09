# pr-batch API — 코딩 에이전트용 레퍼런스 (한국어)

> pr-batch 클라이언트 코드를 만드는 코딩 에이전트(Claude Code, Cursor, Copilot 등)를 위한 문서다.
> 코드를 쓰기 전에 처음부터 끝까지 읽는다. 요청·응답 스키마는 같은 폴더의 `openapi.json`이 정본이다.
> 스키마가 이 문서와 `openapi.json`에서 다르면 `openapi.json`을 따른다. 아래의 동작 규칙은 `openapi.json`에 없으며
> **반드시** 지켜야 한다.
>
> 영문판: [pr-batch-for-agents.md](pr-batch-for-agents.md) (내용 같음). 사람이 읽는 상세 명세: 같은 폴더의 `00-common.md` … `04-runs.md`.
> 모든 내용은 쿠버네티스에 배포된 pr-batch 서비스를 호출하는 경우를 기준으로 한다.

## 1. pr-batch란

Jupyter 노트북을 보관하고, 정해진 일정이나 요청에 따라 Executor 서비스로 실행하는 서비스다.
실제 실행은 Executor가 하고, pr-batch는 스크립트 보관, 일정 관리, 실행 접수와 추적을 맡는다.

```
Script (노트북) 1──* Version (바뀌지 않는 원본, 번호 1,2,3…)
Batch  ──고정──▶ Version 하나 + runtime + schedule + policy      (비활성 상태로 생성)
Batch  1──* Run (실행 한 번, Executor execution과 1:1)
Batch  1──* SkippedWindow (실행하지 않고 건너뛴 예약 구간)
```

모든 자원은 **(tenant, user)** 소유다. tenant는 API key로 정해지고, user는 `X-User-Id` 헤더다.

## 2. 연결

| 항목 | 값 |
| --- | --- |
| 실행 위치 | 쿠버네티스 내부 Service `pr-batch`, 포트 8000. Ingress가 없으므로 클러스터 안에서 호출한다 |
| Base URL | 같은 네임스페이스 `http://pr-batch:8000`, 다른 네임스페이스 `http://pr-batch.<namespace>.svc.cluster.local:8000`. 네임스페이스와 key는 환경(dev/stg/prd)마다 다르므로 배치팀에 확인한다 |
| API 접두사 | `/api/v1` (공개 엔드포인트 24개 모두) |
| Swagger / OpenAPI | 모든 환경에서 꺼져 있다. 이 폴더의 `openapi.json`을 쓰고, 서버에서 `/openapi.json`을 받으려 하지 않는다 |
| Content type | `application/json`. 스크립트 업로드만 `multipart/form-data` |
| 시각 | 응답은 UTC ISO 8601. 보낼 때는 시간대가 있는 datetime을 쓴다. 일정 시간대는 항상 `Asia/Seoul` |
| ID | UUID 문자열 |

`BATCH_URL`과 `BATCH_API_KEY`는 환경 변수에서 읽는다(key는 호출 서비스의 배포 설정으로 받는다). key를 코드에 직접 쓰지 않는다.

## 3. 헤더 — 반드시 / 절대 금지

| 헤더 | 규칙 |
| --- | --- |
| `X-Api-Key` | 모든 환경에서 모든 `/api/v1` 요청에 **반드시** 보낸다. 없거나 모르는 key → 401 `UNAUTHENTICATED` |
| `X-User-Id` | 운영 API 2개를 뺀 모든 쓰기(POST/PATCH/DELETE)에 **반드시** 보낸다. 읽기(GET)는 선택: 보내면 그 사용자 자원만, 빼면 tenant의 모든 자원(§3.1). 소문자로 바꾼 뒤 형식 `^[a-z0-9][a-z0-9._-]{0,63}$` (SSO 사번). 쓰기에 없거나 형식이 틀리면 → 400 `INVALID_IDENTITY` |
| `Idempotency-Key` | 운영 API 2개를 뺀 모든 POST/PATCH/DELETE에 **반드시** 보낸다. 공백 없는 ASCII 출력 문자 1~255자(`^[!-~]+$`). `uuid4()`를 쓴다. 없으면 → 422 |
| `If-Match` | `PATCH /scripts/{id}`, `DELETE /scripts/{id}`, `PATCH /batches/{id}`, `DELETE /batches/{id}`, `POST /batches/{id}/enable`, `POST /batches/{id}/disable`에 **반드시** 보낸다. 값은 `ETag` 헤더를 **큰따옴표까지 그대로**, 예: `"3"` |
| `X-Tenant-Id` | relay key일 때**만** 보낸다. `X-Tenant-Id`로 만든 자원은 이후 모든 호출에 **같은 값**을 보내야 하며, 빼면 404. relay가 아닌 key로 보내면 → 403 |
| `X-Project-Id` | 선택. `POST /batches`에서만 저장하고 다른 곳에서는 무시한다 |

- JSON 본문에 `tenant_id`(또는 모르는 필드)를 **넣지 않는다** → 422 `VALIDATION_ERROR`. 모든 본문은 모르는 필드를 거부한다.
- 브라우저나 상위 호출자가 보낸 `X-User-Id`를 검증 없이 **넘기지 않는다**. 호출 서비스가 인증한 사용자로 새로 만든다.

### 3.1 조회 범위 (Executor와 같은 규칙)

| 읽기 요청 | 결과 |
| --- | --- |
| `X-User-Id` 있음 | 그 사용자 자원만 |
| `X-User-Id` 없음 | 요청 tenant의 모든 사용자 자원 |
| 목록 + 쿼리 `user_id` | tenant 안에서 그 사용자 자원만. `X-User-Id`도 보내면 **같은 값**이어야 하고, 다르면 403 `FORBIDDEN` |

- `user_id`는 `GET /scripts`, `GET /batches`에서 받는다(대소문자 구분 없음, 형식 오류 → 422 `VALIDATION_ERROR`).
- tenant를 넘어가지 않는다. 응답의 소유자는 `user_id` 필드다(스크립트, 배치, 실행).
- 쓰기는 항상 `X-User-Id` 사용자로, 그 사용자 자원에만 한다. tenant 전체 조회로 찾은 남의 자원은 그 소유자의
  `X-User-Id`로만 바꿀 수 있다.
- 최종 사용자용 코드라면 `X-User-Id`를 항상 보낸다(사용자는 자기 자원만 봐야 한다).
  게이트웨이가 허용한 운영자·"마스터" 화면에서만 뺀다.

## 4. 멱등성과 재시도 — 반드시

1. 논리적 작업 하나마다 첫 시도 전에 `Idempotency-Key`를 **하나** 만든다.
2. 타임아웃, 연결 오류, 503이면 **같은 키와 같은 본문**으로 재시도한다. 서버는 일을 두 번 하지 않고 첫 응답(상태 코드 포함)을
   그대로 돌려준다.
3. 다른 본문에 같은 키를 **다시 쓰지 않는다** → 409 `IDEMPOTENCY_CONFLICT`.
4. 재전송된 응답은 첫 호출 시점의 상태다. 현재 상태는 GET으로 읽는다.
5. 키는 7일 동안 보관된다.

생성 코드의 재시도 정책:

| 응답 | 재시도 |
| --- | --- |
| 네트워크 오류 / 타임아웃 | 예. 같은 키, 지수 백오프 |
| 503 (`DATABASE_UNAVAILABLE`, `STORAGE_UNAVAILABLE`, `INCONSISTENT_STATE`) | 예. 같은 키, 백오프 |
| 503 `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` | 아니오 (서버 설정 문제, 운영자에게 알림) |
| 409 `REVISION_CONFLICT` | 자원을 다시 GET한 뒤 새 `ETag`와 **새** Idempotency-Key로 재시도 |
| 400, 401, 403, 404, 413, 422, 500, 그 밖의 409 | 아니오 |

## 5. 낙관적 잠금 (If-Match)

- `GET /scripts/{id}`와 `GET /batches/{id}`는 `ETag: "<revision>"` 헤더를 준다(본문 `revision`과 같은 값).
- 모든 쓰기 응답도 새 `ETag`를 준다. 다음 쓰기에는 다시 GET하지 말고 이 값을 쓴다.
- 오래된 값 → 409 `REVISION_CONFLICT` (`details[0].context`에 `expected`, `current`).
- 따옴표 누락(예: `If-Match: 3`) → 422 `INVALID_REVISION`. 헤더 없음 → 422 `VALIDATION_ERROR`.
- 스크립트 새 버전 업로드(201)는 스크립트의 `revision`을 올린다.

## 6. 페이지네이션

목록 엔드포인트는 `limit`(1~100, 기본 50)과 `cursor`를 받는다. 응답: `{"items": [...], "next_cursor": str | null}`.
`next_cursor`가 `null`이 될 때까지 받은 값을 그대로 `cursor`에 넣어 반복한다. cursor를 해석하거나 만들지 **않는다**(→ 422 `INVALID_CURSOR`).

## 7. 오류

모든 오류는 한 가지 모양이다.

```json
{"error": {"code": "REVISION_CONFLICT", "message": "Reload the resource.",
           "details": [{"location": ["header", "If-Match"], "type": "revision_conflict", "context": {"expected": 1, "current": 2}}]}}
```

- 분기는 `error.code`로 한다. `message`나 HTTP 상태만으로 분기하지 않는다.
- **예외:** `POST /batches/{id}/runs`는 오류 본문이 아니라 **RunView 본문과 함께 409**(`status: "SKIPPED"`,
  `skip_reason: "SKIPPED_CAPACITY"`)를 돌려줄 수 있다. 파싱 전에 본문에 `"error"`가 있는지 확인한다.
- 404 `NOT_FOUND`는 "다른 tenant·사용자 자원"이나 "삭제됨"도 뜻한다. 존재 여부는 드러나지 않는다.

## 8. 엔드포인트 (24개)

`*` = `Idempotency-Key` 필요, `M` = `If-Match` 필요. 경로는 `/api/v1` 아래다. `*` 경로는 모두 `X-User-Id`가 필요하고, GET은 선택이다(§3.1).

| # | Method | 경로 | 본문 | 성공 | 비고 |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `/runtimes` | — | 200 `{"items":[{"type":"JUPYTER","profile":str}]}` | `X-User-Id` 불필요 |
| 2 | POST* | `/scripts` | multipart: `file` (.ipynb), `type`=`NOTEBOOK`, `name`? | 201 ScriptView | |
| 3 | GET | `/scripts` | `?limit&cursor&user_id` | 200 ScriptPage | |
| 4 | GET | `/scripts/{script_id}` | — | 200 ScriptView + ETag | |
| 5 | PATCH*M | `/scripts/{script_id}` | `{"name": str}` | 200 ScriptView | |
| 6 | DELETE*M | `/scripts/{script_id}` | — | 204 | 409 `SCRIPT_IN_USE`, `SCRIPT_HAS_ACTIVE_RUNS` |
| 7 | POST* | `/scripts/{script_id}/versions` | multipart: `file` | 201 VersionView, 같은 바이트가 이미 있으면 200 | |
| 8 | GET | `/scripts/{script_id}/versions` | `?limit&cursor` | 200 VersionPage | |
| 9 | GET | `/scripts/{script_id}/versions/{version_id}` | — | 200 VersionDetail | |
| 10 | GET | `/scripts/{script_id}/versions/{version_id}/content` | — | 200 `.ipynb` 원본 바이트 | |
| 11 | POST* | `/batches` | CreateBatch | 201 BatchView (`enabled:false`) | |
| 12 | GET | `/batches` | `?limit&cursor&script_id&user_id` | 200 BatchPage | |
| 13 | GET | `/batches/{batch_id}` | — | 200 BatchView + ETag | |
| 14 | PATCH*M | `/batches/{batch_id}` | UpdateBatch (부분 수정) | 200 BatchView | |
| 15 | DELETE*M | `/batches/{batch_id}` | — | 204 | 409 `BATCH_HAS_ACTIVE_RUNS` |
| 16 | POST*M | `/batches/{batch_id}/enable` | — | 200 BatchView | |
| 17 | POST*M | `/batches/{batch_id}/disable` | — | 200 BatchView | |
| 18 | GET | `/batches/{batch_id}/skipped-windows` | `?limit&cursor` | 200 SkippedPage | |
| 19 | POST | `/batches:disable-by-owner` | `{"owner_id": str, "reason": "USER_DEACTIVATED"}` | 200 `{"disabled": int}` | admin key 전용, 운영용 |
| 20 | POST | `/batches:disable-by-tenant` | `{"reason": "TENANT_REVOKED"}` | 200 `{"disabled": int}` | admin key 전용, `X-Tenant-Id` 필수, 운영용 |
| 21 | POST* | `/batches/{batch_id}/runs` | 없음 또는 `{}` | 202 RunView (자리가 없으면 409 RunView) | |
| 22 | GET | `/batches/{batch_id}/runs` | `?limit&cursor` | 200 RunPage | |
| 23 | GET | `/batches/{batch_id}/runs/{run_id}` | — | 200 RunDetail | 5초 이상 간격으로 폴링 |
| 24 | POST* | `/batches/{batch_id}/runs/{run_id}/cancel` | — | 202 RunView | 409 `NOT_CANCELLABLE` |

19·20번 경로의 콜론에 주의한다: `/batches:disable-by-owner` (슬래시 아님).

## 9. 주요 스키마 (요약 — 전체는 `openapi.json`)

```jsonc
// CreateBatch
{
  "name": "string 1-255",                       // 필수
  "script_id": "uuid",                          // 필수
  "script_version_number": 2,                   // 선택. 생략하면 생성 시점의 최신 버전
  "runtime": {"type": "JUPYTER", "profile": "default"},  // 필수. GET /runtimes 항목과 같아야 함
  "schedule": {                                  // 선택. null/생략 = 수동 실행 전용(활성화 불가)
    "definition": {"kind": "CRON", "expression": "0 8 * * MON-FRI"},
    "timezone": "Asia/Seoul",                    // 선택. 허용 값은 이것 하나
    "start_at": null, "end_at": null             // 선택. 시간대 포함. 시작 포함, 끝 미포함
  },
  "policy": {                                    // 선택. 기본 overlap SKIP, misfire SKIP
    "overlap": {"mode": "QUEUE", "max_queued": 100},
    "misfire": {"mode": "LATEST", "grace_seconds": 300}
  }
}
```

일정 `definition` 종류 (구분 필드 `kind`):

```jsonc
{"kind": "ONCE", "at": "2026-12-01T09:00:00+09:00"}
{"kind": "INTERVAL", "seconds": 1800, "anchor": null}               // 60..31536000
{"kind": "CALENDAR", "frequency": "DAILY", "at": "08:00:00"}         // at = 현지 시각, 시간대 없음
{"kind": "CALENDAR", "frequency": "WEEKLY", "at": "08:00:00", "weekdays": ["MON", "FRI"]}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "month_days": [1, 15]}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "last_day": true}
{"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "nth_weekdays": [{"weekday": "MON", "nth": 1}]}  // nth 1..5 또는 -1
{"kind": "CRON", "expression": "*/10 9-17 * * MON-FRI"}             // 5필드 또는 @hourly/@daily/@weekly/@monthly/@yearly. 초, L/W/# 불가
```

정책 종류 — 고른 `mode`가 쓰는 필드**만** 보낸다(다른 필드 → 422):

| 구역 | mode | 필드 (기본값) |
| --- | --- | --- |
| `overlap` | `SKIP` | — |
| | `QUEUE` | `max_queued` (100, 1..10000) |
| | `PARALLEL` | `max_concurrent` (필수, 1..100), `max_queued` (100) |
| | `REPLACE` | — (실행 중인 것을 취소하고 새로 실행) |
| `misfire` | `SKIP` | `grace_seconds` (300, 1..86400) |
| | `LATEST` | `grace_seconds` |
| | `CATCH_UP` | `grace_seconds`, `max_runs` (10, 1..1000), `max_age_seconds` (86400, 60..31536000) |

UpdateBatch(PATCH) 규칙: 바꿀 필드만 보낸다. `{}` → 422. `name`/`script_id`/`runtime`/`policy`는 `null` 불가.
`script_version_number: null` → 최신 버전으로 다시 고정. `schedule: null` → 일정 제거(활성 상태면 409 `SCHEDULE_REQUIRED`).
`policy`는 구역별로 병합되고, 다른 `mode`를 보내면 그 구역은 새 모드의 기본값에서 다시 시작한다(잘못된 병합 → 422 `INVALID_POLICY`).

모든 자원의 감사 필드 (Executor와 같음): `created_by`/`updated_by` = 변경 요청의 `X-User-Id`(백그라운드 작업과 운영 API는 null),
`created_by_client`/`updated_by_client` = API key의 호출자 이름(백그라운드 작업은 null).

UI·로직에 쓸 BatchView 필드: `user_id`(소유자), `enabled`, `can_enable`, `can_disable`, `enable_blocked_reason`, `disabled_reason`
(`OWNER` | `USER_DEACTIVATED` | `TENANT_REVOKED` | null), `next_run_at`, `preview_runs`(최대 5개), `revision`, `config_number`.

RunView 주요 필드: `id`, `user_id`(소유자), `status`, `skip_reason`, `failure_type`, `cancel_requested`, `trigger`(`MANUAL`|`SCHEDULED`),
`scheduled_for`, `execution_id`, `notebook_status`, `result.notebook_artifact_id`, `completed_at`.

## 10. 상태 전이

Batch:

```
생성 ──▶ disabled ──enable──▶ enabled ──disable──▶ disabled (disabled_reason=OWNER)
              ▲                   │
              │                   ├─ disable-by-owner  ▶ disabled (USER_DEACTIVATED)  수동 실행 → 409 OWNER_DEACTIVATED
              │                   └─ disable-by-tenant ▶ disabled (TENANT_REVOKED)    수동 실행 → 409 TENANT_REVOKED
              └── enable이 disabled_reason을 지운다 (되돌리는 유일한 방법)
```

- 활성화하려면 앞으로 도래할 일정, 설정된 Executor, 사용 가능한 런타임이 있어야 한다.
  호출하고 오류를 잡기보다 먼저 `can_enable` / `enable_blocked_reason`을 확인한다.
- 이미 활성인 배치를 활성화(또는 비활성 배치를 비활성화)하면 200이고 아무것도 바뀌지 않는다.
- 활성화는 지금부터의 일정만 잡는다. 비활성 동안 지난 일정은 채워 넣지 않는다.
- 수동 실행은 비활성 배치와 일정 없는 배치에서도 된다(위 두 비활성 사유는 제외).

Run:

```
WAITING ─▶ QUEUED ─▶ RUNNING ─▶ COLLECTING ─▶ COMPLETED | FAILED
   └──────────┴─────────┴─▶ CANCELLED (취소는 WAITING/QUEUED/RUNNING에서만)
SKIPPED (정책상 실행 안 함, 생성 시점에 최종)
```

최종 상태: `COMPLETED`, `FAILED`, `CANCELLED`, `SKIPPED`. 이 상태가 되면 폴링을 멈춘다.
QUEUED/RUNNING 취소는 202와 `cancel_requested: true`, **같은** 상태를 돌려준다. `CANCELLED`가 될 때까지 폴링한다.

## 11. 예제 (Python, httpx)

```python
"""최소 pr-batch 클라이언트. 복사해서 고쳐 쓰고, 주석의 규칙은 지킨다."""

import os
import time
import uuid

import httpx

BATCH_URL = os.environ["BATCH_URL"]  # 예: http://pr-batch:8000
BATCH_API_KEY = os.environ["BATCH_API_KEY"]  # 코드에 직접 쓰지 않는다
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
    # relay key 전용. 이 값으로 만든 자원에는 계속 같은 값을 보낸다.
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
    """논리적 작업 하나. 쓰기는 재시도 동안 Idempotency-Key 하나를 재사용한다."""
    headers = dict(kwargs.pop("headers", {}))
    if write:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    if if_match:
        headers["If-Match"] = if_match  # ETag 값을 따옴표까지 그대로
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
        # POST /runs의 409 RunView도 여기로 온다("error" 키 없음)
        return response
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
    # 목록의 항목 하나를 그대로 쓴다
    runtime = call(api, "GET", "/runtimes").json()["items"][0]
    body = {"name": name, "script_id": script_id, "runtime": runtime}
    if cron:
        body["schedule"] = {"definition": {"kind": "CRON", "expression": cron}}
    # 비활성(enabled: false)으로 만들어진다
    return call(api, "POST", "/batches", write=True, json=body).json()


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
    # COMPLETED면 run["result"]["notebook_artifact_id"]를 Executor에서 받는다
    return run


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


# 일반적인 흐름
with client("e12345") as api:
    script = upload_notebook(api, "daily.ipynb", "Daily line report")
    batch = create_batch(
        api, script["id"], "Weekdays 08:00", cron="0 8 * * MON-FRI"
    )
    result = run_and_wait(api, batch["id"])  # 먼저 수동으로 한 번 실행
    if result["status"] == "COMPLETED":
        enable_batch(api, batch["id"])  # 그다음 일정을 켠다
```

삭제 순서: 삭제되지 않은 배치가 쓰는 스크립트는 지울 수 없다(409 `SCRIPT_IN_USE`). `WAITING`/`QUEUED`/`RUNNING`/`COLLECTING`
실행이 있는 배치는 지울 수 없다(409 `BATCH_HAS_ACTIVE_RUNS`). 순서: 실행 취소 또는 종료 대기 → 배치 삭제 → 스크립트 삭제.
삭제마다 새로 GET해서 `ETag`를 받는다.

## 12. 결과 노트북과 이벤트

- pr-batch는 결과 파일을 주지 않는다. 실행이 `COMPLETED`가 되면 `result.notebook_artifact_id`를 **Executor**에서 받는다:
  `GET {EXECUTOR_URL}/api/v1/artifacts/{artifact_id}/content`. 호출 서비스 자신의 Executor key와 같은 `X-User-Id`를 보낸다
  (배치 tenant가 key 기본값이 아니면 `X-Tenant-Id`도).
- 배치 실행의 Executor 이벤트는 Redis 스트림 `executor.events.<배치 tenant>`로 가고, `execution.metadata`에
  `batch_id`, `script_id`, `script_version_id`, `run_id`가 들어 있다. 이벤트는 힌트로만 쓰고
  `GET /batches/{batch_id}/runs/{run_id}`를 다시 읽어 확정한다. 모르는 `run_id`의 이벤트는 버린다.
- `notebook_status`는 실행이 최종 상태가 된 뒤에만 읽는다.

## 13. 노트북 업로드 한도

| 규칙 | 기본값 | 오류 |
| --- | --- | --- |
| 파일 이름이 `.ipynb`로 끝나고 올바른 nbformat v4 JSON | — | 422 `INVALID_NOTEBOOK` |
| 파일 크기 ≤ 10 MiB | 10 MiB | 413 `UPLOAD_TOO_LARGE` / `REQUEST_TOO_LARGE` |
| 전체 셀 수 ≤ 1000 (마크다운·raw 포함) | 1000 | 422 `TOO_MANY_CELLS` |
| 비어 있지 않은 코드 셀 수 ≤ 100 (Executor 한도) | 100 | 422 `TOO_MANY_CELLS` |
| 코드 셀 하나의 소스 ≤ 1 MiB | 1 MiB | 422 `CELL_TOO_LARGE` |
| 비어 있지 않은 코드 셀 1개 이상 | — | 422 `EMPTY_NOTEBOOK` |
| 언어(선언된 경우)가 Python | — | 422 `UNSUPPORTED_LANGUAGE` |

비어 있지 않은 코드 셀 하나가 순서대로 Executor step 하나가 된다. 마크다운·raw·빈 셀은 보관되지만 실행하지 않는다.
같은 바이트를 새 버전으로 올리면 새로 만들지 않고 기존 버전을 200으로 돌려준다.

## 14. 자주 하는 실수 — 생성 코드 점검표

- [ ] 모든 환경에서 모든 호출에 `X-Api-Key`를 보낸다.
- [ ] 모든 쓰기와 최종 사용자 조회에 `X-User-Id`를 보낸다. 허용된 tenant 전체 화면에서만 빼고, 검증 안 된 상위 헤더 값을 쓰지 않는다.
- [ ] 작업마다 새 `Idempotency-Key`, 그 작업의 재시도에는 **같은** 키.
- [ ] `If-Match`는 `ETag`를 **따옴표까지** 복사하고, 쓰기마다 새 값으로 바꾼다.
- [ ] JSON 본문에 `tenant_id`, `enabled`, `id` 같은 모르는 필드를 넣지 않는다.
- [ ] relay 호출자는 `X-Tenant-Id`로 만든 자원에 같은 `X-Tenant-Id`를 계속 보낸다(안 보내면 404).
- [ ] `POST /runs`의 409는 오류가 아니라 RunView(`SKIPPED_CAPACITY`)로 파싱한다.
- [ ] 새 배치는 비활성이다. `can_enable`이 true일 때만 `enable`을 명시적으로 부른다.
- [ ] 새 스크립트 버전은 기존 배치에 반영되지 않는다. `script_version_number`를 PATCH한다(최신은 `null`).
- [ ] 런타임 객체는 `GET /runtimes`에서 그대로 복사한다.
- [ ] 정책 구역에는 그 `mode`의 필드만 넣는다.
- [ ] 폴링 간격 ≥ 5초, `COMPLETED`/`FAILED`/`CANCELLED`/`SKIPPED`에서 멈춘다.
- [ ] `message`가 아니라 `error.code`로 분기한다.
- [ ] tenant 전체 목록 필터는 쿼리 `user_id`를 쓰고, `X-User-Id`와 다른 `user_id`를 보내지 않는다.
- [ ] 결과 노트북은 pr-batch가 아니라 Executor에서 받는다.
- [ ] 운영 API(`:disable-by-owner`, `:disable-by-tenant`)는 최종 사용자 기능에 쓰지 않는다.
