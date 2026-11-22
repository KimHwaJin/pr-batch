# pr-batch DB 논리 설계서

기준: pr-batch `main` (2026-11-13), DB revision `0005_audit_client`. 작성일 2026-11-22.
테이블·컬럼·인덱스 같은 구현 세부는 [DB 물리 설계서](03-db-physical-design.md)에 있다. 이 문서는 **무엇을 왜 저장하는지**를 다룬다.
레포 `docs/domain-model.md`, `docs/execution-history.md`를 정리·보완했다.

---

## 1. 주제 영역

| 영역 | 엔터티 | 성격 |
| --- | --- | --- |
| 스크립트 | Script, ScriptVersion, (Cell) | 사용자가 올린 코드. 버전은 불변 |
| 배치 | Batch, BatchConfig, SkippedWindow | "어떤 버전을 언제 어떻게 돌릴지"와 그 이력 |
| 실행 | Run, Step | 실행 1회와 셀 단위 결과 참조. 영구 보존 |
| 내부 처리 | Task, IdempotencyRecord | 사용자에게 보이지 않는 작업 큐와 재전송 처리. 기간 뒤 삭제 |

```text
사용자 (tenant + owner)
├── Script ──────────────┐
│   └── ScriptVersion    │ (배치는 특정 버전을 가리킴)
│       └── Cell (목록)  │
└── Batch ◀──────────────┘
    ├── BatchConfig #1, #2, …   (불변 설정 스냅샷)
    ├── SkippedWindow           (실행 안 한 예약 구간)
    └── Run                     (실행 1회)
        ├── Step                (셀 1개 결과 참조)
        └── Task                (외부 호출 1건, 내부 전용)
```

---

## 2. 핵심 설계 원칙

1. **소유는 tenant + 사용자**: 모든 사용자 자원은 `tenant_id` + `owner_id`가 소유한다(Executor와 같은 체계). 등록 시 고정되고 바뀌지 않는다. 다른 tenant·사용자의 자원은 없는 것처럼(404) 보인다. project는 소유 범위가 아니라 배치의 속성이다.
2. **재현 가능성 = 불변 스냅샷**: ScriptVersion, BatchConfig, Run의 snapshot은 만들어진 뒤 바뀌지 않는다. 과거 실행이 "어떤 코드를 어떤 설정으로 돌렸는지" 항상 알 수 있다.
3. **이력은 지우지 않는다**(요건): 사용자 삭제는 soft delete이고, 원본·버전·설정·실행 이력은 DB와 스토리지에 남는다. 내부 처리 기록(Task, 멱등성 기록)만 기간 뒤 지운다.
4. **Run 상태가 유일한 진실**: Task는 "그 상태가 되었으니 해야 할 외부 호출"이다. 둘은 같은 트랜잭션에서 함께 기록된다(트랜잭션 outbox).
5. **외부 사실은 원본 그대로**: Executor 상태·실패 사유·artifact는 해석하지 않고 별도 속성에 보존한다. 사용자에게 보이는 상태는 Run 상태 하나로 요약한다.

---

## 3. 엔터티 정의

### 3.1 Script (스크립트)

| 항목 | 내용 |
| --- | --- |
| 정의 | 사용자가 올린 실행 코드 하나. 이름·타입과 버전 목록을 가진 "폴더" |
| 식별자 | script_id |
| 주요 속성 | 이름, 타입(`NOTEBOOK`), 최신 버전 번호, revision |
| 바뀌는 것 | 이름, 버전 추가 |
| 생명주기 | 생성(v1 함께 생성) → 이름 변경·버전 추가 → 삭제(soft) |
| 규칙 | 타입은 생성 시 고정. 삭제되지 않은 배치의 현재 설정이 이 스크립트를 쓰거나 진행 중 Run이 있으면 삭제 불가(`SCRIPT_HAS_ACTIVE_RUNS`, `SCRIPT_IN_USE`) |

### 3.2 ScriptVersion (스크립트 버전)

| 항목 | 내용 |
| --- | --- |
| 정의 | 업로드한 원본 파일 하나. 불변 |
| 식별자 | version_id. 스크립트 안에서는 version_number(v1, v2 …) |
| 주요 속성 | 파일명(표시용), 원본 경로, 원본 SHA-256, 코드 해시, 크기, 셀 목록 |
| 규칙 | 같은 스크립트에 바이트가 같은 파일을 다시 올리면 새 버전을 만들지 않고 기존 버전을 돌려준다 |
| 저장 | 원본과 실행용 셀 파일은 공유 스토리지, 메타데이터만 DB |

**Cell**은 별도 엔터티가 아니라 버전 안의 목록이다.
- `cell_list`: 원본의 모든 셀(위치, 종류 code/markdown/raw, 실행 여부).
- `cell_map`: 실행할 코드 셀만(실행 순서 `sequence`, 원본 위치 `original_index`, 셀 파일 경로, 해시). 비어 있지 않은 코드 셀만 대상이며 최대 100개(Executor 한 실행의 단계 한도).

### 3.3 Batch (배치)

| 항목 | 내용 |
| --- | --- |
| 정의 | "이 스크립트 버전을 이 일정·정책으로 돌린다"는 등록 |
| 식별자 | batch_id |
| 주요 속성 | 이름, 활성 여부, 다음 예약 시각, 현재 설정 번호, revision, 비활성 사유, project, 등록 client |
| 생명주기 | 등록(항상 비활성) → 활성 ⇄ 비활성 → 삭제(soft). 일정이 끝나면 활성이지만 다음 예약 시각 없음 |
| 규칙 | 일정이 없으면 활성화 불가(수동 전용). 비활성이어도 수동 실행 가능(단, 사용자·tenant 일괄 비활성은 수동 실행도 막음). 비활성화는 앞으로의 예약만 막고 접수된 실행은 계속. 진행 중 Run이 있으면 삭제 불가(`BATCH_HAS_ACTIVE_RUNS`) |

비활성 사유:

| 값 | 언제 | 수동 실행 |
| --- | --- | --- |
| `OWNER` | 소유자가 `/disable` | 가능 |
| `USER_DEACTIVATED` | 관리 호출 `disable-by-owner` (사용자 비활성) | 불가 (`OWNER_DEACTIVATED`) |
| `TENANT_REVOKED` | 관리 호출 `disable-by-tenant` (tenant 폐기) | 불가 (`TENANT_REVOKED`) |

활성화를 막는 조건(배치 응답의 `enable_blocked_reason`과 같음): Executor 미설정, 일정 없음, 런타임 미지원, 미래 회차 없음.

### 3.4 BatchConfig (배치 설정 이력)

| 항목 | 내용 |
| --- | --- |
| 정의 | 배치 실행 설정의 불변 스냅샷 |
| 식별자 | (batch_id, number) |
| 주요 속성 | 스크립트 버전, 런타임, 일정, 정책 |
| 생성 | 배치 등록 시 #1, 버전·런타임·일정·정책 중 하나라도 바뀌면 다음 번호(이름 변경은 제외) |
| 왜 필요한가 | Run이 "접수 당시 설정"을 정확히 가리키게 하려고. 대기 중인 Run은 이후 설정을 바꿔도 원래 설정으로 돈다 |

### 3.5 Run (실행)

| 항목 | 내용 |
| --- | --- |
| 정의 | 배치의 실행 1회 |
| 식별자 | run_id |
| 생성 | 예약(`SCHEDULED`, 예약 시각 있음) 또는 수동(`MANUAL`) |
| 고정되는 것 | 설정 번호, 스크립트 버전, 런타임, 일정·정책 사본, tenant, project (snapshot) |
| 외부 연결 | Executor execution_id, operation_id, artifact 목록, 시도 목록 |
| 규칙 | 같은 설정의 같은 예약 회차는 한 번만 생긴다. 영구 보존 |

### 3.6 Step (셀 실행 결과 참조)

| 항목 | 내용 |
| --- | --- |
| 정의 | Run 안에서 코드 셀 1개. Executor step과 1:1 |
| 식별자 | (run_id, sequence) |
| 생성 | Run 접수 시 실행 대상 셀 수만큼. Executor step_id는 제출 후, 결과는 수집 후 채움 |

### 3.7 SkippedWindow (생략 기록)

실행하지 않고 넘긴 예약 구간. 장기 장애로 수천 회를 놓쳐도 "구간 + 건수" 한 행으로 압축한다. 사유는 5.4절.

### 3.8 Task (내부 작업)

Run을 진행시키는 외부 호출 1건. 사용자에게 노출하지 않는다. 종류는 5.5절. 여러 인스턴스가 같은 큐를 나눠 처리하며, 한 Task는 lease로 한 곳만 잡는다. 끝나면(`DONE`) 30일 뒤 삭제.

### 3.9 IdempotencyRecord (멱등성 기록)

같은 `Idempotency-Key`로 재전송된 쓰기 요청에 첫 응답을 그대로 돌려주기 위한 기록. 범위는 (tenant, 사용자, 작업, key). 7일 뒤 삭제.

---

## 4. 관계

| 관계 | 카디널리티 | 설명 |
| --- | --- | --- |
| Script — ScriptVersion | 1 : 1..N | 스크립트는 최소 한 버전 |
| Batch — BatchConfig | 1 : 1..N | 현재 설정은 Batch의 `config_number` |
| ScriptVersion — BatchConfig | 1 : 0..N | 설정은 반드시 "그 스크립트의" 버전을 가리킴(복합 참조). 같은 tenant·사용자의 스크립트만 |
| Batch — Run | 1 : 0..N | |
| BatchConfig — Run | 1 : 0..N | 접수 당시 설정 |
| ScriptVersion — Run | 1 : 0..N | 실행한 버전 (설정과 같은 버전) |
| Run — Step | 1 : 0..100 | 코드 셀 수만큼 |
| Run — Task | 1 : 0..N | 단계별 외부 호출 |
| Batch — SkippedWindow | 1 : 0..N | 생략 당시 설정 번호도 함께 참조 |

Script와 Batch는 독립이다. 스크립트 하나를 여러 배치가 쓸 수 있다.

---

## 5. 코드값 정의

### 5.1 Run 상태 (`status`)

| 값 | 의미 | 다음 실행을 막음(점유) | 진행 중(삭제 막음) | 취소 가능 |
| --- | --- | --- | --- | --- |
| `WAITING` | 동시 실행 한도로 대기 | | ✓ | ✓ |
| `QUEUED` | 자리 확보, Executor 제출 대기 | ✓ | ✓ | ✓ |
| `RUNNING` | Executor에서 코드 실행 중 | ✓ | ✓ | ✓ |
| `COLLECTING` | 실행 종료, 결과 수집 중 | | ✓ | |
| `COMPLETED` | 성공 | | | |
| `FAILED` | 실패 | | | |
| `CANCELLED` | 취소 | | | |
| `SKIPPED` | 실행 안 함 | | | |

```text
접수 ─┬─▶ QUEUED ─▶ RUNNING ─▶ COLLECTING ─┬─▶ COMPLETED
      ├─▶ WAITING ─▶ QUEUED                 ├─▶ FAILED
      └─▶ SKIPPED                           └─▶ CANCELLED
WAITING ─▶ CANCELLED / SKIPPED(일괄 비활성)
QUEUED  ─▶ CANCELLED(제출 전 취소) / FAILED(제출 거절·포기)
RUNNING ─▶ FAILED(포기)
```

### 5.2 Run 부가 코드

| 속성 | 값 |
| --- | --- |
| `trigger` | `SCHEDULED`, `MANUAL` |
| `skip_reason` | `OVERLAP`(SKIP 정책에서 앞선 실행 점유 중), `SKIPPED_CAPACITY`(대기열 초과), `OWNER_DEACTIVATED`, `TENANT_REVOKED`(일괄 비활성 때 대기 중이던 Run) |
| `cancel_source` | `USER`(소유자), `POLICY`(REPLACE 정책) |
| `failure_type` | `SUBMIT_REJECTED`(Executor가 제출을 확정 거절), `ABANDONED`(원격 호출이 한도 시간 동안 계속 실패), 그 밖에는 Executor가 준 실패 사유 코드 |
| `execution_status` | Executor 원본 상태. 종료로 보는 값은 `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `last_error` | 진행 중 마지막 오류 코드: `EXECUTOR_UNREACHABLE`, `EXECUTOR_HTTP_<status>[:<code>]`, `EXECUTOR_INVALID_RESPONSE`, `EXECUTOR_CONTRACT_MISMATCH`, `EXECUTOR_RESPONSE_LIMIT`, `UNEXPECTED_ERROR: <예외명>` |

### 5.3 Batch·Script 코드

| 속성 | 값 |
| --- | --- |
| `batches.disabled_reason` | `OWNER`, `USER_DEACTIVATED`, `TENANT_REVOKED` |
| `scripts.type` | `NOTEBOOK` |

### 5.4 SkippedWindow 사유 (`reason`)

| 값 | 언제 |
| --- | --- |
| `MISFIRE_SKIP` / `MISFIRE_LATEST` / `MISFIRE_CATCH_UP` | 장애 등으로 유예 시간을 넘겨 놓친 회차를 정책대로 생략 |
| `REPLACED_BY_LATEST` | REPLACE 정책에서 여러 회차가 한꺼번에 도래했을 때 마지막을 뺀 나머지 |
| `DISABLED` | 비활성화 시점에 아직 접수 못 한 과거 회차 |
| `SCHEDULE_REPLACED` | 활성 중 일정을 바꿀 때 옛 일정의 미접수분 |
| `DELETED` | 활성 배치 삭제 시 미접수분 |
| `OWNER_DEACTIVATED` / `TENANT_REVOKED` | 일괄 비활성 시 미접수분 |
| `SCHEDULER_ERROR` | 예약 접수가 재시도 한도를 넘어 실패한 회차 |

### 5.5 Task 코드

| 속성 | 값 |
| --- | --- |
| `kind` | `SUBMIT_EXECUTION`, `SYNC_EXECUTION`, `COLLECT_RESULT`, `CANCEL_EXECUTION` |
| `status` | `PENDING`, `DONE` |
| `payload` 키 | `interval`(다음 상태 조회 간격), `uncertain`(제출 결과 불확실), `abandoned`(포기 후 1회 취소) |

---

## 6. 구조화 속성 (JSONB)

| 엔터티.속성 | 구조 | 정의 위치 |
| --- | --- | --- |
| BatchConfig.runtime, Run.runtime | `{"type": "JUPYTER", "profile": "<커널>"}`. 선택지는 설정 `BATCH_RUNTIMES` | `schemas/batch.py`, `domain/runtimes.py` |
| BatchConfig.schedule | `{definition, timezone, start_at, end_at}`. `definition.kind`별: `ONCE{at}`, `INTERVAL{seconds(60~1년), anchor}`, `CALENDAR{frequency DAILY/WEEKLY/MONTHLY, at, weekdays, month_days, last_day, nth_weekdays}`, `CRON{expression}`. timezone은 `Asia/Seoul`만 | `schemas/schedule.py` |
| BatchConfig.policy | `{overlap, misfire}`. overlap `mode`: `SKIP`/`QUEUE{max_queued=100}`/`PARALLEL{max_concurrent, max_queued}`/`REPLACE`. misfire `mode`: `SKIP`/`LATEST`/`CATCH_UP{max_runs=10, max_age_seconds=86400}`, 공통 `grace_seconds=300`. 기본은 overlap SKIP, misfire SKIP | `schemas/schedule.py` |
| Run.snapshot | `{script_id, script_version_id, sha256, code_hash, schedule, policy, tenant_id, project_id}` | `services/run_service.py::admit_run` |
| Run.artifacts / attempts / retry_info | Executor 결과 응답의 해당 부분 그대로 | `integrations/executor/contracts.py` |
| Step.result_ref | Executor 셀 결과 참조(실행·step ID, 경로·해시) | 위와 같음 |
| ScriptVersion.cell_map | `[{original_index, sequence, path, sha256}]` | `storage/script_storage.py` |
| ScriptVersion.cell_list | `[{index, type, executed, …}]` | `domain/notebook_parser.py` |
| IdempotencyRecord.response | 첫 응답 본문 | `services/idempotency.py` |

---

## 7. 이력·삭제·감사 규칙

| 대상 | 규칙 |
| --- | --- |
| Script, Batch | soft delete (`deleted_at/by/by_client`). 목록·조회에서 사라지지만 행은 남음 |
| ScriptVersion, BatchConfig | 불변. 삭제 없음 |
| Run, Step, SkippedWindow | 영구 보존 |
| Task | `DONE` 후 30일 (`TASK_RETENTION_DAYS`) |
| IdempotencyRecord | 생성 후 7일 (`IDEMPOTENCY_RETENTION_DAYS`) |
| 감사 | 사용자 자원은 `created/updated_at`, `*_by`(X-User-Id), `*_by_client`(API key 이름). 스케줄러·dispatcher가 바꾼 것은 둘 다 NULL. 스케줄러가 다음 예약 시각을 옮기는 것은 "수정"으로 보지 않음 |
| 동시 수정 | Script·Batch는 `revision`으로 낙관적 잠금(`If-Match`) |

---

## 8. 공유 스토리지 데이터 (DB 밖)

DB에는 경로와 해시만 두고 파일은 공유 스토리지에 둔다. Executor가 같은 경로로 셀 파일을 읽는다.

```text
{STORAGE_ROOT}/batch/inputs/tenants/{tenant}/users/{user}/scripts/{script_id}/versions/{version_id}/
    original.ipynb                  (업로드 원본)
    cells/000000.py, 000001.py, …   (실행할 코드 셀, sequence 순)
```

임시 폴더에 쓰고 fsync 후 rename으로 한 번에 나타나게 한다. DB 커밋이 실패하면 써 둔 폴더를 지운다.
