# 04. 실행 API (4개)

라우터: `src/app/api/v1/runs.py` · 태그 `runs` · 공통 규칙은 [00-common.md](00-common.md)

| Method | 경로 | 설명 |
| --- | --- | --- |
| POST | `/api/v1/batches/{batch_id}/runs` | 수동 실행 |
| GET | `/api/v1/batches/{batch_id}/runs` | 실행 이력 |
| GET | `/api/v1/batches/{batch_id}/runs/{run_id}` | 실행 상세 (셀별 step 포함) |
| POST | `/api/v1/batches/{batch_id}/runs/{run_id}/cancel` | 실행 취소 |

모든 요청에 `X-Api-Key`가 필요하다. `X-User-Id`는 실행·취소에 필수, 조회에 선택이다(빼면 tenant 전체 배치의 실행,
[조회 범위](00-common.md#조회-범위-읽기)). 배치가 없거나 보이지 않거나 삭제됐으면 404 `NOT_FOUND`다.

## 실행 상태

```
              ┌──────────┐ 슬롯 생김 ┌────────┐ Executor 제출 ┌─────────┐ 코드 종료 ┌────────────┐
접수 ──────▶ │ WAITING  │ ────────▶ │ QUEUED │ ────────────▶ │ RUNNING │ ────────▶ │ COLLECTING │ ──▶ COMPLETED / FAILED
              └──────────┘           └────────┘               └─────────┘           └────────────┘
   (정책상 실행 안 함) ──▶ SKIPPED          취소 ──▶ CANCELLED (WAITING·QUEUED·RUNNING에서만)
```

| `status` | 의미 | 끝난 상태 | 취소 가능 |
| --- | --- | --- | --- |
| `WAITING` | 동시 실행 자리(overlap 정책) 대기 | | O |
| `QUEUED` | 자리 확보, Executor 제출 대기·진행 | | O |
| `RUNNING` | Executor에서 코드 실행 중 | | O |
| `COLLECTING` | 코드 종료, 셀 결과 수집 중 | | |
| `COMPLETED` | 성공 | O | |
| `FAILED` | 실패 (`failure_type` 참고) | O | |
| `CANCELLED` | 취소됨 (`cancel_source`: `USER` 사용자 취소, `POLICY` overlap `REPLACE`로 취소) | O | |
| `SKIPPED` | 실행하지 않음 (`skip_reason` 참고) | O | |

| `skip_reason` | 원인 |
| --- | --- |
| `OVERLAP` | overlap `SKIP`인데 이미 실행 중 |
| `SKIPPED_CAPACITY` | 대기 실행이 `max_queued`만큼 쌓여 있음 |
| `OWNER_DEACTIVATED` | 대기 중 disable-by-owner |
| `TENANT_REVOKED` | 대기 중 disable-by-tenant |

| `failure_type` | 원인 |
| --- | --- |
| Executor 실패 유형 (코드 오류 등) | Executor가 돌려준 값 그대로 |
| `SUBMIT_REJECTED` | Executor가 제출을 거절함 |
| `ABANDONED` | Executor와 통신이 24시간(`TASK_GIVE_UP_SECONDS`) 동안 실패해 포기 |

## 응답 모델 — RunView

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | UUID | 실행 ID (`run_id`) |
| `batch_id`, `script_id`, `script_version_id` | UUID | 실행 당시 대상 |
| `owner_id` | string | 배치 소유자 (Executor 실행의 주인) |
| `config_number` | int | 실행 당시 배치 설정 번호 |
| `runtime` | object | 실행 당시 런타임 |
| `snapshot` | object | 실행 당시 설정 사본: `script_id`, `script_version_id`, `sha256`, `code_hash`, `schedule`, `policy`, `tenant_id`, `project_id` |
| `trigger` | `MANUAL` \| `SCHEDULED` | 수동 / 예약 |
| `scheduled_for` | date-time \| null | 예약 실행의 일정 시각 |
| `status` | string | [실행 상태](#실행-상태) |
| `skip_reason` | string \| null | `SKIPPED`일 때 |
| `cancel_requested` | bool | 취소 요청이 접수됐는지 |
| `cancel_source`, `cancel_reason` | string \| null | 취소 주체(`USER`·`POLICY`)와 사유 |
| `execution_id`, `operation_id` | UUID \| null | Executor 실행·작업 ID. 제출 전 `null` |
| `execution_status` | string \| null | Executor 실행 상태 (`SUCCEEDED`, `FAILED`, `CANCELLED` 등) |
| `observed_version` | int \| null | 마지막으로 본 Executor 상태 버전 |
| `failure_type` | string \| null | `FAILED`일 때 |
| `notebook_status` | string \| null | Executor 결과 노트북 작업 상태. 실행이 끝난 뒤의 값만 의미가 있다 |
| `last_error` | string \| null | pr-batch↔Executor 통신 오류 기록. 서버가 자동 재시도하며 코드 실패와 별개 |
| `artifacts` | object[] | Executor 산출물 목록 |
| `attempts` | object[] | Executor 시도 기록 |
| `retry_info` | object \| null | Executor 재시도 정보 |
| `result.notebook_artifact_id` | string \| null | 결과 노트북의 Executor artifact ID (`AVAILABLE`일 때만) |
| `submitted_at`, `started_at`, `finished_at`, `observed_at`, `completed_at` | date-time \| null | 제출 / 코드 시작 / 코드 종료 / 마지막 상태 확인 / pr-batch에서 끝난 시각 |
| 감사 필드 | | [공통 응답 필드](00-common.md#7-공통-응답-필드) |

**RunDetail**은 RunView에 `steps`가 추가된다.

| 필드 | 설명 |
| --- | --- |
| `steps[].sequence` | 실행 순서 (0부터). 버전 상세 `cell_list[].sequence`와 같다 |
| `steps[].original_index` | 원본 노트북의 셀 위치 |
| `steps[].source_sha256` | 셀 코드 해시 |
| `steps[].step_id` | Executor step ID. 제출 전 `null` |
| `steps[].status` | Executor step 상태 |
| `steps[].result_ref` | Executor step 결과 참조 |

---

## POST /api/v1/batches/{batch_id}/runs — 수동 실행

배치의 **현재 설정**으로 실행을 접수한다. 비활성 배치나 일정이 없는 배치도 실행할 수 있다.
수동 실행도 overlap 정책을 따른다.

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | 재시도는 같은 키 (같은 키는 실행을 하나만 만든다) |
| 본문 | — | X | 생략하거나 `{}`만. 버전·런타임 등을 넣으면 422 |

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/runs" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)"
```

### 응답 — `RunView`

| 결과 | HTTP | `status` | `skip_reason` |
| --- | --- | --- | --- |
| 접수, 제출 대기 | 202 | `QUEUED` | |
| 동시 실행 한도로 대기 | 202 | `WAITING` | |
| overlap `SKIP`인데 이미 실행 중 | 202 | `SKIPPED` | `OVERLAP` |
| 대기열(`max_queued`) 초과 | **409** | `SKIPPED` | `SKIPPED_CAPACITY` |

409 `SKIPPED_CAPACITY`여도 본문은 오류 형식이 아니라 **RunView**다(생략 이력으로 남는다). `body.error`가 아니라
`body.status`를 본다. overlap `REPLACE`면 실행 중이던 실행에 취소를 요청하고(`cancel_source: POLICY`) 새 실행을 접수한다.

### 오류

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 배치 없음 |
| 409 | `OWNER_DEACTIVATED` | 사용자 비활성으로 꺼진 배치 (`disabled_reason=USER_DEACTIVATED`). 배치를 다시 켜야 실행 가능 |
| 409 | `TENANT_REVOKED` | tenant 폐기로 꺼진 배치 (`disabled_reason=TENANT_REVOKED`). 배치를 다시 켜야 실행 가능 |
| 422 | `VALIDATION_ERROR` | 본문에 필드가 있음 |
| 503 | `EXECUTOR_NOT_CONFIGURED` | Executor 미설정 |

---

## GET /api/v1/batches/{batch_id}/runs — 실행 이력

쿼리 `limit`(1~100, 기본 50), `cursor`. 응답 200 `{"items": [RunView, ...], "next_cursor": ...}`. 접수 역순.
예약 실행은 `trigger: "SCHEDULED"`와 `scheduled_for`가 채워진다.

```sh
curl "$BATCH_URL/api/v1/batches/$BATCH_ID/runs?limit=20" -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

---

## GET /api/v1/batches/{batch_id}/runs/{run_id} — 실행 상세

응답 200 `RunDetail`. 실행이 끝날 때까지(`COMPLETED`·`FAILED`·`CANCELLED`·`SKIPPED`) 이 API를 주기적으로 조회한다
(5초 이상 간격 권장).

```json
{
  "id": "a7b8c9d0-…",
  "batch_id": "5f1e8a3b-…",
  "owner_id": "e12345",
  "script_id": "8d3c2f0e-…",
  "script_version_id": "0b9a7e52-…",
  "config_number": 1,
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "snapshot": {"script_id": "8d3c2f0e-…", "script_version_id": "0b9a7e52-…", "sha256": "…", "code_hash": "…",
               "schedule": {}, "policy": {}, "tenant_id": "dtest-agent-service", "project_id": null},
  "trigger": "MANUAL",
  "scheduled_for": null,
  "status": "COMPLETED",
  "skip_reason": null,
  "cancel_requested": false,
  "cancel_source": null,
  "cancel_reason": null,
  "execution_id": "e1f2…",
  "operation_id": "0a1b…",
  "execution_status": "SUCCEEDED",
  "observed_version": 7,
  "failure_type": null,
  "notebook_status": "SUCCEEDED",
  "last_error": null,
  "artifacts": [{"type": "NOTEBOOK", "status": "AVAILABLE", "artifact_id": "art_…"}],
  "attempts": [],
  "retry_info": {},
  "result": {"notebook_artifact_id": "art_…"},
  "submitted_at": "2026-11-08T05:10:01Z",
  "started_at": "2026-11-08T05:10:03Z",
  "finished_at": "2026-11-08T05:12:40Z",
  "observed_at": "2026-11-08T05:12:45Z",
  "completed_at": "2026-11-08T05:12:45Z",
  "steps": [
    {"sequence": 0, "original_index": 1, "source_sha256": "…", "step_id": "…", "status": "SUCCEEDED", "result_ref": {}}
  ]
}
```

- `notebook_status`는 Executor가 결과 노트북을 만드는 작업의 상태다. 실행 중에는 앞선 셀까지의 중간 결과 기준이라
  나중에 바뀔 수 있으므로, 실행이 끝난 뒤의 값만 본다. 취소된 실행은 노트북이 마무리되지 않아 보통 `FAILED`다.
- 404 `NOT_FOUND`: 배치·실행 없음, 또는 실행이 그 배치의 것이 아님.

---

## POST /api/v1/batches/{batch_id}/runs/{run_id}/cancel — 취소

본문 없음.

| 구분 | 이름 | 필수 |
| --- | --- | --- |
| 헤더 | `Idempotency-Key` | O |

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/runs/$RUN_ID/cancel" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)"
```

응답 202, 취소 요청이 반영된 RunView.

| 취소 전 상태 | 결과 |
| --- | --- |
| `WAITING` | 바로 `CANCELLED` |
| `QUEUED` | 상태 그대로, `cancel_requested: true`. 제출 전이면 곧 `CANCELLED`, 제출됐으면 Executor 종료 확인 뒤 `CANCELLED` |
| `RUNNING` | 상태 그대로, `cancel_requested: true`. Executor에 취소를 요청하고 종료가 확인되면 `CANCELLED` |

최종 상태는 상세 GET으로 확인한다. 이미 취소 요청된 실행에 다시 보내도 202다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | |
| 409 | `NOT_CANCELLABLE` | `COLLECTING` 이후 또는 이미 끝난 실행 |

---

## 결과 노트북 받기

pr-batch는 결과 파일을 중계하지 않는다.

1. `GET /batches/{batch_id}/runs/{run_id}`로 실행이 `COMPLETED`인지, 그 사용자 것인지 확인한다.
2. `result.notebook_artifact_id`를 Executor `GET /api/v1/artifacts/{artifact_id}/content`로 받는다.
   이때 **호출 서비스 자기의 Executor key**와 같은 `X-User-Id`(배치 tenant가 기본이 아니면 `X-Tenant-Id`도)를 보낸다.
   Executor 실행의 주인은 배치 소유자다.

## 완료 알림 (Executor 이벤트)

- 배치 실행의 Executor 이벤트는 배치의 tenant 스트림 `executor.events.<tenant>`로 간다.
- 이벤트의 `execution.metadata`에 `batch_id`, `script_id`, `script_version_id`, `run_id`가 실린다.
  `run_id`가 pr-batch 실행 ID다.
- **이벤트는 "바뀌었다"는 알림으로만 쓴다.** Redis에 접근 제어가 없어 위조될 수 있으므로, 상태와 결과는 `run_id`로
  pr-batch를 다시 조회해 확정한다. 모르는 `run_id`의 이벤트는 버린다.
- 이벤트를 받지 않는 서비스는 실행 상세를 5초 이상 간격으로 폴링한다.
