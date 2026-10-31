# API별 호출 가이드

pr-batch를 호출하는 서비스를 위한 엔드포인트별 사용법이다. key와 신원 헤더는
[호출 서비스 인증 가이드](client-auth.md)를 먼저 본다. 동작 원리와 세부 규칙은
[등록 API](registration-api.md), [실행 API](execution-api.md), [예약 실행](scheduling-api.md) 문서에 있다.
로컬에서 `DOCS_ENABLED=true`로 띄운 서버의 `/docs`(Swagger)와 `/openapi.json`이 항상 최신 명세다.
공유 환경(dev·stg·prd)에서는 내부망 규정으로 꺼져 있다.

2026-10-21에 처음 전달한 이후 바뀐 점은 [API 사용 변경 사항](api-changes.md)에, 2026-10-29 인증·tenant
변경은 [Batch API 변경 사항](batch-api-changes.md)에 정리했다.

## 공통 규칙

### 주소

- Base URL: 클러스터 안 `http://pr-batch:8000`, 로컬 `http://127.0.0.1:8100` (환경별 주소는 배치팀에 확인)
- 모든 API는 `/api/v1` 아래에 있다.
- 예시의 `$BATCH_URL`은 Base URL, `$BATCH_API_KEY`는 호출 서비스 key, `$USER_ID`는 대신하는 사용자다
  ([인증 가이드](client-auth.md)). dev는 key 없이 `X-User-Id`만 보내도 된다.

### 헤더

| 헤더 | 언제 | 값 |
| --- | --- | --- |
| `X-Api-Key` | 모든 `/api/v1` 요청 (dev 제외) | 호출 서비스 key |
| `X-User-Id` | `GET /runtimes`, `disable-by-owner` 외 모든 요청 | 대신하는 사용자. 소문자로 바꿔 저장·비교 |
| `X-Project-Id`, `X-Tenant-Id` | 배치 등록 때 선택 | 등록 시점에 고정. [인증 가이드](client-auth.md#헤더) |
| `Idempotency-Key` | 모든 쓰기(POST, PATCH, DELETE) | 1~255자, 공백 없는 ASCII 출력 문자. 요청마다 새 값(UUID 권장) |
| `If-Match` | 스크립트 이름 변경·삭제, 배치 수정·삭제·활성화·비활성화 | 상세 GET 응답의 `ETag` 값 그대로, 예: `"3"` (따옴표 포함) |
| `Content-Type` | JSON 본문 | `application/json` (파일 업로드는 `multipart/form-data`) |

**Idempotency-Key**: 같은 사용자·같은 작업·같은 키로 같은 본문을 다시 보내면 처리하지 않고 최초 응답(상태
코드 포함)을 그대로 돌려준다. 네트워크 오류·타임아웃 뒤에는 **같은 키와 같은 본문**으로 재시도한다. 같은
키에 다른 본문을 보내면 409 `IDEMPOTENCY_CONFLICT`다. 재전송 응답은 최초 시점의 값이므로 최신 상태는 GET으로
읽는다. 키 기록은 7일 보관되며, 그 뒤 같은 키는 새 요청으로 처리된다. 런타임 목록이 비어 503이 나도 이미 처리된
요청의 재전송은 최초 응답을 돌려준다.

**If-Match**: 자원의 `revision`이 바뀌었으면 409 `REVISION_CONFLICT`다. 다시 GET해서 새 `ETag`로 요청한다.
형식이 틀리면(따옴표 누락 등) 422 `INVALID_REVISION`, 헤더가 없으면 422 `VALIDATION_ERROR`다.
쓰기 응답에도 새 `ETag`가 붙으므로 연속으로 수정할 때는 그 값을 쓰면 된다.

### 목록 조회

`limit`(1~100, 기본 50)과 `cursor`를 쿼리로 받는다. 응답은 `{"items": [...], "next_cursor": ...}`이며
`next_cursor`가 `null`이면 마지막 페이지다. 다음 페이지는 받은 `next_cursor`를 그대로 `cursor`에 넣는다.
`cursor`는 불투명 문자열이다. 내용을 해석하거나 만들어 보내지 않는다(잘못된 값은 422 `INVALID_CURSOR`).
목록은 최신순(버전은 번호 역순)이다.

### 소유권

모든 자원은 **tenant + 사용자**(`X-User-Id`) 소유다. Executor와 같은 체계다. 요청의 tenant는 key의 기본 tenant이고,
중계자 key는 `X-Tenant-Id`로 고른다([인증 가이드](client-auth.md#헤더)). 다른 tenant나 다른 사용자의 자원, 삭제된
자원은 존재 여부를 숨기고 404로 응답한다. 같은 `X-User-Id`라도 tenant가 다르면 서로의 자원이 보이지 않으므로, 한
자원은 만들 때와 **같은 tenant로** 계속 호출한다. `Idempotency-Key`도 tenant·사용자별로 따로 기록된다.

### 오류 형식

모든 오류는 Executor API와 같은 한 가지 모양이다. `details`는 비어 있을 수 있다.

```json
{"error": {"code": "REVISION_CONFLICT", "message": "Reload the resource.",
  "details": [{"location": ["header", "If-Match"], "type": "revision_conflict",
               "context": {"expected": 1, "current": 2}}]}}
```

- `error.code`: 기계가 읽는 오류 코드. 분기는 이 값으로 한다.
- `details[].location`: 어느 값인지(`["body", "schedule", ...]`, `["body", "file", "cells"]` 등).
- `details[].type`: 왜인지(`missing`, `notebook_cell_limit_exceeded` 등).
- `details[].context`: 근거 값(한도, 개수 등). 보낸 값(코드, key, 입력값)은 되돌려주지 않는다.

요청 형식 오류(422 `VALIDATION_ERROR`)는 항목마다 사람이 읽는 `message`도 붙는다.

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed.",
  "details": [{"location": ["body", "name"], "type": "missing", "context": {}, "message": "Field required"}]}}
```

한도 초과 예: 노트북 전체 셀 초과는 `{"location": ["body", "file", "cells"], "type": "notebook_cell_limit_exceeded",
"context": {"limit": 1000, "requested": 1200}}`.

| HTTP | 대표 code | 의미·대응 |
| --- | --- | --- |
| 400 | `INVALID_IDENTITY` | `X-User-Id` 없음·형식 오류 등 신원 헤더 오류 |
| 401 | `UNAUTHENTICATED` | key 오류. [인증 가이드](client-auth.md#인증-오류) 확인 |
| 403 | `FORBIDDEN` | 허용되지 않은 tenant, admin이 아닌 key |
| 404 | `NOT_FOUND` | 없는 자원, 다른 tenant·사용자 자원, 삭제된 자원 |
| 409 | `IDEMPOTENCY_CONFLICT`, `REVISION_CONFLICT` 등 | 키 재사용·동시 수정 충돌·상태상 불가. 엔드포인트별 표 참고 |
| 413 | `REQUEST_TOO_LARGE`, `UPLOAD_TOO_LARGE` | 업로드 크기 초과 (기본 10 MiB) |
| 422 | `VALIDATION_ERROR` 등 | 입력 오류. 같은 요청을 재시도해도 실패한다 |
| 503 | `DATABASE_UNAVAILABLE`, `STORAGE_UNAVAILABLE`, `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` 등 | 일시 장애·설정 누락. 같은 `Idempotency-Key`로 잠시 뒤 재시도 |
| 500 | `INTERNAL_ERROR` | DB 제약 위반 등 서버 쪽 오류. 재시도해도 같다. 응답 `details[0].context`(`error_id`, `type`, `sqlstate`, `table`, `constraint`)를 pr-batch 담당자에게 전달한다. `error_id`로 서버 로그를 찾는다 |

DB 오류 응답(503 `DATABASE_UNAVAILABLE`, 500 `INTERNAL_ERROR`)은 `details`에 `location: ["database"]` 항목 하나가 붙는다. SQL 문이나 요청 값은 들어가지 않는다.

### 공통 응답 필드

스크립트·버전·배치·실행·생략 구간 응답에는 감사 필드 `created_at`, `created_by_type`, `created_by`,
`updated_at`, `updated_by_type`, `updated_by`가 있다. 시각은 모두 UTC ISO 8601이다. 아래 예시에서는 생략한다.

## 전체 목록

| 분류 | Method | 경로 | 성공 | Idempotency-Key | If-Match |
| --- | --- | --- | --- | --- | --- |
| 런타임 | GET | `/api/v1/runtimes` | 200 | | |
| 스크립트 | POST | `/api/v1/scripts` | 201 | O | |
| | GET | `/api/v1/scripts` | 200 | | |
| | GET | `/api/v1/scripts/{script_id}` | 200 | | |
| | PATCH | `/api/v1/scripts/{script_id}` | 200 | O | O |
| | DELETE | `/api/v1/scripts/{script_id}` | 204 | O | O |
| 버전 | POST | `/api/v1/scripts/{script_id}/versions` | 201 / 200 | O | |
| | GET | `/api/v1/scripts/{script_id}/versions` | 200 | | |
| | GET | `/api/v1/scripts/{script_id}/versions/{version_id}` | 200 | | |
| | GET | `/api/v1/scripts/{script_id}/versions/{version_id}/content` | 200 | | |
| 배치 | POST | `/api/v1/batches` | 201 | O | |
| | GET | `/api/v1/batches` | 200 | | |
| | GET | `/api/v1/batches/{batch_id}` | 200 | | |
| | PATCH | `/api/v1/batches/{batch_id}` | 200 | O | O |
| | DELETE | `/api/v1/batches/{batch_id}` | 204 | O | O |
| | POST | `/api/v1/batches/{batch_id}/enable` | 200 | O | O |
| | POST | `/api/v1/batches/{batch_id}/disable` | 200 | O | O |
| | GET | `/api/v1/batches/{batch_id}/skipped-windows` | 200 | | |
| | POST | `/api/v1/batches:disable-by-owner` (admin key) | 200 | | |
| 실행 | POST | `/api/v1/batches/{batch_id}/runs` | 202 | O | |
| | GET | `/api/v1/batches/{batch_id}/runs` | 200 | | |
| | GET | `/api/v1/batches/{batch_id}/runs/{run_id}` | 200 | | |
| | POST | `/api/v1/batches/{batch_id}/runs/{run_id}/cancel` | 202 | O | |

`/healthz`(프로세스 생존), `/readyz`(DB·저장소 준비)는 인증 없이 호출하는 운영용 엔드포인트다.

일반적인 호출 순서는 다음과 같다.

1. `GET /runtimes`로 고를 수 있는 런타임 확인
2. `POST /scripts`로 노트북 업로드
3. `POST /batches`로 배치 등록 (비활성 상태로 생성)
4. `POST /batches/{id}/runs`로 수동 실행하거나, `POST /batches/{id}/enable`로 예약 활성화
5. `GET /batches/{id}/runs/{run_id}`로 상태 확인

---

## 런타임

### GET /api/v1/runtimes

배치 등록 때 `runtime`에 넣을 수 있는 조합 목록이다. pr-batch 설정 `BATCH_RUNTIMES`에서 읽는다.
key만 필요하고 `X-User-Id`는 없어도 된다.

```sh
curl "$BATCH_URL/api/v1/runtimes" -H "X-Api-Key: $BATCH_API_KEY"
```

```json
{"items": [{"type": "JUPYTER", "profile": "default"}]}
```

| 오류 | 의미 |
| --- | --- |
| 503 `RUNTIMES_NOT_CONFIGURED` | `BATCH_RUNTIMES`가 비어 있음 |

---

## 스크립트

### POST /api/v1/scripts — 업로드

`multipart/form-data`로 보낸다.

| 필드 | 필수 | 값 |
| --- | --- | --- |
| `file` | O | `.ipynb` 파일 |
| `type` | O | `NOTEBOOK` |
| `name` | X | 1~255자. 생략하면 파일명에서 확장자를 뺀 값 |

```sh
curl "$BATCH_URL/api/v1/scripts" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'file=@daily.ipynb' -F 'type=NOTEBOOK' -F 'name=생산라인 일일 분석'
```

201, 헤더 `Location: /api/v1/scripts/{id}`, `ETag: "1"`.

```json
{
  "id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
  "name": "생산라인 일일 분석",
  "type": "NOTEBOOK",
  "revision": 1,
  "latest_version": {
    "id": "0b9a7e52-2f4d-4c33-8a51-6f0e9d2c7b10",
    "script_id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
    "version_number": 1,
    "filename": "daily.ipynb",
    "sha256": "9f86d08…",
    "code_hash": "2c26b46…",
    "size_bytes": 18432,
    "cells": {"total": 12, "executable": 8, "skipped": 4}
  }
}
```

| 오류 | 의미 |
| --- | --- |
| 413 `UPLOAD_TOO_LARGE`, `REQUEST_TOO_LARGE` | 파일 10 MiB 초과(기본) |
| 422 `INVALID_NOTEBOOK` | 노트북 JSON 형식 오류 |
| 422 `EMPTY_NOTEBOOK` | 실행할 코드 셀이 없음 |
| 422 `TOO_MANY_CELLS`, `CELL_TOO_LARGE` | 코드 셀 100개, 전체 셀(마크다운·raw 포함) 1,000개, 셀당 1 MiB 초과(기본) |
| 422 `UNSUPPORTED_LANGUAGE` | Python이 아닌 노트북 |
| 503 `STORAGE_UNAVAILABLE` | 저장소 장애 |

### GET /api/v1/scripts — 목록

쿼리: `limit`, `cursor`. 응답 `{"items": [ScriptView...], "next_cursor": "..."}`.

```sh
curl "$BATCH_URL/api/v1/scripts?limit=20" -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

### GET /api/v1/scripts/{script_id} — 상세

응답은 업로드 응답과 같은 ScriptView이고 `ETag` 헤더가 붙는다. 이름 변경·삭제 전에 호출해 `ETag`를 얻는다.

### PATCH /api/v1/scripts/{script_id} — 이름 변경

```sh
curl -X PATCH "$BATCH_URL/api/v1/scripts/$SCRIPT_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'If-Match: "1"' \
  -H 'Content-Type: application/json' \
  -d '{"name": "A라인 일일 분석"}'
```

200, 변경된 ScriptView와 새 `ETag`.

### DELETE /api/v1/scripts/{script_id} — 삭제

soft delete이며 되살리는 API는 없다. 204, 본문 없음.

```sh
curl -X DELETE "$BATCH_URL/api/v1/scripts/$SCRIPT_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'If-Match: "2"'
```

| 오류 | 의미 |
| --- | --- |
| 409 `SCRIPT_IN_USE` | 삭제되지 않은 배치가 이 스크립트를 쓰고 있음. 배치를 지우거나 다른 스크립트로 바꾼다 |
| 409 `SCRIPT_HAS_ACTIVE_RUNS` | 이 스크립트로 진행 중인 실행이 있음 |

### POST /api/v1/scripts/{script_id}/versions — 새 버전 업로드

multipart `file`만 보낸다. 스크립트의 `type`으로 파싱한다. `If-Match`는 필요 없다.

```sh
curl "$BATCH_URL/api/v1/scripts/$SCRIPT_ID/versions" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'file=@daily.ipynb'
```

새 버전이면 201(VersionView, `Location`), 이미 올린 어떤 버전과 바이트가 같으면 새로 만들지 않고 그 버전을
200으로 돌려준다. 기존 배치는 새 버전을 따라가지 않는다(배치 PATCH로 다시 고정해야 한다). 오류는 업로드와 같고, 스크립트 타입과 맞지 않는 파일은 422
`SCRIPT_TYPE_MISMATCH`다.

### GET /api/v1/scripts/{script_id}/versions — 버전 목록

쿼리: `limit`, `cursor`. 버전 번호 역순.

### GET /api/v1/scripts/{script_id}/versions/{version_id} — 버전 상세

VersionView에 원본 셀별 `cell_list`가 추가된다.

```json
{
  "id": "0b9a7e52-…", "version_number": 1, "cells": {"total": 3, "executable": 2, "skipped": 1},
  "cell_list": [
    {"index": 0, "type": "markdown", "executed": false, "sequence": null},
    {"index": 1, "type": "code", "executed": true, "sequence": 0},
    {"index": 2, "type": "code", "executed": true, "sequence": 1}
  ]
}
```

### GET /api/v1/scripts/{script_id}/versions/{version_id}/content — 원본 다운로드

업로드한 원본 바이트를 그대로 돌려준다. `Content-Type: application/x-ipynb+json`,
`Content-Disposition: attachment; filename="{version_id}.ipynb"`.

```sh
curl -o daily.ipynb "$BATCH_URL/api/v1/scripts/$SCRIPT_ID/versions/$VERSION_ID/content" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

| 오류 | 의미 |
| --- | --- |
| 503 `STORAGE_INTEGRITY_ERROR` | 저장된 파일의 해시가 맞지 않음 |

---

## 배치

### POST /api/v1/batches — 등록

새 배치는 항상 **비활성**(`enabled: false`)으로 만들어진다.

| 필드 | 필수 | 값 |
| --- | --- | --- |
| `name` | O | 1~255자 |
| `script_id` | O | 내 스크립트 ID |
| `script_version_number` | X | 고정할 버전 번호(1, 2, …). 생략하면 등록 시점 최신 버전 |
| `runtime` | O | `{"type": "JUPYTER", "profile": "..."}`. `GET /runtimes` 목록 중 하나 |
| `schedule` | X | 일정. 없으면 수동 실행 전용(활성화 불가) |
| `policy` | X | 겹침(`overlap`)·누락(`misfire`) 정책. 생략하면 둘 다 `SKIP` |

tenant와 project는 본문이 아니라 헤더로 정한다. tenant는 요청의 tenant(key의 기본 tenant, 중계자 key는
`X-Tenant-Id`)이고 배치의 소유 범위이자 실행 이벤트를 받을 Executor tenant다. `script_id`는 같은 tenant·사용자의
스크립트여야 한다(아니면 404). `X-Project-Id`를 보내면 `project_id`로 저장한다. 둘 다 등록 후에는 바뀌지 않는다.
본문에 `tenant_id`를 보내면 422다.

```sh
curl "$BATCH_URL/api/v1/batches" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "A라인 평일 8시",
    "script_id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
    "runtime": {"type": "JUPYTER", "profile": "default"},
    "schedule": {"definition": {"kind": "CRON", "expression": "0 8 * * MON-FRI"}},
    "policy": {"overlap": {"mode": "QUEUE"}, "misfire": {"mode": "LATEST"}}
  }'
```

201, `Location`, `ETag: "1"`.

```json
{
  "id": "5f1e8a3b-9c2d-4e7f-8a6b-1c0d2e3f4a5b",
  "name": "A라인 평일 8시",
  "script_id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
  "script_version_id": "0b9a7e52-2f4d-4c33-8a51-6f0e9d2c7b10",
  "script_version_number": 1,
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "schedule": {
    "definition": {"kind": "CRON", "expression": "0 8 * * MON-FRI"},
    "timezone": "Asia/Seoul", "start_at": null, "end_at": null
  },
  "policy": {
    "overlap": {"mode": "QUEUE", "max_queued": 100},
    "misfire": {"mode": "LATEST", "grace_seconds": 300}
  },
  "tenant_id": "pr-agentic-service",
  "project_id": null,
  "enabled": false,
  "disabled_reason": null,
  "disabled_at": null,
  "next_run_at": null,
  "preview_runs": ["2026-10-21T23:00:00Z", "2026-10-22T23:00:00Z", "…"],
  "config_number": 1,
  "revision": 1
}
```

**일정(`schedule`)** — `definition.kind`별로 필드가 다르다. `timezone`은 `Asia/Seoul`만 허용하고,
`start_at`(포함)·`end_at`(미포함)은 선택이다.

```json
{"definition": {"kind": "ONCE", "at": "2026-11-01T09:00:00+09:00"}}
{"definition": {"kind": "INTERVAL", "seconds": 1800}}
{"definition": {"kind": "CALENDAR", "frequency": "DAILY", "at": "08:00:00"}}
{"definition": {"kind": "CALENDAR", "frequency": "WEEKLY", "at": "08:00:00", "weekdays": ["MON", "WED", "FRI"]}}
{"definition": {"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "last_day": true}}
{"definition": {"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "nth_weekdays": [{"weekday": "MON", "nth": 1}]}}
{"definition": {"kind": "CRON", "expression": "*/10 9-17 * * MON-FRI"}}
```

**정책(`policy`)** — 각 구역에서 `mode`를 고르고 그 모드가 쓰는 값만 보낸다. 생략한 값은 기본값으로 채워진다.

| 구역 | mode | 추가 값 (기본) |
| --- | --- | --- |
| overlap (이전 실행이 아직 돌 때) | `SKIP` | 없음 |
| | `QUEUE` | `max_queued` (100) |
| | `PARALLEL` | `max_concurrent` (필수, 1~100), `max_queued` (100) |
| | `REPLACE` | 없음. 실행 중인 것을 취소하고 새로 실행. 한 번에 여러 일정이 도래하면 마지막 하나만 실행 |
| misfire (장애 등으로 놓친 일정) | `SKIP` | `grace_seconds` (300) |
| | `LATEST` | `grace_seconds` (300) |
| | `CATCH_UP` | `grace_seconds` (300), `max_runs` (10), `max_age_seconds` (86400) |

자세한 규칙은 [등록 API의 일정과 정책](registration-api.md#일정과-정책)을 본다.

| 오류 | 의미 |
| --- | --- |
| 404 `NOT_FOUND` | `script_id`가 없거나 이 tenant·사용자의 것이 아님 |
| 422 `VALIDATION_ERROR` | 필드 형식, 일정 정의, 모드에 없는 정책 값, 본문 `tenant_id` 등 모르는 필드 |
| 422 `INVALID_POLICY` | 정책 조합 오류 |
| 422 `UNKNOWN_RUNTIME` | 런타임 목록에 없는 조합 |
| 422 `RUNTIME_NOT_SUPPORTED` | 스크립트 타입이 그 런타임에서 돌 수 없음 |
| 503 `RUNTIMES_NOT_CONFIGURED` | `BATCH_RUNTIMES`가 비어 있음 |

### GET /api/v1/batches — 목록

쿼리: `limit`, `cursor`, `script_id`(선택, 특정 스크립트를 쓰는 배치만).

```sh
curl "$BATCH_URL/api/v1/batches?script_id=$SCRIPT_ID" -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

### GET /api/v1/batches/{batch_id} — 상세

BatchView와 `ETag`. `enabled`, `next_run_at`(처리할 다음 예약), `preview_runs`(앞으로의 일정 최대 5개)로
예약 상태를 확인한다.

### PATCH /api/v1/batches/{batch_id} — 수정

보낸 필드만 바뀐다. 필드는 등록과 같고, `policy`는 바꿀 하위 값만 보내면 된다.

```sh
curl -X PATCH "$BATCH_URL/api/v1/batches/$BATCH_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'If-Match: "1"' \
  -H 'Content-Type: application/json' \
  -d '{"script_version_number": 2, "policy": {"overlap": {"max_queued": 10}}}'
```

- 빈 본문, `name`·`script_id`·`runtime`·`policy`에 `null`은 422.
- `script_version_number: null` 또는 `script_id`만 보내면 최신 버전으로 다시 고정.
- `schedule: null`은 일정 제거. 활성 배치에서는 409 `SCHEDULE_REQUIRED`.
- 다른 `mode`를 보내면 그 구역은 새 모드의 기본값에서 다시 시작한다.
- `X-Project-Id`는 무시한다. tenant는 바꿀 수 없다. 등록 때와 다른 tenant로 부르면(중계자 key가 `X-Tenant-Id`를 빼서
  기본 tenant가 된 경우 포함) 그 tenant에는 배치가 없으므로 404다.

200, 변경된 BatchView와 새 `ETag`. 오류는 등록과 같고 409 `REVISION_CONFLICT`, `SCHEDULE_REQUIRED`,
422 `NO_FUTURE_OCCURRENCE`(활성 배치의 새 일정에 앞으로 실행할 시각이 없음)가 추가된다.

### DELETE /api/v1/batches/{batch_id} — 삭제

soft delete. 활성 배치는 비활성화되고 이후 배치와 그 실행·생략 구간 조회는 404다. 204.

```sh
curl -X DELETE "$BATCH_URL/api/v1/batches/$BATCH_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'If-Match: "3"'
```

| 오류 | 의미 |
| --- | --- |
| 409 `BATCH_HAS_ACTIVE_RUNS` | WAITING·QUEUED·RUNNING·COLLECTING 실행이 있음. 취소하거나 끝나길 기다린다 |

### POST /api/v1/batches/{batch_id}/enable — 예약 활성화

본문 없음. 현재 시각 이후의 첫 일정부터 예약한다. 비활성 동안 지난 일정은 소급하지 않는다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/enable" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'If-Match: "1"'
```

200, `enabled: true`인 BatchView와 새 `ETag`.

| 오류 | 의미 |
| --- | --- |
| 409 `SCHEDULE_REQUIRED` | 일정이 없는 배치 |
| 422 `NO_FUTURE_OCCURRENCE` | 앞으로 실행할 시각이 없음(지난 ONCE, 끝난 기간) |
| 422 `UNKNOWN_RUNTIME`, `RUNTIME_NOT_SUPPORTED` | 배치의 런타임을 지금 쓸 수 없음 |
| 503 `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` | Executor·런타임 목록 미설정 |

### POST /api/v1/batches/{batch_id}/disable — 예약 중지

본문 없음. 새 예약 접수만 멈추고 이미 접수·실행 중인 실행은 취소하지 않는다. 200, `enabled: false`,
`disabled_reason: "OWNER"`인 BatchView. 다시 활성화하면 `disabled_reason`·`disabled_at`이 지워진다.

### POST /api/v1/batches:disable-by-owner — 사용자 비활성 (운영)

admin key만 부를 수 있다(`X-User-Id` 불필요). 본문 `{"owner_id": "e12345", "reason": "USER_DEACTIVATED"}`.
요청의 tenant(admin key의 기본 tenant, 중계자 admin key는 `X-Tenant-Id`) 안에서 그 사용자의 켜진 배치를 모두 끄고 `{"disabled": n}`을 돌려준다. 자세한 동작은
[인증 가이드](client-auth.md#사용자-비활성)를 본다.

### GET /api/v1/batches/{batch_id}/skipped-windows — 생략된 예약 구간

실행 행을 만들지 않고 건너뛴 일정 구간이다. 쿼리: `limit`, `cursor`.

```json
{
  "items": [{
    "id": "c1d2…", "batch_id": "5f1e…", "config_number": 1,
    "from_at": "2026-10-20T23:00:00Z", "until_at": "2026-10-21T23:00:00Z",
    "occurrences": 1, "reason": "DISABLED"
  }],
  "next_cursor": null
}
```

`reason`: `MISFIRE_SKIP`, `MISFIRE_LATEST`, `MISFIRE_CATCH_UP`, `REPLACED_BY_LATEST`, `DISABLED`, `SCHEDULE_REPLACED`, `DELETED`.

---

## 실행

### POST /api/v1/batches/{batch_id}/runs — 수동 실행

배치의 현재 설정으로 실행을 접수한다. 비활성 배치나 일정이 없는 배치도 실행할 수 있다. 본문은 생략하거나 `{}`만
보낸다(버전·런타임 지정은 422). 수동 실행도 overlap 정책을 따른다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/runs" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)"
```

| 결과 | HTTP | `status` | `skip_reason` |
| --- | --- | --- | --- |
| 접수, 제출 대기 | 202 | `QUEUED` | |
| 동시 실행 한도로 대기 | 202 | `WAITING` | |
| overlap `SKIP`인데 이미 실행 중 | 202 | `SKIPPED` | `OVERLAP` |
| 대기열(`max_queued`) 초과 | 409 | `SKIPPED` | `SKIPPED_CAPACITY` |

409여도 본문은 오류 형식이 아니라 RunView다(생략 이력으로 남는다). 응답은 아래 RunView와 같다.

| 오류 | 의미 |
| --- | --- |
| 409 `OWNER_DEACTIVATED` | 사용자 비활성으로 꺼진 배치(`disabled_reason=USER_DEACTIVATED`) |
| 503 `EXECUTOR_NOT_CONFIGURED` | Executor 미설정 |

### GET /api/v1/batches/{batch_id}/runs — 실행 이력

접수 역순. 쿼리: `limit`, `cursor`. 예약 실행은 `trigger: "SCHEDULED"`와 `scheduled_for`가 채워진다.

### GET /api/v1/batches/{batch_id}/runs/{run_id} — 실행 상세

RunView에 셀별 `steps`가 추가된다. 실행이 끝날 때까지 이 API를 주기적으로 조회한다(5초 이상 간격 권장).

```json
{
  "id": "a7b8c9d0-…",
  "batch_id": "5f1e8a3b-…",
  "script_id": "8d3c2f0e-…",
  "script_version_id": "0b9a7e52-…",
  "config_number": 1,
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "snapshot": {"script_version_id": "0b9a7e52-…", "sha256": "…", "code_hash": "…", "schedule": {…}, "policy": {…}},
  "trigger": "MANUAL",
  "scheduled_for": null,
  "status": "COMPLETED",
  "skip_reason": null,
  "cancel_requested": false,
  "cancel_source": null,
  "cancel_reason": null,
  "execution_id": "e1f2…",
  "execution_status": "SUCCEEDED",
  "failure_type": null,
  "notebook_status": "SUCCEEDED",
  "last_error": null,
  "result": {"notebook_artifact_id": "art_…"},
  "submitted_at": "2026-10-21T05:10:01Z",
  "started_at": "2026-10-21T05:10:03Z",
  "finished_at": "2026-10-21T05:12:40Z",
  "completed_at": "2026-10-21T05:12:45Z",
  "steps": [
    {"sequence": 0, "original_index": 1, "source_sha256": "…", "step_id": "…", "status": "SUCCEEDED", "result_ref": {…}}
  ]
}
```

(일부 필드 `operation_id`, `observed_version`, `artifacts`, `attempts`, `retry_info`, `observed_at` 생략)

| `status` | 의미 | 끝난 상태 |
| --- | --- | --- |
| `WAITING` | 동시 실행 자리 대기 | |
| `QUEUED` | Executor 제출 대기·진행 | |
| `RUNNING` | 코드 실행 중 | |
| `COLLECTING` | 코드 종료, 결과 수집 중 | |
| `COMPLETED` | 성공 | O |
| `FAILED` | 실패. `failure_type`: Executor 실패 유형(코드 오류 등), `SUBMIT_REJECTED`(Executor가 제출 거절), `ABANDONED`(Executor와 24시간 통신 실패로 포기) | O |
| `CANCELLED` | 사용자 취소 또는 REPLACE 정책 취소 (`cancel_source`: `USER`·`POLICY`) | O |
| `SKIPPED` | 실행하지 않음 (`skip_reason`) | O |

`last_error`는 pr-batch와 Executor 사이 통신 오류 기록이며 서버가 자동으로 재시도한다. 코드 실패와는 별개다.

**결과 노트북**은 pr-batch가 중계하지 않는다. `result.notebook_artifact_id`로 Executor
`GET /api/v1/artifacts/{artifact_id}/content`에서 받는다. 호출 서비스가 먼저 이 API로 사용자 소유를 확인한 뒤,
**자기 Executor key**(`X-Api-Key`)와 같은 `X-User-Id`·`X-Tenant-Id`(배치의 `tenant_id`)로 그 artifact만 내려받아
전달한다. Executor도 tenant·사용자가 다르면 404다.

### POST /api/v1/batches/{batch_id}/runs/{run_id}/cancel — 취소

본문 없음. 202, 취소 요청이 반영된 RunView.

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/runs/$RUN_ID/cancel" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)"
```

`WAITING`은 바로 `CANCELLED`가 된다. `QUEUED`·`RUNNING`은 상태가 그대로이고 `cancel_requested: true`로
응답한 뒤, 제출 전이면 곧바로, 제출됐으면 Executor 종료가 확인되면 `CANCELLED`가 된다. 최종 상태는 상세 GET으로 확인한다.

| 오류 | 의미 |
| --- | --- |
| 409 `NOT_CANCELLABLE` | `COLLECTING` 이후라 취소할 수 없음 |

---

## Python 클라이언트 예시

key와 사용자 헤더는 [인증 가이드](client-auth.md#헤더)를 따른다.

```python
import time
import uuid

import httpx

BATCH_URL = "http://pr-batch:8000"
BATCH_API_KEY = "bat_live_…"  # 호출 서비스 설정에서 읽는다


def client(user_id: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{BATCH_URL}/api/v1",
        headers={"X-Api-Key": BATCH_API_KEY, "X-User-Id": user_id},
        timeout=30,
    )


def run_and_wait(user_id: str, batch_id: str) -> dict:
    key = str(uuid.uuid4())  # 재시도할 때도 같은 키를 쓴다
    with client(user_id) as api:
        run = api.post(
            f"/batches/{batch_id}/runs", headers={"Idempotency-Key": key}
        )
        if run.status_code not in (202, 409):
            run.raise_for_status()
        run = run.json()
    while run["status"] not in ("COMPLETED", "FAILED", "CANCELLED", "SKIPPED"):
        time.sleep(5)
        with client(user_id) as api:
            run = (
                api.get(f"/batches/{batch_id}/runs/{run['id']}")
                .raise_for_status()
                .json()
            )
    return run


def update_batch_name(user_id: str, batch_id: str, name: str) -> dict:
    with client(user_id) as api:
        current = api.get(f"/batches/{batch_id}").raise_for_status()
        return (
            api.patch(
                f"/batches/{batch_id}",
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "If-Match": current.headers["ETag"],
                },
                json={"name": name},
            )
            .raise_for_status()
            .json()
        )
```
