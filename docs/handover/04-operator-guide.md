# pr-batch 운영자 가이드

기준: pr-batch `main` (2026-11-22 커밋 `7cf5439`, DB revision `0005_audit_client`). 작성일 2026-11-22.
배포 플랫폼(Kubernetes) 운영은 플랫폼 팀 소관이라 다루지 않고, **서비스와 소스코드 관점**에서
운영할 때 알아야 할 것만 정리한다.

관련 문서: [소스코드 설명서](01-source-code-guide.md) · [DB 논리 설계서](02-db-logical-design.md) ·
[DB 물리 설계서](03-db-physical-design.md) · [인수인계서](05-handover.md)

---

## 1. 서비스 한눈에 보기

pr-batch는 사용자가 올린 Jupyter 노트북을 정해진 일정 또는 수동으로 Executor에서 실행하고, 실행 이력을 남기는 서비스다.

```text
호출 서비스(pr-agentic-service 등)
    │ X-Api-Key + X-User-Id (+ X-Tenant-Id, X-Project-Id)
    ▼
┌──────────────────────── pr-batch 프로세스 (인스턴스 1..N, 모두 동일) ───────────────────────┐
│  HTTP API (/api/v1, /healthz, /readyz)   ── DB에 기록만 하고 원격 호출은 하지 않음            │
│  Scheduler 루프 (1개)                    ── 도래한 예약 → Run + 첫 Task 생성                │
│  Task dispatcher 루프 (TASK_CONCURRENCY개) ── Task claim → Executor 호출 → 결과 반영         │
│  Retention 루프 (1개)                    ── 오래된 Task·멱등성 기록 삭제                     │
└───────────────────────────────────────────────────────────────────────────────────────────┘
    │                         │                                    │
    ▼                         ▼                                    ▼
PostgreSQL (batch 스키마)   공유 스토리지 (노트북 원본·셀 파일)    Executor (실행·상태·결과·취소)
```

| 의존 대상 | 용도 | 없으면 |
| --- | --- | --- |
| PostgreSQL | 모든 상태(배치, 실행, 작업 큐)와 인스턴스 간 조정(잠금) | API 503 `DATABASE_UNAVAILABLE`, 백그라운드 루프 정지, `/readyz` 실패 |
| 공유 스토리지 (`STORAGE_ROOT`) | 업로드한 노트북 원본과 셀 파일. Executor가 같은 경로로 읽음 | 업로드 503 `STORAGE_UNAVAILABLE`, `/readyz` 실패 |
| Executor (`EXECUTOR_BASE_URL`) | 실제 노트북 실행 | 설정이 비면 백그라운드 런타임이 아예 뜨지 않음(실행 접수·활성화 503 `EXECUTOR_NOT_CONFIGURED`). 설정은 됐는데 응답이 없으면 Run이 QUEUED/RUNNING에 머물며 재시도 |

결과 리포트 작성은 pr-batch가 하지 않는다. pr-agentic-service가 Executor 이벤트를 보고 만든다.

### 1.1 핵심 원리 세 가지 (장애 분석의 출발점)

1. **Run 상태가 유일한 진실이다.** `batch_runs.status`가 "지금 어디까지 왔나"를 말한다. `tasks`는 그 상태에서 해야 할 외부 호출 목록일 뿐이다.
2. **API는 원격 호출을 하지 않는다.** 실행 접수 API는 Run과 Task를 DB에 넣고 바로 202를 돌려준다. Executor 호출은 전부 dispatcher가 나중에 한다. 그래서 "API는 성공했는데 실행이 안 된다"는 dispatcher 쪽을 봐야 한다.
3. **인스턴스끼리는 DB 잠금으로만 조정한다.** 리더가 없고, 모든 인스턴스가 같은 일을 한다. 예약은 `FOR UPDATE SKIP LOCKED`, Task는 lease(기본 90초)로 나눈다. 인스턴스가 죽으면 lease가 끝난 뒤 다른 인스턴스가 이어받는다.

---

## 2. 실행 흐름과 상태

### 2.1 Run 상태

| 상태 | 의미 | 다음으로 넘어가는 조건 |
| --- | --- | --- |
| `WAITING` | 접수됨. 배치의 동시 실행 한도가 차서 자리를 기다림 | 같은 배치의 앞선 실행이 끝나 자리가 나면 QUEUED |
| `QUEUED` | 자리 확보. Executor 제출 대기 (`SUBMIT_EXECUTION` Task 있음) | 제출 성공 → RUNNING / 제출 거절 → FAILED / 제출 전 취소 → CANCELLED |
| `RUNNING` | Executor에 제출됨. 코드 실행 중 (`SYNC_EXECUTION`이 주기 조회) | Executor 종료 상태 관찰 → COLLECTING |
| `COLLECTING` | 코드 실행 끝. 셀별 결과 참조 수집 중 (`COLLECT_RESULT`) | 결과 조회 1회 → COMPLETED / FAILED / CANCELLED |
| `COMPLETED` | Executor `SUCCEEDED` | 끝 |
| `FAILED` | Executor 실패, 제출 거절(`SUBMIT_REJECTED`), 또는 pr-batch가 포기(`ABANDONED`) | 끝 |
| `CANCELLED` | 사용자 취소 또는 REPLACE 정책에 의한 취소 | 끝 |
| `SKIPPED` | 실행하지 않음 (`skip_reason`: `OVERLAP`, `SKIPPED_CAPACITY`, 일괄 비활성 때 대기 중이던 Run은 `OWNER_DEACTIVATED`/`TENANT_REVOKED`) | 끝 |

- 같은 배치의 다음 실행을 막는 "점유" 상태는 **QUEUED, RUNNING** 두 개다. COLLECTING은 막지 않는다.
- 배치·스크립트 삭제를 막는 "진행 중" 상태는 WAITING, QUEUED, RUNNING, COLLECTING이다.
- 아예 Run을 만들지 않은 예약 회차는 `skipped_windows`에 기록된다(3.3절).

### 2.2 Task 종류와 Executor 호출

| Task | Executor 호출 | 성공 시 | 비고 |
| --- | --- | --- | --- |
| `SUBMIT_EXECUTION` | `POST /api/v1/executions` | Run → RUNNING, `SYNC_EXECUTION` 예약(5초 뒤) | 멱등성 키 `batch-run:<run_id>:submit`. 재시도해도 실행은 하나 |
| `SYNC_EXECUTION` | `GET /api/v1/executions/{id}` | 진행 중이면 간격을 두 배로 늘려 재예약(5→10→20→40→60초), 종료면 Run → COLLECTING, `COLLECT_RESULT` 예약 | 조회 수는 실행 중인 Run 수에 비례 |
| `COLLECT_RESULT` | `GET /api/v1/executions/{id}/result` | Run 종료 (COMPLETED/FAILED/CANCELLED), 셀 결과·artifact 저장 | |
| `CANCEL_EXECUTION` | `POST /api/v1/executions/{id}/cancel` | 없음 (SYNC가 CANCELLED를 관찰) | 키 `batch-run:<run_id>:cancel`, 포기 시 `:abandon-cancel` |

Executor 호출에는 pr-batch의 `EXECUTOR_API_KEY`와 **Run을 만들 때 고정된** `X-User-Id`·`X-Tenant-Id`·`X-Project-Id`가 함께 간다. 배치를 나중에 고쳐도 진행 중인 Run의 신원은 바뀌지 않는다.

### 2.3 실패와 재시도 규칙

| 상황 | 동작 |
| --- | --- |
| Executor 불통, 타임아웃, 5xx, 응답 계약 불일치 | Task를 지수 backoff(2,4,8…최대 300초 + jitter)로 재시도. `tasks.failing_since`에 첫 실패 시각 기록, `batch_runs.last_error`에 마지막 오류 코드 |
| 제출이 422 또는 409 `INVALID_STATE_TRANSITION`으로 거절 (앞선 시도가 확실하지 않을 때만) | 같은 요청은 다시 보내도 성공할 수 없으므로 Run을 FAILED(`SUBMIT_REJECTED`)로 끝냄 |
| 400/401/403 (key·tenant 설정 문제) | ERROR 로그로 눈에 띄게 남김. 재시도는 하지만 설정을 고치기 전엔 풀리지 않음 |
| 응답을 못 받은 제출 (타임아웃 등) | Executor에 실행이 생겼을 수 있어 "uncertain" 표시를 남기고 같은 키로 재제출 |
| 같은 호출이 `TASK_GIVE_UP_SECONDS`(기본 24시간) 동안 계속 실패 | Run을 FAILED(`failure_type=ABANDONED`)로 끝내 자리를 비우고, 실행이 남아 있으면 취소를 한 번 시도 |
| 예약 접수 자체가 오류 (배치 데이터 문제 등) | 300초 뒤 재시도, 3회 넘으면 그 회차들을 `SCHEDULER_ERROR`로 건너뛰고 다음 예약으로 |
| 인스턴스가 처리 중 죽음 | lease(90초) 만료 뒤 다른 인스턴스가 이어받음. 멱등성 키 때문에 Executor 중복 실행 없음 |

**자동 재실행은 없다.** 노트북이 실패해도 pr-batch가 다시 돌리지 않는다(요건상 보류). Executor 쪽 재시도 정보는 `batch_runs.retry_info`에 그대로 남는다.

---

## 3. 운영 시 주의할 점

### 3.1 설정

| 주의점 | 설명 |
| --- | --- |
| 설정 위치와 우선순위 | 환경변수 > `.env` > `config.{APP_ENV}.yml` > `config.yml` > 코드 기본값(`src/app/core/config.py`). 전체 키와 설명은 `config.example.yml`(앱이 읽지 않는 참고 파일) |
| **오타가 조용히 무시됨** | 모르는 키는 오류 없이 무시되어 기본값으로 동작한다. 설정을 바꾼 뒤 동작이 그대로면 키 이름부터 확인한다. 범위를 벗어난 값은 시작할 때 실패한다 |
| 설정은 코드와 같이 배포됨 | ConfigMap·Secret이 없어 `config.*.yml`이 이미지에 들어간다. 설정 변경은 곧 새 빌드·배포다 |
| 비밀값 | DB 비밀번호, `EXECUTOR_API_KEY`가 레포 설정 파일에 있다. 호출자 key는 SHA-256 해시만 둔다. 레포 접근 권한 = 비밀 접근 권한이다 |
| `EXECUTOR_BASE_URL` 비움 | 백그라운드 런타임이 아예 시작하지 않는다. 등록·조회는 되지만 실행·예약은 전혀 진행되지 않는다 |
| `BACKGROUND_ENABLED=false` | 위와 같음. API만 띄워 점검할 때 쓴다 |
| `BATCH_RUNTIMES` | 배치가 고를 수 있는 커널 목록. Executor BATCH 풀과 **수동으로** 맞춘다. Executor에서 커널을 빼면 그 커널을 쓰는 배치의 제출이 거절된다(FAILED `SUBMIT_REJECTED`) |
| `DOCS_ENABLED` | Swagger(`/docs`) 노출. 내부망 규정상 dev·stg·prd는 false |
| `DB_AUTO_MIGRATE` | 기본 true. 시작할 때 advisory lock을 잡고 `alembic upgrade head`. 여러 인스턴스가 동시에 떠도 한 곳만 수행 |

### 3.2 인증 key 관리

- 호출 서비스 등록·변경은 `AUTH_CLIENTS`(이름, `key_sha256` 목록, `tenant`, `relay`, `allowed_tenants`, `admin`)를 고친다. 상세는 레포 `docs/client-auth.md`.
- **key 교체는 반드시 3단계**: ① 신·구 해시를 함께 넣어 배포 → ② 호출 서비스가 새 key로 전환 → ③ 구 해시를 빼고 배포. 한 번에 바꾸면 그 사이 호출이 모두 401이다.
- `EXECUTOR_API_KEY`는 Executor 팀이 발급한다. 바뀌면 진행 중인 모든 Run의 Task가 401/403으로 실패하고 ERROR 로그가 쏟아진다.
- 해시 만들기: `printf '%s' "$KEY" | shasum -a 256`

### 3.3 예약과 시간

- 모든 예약·lease 판단은 **DB 서버 시계**(`clock_timestamp()`) 기준이다. 앱 서버 시계와 무관하다.
- 일정 시간대는 `Asia/Seoul` 하나만 지원한다.
- 서비스가 멈춰 있던 동안 놓친 예약은 배치별 misfire 정책대로 처리된다: `SKIP`(생략), `LATEST`(최근 1회만), `CATCH_UP`(최대 `max_runs`회, `max_age_seconds` 이내). 기본 유예 `grace_seconds`는 300초. **장애 복구 직후 CATCH_UP 배치들의 보충 실행이 한꺼번에 몰릴 수 있다.**
- 실행하지 않은 회차는 `skipped_windows`에 `reason`과 개수로 남는다: `MISFIRE_SKIP`, `MISFIRE_LATEST`, `MISFIRE_CATCH_UP`(장애 등으로 놓침), `REPLACED_BY_LATEST`(REPLACE 정책), `SCHEDULER_ERROR`(접수 오류), `DISABLED`·`SCHEDULE_REPLACED`·`DELETED`(이미 도래한 회차가 있을 때 비활성·일정 변경·삭제), `OWNER_DEACTIVATED`·`TENANT_REVOKED`(일괄 비활성).
- 한 번의 예약 처리에서 만드는 회차는 최대 100개로 묶여 있다.

### 3.4 겹침(overlap) 정책

| mode | 앞선 실행이 점유 중일 때 새 회차 |
| --- | --- |
| `SKIP` (기본) | Run을 SKIPPED(`OVERLAP`)로 남김 |
| `QUEUE` | WAITING으로 대기 (최대 `max_queued`, 기본 100. 넘으면 SKIPPED `SKIPPED_CAPACITY`) |
| `PARALLEL` | `max_concurrent`까지 동시 실행, 넘으면 대기 |
| `REPLACE` | 대기 중인 것은 바로 취소, 실행 중인 것은 Executor에 취소 요청(`cancel_source=POLICY`). 새 Run은 앞선 실행의 종료가 확인될 때까지 WAITING으로 기다렸다가 시작 |

### 3.5 데이터를 직접 고칠 때

- 가능하면 API로 처리한다: 실행 취소 `POST /batches/{id}/runs/{run_id}/cancel`, 배치 비활성 `POST /batches/{id}/disable`, 사용자·tenant 단위 일괄 비활성 `POST /batches:disable-by-owner`, `POST /batches:disable-by-tenant` (admin key 필요).
- **`tasks`만 지우거나 고치지 않는다.** Run은 그 Task를 기다리며 영원히 멈춘다. 반대로 Run 상태를 수동으로 끝낼 때는 남은 PENDING Task가 있어도 handler가 상태를 보고 아무것도 하지 않고 끝내므로 안전하다.
- 수동 UPDATE가 필요하면 잠금 순서를 지킨다: **batch 행 → run 행**. 코드도 모든 곳에서 이 순서다.
- QUEUED/RUNNING Run을 수동으로 끝내면 같은 배치의 WAITING Run이 자동으로 올라가지 않는다(승격은 코드 경로에서만 일어남). 필요하면 대기 Run도 함께 정리한다.
- 삭제는 soft delete다(`deleted_at`). 원본, 버전, 실행 이력은 요건상 지우지 않는다.

### 3.6 DB 스키마 변경 (개발 인수자용)

- 운영 이후 migration은 "추가 → 코드 전환 → 제거"(expand/contract)로 나눈다. 이전 버전 코드가 새 스키마에서 잠시 함께 돌기 때문이다.
  - 한 번에 해도 되는 것: 테이블·nullable 컬럼·인덱스 추가, 기본값 있는 컬럼 추가.
  - 다음 배포로 미룰 것: 컬럼·테이블 삭제, 이름 변경, NOT NULL 추가, 타입 축소.
- 코드는 자신이 모르는 더 새 revision을 만나면 migration을 건너뛰고 정상 기동한다(롤백 가능). 단, 위 규칙이 지켜졌다는 전제다.
- 예외: `0005_audit_client`는 `*_by_type` 컬럼을 지웠기 때문에 이전 버전과 동시에 돌 수 없었다.

### 3.7 기타

- 같은 `Idempotency-Key`는 7일(`IDEMPOTENCY_RETENTION_DAYS`)만 같은 요청으로 인정된다. 같은 key에 다른 본문이면 409 `IDEMPOTENCY_CONFLICT`.
- 수정 API는 `If-Match`(revision)를 요구한다. 어긋나면 409 `REVISION_CONFLICT`.
- 공유 스토리지에는 원본과 셀 파일이 계속 쌓인다(자동 삭제 없음). 경로 규칙은 `batch/inputs/tenants/{tenant}/users/{user}/scripts/{script_id}/versions/{version_id}/`.
- 공유 스토리지 파일은 UID 1000 소유. Executor도 같은 UID로 읽는다.

---

## 4. 부하를 받는 지점

| 지점 | 무엇이 부하가 되나 | 조정 값 | 징후 |
| --- | --- | --- | --- |
| **노트북 업로드** | 요청 본문 전체(최대 `UPLOAD_MAX_BYTES`+64KiB, 기본 약 10MiB)를 메모리에 받은 뒤 JSON 파싱·셀 검증·파일 쓰기(fsync). API 중 CPU·메모리를 가장 많이 씀 | `UPLOAD_MAX_BYTES`, `MAX_NOTEBOOK_CELLS`(1000), `MAX_CODE_CELLS`(100), `CELL_MAX_BYTES`(1MiB) | 업로드 지연, CPU 상승, 큰 파일 동시 업로드 시 메모리 급증 |
| **정각 몰림** (예: 매일 09:00) | 스케줄러는 루프 한 바퀴에 배치 1개씩 처리. dispatcher는 인스턴스당 `TASK_CONCURRENCY`(2)개씩 Executor 호출. 실제 처리량 상한은 **Executor BATCH 슬롯 수** | `TASK_CONCURRENCY`, Executor 쪽 슬롯 | `scheduled_for` 대비 `submitted_at` 지연, QUEUED 적체 |
| **상태 조회 폴링** | 등록 배치 수가 아니라 **실행 중 Run 수**에 비례. Run당 5초에서 시작해 두 배씩 최대 60초 간격 | `SYNC_INITIAL_SECONDS`, `SYNC_MAX_SECONDS` | Executor 조회 증가. 종료 감지 지연 최대 약 60초 |
| **Executor 동시 요청** | 인스턴스 수 × `TASK_CONCURRENCY`로 상한이 묶임. 늘려도 Executor 슬롯 이상으로 빨라지지 않음(부하 측정 결과) | `TASK_CONCURRENCY` | Task `run_at` 대비 처리 지연 |
| **DB 커넥션** | 인스턴스당 풀 `DB_POOL_SIZE`(2) + `DB_MAX_OVERFLOW`(4) = 최대 6. 원격 호출 중에는 커넥션을 잡지 않음. 풀이 차면 10초 기다리다 실패 | `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, DB 역할의 `CONNECTION LIMIT` | `Database error` 로그, API 503 |
| **유휴 폴링** | 할 일이 없어도 루프(스케줄러 1 + dispatcher N + retention 1)가 최대 1초마다 "다음 할 일" 조회 | `IDLE_MAX_SECONDS` | 유휴 시에도 인스턴스당 초당 몇 건의 가벼운 쿼리 |
| **동시 실행 수 계산** | 예약·수동 접수마다 그 배치의 QUEUED/RUNNING/WAITING 수를 셈. `ix_runs_batch_status` 인덱스 사용 | — | 한 배치에 Run이 매우 많을 때 접수 지연 |
| **영구 보존 테이블** | `batch_runs`, `run_step_bindings`(Run당 코드 셀 수만큼, 최대 100행), `skipped_windows`는 지우지 않음 | 보존 정책 없음 | 목록 조회 점진적 저하, 디스크 증가 |
| **공유 스토리지** | 업로드마다 원본 + 셀 파일 N개를 fsync로 씀. 지우지 않음 | — | 업로드 지연, 쓰기 오류 |

참고 측정치(로컬, 2026-10, `scripts/load_check.py`): 단일 프로세스 CPU 0.35코어에서 API 조회 약 80건/s, 최대 약 1.3코어(GIL 한계), 메모리 약 100MiB. 정각 몰림의 처리량은 Executor 슬롯이 결정했다. stg 기준 재측정은 아직 없다.

### 4.1 규모 감각

- 배치 500개가 같은 시각에 10분짜리 실행을 시작하는 최악의 경우에도 Run당 조회는 약 13회, 평균 초당 약 11건이다.
- 동시 RUNNING이 수백 개를 넘거나 종료 직후 처리 같은 지연 요건이 생기면 Executor 이벤트(`executor.events`) 구독을 검토한다(레포 `docs/architecture.md` 8장의 도입 기준).

---

## 5. 장애 시 어디서 무엇을 보나

### 5.1 먼저 볼 곳

0. **알림과 Kibana**: CPU 급증·Pod 중단 알림은 내부 메신저로 온다. 로그는 내부 Kibana에서 본다(8장).
1. **헬스 엔드포인트**
   - `GET /healthz`: 프로세스와 백그라운드 루프가 살아 있는지. 어떤 루프든 120초 넘게 heartbeat가 없으면 503 `{"status":"stalled"}`.
   - `GET /readyz`: 요청을 받을 수 있는지. 503일 때 `error.code`로 원인이 갈린다 (5.2).
2. **애플리케이션 로그** (5.4의 메시지 목록으로 검색). 형식은 `시각 레벨 로거: 메시지 key=value ...`.
3. **DB** (`batch` 스키마). 상태의 진실은 여기 있다. 진단 SQL은 5.5.

### 5.2 `/readyz` 응답 코드

| 코드 | 의미 | 조치 |
| --- | --- | --- |
| `NOT_READY` | 시작 중(migration 포함)이거나 종료 중 | 시작 중이면 기다림. 오래가면 다른 인스턴스가 migration advisory lock을 쥐고 있는지 확인 |
| `MIGRATION_REQUIRED` | DB가 이 코드가 아는 옛 revision이거나 비어 있음 | `DB_AUTO_MIGRATE` 값 확인, 꺼져 있으면 `alembic upgrade head` |
| `STORAGE_UNAVAILABLE` | `STORAGE_ROOT` 경로가 없음 | 공유 스토리지 마운트·경로 확인 |
| `DATABASE_UNAVAILABLE` (또는 연결 오류) | DB 접속 실패 | DB 상태, 접속 정보, 커넥션 한도 확인 |

### 5.3 증상별 확인 순서

| 증상 | 확인할 것 | 원인 후보와 조치 |
| --- | --- | --- |
| 프로세스가 반복 재시작 | `/healthz`가 `stalled`인지. 로그 `Loop database unavailable`, `Loop unexpected error` | DB 장애(루프가 계속 실패), 루프 내부 버그. 로그의 traceback으로 판단 |
| 예약도 실행도 전혀 안 됨 | 시작 로그에 `Background runtime started loops=N`이 있는지 | 없으면 `EXECUTOR_BASE_URL` 비어 있음 또는 `BACKGROUND_ENABLED=false` |
| 특정 배치만 예약이 안 됨 | `batches`의 `enabled`, `next_run_at`, `schedule_failures`, `schedule_retry_at`, `disabled_reason` / `skipped_windows` 최근 행 / 로그 `Schedule admission failed`, `Schedule admission given up`, `Schedule stopped` | 비활성(`OWNER`, `USER_DEACTIVATED`, `TENANT_REVOKED`), 접수 오류 재시도 중, 일정 종료(`next_run_at` NULL), 겹침 정책으로 SKIPPED |
| 실행 접수 API가 409 | 응답 본문 | 오류 본문의 `error.code`가 `OWNER_DEACTIVATED`/`TENANT_REVOKED`면 사용자·tenant가 비활성. 본문이 Run이고 `status=SKIPPED`, `skip_reason=SKIPPED_CAPACITY`면 대기열이 가득 참(Run은 SKIPPED로 기록됨) |
| Run이 QUEUED에 오래 머묾 | 그 Run의 `tasks`(kind, attempts, last_error, failing_since, run_at, lease_until), `batch_runs.last_error` / 로그 `Task failed kind=SUBMIT_EXECUTION` | Executor 불통(`EXECUTOR_UNREACHABLE`), 인증 거절(`EXECUTOR_HTTP_401/403`), 응답 형식 불일치(`EXECUTOR_INVALID_RESPONSE`, `EXECUTOR_CONTRACT_MISMATCH`). Task가 아예 없으면 3.5절 "Task만 지운 경우"를 의심 |
| Run이 RUNNING에 오래 머묾 | `batch_runs.execution_id`로 Executor에서 실행 상태 확인, `execution_status`, `observed_at` | Executor에서 실제로 오래 도는 중(정상), 또는 조회 실패 반복(`last_error`) |
| Run이 FAILED | `failure_type` | `SUBMIT_REJECTED`(제출 거절, 로그 `Run submission rejected`), `ABANDONED`(24시간 연속 통신 실패, 로그 `Task abandoned`), 그 밖의 값은 Executor가 준 실패 사유(`state.reason.code`) |
| `ABANDONED` 발생 | Executor에서 해당 `execution_id`가 아직 도는지 | pr-batch는 취소를 한 번만 시도한다. 남아 있으면 Executor 쪽에서 정리 |
| WAITING이 계속 쌓임 | 같은 배치의 QUEUED/RUNNING Run, 배치 policy의 `max_concurrent` | 앞선 실행이 오래 걸리거나 멈춤. 앞선 Run부터 진단 |
| 로그에 `Task failed`가 **ERROR** 레벨 | `status=400/401/403` | Executor key 또는 tenant 설정 문제. 재시도로 안 풀림. `EXECUTOR_API_KEY`, Executor 쪽 client 설정 확인 |
| 호출자가 401 | 요청 헤더 `X-Api-Key` | key 해시가 `AUTH_CLIENTS`에 없음 (이 경우 감사 로그도 남지 않음) |
| 호출자가 403/404 | 감사 로그 `Caller write request client= tenant= user= status=` | 403: 권한 없는 tenant 지정·admin 아님 / 404: 다른 tenant·사용자의 자원 |
| 업로드 실패 | 응답 `error.code` | 413 `UPLOAD_TOO_LARGE`, 422 `INVALID_NOTEBOOK`, `TOO_MANY_CELLS`, `CELL_TOO_LARGE`, `EMPTY_NOTEBOOK`, `UNSUPPORTED_LANGUAGE`, 503 `STORAGE_UNAVAILABLE` |
| 다운로드가 503 `STORAGE_INTEGRITY_ERROR` | 공유 스토리지의 원본 파일 | 파일이 바뀌었거나 사라져 저장된 sha256과 맞지 않음 |
| API가 503 `DATABASE_UNAVAILABLE` / 500 `INTERNAL_ERROR` | 로그 `Database error method= path=` | 503: 연결 문제(재시도 가능) / 500: 쿼리·제약 오류(재시도 무의미, 버그 가능성) |
| API가 503 `INCONSISTENT_STATE` | 해당 배치의 `config_number`와 `batch_configs` | 현재 설정 행이 없는 등 데이터 불일치. 수동 데이터 수정 이력 확인 |

### 5.4 로그 메시지 목록

로그 값은 메시지 안에 `key=value`로 들어간다(사내 HCP 로거 호환). API key는 자동으로 `[REDACTED]` 처리되고, `/healthz`·`/readyz` 접근 로그는 남기지 않는다.

| 레벨 | 메시지 (앞부분) | 의미 | 조치 |
| --- | --- | --- | --- |
| INFO | `Applying database migrations.` | 시작 시 migration 수행 | — |
| INFO | `Background runtime started loops=N` | 백그라운드 루프 N개 시작 (2 + `TASK_CONCURRENCY`) | 없으면 런타임 미기동 |
| INFO | `Background runtime stopped.` | 정상 종료 | — |
| ERROR | `Loop database unavailable loop=…` | 루프가 DB 오류로 한 바퀴 실패 (1초 뒤 재시도) | 계속되면 DB 점검 |
| ERROR | `Loop unexpected error loop=…` | 루프 내 예상 못한 예외 | traceback 확인, 버그 가능성 |
| WARNING | `Schedule admission failed batch_id= attempt= retry_in_seconds=` | 예약 접수 오류, 재시도 예정 | 같은 배치가 반복되면 배치 데이터 확인 |
| ERROR | `Schedule admission given up batch_id= attempts= action=skip_due_occurrences` | 재시도 한도 초과, 해당 회차 생략(`SCHEDULER_ERROR`) | 배치 설정·일정 확인 |
| ERROR | `Schedule stopped batch_id= reason=next_occurrence_unavailable` | 다음 회차를 계산할 수 없어 예약 중단 | 일정 정의 확인 |
| WARNING | `Task failed kind= task_id= run_id= batch_id= attempt= status= code= error=` | Executor 호출 실패, backoff 재시도 | `error`로 원인 분류 |
| ERROR | `Task failed …` (status 400/401/403) | 인증·tenant 설정 거절 | 설정 수정 필요 |
| ERROR | `Task unexpected error kind= task_id= run_id= stage=` | handler 버그 등. 통신 오류처럼 재시도·포기 규칙 적용 | traceback 확인 |
| ERROR | `Task abandoned kind= task_id= run_id= batch_id= error=` | 24시간 연속 실패로 포기, Run FAILED(ABANDONED) | Executor 쪽 실행 잔존 확인 |
| INFO/ERROR | `Run submission rejected run_id= batch_id= status= code= error=` | 제출 확정 거절, Run FAILED(SUBMIT_REJECTED). 인증 거절이면 ERROR | `code`로 원인 확인 (커널 미지원 등) |
| ERROR | `Database error method= path= …` | API 처리 중 DB 오류 | 5.3 표 참고 |
| INFO | `Run status changed run_id= batch_id= from= to= trigger= execution_id= reason=` | Run 상태가 바뀔 때마다 한 줄. 새 Run은 `from=-`. `reason`은 SKIPPED면 skip_reason, FAILED면 failure_type, CANCELLED면 cancel_source | `run_id=`로 검색하면 Run 한 건의 전체 경과가 보임 |
| INFO | `Run cancel requested run_id= batch_id= status= source= execution_id=` | QUEUED·RUNNING Run에 취소 요청이 들어옴(`source` USER/POLICY). 상태는 아직 그대로 | 뒤이어 `Run status changed … to=CANCELLED`가 나와야 정상 |
| INFO | `Caller write request client= tenant= user= method= path= status=` | 쓰기 요청 감사 로그 (key가 맞은 요청만) | 누가 언제 무엇을 바꿨는지 추적 |
| INFO | `Batches disabled by owner client= tenants= owner= disabled=` | 사용자 단위 일괄 비활성 | — |
| INFO | `Batches disabled by tenant client= tenant= disabled=` | tenant 단위 일괄 비활성 | — |

**Run 한 건 추적**: Kibana에서 `run_id=<값>`으로 검색하면 접수(`from=-`) → QUEUED → RUNNING(`execution_id` 포함) → COLLECTING → 종료까지 `Run status changed` 줄이 시간순으로 나온다. 중간에 멈췄다면 마지막 줄 이후의 `Task failed … run_id=<값>`을 본다. 상태가 바뀌지 않는 Executor 상태 조회(폴링)는 로그를 남기지 않는다. 이 로그는 트랜잭션 커밋 직전에 남으므로, DB 오류로 롤백되면 같은 전이가 재시도 때 한 번 더 찍힐 수 있다.

**로그로 알 수 없는 것**: 요청 ID가 없어 서비스 간 추적은 `run_id`·`execution_id`·시각으로 맞춘다. Executor가 보고한 실행 시작·종료 시각은 `batch_runs`(`started_at`, `finished_at`)에 있다. 2026-11-22 이전 배포분에는 `Run status changed` 로그가 없다.

### 5.5 진단 SQL

모두 읽기 전용이다. 스키마는 `batch`.

```sql
-- 1) 30분 넘게 진행 중인 Run과 걸려 있는 Task
SELECT r.id AS run_id, r.batch_id, r.status, r.last_error, r.execution_id,
       r.execution_status, r.updated_at,
       t.kind, t.attempts, t.last_error AS task_error, t.failing_since,
       t.run_at, t.lease_until
FROM batch.batch_runs r
LEFT JOIN batch.tasks t ON t.run_id = r.id AND t.status = 'PENDING'
WHERE r.status IN ('QUEUED', 'RUNNING', 'COLLECTING')
  AND r.updated_at < now() - interval '30 minutes'
ORDER BY r.updated_at;

-- 2) 진행 중인데 PENDING Task가 하나도 없는 Run (멈춘 Run 후보)
SELECT r.id, r.batch_id, r.status, r.updated_at
FROM batch.batch_runs r
WHERE r.status IN ('QUEUED', 'RUNNING', 'COLLECTING')
  AND NOT EXISTS (SELECT 1 FROM batch.tasks t
                  WHERE t.run_id = r.id AND t.status = 'PENDING');

-- 3) 예약이 막힌 배치
SELECT id, tenant_id, owner_id, name, next_run_at, schedule_failures, schedule_retry_at
FROM batch.batches
WHERE enabled
  AND (schedule_failures > 0 OR next_run_at < now() - interval '5 minutes');

-- 4) 대기 중 Task 적체와 가장 오래된 예정 시각
SELECT kind, count(*) AS pending, min(run_at) AS oldest_run_at,
       count(*) FILTER (WHERE failing_since IS NOT NULL) AS failing
FROM batch.tasks WHERE status = 'PENDING' GROUP BY kind;

-- 5) 최근 24시간 실행 결과 분포
SELECT status, coalesce(failure_type, skip_reason, '-') AS reason, count(*)
FROM batch.batch_runs
WHERE created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 3 DESC;

-- 6) 예약 → 제출 지연 (최근 24시간 예약 실행)
SELECT percentile_cont(ARRAY[0.5, 0.95]) WITHIN GROUP
         (ORDER BY extract(epoch FROM submitted_at - scheduled_for)) AS p50_p95_seconds
FROM batch.batch_runs
WHERE trigger = 'SCHEDULED' AND submitted_at IS NOT NULL
  AND created_at > now() - interval '24 hours';

-- 7) 최근 생략된 예약 회차
SELECT batch_id, reason, occurrences, from_at, until_at, created_at
FROM batch.skipped_windows
WHERE created_at > now() - interval '24 hours'
ORDER BY created_at DESC;

-- 8) pr-batch 커넥션 사용량과 잠금 대기
SELECT state, wait_event_type, count(*)
FROM pg_stat_activity WHERE usename = 'batch' GROUP BY 1, 2;

-- 9) 테이블 크기
SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_stat_user_tables WHERE schemaname = 'batch' ORDER BY pg_total_relation_size(relid) DESC;
```

---

## 6. 정기 점검 (권장 주기: 주 1회)

| 항목 | 방법 | 기준 |
| --- | --- | --- |
| 멈춘 Run | 5.5의 1), 2) | 0건이어야 정상 |
| ABANDONED·SUBMIT_REJECTED 건수 | 5.5의 5) | 늘면 Executor 연동·커널 목록 확인 |
| SCHEDULER_ERROR 생략 | 5.5의 7) | 0건이어야 정상 |
| 예약 → 제출 지연 | 5.5의 6) | p95가 수 분 이상이면 Executor 슬롯·`TASK_CONCURRENCY` 검토 |
| Task 적체 | 5.5의 4) | `oldest_run_at`이 현재보다 많이 과거면 dispatcher 처리 지연 |
| 테이블·스토리지 증가 | 5.5의 9), 스토리지 사용량 | 증가 추세 기록, 보존 정책 재논의 기준으로 사용 |
| key 만료·교체 일정 | `AUTH_CLIENTS`, `EXECUTOR_API_KEY` | 교체 시 3.2절 절차 |

---

## 7. 비상 조치

| 상황 | 조치 | 영향 |
| --- | --- | --- |
| Executor 장애가 길어져 오류 로그가 넘침 | 그대로 둬도 된다. backoff로 최대 5분 간격 재시도. 단, 24시간을 넘기면 ABANDONED 처리됨 | 장애가 24시간을 넘을 것 같으면 `TASK_GIVE_UP_SECONDS` 상향 검토(최대 30일) |
| 백그라운드 처리를 멈추고 API만 유지 | `BACKGROUND_ENABLED=false`로 재배포 | 예약·실행 진행 중단. 재개 시 놓친 예약은 misfire 정책대로 |
| 특정 사용자·tenant 배치를 모두 멈춤 | `POST /api/v1/batches:disable-by-owner` / `:disable-by-tenant` (admin key) | 비활성 사유가 `USER_DEACTIVATED` / `TENANT_REVOKED`로 남음. 새 실행 접수도 409 |
| 특정 실행 중단 | `POST /api/v1/batches/{batch_id}/runs/{run_id}/cancel` | Executor에 취소 요청 |
| migration을 수동으로 적용 | `DB_AUTO_MIGRATE=false` 상태에서 `alembic upgrade head` (DB URL은 앱과 같은 설정을 읽음) | 비상용. 평소에는 자동 migration |
| 멈춘 Run 정리 | 먼저 원인(Task 유무, `last_error`)을 기록한 뒤, 가능하면 cancel API. 불가하면 3.5절 규칙대로 상태 수동 종료 | WAITING 승격은 수동으로 함께 처리 |

---

## 8. 운영 환경 메모

- **로그**: 내부 Kibana에서 본다. 5.4의 메시지 앞부분(예: `Task failed`, `Schedule admission`)과 `run_id=`, `batch_id=` 값으로 검색한다.
- **알림**: CPU 사용량 급증과 Pod 중단만 내부 메신저로 온다. Executor 불통, 멈춘 Run, 예약 생략 같은 서비스 수준 이상은 알림이 없으므로 6장 정기 점검으로 찾는다. Kibana에서 ERROR 레벨 로그(`Task abandoned`, `Schedule admission given up`, `Task failed` 중 status 401/403)에 알림을 걸 수 있으면 거는 것을 권한다.
- **DB 백업·복구**: 전담 DBA가 없다. 권장 설정과 복구 절차는 [DB 물리 설계서](03-db-physical-design.md) 8장.
- **연동 서비스**: Executor, pr-agentic-service, prj_re도 같은 운영자가 맡는다. key 교체나 커널 목록 변경은 양쪽 설정을 함께 바꾼다(3.1, 3.2).
