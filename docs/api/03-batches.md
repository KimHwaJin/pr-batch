# 03. 배치 API (10개)

라우터: `src/app/api/v1/batches.py` · 태그 `batches` · 공통 규칙은 [00-common.md](00-common.md)

| Method | 경로 | 설명 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 배치 등록 (비활성으로 생성) |
| GET | `/api/v1/batches` | 배치 목록 |
| GET | `/api/v1/batches/{batch_id}` | 배치 상세 |
| PATCH | `/api/v1/batches/{batch_id}` | 배치 수정 (부분 수정) |
| DELETE | `/api/v1/batches/{batch_id}` | 배치 삭제 (soft delete) |
| POST | `/api/v1/batches/{batch_id}/enable` | 예약 활성화 |
| POST | `/api/v1/batches/{batch_id}/disable` | 예약 중지 |
| GET | `/api/v1/batches/{batch_id}/skipped-windows` | 건너뛴 예약 구간 |
| POST | `/api/v1/batches:disable-by-owner` | (운영) 사용자 비활성 시 그 사용자의 배치 일괄 중지 |
| POST | `/api/v1/batches:disable-by-tenant` | (운영) tenant 폐기 시 그 tenant의 배치 일괄 중지 |

운영 API 두 개는 admin key만 부를 수 있고 `X-User-Id`가 필요 없다. 나머지는 `X-Api-Key`, `X-User-Id`가 필요하다.

## 응답 모델 — BatchView

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | UUID | 배치 ID |
| `name` | string | 이름 |
| `script_id` | UUID | 실행할 스크립트 |
| `script_version_id` | UUID | 고정된 버전 ID |
| `script_version_number` | int | 고정된 버전 번호 |
| `runtime` | `{"type": "JUPYTER", "profile": string}` | 실행 런타임 |
| `schedule` | Schedule \| null | 일정. `null`이면 수동 실행 전용 |
| `policy` | Policy | 겹침·누락 정책 (기본값이 채워진 전체 값) |
| `tenant_id` | string | 등록 때 정해진 tenant. 바뀌지 않는다 |
| `project_id` | string \| null | 등록 때의 `X-Project-Id`. 바뀌지 않는다 |
| `enabled` | bool | 예약 활성 여부 |
| `can_enable` | bool | 지금 `enable`을 부르면 켜지는지. 이미 켜져 있으면 `false` |
| `can_disable` | bool | 지금 `disable`을 부르면 꺼지는지 (= `enabled`) |
| `enable_blocked_reason` | string \| null | 꺼져 있고 켤 수 없을 때 `enable`이 돌려줄 오류 코드: `EXECUTOR_NOT_CONFIGURED`, `SCHEDULE_REQUIRED`, `RUNTIMES_NOT_CONFIGURED`, `RUNTIME_NOT_SUPPORTED`, `UNKNOWN_RUNTIME`, `NO_FUTURE_OCCURRENCE` |
| `disabled_reason` | string \| null | 꺼진 이유: `OWNER`(사용자가 끔), `USER_DEACTIVATED`(disable-by-owner), `TENANT_REVOKED`(disable-by-tenant). 켜져 있거나 한 번도 켠 적 없으면 `null` |
| `disabled_at` | date-time \| null | 꺼진 시각 |
| `next_run_at` | date-time \| null | 스케줄러가 처리할 다음 예약 시각. 꺼져 있으면 `null` |
| `preview_runs` | date-time[] | 지금 이후 일정 최대 5개 (꺼져 있어도 계산) |
| `config_number` | int | 실행 설정 번호. 스크립트·런타임·일정·정책이 바뀔 때마다 오른다(이름만 바뀌면 그대로) |
| `revision` | int | `If-Match`에 쓰는 값. 모든 변경 때 오른다 |
| 감사 필드 | | [공통 응답 필드](00-common.md#7-공통-응답-필드) |

`can_enable`이 `true`여도 호출 직전 마지막 일정이 지나가거나 배치가 바뀌면 `enable`이 거절될 수 있다.

---

## 일정 (`schedule`)

```json
{"definition": {...}, "timezone": "Asia/Seoul", "start_at": null, "end_at": null}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `definition` | O | `kind`별 정의 (아래) |
| `timezone` | X | `Asia/Seoul`만 허용 (기본값) |
| `start_at` | X | 이 시각 이후 일정만 (포함). 시간대 포함 date-time |
| `end_at` | X | 이 시각 전 일정만 (미포함). `start_at`보다 뒤여야 함 |

### `definition.kind`

| kind | 필드 | 예 |
| --- | --- | --- |
| `ONCE` | `at` (시간대 포함 date-time, 필수) | `{"kind": "ONCE", "at": "2026-11-01T09:00:00+09:00"}` |
| `INTERVAL` | `seconds` (60~31536000, 필수), `anchor` (date-time, 선택) | `{"kind": "INTERVAL", "seconds": 1800}` |
| `CALENDAR` | `frequency` (`DAILY`·`WEEKLY`·`MONTHLY`), `at` (시간대 없는 `HH:MM:SS`, 필수) | `{"kind": "CALENDAR", "frequency": "DAILY", "at": "08:00:00"}` |
| `CRON` | `expression` (1~128자, 5필드 또는 단축어) | `{"kind": "CRON", "expression": "0 8 * * MON-FRI"}` |

- **CALENDAR WEEKLY**: `weekdays` 필수, `MON`~`SUN` 중 중복 없이. WEEKLY 외에는 쓸 수 없다.
- **CALENDAR MONTHLY**: 다음 중 하나 이상. MONTHLY 외에는 쓸 수 없다.
  - `month_days`: 1~31 (그 날짜가 없는 달은 건너뜀)
  - `last_day: true`: 말일
  - `nth_weekdays: [{"weekday": "MON", "nth": 1}]`: n번째 요일 (`nth` 1~5, -1은 마지막)
- **INTERVAL `anchor`**: "이 시각부터 N초마다"의 기준. 생략하면 `start_at`, 없으면 설정을 저장한 시각으로 채워 응답에 보인다.
- **CRON**: 숫자·범위·목록·간격, 요일·월 이름(`MON`, `JAN`), 단축어 `@hourly`, `@daily`(=`@midnight`), `@weekly`,
  `@monthly`, `@yearly`(=`@annually`). 일·요일을 둘 다 제한하면 OR. 초 단위와 `L`·`W`·`#`는 받지 않는다
  (월말·n번째 요일은 CALENDAR를 쓴다). 실행 시각이 영영 오지 않는 식(예: `0 0 31 2 *`)은 422.
- ONCE의 `at`은 `start_at` 이후, `end_at` 전이어야 한다.

```json
{"definition": {"kind": "CALENDAR", "frequency": "WEEKLY", "at": "08:00:00", "weekdays": ["MON", "WED", "FRI"]}}
{"definition": {"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "last_day": true}}
{"definition": {"kind": "CALENDAR", "frequency": "MONTHLY", "at": "09:00:00", "nth_weekdays": [{"weekday": "MON", "nth": 1}]}}
{"definition": {"kind": "CRON", "expression": "*/10 9-17 * * MON-FRI"}}
```

## 정책 (`policy`)

두 구역(`overlap`, `misfire`)에서 각각 `mode`를 고르고 **그 모드가 쓰는 값만** 보낸다. 모드에 없는 값은 422다.
생략한 값은 기본값으로 채워져 응답에 보인다. `policy`를 통째로 생략하면 둘 다 `SKIP`이다.

| 구역 | mode | 동작 | 추가 값 (기본) |
| --- | --- | --- | --- |
| `overlap` — 이전 실행이 아직 돌 때 | `SKIP` (기본) | 이번 건을 실행하지 않음 (`SKIPPED`/`OVERLAP`) | 없음 |
| | `QUEUE` | 순서대로 대기 | `max_queued` (100) |
| | `PARALLEL` | 동시 실행, 한도를 넘으면 대기 | `max_concurrent` (필수, 1~100), `max_queued` (100) |
| | `REPLACE` | 실행 중인 것을 취소하고 새로 실행 | 없음 |
| `misfire` — 장애 등으로 `grace_seconds` 넘게 놓친 일정 | `SKIP` (기본) | 버림 | `grace_seconds` (300) |
| | `LATEST` | 가장 최근 1회만 실행 | `grace_seconds` (300) |
| | `CATCH_UP` | 놓친 일정을 따라잡아 실행 | `grace_seconds` (300), `max_runs` (10), `max_age_seconds` (86400) |

| 값 | 범위 |
| --- | --- |
| `max_concurrent` | 1~100 |
| `max_queued` | 1~10000. 대기(`WAITING`) 실행이 이만큼 있으면 새 실행은 `SKIPPED`/`SKIPPED_CAPACITY` |
| `grace_seconds` | 1~86400 (기본 300초: 재배포·Pod 재시작 지연을 정상으로 본다) |
| `max_runs` | 1~1000 |
| `max_age_seconds` | 60~31536000 |

수동 실행도 overlap 정책을 따른다. "실행 중"은 `QUEUED`·`RUNNING` 실행을 말한다(`COLLECTING`은 막지 않는다).

```json
{"overlap": {"mode": "PARALLEL", "max_concurrent": 3}, "misfire": {"mode": "CATCH_UP", "max_runs": 5}}
```

---

## POST /api/v1/batches — 등록

새 배치는 항상 **비활성**(`enabled: false`)으로 만들어진다.

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | |
| 헤더 | `X-Project-Id` | X | 보내면 `project_id`로 저장 (이후 변경 불가) |
| 헤더 | `X-Tenant-Id` | X | relay key만. 배치의 tenant가 된다 |
| 본문 | `name` | O | 1~255자 |
| 본문 | `script_id` | O | 같은 tenant·사용자의 스크립트. 아니면 404 |
| 본문 | `script_version_number` | X | 고정할 버전 번호(1 이상). 생략하면 등록 시점 최신 버전 |
| 본문 | `runtime` | O | `GET /runtimes` 목록의 객체 하나 |
| 본문 | `schedule` | X | 일정. 없으면 수동 실행 전용(활성화 불가) |
| 본문 | `policy` | X | 정책. 생략하면 둘 다 `SKIP` |

본문에 다른 필드(`tenant_id`, `enabled` 등)를 넣으면 422다.

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

### 응답 201 — `BatchView`

헤더 `Location: /api/v1/batches/{id}`, `ETag: "1"`.

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
  "tenant_id": "dtest-agent-service",
  "project_id": null,
  "enabled": false,
  "can_enable": true,
  "can_disable": false,
  "enable_blocked_reason": null,
  "disabled_reason": null,
  "disabled_at": null,
  "next_run_at": null,
  "preview_runs": ["2026-11-09T23:00:00Z", "2026-11-10T23:00:00Z", "…"],
  "config_number": 1,
  "revision": 1
}
```

### 오류

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | `script_id`·`script_version_number`가 없거나 내 것이 아님 |
| 422 | `VALIDATION_ERROR` | 필드 형식, 일정 정의, 모드에 없는 정책 값, 모르는 필드 |
| 422 | `UNKNOWN_RUNTIME` | 런타임 목록에 없는 조합 |
| 422 | `RUNTIME_NOT_SUPPORTED` | 스크립트 타입이 그 런타임에서 돌 수 없음 |
| 503 | `RUNTIMES_NOT_CONFIGURED` | `BATCH_RUNTIMES`가 비어 있음 |

---

## GET /api/v1/batches — 목록

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 쿼리 | `limit` | X | 1~100, 기본 50 |
| 쿼리 | `cursor` | X | |
| 쿼리 | `script_id` | X | 이 스크립트를 쓰는 배치만 |

응답 200 `{"items": [BatchView, ...], "next_cursor": ...}`. 최신순. 삭제된 배치는 나오지 않는다.

```sh
curl "$BATCH_URL/api/v1/batches?script_id=$SCRIPT_ID" -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

---

## GET /api/v1/batches/{batch_id} — 상세

응답 200 `BatchView`, 헤더 `ETag`. `enabled`, `can_enable`, `enable_blocked_reason`, `next_run_at`, `preview_runs`로
예약 상태를 확인한다. 404 `NOT_FOUND`.

---

## PATCH /api/v1/batches/{batch_id} — 수정

보낸 필드만 바뀐다(JSON merge 방식).

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | |
| 헤더 | `If-Match` | O | |
| 본문 | `name` | X | `null` 불가 |
| 본문 | `script_id` | X | 다른 스크립트로 변경. `null` 불가 |
| 본문 | `script_version_number` | X | 버전 번호. `null`이면 최신 버전으로 다시 고정 |
| 본문 | `runtime` | X | `null` 불가 |
| 본문 | `schedule` | X | 새 일정 전체. `null`이면 일정 제거 |
| 본문 | `policy` | X | 바꿀 하위 값만. `null` 불가 |

```sh
curl -X PATCH "$BATCH_URL/api/v1/batches/$BATCH_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: "1"' \
  -H 'Content-Type: application/json' \
  -d '{"script_version_number": 2, "policy": {"overlap": {"max_queued": 10}}}'
```

### 규칙

- 빈 본문 `{}`는 422 (`Send at least one field to change.`).
- `script_id`만 보내면 그 스크립트의 최신 버전으로 고정한다.
- `policy`: 같은 모드면 보낸 값만 바뀐다. 다른 `mode`를 보내면 그 구역은 새 모드의 기본값에서 다시 시작한다.
  합친 결과가 맞지 않으면 422 `INVALID_POLICY`(메시지에 어느 값인지 포함). 예: QUEUE에 `max_concurrent`만 보냄.
- 이름 외의 값이 바뀌면 `config_number`가 오른다. 이미 접수된 실행은 접수 당시 설정으로 돈다.
- 활성 배치:
  - `schedule: null`은 409 `SCHEDULE_REQUIRED`. 먼저 비활성화한다.
  - 새 일정에 앞으로의 실행 시각이 없으면 422 `NO_FUTURE_OCCURRENCE`.
  - 일정을 바꾸면 다음 예약은 새 일정으로 다시 계산한다. 이미 도래했지만 아직 처리하지 않은 예약은
    생략 구간(`SCHEDULE_REPLACED`)으로 남는다.
- `X-Project-Id`는 무시한다. 등록 때와 다른 tenant로 요청하면 404다.

### 응답 200 — `BatchView`

변경된 BatchView와 새 `ETag`.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 배치·새 스크립트·버전 없음 |
| 409 | `REVISION_CONFLICT` | |
| 409 | `SCHEDULE_REQUIRED` | 활성 배치의 일정 제거 |
| 422 | `VALIDATION_ERROR`, `INVALID_REVISION`, `INVALID_POLICY` | |
| 422 | `NO_FUTURE_OCCURRENCE` | 활성 배치의 새 일정에 앞으로 실행할 시각이 없음 |
| 422 | `UNKNOWN_RUNTIME`, `RUNTIME_NOT_SUPPORTED` | 런타임·스크립트 변경 시 |
| 503 | `RUNTIMES_NOT_CONFIGURED` | 런타임·스크립트 변경 시 |

---

## DELETE /api/v1/batches/{batch_id} — 삭제

soft delete. 활성 배치는 비활성화된다. 이후 배치와 그 실행·생략 구간 조회는 404다.

| 구분 | 이름 | 필수 |
| --- | --- | --- |
| 헤더 | `Idempotency-Key` | O |
| 헤더 | `If-Match` | O |

```sh
curl -X DELETE "$BATCH_URL/api/v1/batches/$BATCH_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: "3"'
```

응답 204, 본문 없음.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | |
| 409 | `REVISION_CONFLICT` | |
| 409 | `BATCH_HAS_ACTIVE_RUNS` | `WAITING`·`QUEUED`·`RUNNING`·`COLLECTING` 실행이 있음. 취소하거나 끝나길 기다린다 |

---

## POST /api/v1/batches/{batch_id}/enable — 예약 활성화

본문 없음. 현재 시각 이후의 첫 일정부터 예약한다. 비활성 동안 지난 일정은 소급하지 않는다.
켜지면 `disabled_reason`·`disabled_at`이 지워진다(`USER_DEACTIVATED`·`TENANT_REVOKED`로 꺼진 배치도 이렇게 다시 켠다).

| 구분 | 이름 | 필수 |
| --- | --- | --- |
| 헤더 | `Idempotency-Key` | O |
| 헤더 | `If-Match` | O |

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/enable" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: "1"'
```

응답 200, `enabled: true`인 BatchView와 새 `ETag`. 이미 켜져 있으면 아무것도 바꾸지 않고 200(`revision` 그대로)이다.
켜기 전에 `can_enable`·`enable_blocked_reason`으로 결과를 미리 알 수 있다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | |
| 409 | `REVISION_CONFLICT` | |
| 409 | `SCHEDULE_REQUIRED` | 일정이 없는 배치 |
| 422 | `NO_FUTURE_OCCURRENCE` | 앞으로 실행할 시각이 없음 (지난 ONCE, 끝난 기간) |
| 422 | `UNKNOWN_RUNTIME`, `RUNTIME_NOT_SUPPORTED` | 배치의 런타임을 지금 쓸 수 없음 |
| 503 | `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` | Executor·런타임 목록 미설정 |

---

## POST /api/v1/batches/{batch_id}/disable — 예약 중지

본문 없음. 새 예약 접수만 멈추고 이미 접수·실행 중인 실행은 취소하지 않는다.
이미 도래했지만 아직 처리하지 않은 예약은 생략 구간(`DISABLED`)으로 남는다.

헤더는 enable과 같다(`Idempotency-Key`, `If-Match`).

응답 200, `enabled: false`, `disabled_reason: "OWNER"`인 BatchView와 새 `ETag`. 이미 꺼져 있으면 200(변경 없음).
오류: 404 `NOT_FOUND`, 409 `REVISION_CONFLICT`.

---

## GET /api/v1/batches/{batch_id}/skipped-windows — 생략된 예약 구간

실행 행을 만들지 않고 건너뛴 일정 구간이다. 쿼리 `limit`, `cursor`. 최신순.

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

| 필드 | 설명 |
| --- | --- |
| `from_at` ~ `until_at` | 건너뛴 구간 (시작 포함, 끝 미포함) |
| `occurrences` | 그 구간 안의 일정 수 |
| `config_number` | 당시 설정 번호 |
| `reason` | 아래 표 |

| `reason` | 원인 |
| --- | --- |
| `MISFIRE_SKIP` | `grace_seconds` 넘게 놓친 일정을 misfire `SKIP`으로 버림 |
| `MISFIRE_LATEST` | misfire `LATEST`로 최근 1회만 실행하고 버린 나머지 |
| `MISFIRE_CATCH_UP` | misfire `CATCH_UP` 한도(`max_runs`, `max_age_seconds`) 밖 |
| `REPLACED_BY_LATEST` | overlap `REPLACE`에서 여러 일정이 한꺼번에 도래해 가장 최근 1회만 실행하고 나머지를 버림 |
| `DISABLED` | 사용자가 비활성화 |
| `SCHEDULE_REPLACED` | 활성 상태에서 일정을 바꿈 |
| `DELETED` | 배치 삭제 |
| `OWNER_DEACTIVATED` | disable-by-owner |
| `TENANT_REVOKED` | disable-by-tenant |
| `SCHEDULER_ERROR` | 예약 접수가 계속 실패해(기본 300초 간격 3회 재시도) 그 일정을 건너뜀 |

---

## POST /api/v1/batches:disable-by-owner — 사용자 비활성 (운영)

퇴사 등으로 사용자가 비활성되면 그 사용자의 켜진 배치를 모두 끈다. 다시 불러도 결과가 같다.

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `X-Api-Key` | O | **admin key** |
| 헤더 | `X-Tenant-Id` | X | 보내면 그 tenant만. relay key의 허용 범위 안이어야 함 |
| 본문 | `owner_id` | O | 사용자 ID (`X-User-Id`와 같은 규칙, 소문자로 비교) |
| 본문 | `reason` | O | `USER_DEACTIVATED` |

`Idempotency-Key`, `X-User-Id`는 필요 없다.

적용 범위:

- `X-Tenant-Id`를 보내면 그 tenant 안의 배치에만.
- 보내지 않으면 그 key가 쓸 수 있는 tenant 전체. relay key는 기본 tenant + `allowed_tenants`
  (`["*"]`면 모든 tenant), relay가 아닌 key는 기본 tenant.

```sh
curl -X POST "$BATCH_URL/api/v1/batches:disable-by-owner" \
  -H "X-Api-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"owner_id": "e12345", "reason": "USER_DEACTIVATED"}'
```

### 응답 200 — `DisabledCount`

```json
{"disabled": 3}
```

- 켜져 있던 배치를 끄고 `disabled_reason=USER_DEACTIVATED`를 남긴다. `disabled`는 범위 전체에서 꺼진 배치 수다.
  이미 꺼져 있던 배치는 그대로 둔다.
- 이미 Executor에 제출된 실행은 계속 돈다. 대기(`WAITING`) 실행은 `SKIPPED`(`OWNER_DEACTIVATED`)가 된다.
- 이렇게 꺼진 배치는 수동 실행도 409 `OWNER_DEACTIVATED`다. 자동으로 되살리지 않으며, 사용자가 직접 켜면 풀린다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | |
| 403 | `FORBIDDEN` | admin이 아닌 key, 허용 밖 `X-Tenant-Id` |
| 422 | `VALIDATION_ERROR` | `owner_id` 형식, `reason` 값 오류 |

---

## POST /api/v1/batches:disable-by-tenant — tenant 폐기 (운영)

tenant를 폐기하면 그 tenant의 배치를 모두 멈춘다. pr-batch는 자기 key로 Executor에 제출하므로, 호출 서비스가
tenant key를 폐기하는 것만으로는 예약 실행이 멈추지 않는다. 다시 불러도 결과가 같다.

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `X-Api-Key` | O | **admin key** |
| 헤더 | `X-Tenant-Id` | **O** | 폐기할 tenant. 그 key가 쓸 수 있는 tenant여야 함 |
| 본문 | `reason` | O | `TENANT_REVOKED` |

`Idempotency-Key`, `X-User-Id`는 필요 없다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches:disable-by-tenant" \
  -H "X-Api-Key: $ADMIN_KEY" -H "X-Tenant-Id: upper-agent" -H "Content-Type: application/json" \
  -d '{"reason": "TENANT_REVOKED"}'
```

### 응답 200 — `DisabledCount`

```json
{"disabled": 5}
```

- 그 tenant의 **모든** 배치를 `disabled_reason=TENANT_REVOKED`로 표시한다. 이미 꺼져 있던 배치도 사유가 바뀐다
  (꺼진 시각은 유지). `disabled`는 켜져 있다가 꺼진 배치 수다.
- 이미 Executor에 제출된 실행은 계속 돈다. 대기(`WAITING`) 실행은 `SKIPPED`(`TENANT_REVOKED`)가 된다.
- 이렇게 표시된 배치는 수동 실행도 409 `TENANT_REVOKED`다. 자동으로 되살리지 않으며, 배치를 직접 켜면 풀린다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 400 | `INVALID_IDENTITY` | `X-Tenant-Id` 없음·형식 오류 |
| 401 | `UNAUTHENTICATED` | |
| 403 | `FORBIDDEN` | admin이 아닌 key, relay가 아니거나 허용 밖 tenant |
| 422 | `VALIDATION_ERROR` | `reason` 값 오류 |
