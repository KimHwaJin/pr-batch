# 실행 API와 처리 파이프라인

수동 실행 접수·취소·이력 조회와, 실행을 끝까지 진행시키는 내부 Task 파이프라인을
설명한다. 예약 활성화와 스케줄러는 [예약 실행 안내](scheduling-api.md), 전체 구조는
[목표 아키텍처](architecture.md)와 [서비스 체계](domain-model.md)를 따른다.

## 실행 준비

API 프로세스 하나가 HTTP API와 백그라운드 런타임(스케줄러 루프 + Task dispatcher)을 함께
실행한다. 별도 워커나 스케줄러 프로세스는 없다. 시작 시 DB migration도 자동으로 적용한다.

```sh
uv sync --locked
# .env: EXECUTOR_BASE_URL=http://127.0.0.1:8000 (/api/v1은 붙이지 않는다)
make serve   # http://127.0.0.1:8100
```

| 설정 | 기본 | 의미 |
| --- | --- | --- |
| `EXECUTOR_BASE_URL` | 없음 | 없으면 실행 접수·배치 등록·수정·활성화는 503 `EXECUTOR_NOT_CONFIGURED`(런타임 목록을 확인할 수 없음), 백그라운드 런타임도 시작하지 않음 |
| `BACKGROUND_ENABLED` | true | 이 프로세스에서 백그라운드 런타임 실행 |
| `DB_AUTO_MIGRATE` | true | 시작 시 advisory lock 아래 `alembic upgrade head` |
| `TASK_CONCURRENCY` | 4 | Pod당 동시에 처리하는 Task 수 (= Executor 동시 요청 상한) |
| `SYNC_INITIAL_SECONDS` / `SYNC_MAX_SECONDS` | 5 / 60 | 상태 조회 간격. 두 배씩 늘려 최대값까지 |
| `TASK_LEASE_SECONDS` | 90 | Task lease. 만료되면 다른 Pod가 이어받음 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 5 / 5 | Pod당 최대 DB 커넥션 10 (백그라운드 루프 6개 + API 요청) |
| `SHUTDOWN_GRACE_SECONDS` | 40 | SIGTERM 후 진행 중인 원격 호출을 끝내는 최대 시간 |
| `TASK_GIVE_UP_SECONDS` | 86400 | 원격 호출이 이만큼 계속 실패하면 Run을 FAILED(`ABANDONED`)로 끝냄 |
| `IDEMPOTENCY_RETENTION_DAYS` | 7 | 멱등성 기록 보존 기간. 지나면 같은 키도 새 요청으로 처리 |
| `TASK_RETENTION_DAYS` | 30 | 끝난 내부 Task 보존 기간. 실행 이력은 지우지 않음 |

Executor의 `INPUT_STORAGE_ROOT`에서 Batch가 저장한
`batch/inputs/tenants/.../users/.../scripts/.../versions/.../cells/*.py`를 같은 상대경로로 읽을 수 있어야 한다.
PV는 프로젝트 루트(예: `/mnt/data/dtest`) 아래 서비스별 폴더(`batch/`, `executor/`)로 나눈다.
`STORAGE_ROOT`와 Executor `INPUT_STORAGE_ROOT`는 프로젝트 루트,
Executor `SHARED_STORAGE_ROOT`는 `<프로젝트 루트>/executor`로 둔다.
런타임 선택지는 설정 `BATCH_RUNTIMES`에서 읽는다([등록 API 안내](registration-api.md)의 런타임 항목).
Executor 호출(제출·조회·결과·취소)에는 `X-Api-Key: <EXECUTOR_API_KEY>`와 실행 스냅샷의 신원 헤더
`X-User-Id`, `X-Tenant-Id`(항상), `X-Project-Id`(있을 때)를 붙인다. 호출 서비스가 pr-batch에 보낸 key는 넘기지 않는다.

## HTTP 계약

공통 prefix는 `/api/v1/batches/{batch_id}/runs`이며 API key와 `X-User-Id`([인증 가이드](client-auth.md))가 필요하다. 다른 사용자의 배치·실행과
삭제된 배치의 실행은 404다(이력은 DB에 보관). 진행 중인 실행이 있는 배치는 삭제할 수 없다
([등록 API 안내](registration-api.md#삭제)).

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | 현재 설정으로 수동 실행 접수. 202 (대기열 초과 409) |
| GET | / | 접수 역순 이력, limit 1~100·cursor(이전 응답의 `next_cursor`를 그대로 보냄) |
| GET | /{run_id} | 상태·스냅샷·셀별 Step |
| POST | /{run_id}/cancel | 취소 요청. 202 |

쓰기에는 `Idempotency-Key`가 필수다. 수동 실행 본문은 생략하거나 `{}`이며 버전·런타임 override는 422다.
일정이 없는 배치나 비활성 배치도 수동 실행할 수 있다.

| 접수 결과 | HTTP | status | skip_reason |
| --- | --- | --- | --- |
| 자리 확보, 제출 대기 | 202 | QUEUED | |
| QUEUE/PARALLEL 한도 초과 | 202 | WAITING | |
| SKIP 정책, 이미 실행 중 | 202 | SKIPPED | OVERLAP |
| 대기열 `max_queued` 초과 | 409 | SKIPPED | SKIPPED_CAPACITY |

## Run 상태

| status | 의미 | 다음 실행을 막는가 |
| --- | --- | --- |
| WAITING | 동시 실행 자리 대기 | 아니오 |
| QUEUED | 자리 확보, Executor 제출 대기·진행 | 예 |
| RUNNING | Executor에서 코드 실행 중 | 예 |
| COLLECTING | 코드 종료, 셀 결과·결과 노트북 수집 | 아니오 |
| COMPLETED | 코드 성공, 결과 수집 완료 | — |
| FAILED | 코드 실행 실패, Executor가 제출을 거절(`failure_type=SUBMIT_REJECTED`), 또는 원격 호출이 계속 실패해 포기(`failure_type=ABANDONED`) | — |
| CANCELLED | 사용자 취소 또는 REPLACE 정책 취소 (`cancel_source`) | — |
| SKIPPED | 겹침·대기열 초과로 실행 안 함 | — |

응답의 다른 필드: `execution_status`(Executor 원본 상태), `failure_type`, `notebook_status`,
`last_error`(통신·계약 오류. 코드 실패와 별개), `cancel_requested`, `cancel_source`(USER·POLICY),
`cancel_reason`, `artifacts`, `attempts`, `retry_info`, `result`, `steps`(상세만). 출력 원문·표·이미지는
복제하지 않고 `result_ref`와 artifact ID로만 가리킨다.

**결과 다운로드는 pr-batch가 중계하지 않는다.** `result.notebook_artifact_id`(실행 결과 노트북)를 보고, prj_re가
Executor `GET /api/v1/artifacts/{artifact_id}/content`로 내려받아 사용자에게 전달한다. Executor를 브라우저에
직접 노출하지 않고, 호출 서비스는 먼저 그 사용자로 pr-batch에서 실행을 조회해 소유권을 확인한 뒤 자기 Executor key와
같은 `X-User-Id`·`X-Tenant-Id`(배치의 `tenant_id`)로 그 artifact만 내려받는다.

**리포트는 pr-batch가 만들지 않는다.** pr-agentic-service가 Executor `execution.completed` 이벤트(성공
회차만)를 받아 직접 작성한다. 이벤트의 `execution.metadata.run_id`가 pr-batch `run_id`다.

취소: WAITING은 즉시 CANCELLED. QUEUED는 응답이 QUEUED + `cancel_requested=true`이고, 제출 Task가
Executor에 닿기 전이면 CANCELLED로 끝난다. 앞선 제출 시도의 결과가 불확실하면 같은 키로 재제출해 실행을
확인한 뒤 취소한다. RUNNING은 Executor에 취소를
요청하고 실제 종료를 확인한 뒤 CANCELLED. COLLECTING 이후는 409 `NOT_CANCELLABLE`.
처음 들어온 취소 요청의 출처와 사유를 `cancel_source`·`cancel_reason`에 남기고, Executor 취소에는 항상
actor를 보낸다. 사용자 취소는 `{type: USER, id: owner_id}`, REPLACE 정책 취소는 수동 실행이 계기여도
`{type: BATCH, id: batch_id}`다.

## 실행과 Executor Execution 연결

pr-batch의 실행(Run)과 Executor의 Execution은 1:1이고 양쪽에서 서로를 찾을 수 있다.

| 방향 | 방법 |
| --- | --- |
| pr-batch → Executor | 실행 응답의 `execution_id`(제출 후 채워짐, QUEUED 동안은 null), `operation_id`, 상세의 셀별 `steps[].step_id` |
| Executor → pr-batch | Execution의 `context.user_id` = 배치 소유자(`X-User-Id`, 소문자), `metadata`에 `batch_id`, `script_id`, `script_version_id`, `run_id`(= pr-batch `run_id`). `execution.completed` 이벤트의 `execution.metadata`에도 같은 값이 실린다 |

제출 trigger는 `{type: BATCH, actor: {type: USER, id: owner_id}}`다. actor가 소유자이므로 배치는
`metadata.batch_id`로 식별한다. Executor가 BATCH trigger에 USER actor를 허용한 뒤에 배포해야 한다.

제출 본문에는 `context`와 `tenant_id`를 넣지 않는다. 신원은 헤더로만 보낸다. tenant와 project는 실행을 만들 때
`snapshot`에 고정되므로, 나중에 배치를 바꿔도 이미 접수된 실행은 같은 헤더로 제출·조회·취소된다(재제출도 같은 값).
회차 식별자는 `metadata.run_id`에 둔다. 실행 완료는 Executor 이벤트가 아니라 `GET /executions/{id}` 폴링으로 확인한다.

예: pr-batch 실행 응답의 `execution_id`로 Executor `GET /api/v1/executions/{execution_id}`, `/events`, `/steps`를 조회해 진행 상황을
자세히 볼 수 있다. Executor는 호출 서비스의 key와 같은 `X-User-Id`·`X-Tenant-Id`를 요구하므로, 사용자에게는
prj_re가 pr-batch로 소유권을 확인한 뒤 중계한다.

## 내부 Task 파이프라인

상태를 바꾸는 트랜잭션 안에서 그 결과로 필요한 외부 호출을 `tasks` 테이블에 넣는다(트랜잭션 outbox).
모든 Pod의 dispatcher가 `FOR UPDATE SKIP LOCKED`와 lease로 Task를 나눠 처리한다.

| Task | 원격 호출 | 결과 |
| --- | --- | --- |
| SUBMIT_EXECUTION | Executor 제출 (멱등성 키 `batch-run:<run_id>:submit`) | RUNNING + SYNC 예약 |
| SYNC_EXECUTION | Executor 상태 조회 | 진행 중이면 간격을 늘려 재조회, 종료면 COLLECTING + 대기 실행 승격 |
| COLLECT_RESULT | Executor 결과 조회 | 성공: COMPLETED / 취소: CANCELLED / 그 외: FAILED |
| CANCEL_EXECUTION | Executor 취소 요청 | SYNC가 실제 종료를 관측 |

- 처리 순서: 짧은 claim 트랜잭션 → 커넥션 반납 → 원격 호출 1회 → 짧은 반영 트랜잭션. 원격 호출 중에는
  DB 커넥션을 잡지 않는다.
- Handler는 실행 직전에 Run 상태를 확인하고, 이미 지난 단계의 Task면 아무것도 하지 않고 끝낸다.
- 반영 시 lease 토큰이 바뀌었거나 만료됐으면 버린다(늦게 끝난 옛 Pod의 쓰기 차단).
- Executor가 제출을 확실히 거절하면(422 전부, 409 `INVALID_STATE_TRANSITION`, 400·401·403 전부) 같은
  요청으로는 성공할 수 없으므로 FAILED(`failure_type=SUBMIT_REJECTED`, `last_error=EXECUTOR_HTTP_{status}:{code}`)로
  끝내고 자리를 푼다. 400·401·403은 key·tenant 설정 문제라 ERROR 로그로 남긴다. 단 앞선 제출 시도가 응답 없이
  끝났거나, 그 작업을 맡았던 Pod가 반영 전에 죽어 lease가 만료됐으면(시도 횟수 2 이상) 그 실행이 이미
  만들어졌을 수 있어(Executor는 멱등성 조회 전에 검증한다) 거절도 재시도하고, 그사이 취소 요청이 오면 같은
  키로 다시 제출한 뒤 취소한다. 409 `IDEMPOTENCY_CONFLICT`(같은 키의 실행이
  있음)·`PERSISTENCE_CONFLICT`도 재시도한다. 취소의 409는 `INVALID_STATE_TRANSITION`일 때만 "이미 종료"다.
- 통신·계약 오류는 실행 실패가 아니다. `last_error`에 남기고 2초부터 최대 300초까지 지수 backoff +
  jitter로 다시 시도한다. 제출 응답이 유실돼도 같은 멱등성 키로 재확인하므로 새 Execution이 생기지 않는다.
- 첫 실패부터 `TASK_GIVE_UP_SECONDS`(기본 24시간) 동안 한 번도 성공하지 못하면 Task를 끝내고 Run을
  FAILED(`failure_type=ABANDONED`)로 바꿔 자리를 푼다. 이때 Executor 실행이 아직 끝나지 않았으면
  (`execution_id`가 있고 종료 상태가 아님) 키 `batch-run:{id}:abandon-cancel`로 취소를 한 번만 보낸다.
  이 취소는 실패해도 재시도하지 않으므로 Executor 쪽 실행이 남아 있을 수 있고, `execution_id`로 확인한다. 취소 Task가 포기되면 Run은 그대로 두고 상태 조회가 계속된다.
- Executor의 `SUCCEEDED`, `FAILED`, `CANCELLED`는 코드 실행 종료이며, 이때 다음 실행 자리가 풀린다.
  Executor는 결과 노트북 projection과 Artifact 등록이 끝난 뒤에만 `SUCCEEDED`를 보고하므로 결과는 한 번만
  조회한다.
- Executor 응답은 요청 10초, 처리 30초, 4 MiB로 제한한다. 낮은 상태 버전, 다른 실행·소유자·셀의 응답은
  반영하지 않는다. Batch는 Executor `/retry`를 호출하지 않는다(자동 재시도 정책 보류).

## 검증 범위

실제 PostgreSQL에서 접수·동시성·대기열 승격·lease 회수·오래된 응답 거절·취소 actor를 검증한다.
Executor는 계약에 맞춘 테스트 서버로 응답 유실, 통신 장애, 불확실한 제출 뒤 취소, 장기 장애 포기를 재현한다. 백그라운드 런타임은 실제 스레드로 시작·처리·정지를 검증한다. Docker에서는 로컬 Executor와
연결해 수동·예약 실행 완료와 결과 다운로드를 확인한다.
