# 실행 API와 처리 파이프라인

수동 실행 접수·취소·리포트 재시도·이력 조회와, 실행을 끝까지 진행시키는 내부 Task 파이프라인을
설명한다. 예약 활성화와 스케줄러는 [예약 실행 안내](scheduling-api.md), 전체 구조는
[목표 아키텍처](architecture.md)와 [서비스 체계](domain-model.md)를 따른다.

## 실행 준비

API 프로세스 하나가 HTTP API와 백그라운드 런타임(스케줄러 루프 + Task dispatcher)을 함께
실행한다. 별도 워커나 스케줄러 프로세스는 없다. 시작 시 DB migration도 자동으로 적용한다.

```sh
uv sync --locked
# .env: BATCH_EXECUTOR_BASE_URL=http://127.0.0.1:8000 (/api/v1은 붙이지 않는다)
make serve   # http://127.0.0.1:8100
```

| 설정 | 기본 | 의미 |
| --- | --- | --- |
| `BATCH_EXECUTOR_BASE_URL` | 없음 | 없으면 실행 접수·활성화는 503 `EXECUTOR_NOT_CONFIGURED`, 백그라운드 런타임도 시작하지 않음 |
| `BATCH_REPORT_BASE_URL` / `BATCH_REPORT_TOKEN_SECRET` | 없음 | prj_re 리포트 API. 없으면 실행 성공 시 리포트 없이 COMPLETED |
| `BATCH_BACKGROUND_ENABLED` | true | 이 프로세스에서 백그라운드 런타임 실행 |
| `BATCH_DB_AUTO_MIGRATE` | true | 시작 시 advisory lock 아래 `alembic upgrade head` |
| `BATCH_TASK_CONCURRENCY` | 4 | Pod당 동시에 처리하는 Task 수 (= Executor 동시 요청 상한) |
| `BATCH_SYNC_INITIAL_SECONDS` / `BATCH_SYNC_MAX_SECONDS` | 5 / 60 | 상태 조회 간격. 두 배씩 늘려 최대값까지 |
| `BATCH_TASK_LEASE_SECONDS` | 90 | Task lease. 만료되면 다른 Pod가 이어받음 |
| `BATCH_DB_POOL_SIZE` / `BATCH_DB_MAX_OVERFLOW` | 3 / 3 | Pod당 최대 DB 커넥션 6 |
| `BATCH_SHUTDOWN_GRACE_SECONDS` | 40 | SIGTERM 후 진행 중인 원격 호출을 끝내는 최대 시간 |

Executor의 `INPUT_STORAGE_ROOT`에서 Batch가 저장한
`batch-input/users/.../scripts/.../versions/.../cells/*.py`를 같은 상대경로로 읽을 수 있어야 한다.
런타임 선택지는 Executor BATCH 풀에서 읽는다([등록 API 안내](registration-api.md)의 런타임 항목). Executor 호출에는 인증이 없으며(내부 서비스)
사용자 JWT를 Executor에 넘기지 않는다.

## HTTP 계약

공통 prefix는 `/api/v1/batches/{batch_id}/runs`이며 사용자 JWT가 필요하다. 다른 사용자의 배치·실행은 404다.

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | 현재 설정으로 수동 실행 접수. 202 (대기열 초과 409) |
| GET | / | 접수 역순 이력, limit 1~100·cursor |
| GET | /{run_id} | 상태·스냅샷·셀별 Step·리포트 |
| POST | /{run_id}/cancel | 취소 요청. 202 |
| POST | /{run_id}/report-retries | 리포트만 재시도 (코드 재실행 없음). 202 |

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
| REPORTING | 리포트 생성 중 | 아니오 |
| COMPLETED | 완료 (리포트 미설정이면 리포트 없이 완료) | — |
| FAILED | 코드 실행 실패 | — |
| REPORT_FAILED | 코드는 성공, 리포트만 실패. `report-retries`로 재시도 | — |
| CANCELLED | 사용자 취소 | — |
| SKIPPED | 겹침·대기열 초과로 실행 안 함 | — |

응답의 다른 필드: `execution_status`(Executor 원본 상태), `failure_type`, `notebook_status`,
`last_error`(통신·계약 오류. 코드 실패와 별개), `cancel_requested`, `artifacts`, `attempts`,
`retry_info`, `report`(최신 generation의 상태·오류·artifact), `result`, `steps`(상세만). 출력 원문·표·이미지는
복제하지 않고 `result_ref`와 artifact ID로만 가리킨다.

**결과 다운로드는 pr-batch가 중계하지 않는다.** `result.notebook_artifact_id`(리포트가 붙은 노트북이
있으면 그것, 없으면 실행 결과 노트북)와 `result.report_artifact_id`(리포트 Markdown)를 보고, prj_re가
Executor `GET /api/v1/artifacts/{artifact_id}/content`로 내려받아 사용자에게 전달한다. Executor는 인증이
없는 내부 서비스이므로 브라우저에 직접 노출하지 않고, prj_re는 먼저 pr-batch에서 사용자 JWT로 실행을
조회해 소유권을 확인한 뒤 그 artifact만 내려받는다.

취소: WAITING은 즉시 CANCELLED. QUEUED는 제출 전에 멈추고 CANCELLED. RUNNING은 Executor에 취소를
요청하고 실제 종료를 확인한 뒤 CANCELLED. COLLECTING 이후는 409 `NOT_CANCELLABLE`.

## 내부 Task 파이프라인

상태를 바꾸는 트랜잭션 안에서 그 결과로 필요한 외부 호출을 `tasks` 테이블에 넣는다(트랜잭션 outbox).
모든 Pod의 dispatcher가 `FOR UPDATE SKIP LOCKED`와 lease로 Task를 나눠 처리한다.

| Task | 원격 호출 | 결과 |
| --- | --- | --- |
| SUBMIT_EXECUTION | Executor 제출 (멱등성 키 `batch-run:<run_id>:submit`) | RUNNING + SYNC 예약 |
| SYNC_EXECUTION | Executor 상태 조회 | 진행 중이면 간격을 늘려 재조회, 종료면 COLLECTING + 대기 실행 승격 |
| COLLECT_RESULT | Executor 상태·결과 조회 | 성공: REPORTING 또는 COMPLETED / 실패: FAILED / 취소: CANCELLED |
| CANCEL_EXECUTION | Executor 취소 요청 | SYNC가 실제 종료를 관측 |
| REQUEST_REPORT | prj_re 리포트 작업 접수 (멱등성 키 `batch-run:<run_id>:report:<generation>`) | SYNC_REPORT 예약 |
| SYNC_REPORT | prj_re 리포트 조회 | COMPLETED 또는 REPORT_FAILED |

- 처리 순서: 짧은 claim 트랜잭션 → 커넥션 반납 → 원격 호출 1회 → 짧은 반영 트랜잭션. 원격 호출 중에는
  DB 커넥션을 잡지 않는다.
- Handler는 실행 직전에 Run 상태를 확인하고, 이미 지난 단계의 Task면 아무것도 하지 않고 끝낸다.
- 반영 시 lease 토큰이 바뀌었거나 만료됐으면 버린다(늦게 끝난 옛 Pod의 쓰기 차단).
- 통신·계약 오류는 실행 실패가 아니다. `last_error`에 남기고 2초부터 최대 300초까지 지수 backoff +
  jitter로 다시 시도한다. 제출 응답이 유실돼도 같은 멱등성 키로 재확인하므로 새 Execution이 생기지 않는다.
- Executor의 `SUCCEEDED`, `FAILED`, `CANCELLED`는 코드 실행 종료이며, 이때 다음 실행 자리가 풀린다.
  결과 노트북 projection이 늦으면 COLLECTING에서 계속 확인한다. projection이 실패하면 리포트를 만들
  노트북이 없으므로 REPORT_FAILED(`RESULT_NOTEBOOK_UNAVAILABLE`)로 남긴다.
- Executor 응답은 요청 10초, 처리 30초, 4 MiB로 제한한다. 낮은 상태 버전, 다른 실행·소유자·셀의 응답은
  반영하지 않는다. Batch는 Executor `/retry`를 호출하지 않는다(자동 재시도 정책 보류).

## prj_re 리포트 API 계약 (제안)

prj_re에 아직 없는 API이며 아래 계약으로 구현을 요청한다. 인증은 pr-batch가 서명한 단기 서비스 JWT
(`iss=pr-batch`, `aud=prj_re-report`, `sub=<owner>`, `scope=report:write`, 5분, HS256
`BATCH_REPORT_TOKEN_SECRET`)다.

```text
POST /internal/v1/execution-reports
  {idempotency_key, batch_run_id, generation, owner_id, execution_id,
   notebook_artifact_id, steps: [{sequence, original_index, step_id, result_ref}]}
  → 202 {report_job_id, status}
GET /internal/v1/execution-reports/{report_job_id}
  → {report_job_id, status: PENDING|RUNNING|SUCCEEDED|FAILED,
     artifacts: [{type: REPORT|NOTEBOOK, artifact_id}], error_code}
```

같은 `idempotency_key` 재요청은 같은 작업으로 연결돼야 한다. 성공 시 artifacts에 Markdown 리포트(REPORT)와
리포트가 추가된 노트북(NOTEBOOK)의 Executor artifact ID를 준다.

## 검증 범위

실제 PostgreSQL에서 접수·동시성·대기열 승격·lease 회수·오래된 응답 거절·취소·리포트 재시도를 검증한다.
Executor와 prj_re는 계약에 맞춘 테스트 서버로 응답 유실, 통신 장애, projection 지연, 리포트 실패를
재현한다. 백그라운드 런타임은 실제 스레드로 시작·처리·정지를 검증한다. Docker에서는 로컬 Executor와
연결해 수동·예약 실행 완료와 결과 다운로드를 확인한다.
