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
| `EXECUTOR_BASE_URL` | 없음 | 없으면 실행 접수·활성화는 503 `EXECUTOR_NOT_CONFIGURED`, 백그라운드 런타임도 시작하지 않음 |
| `BACKGROUND_ENABLED` | true | 이 프로세스에서 백그라운드 런타임 실행 |
| `DB_AUTO_MIGRATE` | true | 시작 시 advisory lock 아래 `alembic upgrade head` |
| `TASK_CONCURRENCY` | 4 | Pod당 동시에 처리하는 Task 수 (= Executor 동시 요청 상한) |
| `SYNC_INITIAL_SECONDS` / `SYNC_MAX_SECONDS` | 5 / 60 | 상태 조회 간격. 두 배씩 늘려 최대값까지 |
| `TASK_LEASE_SECONDS` | 90 | Task lease. 만료되면 다른 Pod가 이어받음 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 3 / 3 | Pod당 최대 DB 커넥션 6 |
| `SHUTDOWN_GRACE_SECONDS` | 40 | SIGTERM 후 진행 중인 원격 호출을 끝내는 최대 시간 |

Executor의 `INPUT_STORAGE_ROOT`에서 Batch가 저장한
`batch/inputs/users/.../scripts/.../versions/.../cells/*.py`를 같은 상대경로로 읽을 수 있어야 한다.
PV는 프로젝트 루트(예: `/mnt/data/dtest`) 아래 서비스별 폴더(`batch/`, `executor/`)로 나눈다.
`STORAGE_ROOT`와 Executor `INPUT_STORAGE_ROOT`는 프로젝트 루트,
Executor `SHARED_STORAGE_ROOT`는 `<프로젝트 루트>/executor`로 둔다.
`batch-input/`으로 시작하는 기존 경로는 DB에 저장된 그대로 계속 읽는다.
런타임 선택지는 Executor BATCH 풀에서 읽는다([등록 API 안내](registration-api.md)의 런타임 항목). Executor 호출에는 인증이 없으며(내부 서비스)
pr-batch 호출용 JWT를 Executor에 넘기지 않는다.

## HTTP 계약

공통 prefix는 `/api/v1/batches/{batch_id}/runs`이며 서비스 JWT(`sub`=사용자, [인증 가이드](client-auth.md))가 필요하다. 다른 사용자의 배치·실행과
삭제된 배치의 실행은 404다(이력은 DB에 보관). 진행 중인 실행이 있는 배치는 삭제할 수 없다
([등록 API 안내](registration-api.md#삭제)).

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | 현재 설정으로 수동 실행 접수. 202 (대기열 초과 409) |
| GET | / | 접수 역순 이력, limit 1~100·cursor |
| GET | /{run_id} | 상태·스냅샷·셀별 Step |
| POST | /{run_id}/cancel | 취소 요청. 202 |

쓰기에는 `Idempotency-Key`가 필수다. 수동 실행 본문은 생략하거나 `{}`이며 버전·런타임 override는 422다.
일정이 없는 배치나 비활성 배치도 수동 실행할 수 있다.

| 접수 결과 | HTTP | status | skip_reason |
| --- | --- | --- | --- |
| 자리 확보, 제출 대기 | 202 | QUEUED | |
| QUEUE/PARALLEL 한도 초과 | 202 | WAITING | |
| SKIP 정책, 이미 실행 중 | 202 | SKIPPED | OVERLAP |
| 대기열 `max_pending` 초과 | 409 | SKIPPED | SKIPPED_CAPACITY |

## Run 상태

| status | 의미 | 다음 실행을 막는가 |
| --- | --- | --- |
| WAITING | 동시 실행 자리 대기 | 아니오 |
| QUEUED | 자리 확보, Executor 제출 대기·진행 | 예 |
| RUNNING | Executor에서 코드 실행 중 | 예 |
| COLLECTING | 코드 종료, 셀 결과·결과 노트북 수집 | 아니오 |
| COMPLETED | 코드 성공, 결과 노트북 projection 종료 (`notebook_status`가 FAILED면 결과 노트북 없음) | — |
| FAILED | 코드 실행 실패, 또는 Executor가 제출을 거절(`failure_type=SUBMIT_REJECTED`) | — |
| CANCELLED | 사용자 취소 또는 REPLACE 정책 취소 (`cancel_source`) | — |
| SKIPPED | 겹침·대기열 초과로 실행 안 함 | — |

응답의 다른 필드: `execution_status`(Executor 원본 상태), `failure_type`, `notebook_status`,
`last_error`(통신·계약 오류. 코드 실패와 별개), `cancel_requested`, `cancel_source`(USER·POLICY),
`cancel_reason`, `artifacts`, `attempts`, `retry_info`, `result`, `steps`(상세만). 출력 원문·표·이미지는
복제하지 않고 `result_ref`와 artifact ID로만 가리킨다.

**결과 다운로드는 pr-batch가 중계하지 않는다.** `result.notebook_artifact_id`(실행 결과 노트북)를 보고, prj_re가
Executor `GET /api/v1/artifacts/{artifact_id}/content`로 내려받아 사용자에게 전달한다. Executor는 인증이
없는 내부 서비스이므로 브라우저에 직접 노출하지 않고, prj_re는 먼저 그 사용자의 서비스 JWT로 pr-batch에서 실행을
조회해 소유권을 확인한 뒤 그 artifact만 내려받는다.

**리포트는 pr-batch가 만들지 않는다.** pr-agentic-service가 Executor `execution.completed` 이벤트(성공
회차만)를 받아 직접 작성한다. 이벤트의 `context.task_id`가 pr-batch `run_id`다.

취소: WAITING은 즉시 CANCELLED. QUEUED는 제출 전에 멈추고 CANCELLED. RUNNING은 Executor에 취소를
요청하고 실제 종료를 확인한 뒤 CANCELLED. COLLECTING 이후는 409 `NOT_CANCELLABLE`.
처음 들어온 취소 요청의 출처와 사유를 `cancel_source`·`cancel_reason`에 남기고, Executor 취소에는 항상
actor를 보낸다. 사용자 취소는 `{type: USER, id: owner_id}`, REPLACE 정책 취소는 수동 실행이 계기여도
`{type: BATCH, id: batch_id}`다.

## 실행과 Executor Execution 연결

pr-batch의 실행(Run)과 Executor의 Execution은 1:1이고 양쪽에서 서로를 찾을 수 있다.

| 방향 | 방법 |
| --- | --- |
| pr-batch → Executor | 실행 응답의 `execution_id`(제출 후 채워짐, QUEUED 동안은 null), `operation_id`, 상세의 셀별 `steps[].step_id` |
| Executor → pr-batch | Execution의 `context.task_id` = pr-batch `run_id`, `context.user_id` = 사용자 UUID, `metadata`에 `batch_id`, `script_id`, `script_version_id`, `run_id` |

제출 trigger는 `{type: BATCH, actor: {type: USER, id: owner_id}}`다. actor가 소유자이므로 배치는
`metadata.batch_id`로 식별한다. Executor가 BATCH trigger에 USER actor를 허용한 뒤에 배포해야 한다.

배치에 `tenant_id`가 있으면 제출 본문 최상위에 싣는다(`context` 안이 아니다). 값은 실행을 만들 때
`snapshot.tenant_id`에 고정되므로, 나중에 배치를 바꿔도 이미 접수된 실행은 그대로 간다. Executor
멱등성 비교에 `tenant_id`와 `metadata`가 들어가므로, 이 필드가 생기기 전에 만든 실행(snapshot에 키가
없음)은 `tenant_id`와 `metadata.run_id` 없이 원래 본문으로 재제출한다. 회차 식별자는 명세 권장대로
`metadata.run_id`에도 둔다. `context.task_id`는 기존 소비자(pr-agentic-service) 때문에 당분간 유지한다.

예: Executor에서 `GET /api/v1/executions?task_id=<run_id>`로 해당 실행을 찾거나, pr-batch 실행 응답의
`execution_id`로 Executor `GET /api/v1/executions/{execution_id}`, `/events`, `/steps`를 조회해 진행 상황을
자세히 볼 수 있다. Executor는 인증이 없는 내부 서비스이므로 사용자에게는 prj_re가 소유권 확인 후 중계한다.

## 내부 Task 파이프라인

상태를 바꾸는 트랜잭션 안에서 그 결과로 필요한 외부 호출을 `tasks` 테이블에 넣는다(트랜잭션 outbox).
모든 Pod의 dispatcher가 `FOR UPDATE SKIP LOCKED`와 lease로 Task를 나눠 처리한다.

| Task | 원격 호출 | 결과 |
| --- | --- | --- |
| SUBMIT_EXECUTION | Executor 제출 (멱등성 키 `batch-run:<run_id>:submit`) | RUNNING + SYNC 예약 |
| SYNC_EXECUTION | Executor 상태 조회 | 진행 중이면 간격을 늘려 재조회, 종료면 COLLECTING + 대기 실행 승격 |
| COLLECT_RESULT | Executor 상태·결과 조회 | 성공: projection이 끝나면 COMPLETED / 실패: FAILED / 취소: CANCELLED |
| CANCEL_EXECUTION | Executor 취소 요청 | SYNC가 실제 종료를 관측 |

- 처리 순서: 짧은 claim 트랜잭션 → 커넥션 반납 → 원격 호출 1회 → 짧은 반영 트랜잭션. 원격 호출 중에는
  DB 커넥션을 잡지 않는다.
- Handler는 실행 직전에 Run 상태를 확인하고, 이미 지난 단계의 Task면 아무것도 하지 않고 끝낸다.
- 반영 시 lease 토큰이 바뀌었거나 만료됐으면 버린다(늦게 끝난 옛 Pod의 쓰기 차단).
- Executor가 제출을 확실히 거절하면(422 전부, 409 `INVALID_STATE_TRANSITION`) 같은 본문으로는 성공할
  수 없으므로 FAILED(`failure_type=SUBMIT_REJECTED`, `last_error=EXECUTOR_HTTP_{status}:{code}`)로
  끝내고 자리를 푼다. 단 앞선 제출 시도가 응답 없이 끝난 적이 있으면 그 실행이 이미 만들어졌을 수 있어
  (Executor는 멱등성 조회 전에 검증한다) 거절도 재시도한다. 409 `IDEMPOTENCY_CONFLICT`(같은 키의 실행이
  있음)·`PERSISTENCE_CONFLICT`도 재시도한다. 취소의 409는 `INVALID_STATE_TRANSITION`일 때만 "이미 종료"다.
- 통신·계약 오류는 실행 실패가 아니다. `last_error`에 남기고 2초부터 최대 300초까지 지수 backoff +
  jitter로 다시 시도한다. 제출 응답이 유실돼도 같은 멱등성 키로 재확인하므로 새 Execution이 생기지 않는다.
- Executor의 `SUCCEEDED`, `FAILED`, `CANCELLED`는 코드 실행 종료이며, 이때 다음 실행 자리가 풀린다.
  결과 노트북 projection이 늦으면 COLLECTING에서 계속 확인한다. projection이 실패해도 코드는 성공했으므로
  COMPLETED로 끝내고 `notebook_status=FAILED`로 구분한다.
- Executor 응답은 요청 10초, 처리 30초, 4 MiB로 제한한다. 낮은 상태 버전, 다른 실행·소유자·셀의 응답은
  반영하지 않는다. Batch는 Executor `/retry`를 호출하지 않는다(자동 재시도 정책 보류).

## 검증 범위

실제 PostgreSQL에서 접수·동시성·대기열 승격·lease 회수·오래된 응답 거절·취소 actor를 검증한다.
Executor는 계약에 맞춘 테스트 서버로 응답 유실, 통신 장애, projection 지연·실패를 재현한다. 백그라운드 런타임은 실제 스레드로 시작·처리·정지를 검증한다. Docker에서는 로컬 Executor와
연결해 수동·예약 실행 완료와 결과 다운로드를 확인한다.
