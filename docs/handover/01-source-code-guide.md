# pr-batch 소스코드 설명서

기준: pr-batch `main` (2026-11-22 커밋 `7cf5439`, Run 상태 변경 로그 포함). 작성일 2026-11-22.
대상: pr-batch를 이어서 개발·유지보수할 개발자.
관련: [운영자 가이드](04-operator-guide.md) · [DB 논리 설계서](02-db-logical-design.md) · [DB 물리 설계서](03-db-physical-design.md) ·
레포 `docs/architecture.md`(설계 근거), `docs/api/`(API 명세)

---

## 1. 서비스 개요

- **무엇을 하나**: 사용자가 올린 Jupyter 노트북을 버전으로 보관하고(스크립트), 일정·정책을 붙여 등록하면(배치) 정해진 시각 또는 수동으로 Executor에서 실행하고 실행 이력을 남긴다.
- **하지 않는 것**: 노트북 실행 자체(Executor), 결과 리포트 작성(pr-agentic-service), 사용자 로그인(SSO는 호출 서비스 책임), 알림, 자동 재실행.
- **사용자 진입점**: 분석 시스템 `prj_re` → pr-agentic-service → pr-batch 내부 API. 규모는 초기 사용자 100~200명.

### 1.1 기술 스택

| 영역 | 사용 |
| --- | --- |
| 언어 | Python 3.12 (`>=3.12,<3.13`) |
| 웹 | FastAPI + uvicorn (워커 1개) |
| DB | PostgreSQL, SQLAlchemy 2 (동기 ORM), psycopg 3, Alembic |
| HTTP 클라이언트 | `httpx2` (Executor 호출) |
| 검증 | Pydantic v2 |
| 도구 | uv(의존성), Ruff(린트·포맷), ty(타입), pytest |
| 로깅 | 사내 HCP 로거가 있으면 그것, 없으면 `logger.yml`(표준 `dictConfig`) |

### 1.2 실행 구조

**프로세스 하나가 HTTP API와 백그라운드 런타임을 함께 돌린다.** 인스턴스를 여러 개 띄워도 모두 같고 리더가 없다. 인스턴스 사이의 조정은 전부 PostgreSQL 잠금으로 한다.

```text
pr-batch 프로세스 (src/app/serve.py → app.main:create_app)
├── FastAPI (이벤트 루프)            /api/v1/*, /healthz, /readyz
│     요청 처리는 동기 함수 → 스레드풀. 원격 호출 없음, DB 기록만
└── BackgroundRuntime (데몬 스레드들, lifespan에서 시작·종료)
      ├── scheduler      1개   Scheduler.tick       도래한 예약 → Run + 첫 Task
      ├── tasks-0..N-1   N개   Dispatcher.tick      Task claim → Executor 호출 → 반영  (N = TASK_CONCURRENCY, 기본 2)
      └── retention      1개   Retention.tick       오래된 Task·멱등성 기록 삭제 (1시간마다)
```

각 루프는 `tick()`이 일을 했으면 바로 다시 돌고, 할 일이 없으면 `next_due()`가 알려 주는 다음 시각까지(최대 `IDLE_MAX_SECONDS`) 잔다(`runtime/runtime.py::BackgroundRuntime.loop`). 루프마다 heartbeat를 메모리에 남기고 `/healthz`가 이를 본다.

---

## 2. 디렉토리·모듈 지도

```text
src/app/
  serve.py                 진입점(pr-batch 명령). uvicorn으로 create_app 실행
  main.py                  앱 생성: 미들웨어, 예외 핸들러, 라우터, /healthz, /readyz, lifespan(migration → 런타임 시작)
  core/
    config.py              Settings (pydantic-settings). YAML·.env·환경변수 병합, 값 검증
    security.py            API key 인증, X-User-Id/X-Tenant-Id/X-Project-Id 해석, 권한(relay, admin)
    exceptions.py          ServiceError, 오류 응답 형식, 검증·DB 오류 핸들러
    middleware.py          RequestSizeLimit(본문 크기 제한), AuditWrites(쓰기 감사 로그)
    logging_setup.py       HCP/표준 로깅 설정, probe 로그 제외, API key 마스킹
  api/
    router.py              /api/v1 라우터 묶음
    deps.py                의존성: 서비스 주입, If-Match, Idempotency-Key, 업로드 파일, Executor 설정 확인
    v1/scripts.py          스크립트·버전 엔드포인트
    v1/batches.py          배치 엔드포인트 (+ 관리용 disable-by-owner/tenant)
    v1/runs.py             실행 엔드포인트 (/batches/{id}/runs)
    v1/runtimes.py         선택 가능한 런타임 목록
  services/                유스케이스(트랜잭션 경계). API와 백그라운드가 같이 씀
    script_service.py      업로드·버전·이름 변경·삭제·다운로드
    batch_service.py       배치 등록·수정(BatchConfig 생성)·활성/비활성·삭제·일괄 비활성
    run_service.py         실행 접수(admit_run), 취소, WAITING 승격, Executor 제출 본문·신원 헤더
    idempotency.py         Idempotency-Key 처리(첫 응답 저장·재생)
    schedule_history.py    SkippedWindow 기록
    runtime_catalog.py     BATCH_RUNTIMES 기반 런타임 검증
    common.py              소유 범위 조건, 커서 페이지, revision 확인, 현재 설정 조회
  domain/                  DB·HTTP와 무관한 순수 로직
    run_state.py           Run 상태와 묶음(OCCUPYING, ACTIVE, FINAL, CANCELLABLE)
    schedule_clock.py      일정 → 다음/이전 회차 계산 (ONCE/INTERVAL/CALENDAR/CRON)
    notebook_parser.py     .ipynb 파싱·검증 → 실행 셀 목록
    script_parser.py, scripts.py, runtimes.py   타입별 파서 등록, 스크립트 타입, 런타임 타입
  schemas/                 Pydantic API 계약 (common, schedule, script, batch, run)
  models/                  SQLAlchemy 테이블 (script, batch, run, idempotency)
  db/
    base.py                Base(스키마 batch), Actor, 감사·soft delete mixin
    session.py             엔진·세션, database_now()(DB 시계)
    migrate.py             advisory lock 아래 alembic upgrade head
  storage/script_storage.py  공유 스토리지 저장·읽기(경로 안전성, 원자적 쓰기, 해시 확인)
  integrations/executor/   Executor HTTP 클라이언트(client.py)와 응답 계약(contracts.py)
  runtime/                 백그라운드 런타임: runtime.py(루프), scheduler.py, dispatcher.py, retention.py
  tasks/                   queue.py(Task 큐: enqueue/claim/lease/backoff), handlers.py(Task 종류별 처리)
migrations/                Alembic (env.py, versions/0001~0005)
scripts/                   docker_smoke.py, load_check.py, multi_instance_check.py (점검용)
tests/                     pytest
```

**의존 방향**: `api → services → (domain, models, storage)`, `runtime → tasks → services/integrations`. `domain`은 아무것도 의존하지 않는다. API 계층은 Executor를 직접 부르지 않는다(원격 호출은 `tasks/handlers.py`에서만).

---

## 3. 핵심 흐름 따라 읽기

### 3.1 노트북 업로드

```text
POST /api/v1/scripts (multipart)
 → api/v1/scripts.py::create_script
 → api/deps.py::uploaded_script              파일 읽기 (RequestSizeLimit가 본문 크기 선제한)
 → services/script_service.py::create_script
     → domain/notebook_parser.py             JSON 파싱, 셀 수·크기·언어 검증 → 실행 셀 목록
     → storage/script_storage.py::save       임시 폴더에 원본+셀 파일 쓰기(fsync) → rename
     → DB: Script + ScriptVersion insert     (실패 시 storage.discard로 파일 정리)
     → services/idempotency.py::run          같은 트랜잭션에서 첫 응답 저장
```

같은 내용(sha256)을 다시 올리면 새 버전 대신 기존 버전을 돌려준다(`uq_version_content`).

### 3.2 배치 등록·수정·활성화

- 등록 `POST /batches`: 항상 비활성. `Batch` + `BatchConfig #1` 생성 (`batch_service.py::create_batch`).
- 수정 `PATCH /batches/{id}`: 이름만 바꾸면 Batch만, 버전·런타임·일정·정책이 바뀌면 `BatchConfig`를 새 번호로 추가하고 `batches.config_number`를 올린다(`next_config`). 정책은 부분 병합(`merged_policy`).
- 활성화 `POST /batches/{id}/enable`: `enable_blocker`가 막을 이유(Executor 미설정, 일정 없음, 런타임 미지원, 미래 회차 없음)를 확인하고 `next_run_at`을 계산한다.
- 비활성화: `stop_schedule`이 `next_run_at`을 지우고, 이미 도래했지만 접수 못 한 회차를 SkippedWindow(`DISABLED`)로 남긴다.
- 일괄 비활성(`disable-by-owner`, `disable-by-tenant`, admin key): `stop_batches`가 대상 배치를 끄고 WAITING Run을 SKIPPED로 정리한다.

### 3.3 예약 접수 (스케줄러)

`runtime/scheduler.py::Scheduler.tick` — 한 번에 배치 하나를 한 트랜잭션으로 처리한다.

1. `enabled AND next_run_at <= now AND (schedule_retry_at IS NULL OR <= now)`인 배치 하나를 `FOR UPDATE SKIP LOCKED`로 잡는다. 다른 인스턴스는 같은 행을 건너뛴다.
2. `admit_due`:
   - `next_run_at`이 유예(`grace_seconds`)보다 오래됐으면 misfire 정책 적용(SKIP/LATEST/CATCH_UP), 버린 구간은 SkippedWindow.
   - 도래한 회차를 최대 100개 모음. REPLACE면 마지막 하나만.
   - 회차마다 `run_service.admit_run` → Run 생성(+ Step 행) → QUEUED면 `SUBMIT_EXECUTION` Task를 같은 트랜잭션에 enqueue.
   - `next_run_at`을 다음 회차로 옮김.
3. 예외가 나면 `record_failure`: `schedule_failures`를 올리고 `SCHEDULE_RETRY_SECONDS` 뒤 재시도, `SCHEDULE_RETRY_LIMIT`를 넘기면 회차를 `SCHEDULER_ERROR`로 생략. DB 연결 오류는 배치 탓이 아니므로 기록하지 않고 루프에 넘긴다.

`admit_run`이 겹침 정책을 적용한다: 점유(QUEUED·RUNNING) 수, 대기(WAITING) 수를 세서 QUEUED / WAITING / SKIPPED를 정한다. 수동 실행(`POST /batches/{id}/runs`)도 같은 함수를 쓴다.

### 3.4 실행 진행 (dispatcher)

`runtime/dispatcher.py::Dispatcher.tick` — Task 하나를 처리한다.

```text
[트랜잭션 1] claim(): PENDING·도래·lease 없음/만료인 Task 하나를 SKIP LOCKED로 잡고
             lease_token(새 UUID), lease_until(now+90초), attempts+1 기록
             prepare(): Task·Run을 잠그고(batch→run 순서) handler.prepare로 호출 준비.
                        Run 상태가 이미 지났으면 Task를 DONE으로 끝냄(no-op)
             커밋 → 커넥션 반납
[원격 호출]  handler.call(): Executor 호출 1회 (DB 커넥션 없음)
[트랜잭션 2] locked_lease(): 내 토큰이 아직 유효한지 확인 (아니면 다른 인스턴스가 가져간 것 → 버림)
             handler.apply(): 응답 검증(require) → Run 상태 변경 + 다음 Task enqueue
실패 시      fail(): backoff 재예약, Run.last_error 기록, 한도 초과 시 abandon
```

handler(`tasks/handlers.py`):

| 클래스 | prepare 조건 | 호출 | apply |
| --- | --- | --- | --- |
| `SubmitExecution` | Run QUEUED, execution_id 없음. 제출 전 취소 요청이면 바로 CANCELLED | `submit` | step_id 매핑, Run RUNNING, `SYNC_EXECUTION` 예약, 취소 요청 있으면 `CANCEL_EXECUTION` |
| `SyncExecution` | Run RUNNING | `detail` | Executor 상태 반영. 진행 중이면 간격 두 배로 재예약, 종료면 COLLECTING + WAITING 승격 + `COLLECT_RESULT` |
| `CollectResult` | Run COLLECTING | `result` | 셀 결과·artifact·attempts 저장, COMPLETED/FAILED/CANCELLED |
| `CancelExecution` | Run RUNNING이고 취소 요청, 또는 ABANDONED 후 1회 | `cancel` | DONE (결과는 SYNC가 관찰) |

**중요한 불변식**
- 상태 변경과 그로 인한 Task 생성은 같은 트랜잭션이다. 둘 중 하나만 커밋되는 일은 없다.
- 모든 handler는 Run 상태를 먼저 확인한다. 중복·지난 Task는 부작용 없이 끝난다.
- 잠금 순서는 어디서나 batch 행 → run 행(`dispatcher.py::lock_run`, `batch_service.py::stop_batches`).
- 시간 판단은 `db/session.py::database_now`(DB `clock_timestamp()`).

### 3.5 제출의 정확히 한 번

- Executor 제출 멱등성 키 `batch-run:<run_id>:submit`. 같은 키로 몇 번을 보내도 실행은 하나다.
- 응답을 못 받은 시도(타임아웃·5xx·lease 만료)는 "uncertain"이다. 그 뒤 받은 거절은 "아무것도 안 만들어졌다"는 증거가 아니므로 재시도한다(`SubmitExecution.call`).
- 확정 거절(`ExecutorError.submit_rejected`: 422 전부, 409 `INVALID_STATE_TRANSITION`, 400/401/403)이고 앞선 시도가 확실할 때만 Run을 FAILED(`SUBMIT_REJECTED`)로 끝낸다.
- Executor로 보내는 신원(`identity_headers`)은 Run snapshot의 tenant·project와 owner로 고정한다. 재제출 때 같은 값이어야 Executor 지문이 맞는다.

### 3.6 실행 취소

`POST /batches/{id}/runs/{run_id}/cancel` → `run_service.cancel`:
- WAITING: 즉시 CANCELLED.
- QUEUED: `cancel_requested`만 표시. 제출 Task가 Executor에 닿기 전이면 CANCELLED, 이미 보냈거나 불확실하면 제출 후 취소.
- RUNNING: `cancel_requested` + `CANCEL_EXECUTION` Task.
- REPLACE 정책은 같은 경로로 `cancel_source=POLICY`.

---

## 4. 인증과 소유 범위

`core/security.py`.

| 헤더 | 규칙 |
| --- | --- |
| `X-Api-Key` | 필수. SHA-256을 `AUTH_CLIENTS[].key_sha256`과 비교 → client 이름, tenant, relay, allowed_tenants, admin |
| `X-User-Id` | 쓰기 필수, 읽기 선택. 소문자로 정규화(`[a-z0-9._-]`, 1~64자). 읽기에서 생략하면 tenant 전체 |
| `X-Tenant-Id` | 선택. relay key만 지정 가능, `allowed_tenants` 안에서. 없으면 key의 tenant |
| `X-Project-Id` | 선택. 배치 등록 시 고정 |

- 소유 조건은 `services/common.py::owned_by` (tenant 일치 + 사용자 지정 시 owner 일치). 다른 tenant·사용자의 자원은 404.
- 쓰기 요청은 `AuditWrites` 미들웨어가 client·tenant·user·경로·상태를 한 줄로 로그에 남긴다.
- 사내 표준(mTLS, OIDC 등)으로 바꿀 때는 이 파일 경계만 교체하면 된다.

---

## 5. API 공통 규칙

| 규칙 | 구현 위치 |
| --- | --- |
| 쓰기는 `Idempotency-Key` 필수. 같은 key + 같은 본문 → 첫 응답 재생, 다른 본문 → 409 `IDEMPOTENCY_CONFLICT` | `services/idempotency.py`, `api/deps.py` |
| 수정·삭제는 `If-Match: <revision>`. 어긋나면 409 `REVISION_CONFLICT` | `api/deps.py::expected_revision`, `services/common.py::check_revision` |
| 목록은 커서 페이지(`created_at`, `id` 역순) | `services/common.py` |
| 오류 형식 `{"error": {"code", "message", "details"}}`. 검증 오류는 입력값을 되돌려주지 않음 | `core/exceptions.py` |
| DB 연결 오류 → 503 `DATABASE_UNAVAILABLE`, 그 밖의 DB 오류 → 500 `INTERNAL_ERROR` | `core/exceptions.py::database_error` |

엔드포인트 목록(모두 `/api/v1` 아래):

| 경로 | 메서드 |
| --- | --- |
| `/scripts` | POST(업로드), GET(목록) |
| `/scripts/{id}` | GET, PATCH(이름), DELETE |
| `/scripts/{id}/versions` | POST(새 버전), GET |
| `/scripts/{id}/versions/{version_id}` | GET |
| `/scripts/{id}/versions/{version_id}/content` | GET(원본 다운로드) |
| `/batches` | POST, GET |
| `/batches:disable-by-owner`, `/batches:disable-by-tenant` | POST (admin) |
| `/batches/{id}` | GET, PATCH, DELETE |
| `/batches/{id}/enable`, `/disable` | POST |
| `/batches/{id}/skipped-windows` | GET |
| `/batches/{id}/runs` | POST(수동 실행), GET |
| `/batches/{id}/runs/{run_id}` | GET (셀 단계 포함) |
| `/batches/{id}/runs/{run_id}/cancel` | POST |
| `/runtimes` | GET |

요청·응답 필드의 상세는 레포 `docs/api/`(종류별 명세, `openapi.json`, 코딩 에이전트용 요약 `pr-batch-for-agents*.md`)를 따른다.

### 5.1 오류 코드

| HTTP | 코드 |
| --- | --- |
| 400 | `INVALID_IDENTITY` |
| 401 | `UNAUTHENTICATED` |
| 403 | `FORBIDDEN` |
| 404 | `NOT_FOUND` |
| 409 | `IDEMPOTENCY_CONFLICT`, `REVISION_CONFLICT`, `NOT_CANCELLABLE`, `SCHEDULE_REQUIRED`, `BATCH_HAS_ACTIVE_RUNS`, `SCRIPT_HAS_ACTIVE_RUNS`, `SCRIPT_IN_USE`, `OWNER_DEACTIVATED`, `TENANT_REVOKED` |
| 413 | `REQUEST_TOO_LARGE`(미들웨어), `UPLOAD_TOO_LARGE` |
| 422 | `VALIDATION_ERROR`, `INVALID_NOTEBOOK`, `EMPTY_NOTEBOOK`, `TOO_MANY_CELLS`, `CELL_TOO_LARGE`, `UNSUPPORTED_LANGUAGE`, `INVALID_POLICY`, `NO_FUTURE_OCCURRENCE`, `UNKNOWN_RUNTIME`, `RUNTIME_NOT_SUPPORTED`, `INVALID_CURSOR`, `INVALID_REVISION` |
| 500 | `INTERNAL_ERROR` |
| 503 | `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED`, `STORAGE_UNAVAILABLE`, `STORAGE_INTEGRITY_ERROR`, `DATABASE_UNAVAILABLE`, `INCONSISTENT_STATE`, `NOT_READY`, `MIGRATION_REQUIRED` |

수동 실행이 대기열 초과로 SKIPPED되면 오류가 아니라 409 상태 코드에 Run 본문(`status=SKIPPED`)을 돌려준다.

---

## 6. Executor 연동

`integrations/executor/client.py`, `contracts.py`.

| 메서드 | Executor API |
| --- | --- |
| `submit` | `POST /api/v1/executions` |
| `detail` | `GET /api/v1/executions/{id}` |
| `result` | `GET /api/v1/executions/{id}/result` |
| `cancel` | `POST /api/v1/executions/{id}/cancel` (409 `INVALID_STATE_TRANSITION`은 "이미 끝남"으로 처리) |

- 연결 timeout 10초, 응답 본문 읽기 최대 30초·4MiB. 오류 본문은 16KiB까지만 읽고 `error.code`만 뽑는다. 원격 응답 본문은 DB에 저장하지 않는다(안전한 코드만 `last_error`).
- 오류는 `ExecutorError(message, status, code)`로 통일: `EXECUTOR_UNREACHABLE`, `EXECUTOR_HTTP_<status>[:<code>]`, `EXECUTOR_INVALID_RESPONSE`, `EXECUTOR_RESPONSE_LIMIT`, `EXECUTOR_CONTRACT_MISMATCH`(handler의 `require` 실패).
- 응답은 Pydantic 계약(`contracts.py`)으로 검증하고, handler가 execution_id·step 수·버전 단조 증가 등을 `require`로 다시 확인한다.
- 이벤트 구독은 하지 않는다(폴링). 도입 기준은 레포 `docs/architecture.md` 8장.

---

## 7. 설정

`core/config.py::Settings`. 우선순위: 환경변수 > `.env` > `config.{APP_ENV}.yml` > `config.yml` > 코드 기본값. 키는 대소문자 무시, **모르는 키는 무시**된다. 전체 목록과 설명은 레포 루트 `config.example.yml`.

| 분류 | 주요 키 |
| --- | --- |
| 연결 | `DATABASE_URL`(필수), `STORAGE_ROOT`(필수), `EXECUTOR_BASE_URL`, `EXECUTOR_API_KEY` |
| 인증 | `AUTH_CLIENTS` |
| 업로드 제한 | `UPLOAD_MAX_BYTES`, `MAX_CODE_CELLS`, `MAX_NOTEBOOK_CELLS`, `CELL_MAX_BYTES` |
| 런타임 | `BATCH_RUNTIMES` |
| 백그라운드 | `BACKGROUND_ENABLED`, `TASK_CONCURRENCY`, `TASK_LEASE_SECONDS`, `IDLE_MAX_SECONDS`, `SYNC_INITIAL_SECONDS`, `SYNC_MAX_SECONDS`, `SHUTDOWN_GRACE_SECONDS`, `TASK_GIVE_UP_SECONDS`, `SCHEDULE_RETRY_SECONDS`, `SCHEDULE_RETRY_LIMIT` |
| 보존 | `IDEMPOTENCY_RETENTION_DAYS`, `TASK_RETENTION_DAYS` |
| DB | `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_AUTO_MIGRATE` |
| 서버·문서 | `HOST`, `PORT`, `DOCS_ENABLED`, `OPENAPI_URL` |

---

## 8. 로컬 개발·테스트

```sh
uv python install && uv sync --locked
cp .env.example .env          # DATABASE_URL, STORAGE_ROOT, EXECUTOR_BASE_URL 설정 (로컬 포트 8100)
make serve                    # API + 스케줄러 + dispatcher, 시작 시 migration
make check                    # ruff check, ty check, pytest, ruff format --check
make format                   # 자동 수정
```

- DB가 필요한 테스트는 `BATCH_TEST_DATABASE_URL`이 있을 때만 돈다. **DB 이름이 `_test`로 끝나야 하고 `batch` 스키마 데이터를 지운다.** 운영 DB를 지정하지 않는다.
- 레포의 CI 정의(`.github/workflows/ci.yml`)는 PostgreSQL 16을 띄워 `make check`와 `alembic check`(모델 ↔ migration 일치)를 돌린다.
- Executor 연계 확인: Executor 레포의 Compose를 먼저 띄운 뒤 `make docker-up`, `make docker-smoke`(업로드 → 등록 → 실행 → 결과). 부하는 `make docker-load`, 여러 인스턴스는 `make docker-multi`.

| 테스트 파일 | 범위 |
| --- | --- |
| `test_registration.py` | 스크립트·배치 등록·수정·인증·소유 범위 |
| `test_runs.py` | 실행 접수·취소·Task 처리·Executor 응답 처리 |
| `test_scheduler.py` | 예약 접수, misfire, 겹침, 재시도 |
| `test_schedule_clock.py` | 일정 계산 |
| `test_validation.py` | 노트북·입력 검증 |
| `test_deletion.py` | 삭제 규칙 |
| `test_errors.py`, `test_config.py`, `test_logging_setup.py`, `test_schema_check.py` | 오류 형식, 설정, 로깅, readiness의 revision 판단 |

---

## 9. 변경 가이드

| 하고 싶은 것 | 손댈 곳 |
| --- | --- |
| DB 컬럼·테이블 추가 | `models/*.py` 수정 → `alembic revision --autogenerate` → 검토 → `make check`. 운영 이후에는 expand/contract 규칙(운영자 가이드 3.6) |
| 새 Task 종류(예: 알림) | `tasks/queue.py::TaskKind`에 추가, `tasks/handlers.py`에 handler(prepare/call/apply) 구현·`HANDLERS` 등록, 상태 전이 트랜잭션에서 `enqueue` |
| 새 스크립트 타입(예: .py) | `domain/scripts.py`, `domain/script_parser.py`에 파서 등록, `models/script.py`의 CHECK, `ORIGINAL_NAMES` |
| 런타임 추가 | 설정 `BATCH_RUNTIMES`만 (Executor 풀과 맞춤) |
| 일정 종류 추가 | `schemas/schedule.py`, `domain/schedule_clock.py`, `test_schedule_clock.py` |
| 인증 방식 교체 | `core/security.py` 경계 |
| Run 상태를 바꾸는 코드 추가 | 상태를 바꾼 직후 `run_service.log_status_change(run, 이전상태)`를 호출한다(취소 요청만 켤 때는 `log_cancel_requested`). 모든 전이가 `Run status changed` 로그를 남기는 것이 규칙 |
| 로그 추가 | 값은 `extra`가 아니라 메시지 안 `key=value`(`%s` 인자). HCP 형식이 메시지만 출력함 |

### 9.1 알려진 개선 후보

- 메트릭 엔드포인트가 없다(적체·지연은 SQL로만 확인).
- 서비스 간 요청 ID가 없다.
- 실행 이력 테이블은 영구 보존이다. 보존 정책은 운영 1년 시점에 재논의 권장.
- tenant 전체 목록 조회(X-User-Id 생략)는 `(tenant_id, owner_id, created_at, id)` 인덱스로 정렬을 못 할 수 있다(실행 계획 미확인).
- 레포 문서 불일치: `docs/architecture.md` 11장의 "전체 상한 24"(실제 12), 9장 "claim은 한 번에 여러 Task"(실제 tick당 1개), 5장 "HTTP timeout 30초"(연결 10초 + 읽기 30초), `docs/client-auth.md`의 "Executor가 header 모드일 때" 문장.
