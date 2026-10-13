# pr-batch

Python 3.12 기반 배치 서비스 프로젝트입니다.
사용자별 노트북 업로드·버전 관리와, 업로드한 노트북을 언제·어떻게 돌릴지 정하는
배치 등록(일정·정책), 수동 실행·실행 이력을 제공합니다.
별도 워커가 Executor 제출과 상태·결과 참조 수집을 담당합니다.
예약 활성화·스케줄러·누락 일정 복구를 지원합니다. 리포트 생성 연동은 다음 단계입니다.

[노트북·배치 등록 API 안내](docs/registration-api.md)와
[수동 실행·워커 안내](docs/execution-api.md),
[예약 실행 안내](docs/scheduling-api.md)를 따릅니다.

## 서비스 설계

인터뷰에서 합의한 범위와 구현 전 검토할 설계를 관리합니다.

- [확정 요구사항](docs/requirements.md)
- [확정 실행 이력 저장 계약](docs/execution-history.md)
- [노트북·배치 분리 설계](docs/notebook-batch-design.md)
- [API·데이터 모델·상태 흐름 설계 초안](docs/design.md)
- [Executor·prj_re 연동 계약과 확인 사항](docs/integrations.md)

설계 초안은 전체 목표이며 실제 구현 범위는 각 API 안내에서 구분합니다.

## 디렉토리 구조

src 레이아웃입니다. 애플리케이션 패키지는 `src/app`이고 `from app.core.config import ...`
형태로 import합니다. `uv sync`가 `app` 패키지를 가상환경에 설치합니다.

```text
src/app/
  main.py                 앱 생성: 라우터·미들웨어·예외 핸들러·헬스체크
  core/                   config(환경 설정), security(JWT), exceptions, middleware
  db/                     Base, 엔진·세션 생성, DB 시각
  models/                 SQLAlchemy 테이블: notebook, batch, run, idempotency
  schemas/                Pydantic 계약: common, schedule, notebook, batch, run
  api/
    deps.py               인증·Idempotency-Key·If-Match·서비스 주입
    router.py             /api/v1 라우터 묶음
    v1/                   notebooks, batches, runs 엔드포인트
  services/               notebook, batch, run, idempotency, 공용 조회
  domain/                 순수 로직: schedule_clock, notebook_parser
  storage/                공유 PV 노트북 저장소
  integrations/executor/  Executor HTTP 클라이언트와 응답 계약
  workers/                executor_worker(제출·상태 수집), scheduler(예약 접수)
migrations/               Alembic (batch 스키마)
scripts/                  Docker smoke 테스트
tests/
```

API(`uvicorn app.main:create_app --factory`), Executor 워커
(`python -m app.workers.executor_worker`), 스케줄러(`python -m app.workers.scheduler`)는
같은 DB와 설정을 쓰는 별도 프로세스입니다.

## 개발 도구

- Python 3.12 (`>=3.12,<3.13`)
- uv: 가상환경 및 의존성 관리
- Ruff: 린트, import 정렬, 코드 포맷
- ty: 정적 타입 검사

설정은 `pyproject.toml`, Python 버전은 `.python-version`에서 관리합니다.
의존성의 실제 설치 버전은 `uv.lock`에 고정하고 함께 커밋합니다.
CI는 uv 0.11.32를 사용합니다.

## 시작하기

[uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라
uv를 설치한 뒤 저장소 루트에서 실행합니다.

```sh
uv python install
uv sync --locked
```

프로젝트 전용 `.venv`가 생성됩니다. `uv run`을 사용하면 가상환경을 별도로
활성화하지 않아도 됩니다.

PostgreSQL과 공유 PV를 준비하고 `.env.example`을 `.env`로 복사한 뒤 연결값,
커널 목록, 인증 비밀키를 설정합니다. 공유 PV 경로는 미리 생성·마운트해야 합니다.

```sh
make migrate
make serve
# 별도 터미널에서 Executor 제출·상태 확인 워커 시작
make worker
# 예약 실행을 사용하면 별도 터미널에서 스케줄러도 시작
make scheduler
```

기본 주소는 `http://127.0.0.1:8000`, API 문서는 `/docs`입니다.
앱 시작은 DB migration을 자동 실행하지 않습니다.
로컬 Executor가 8000 포트를 쓰면 `make serve` 대신 다른 포트를 지정합니다.

## Docker로 Executor 연계 테스트

Executor 저장소의 분리된 Compose를 먼저 띄웁니다. `shared-infra`는 공용
PostgreSQL·Redis, `executor-runtimes`는 Jupyter 런타임, `executor`는 실행기입니다.
pr-batch는 외부 네트워크 `shared-infra`에 붙어 `postgres`와 `executor`를
서비스 이름으로 호출합니다.

```sh
make docker-up      # DB 준비, migration, API·워커·스케줄러 기동
make docker-smoke   # 노트북 업로드 → 배치 등록 → 수동 실행 → Executor 결과 확인
make docker-logs
make docker-down
```

- API는 `http://127.0.0.1:8100`에 노출합니다(`PR_BATCH_PORT`).
- `db-init`이 공용 PostgreSQL에 `batch` 역할과 DB를 멱등 생성합니다.
  관리자 계정 기본값은 Executor Compose와 같은 `executor`이며
  `SHARED_POSTGRES_ADMIN_*`로 바꿉니다.
- 공유 PV는 Executor가 `INPUT_STORAGE_ROOT`로 마운트하는
  `../executor/shared_dir`을 같은 경로로 마운트합니다(`EXECUTOR_SHARED_DIR`).
  두 이미지 모두 UID 1000으로 실행합니다.
- 커널 기본값은 로컬 BATCH 런타임의 `default`입니다(`PR_BATCH_KERNEL_PROFILES`).
- 인증 비밀키 기본값은 로컬 전용입니다. 다른 환경에서는 `PR_BATCH_AUTH_SECRET`을
  반드시 지정합니다.

변수는 셸이나 Compose 변수 파일로 지정합니다. 컨테이너는 저장소의 `.env`를
읽지 않습니다.

## 품질 검사

```sh
make check
```

Make가 없는 환경에서는 아래 명령을 직접 실행합니다.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked pytest -q
```

자동 수정과 포맷 적용은 `make format`으로 실행합니다.
GitHub Actions에서도 push와 pull request마다 동일한 검사를 수행합니다.
`make check`는 정적 검사와 테스트를 실행합니다. 실제 PostgreSQL 통합 테스트는
`BATCH_TEST_DATABASE_URL`을 설정해야 실행되며, 없으면 명시적으로 skip합니다.
테스트 DB 이름은 `_test`로 끝나야 하며 `batch` 스키마 데이터가 초기화됩니다.
운영 DB나 보존할 데이터가 있는 DB를 지정하지 않습니다. CI는 일회성 PostgreSQL로
전체 테스트와 모델·migration 일치 검사를 실행합니다.

## 의존성 변경

```sh
uv add <package>
uv add --dev <tool>
```

변경된 `pyproject.toml`과 `uv.lock`을 함께 커밋합니다.
`.venv`, 도구 캐시, `.env` 등 로컬 환경 파일은 커밋하지 않습니다.

## Git 작업 흐름

원격 저장소: [KimHwaJin/pr-batch](https://github.com/KimHwaJin/pr-batch)

```sh
# 작업 시작 전, 작업 트리가 깨끗한 상태에서 원격 변경 반영
git pull --ff-only

# 변경 후 검사 및 동기화
make check
git add <changed-files>
git commit -m "Describe the change"
git push
```

원격 이력이 분기되어 pull이 실패하면 차이를 확인한 뒤 통합합니다.
서비스 목적, 배치 실행 방식, 데이터 입출력, 재시도 및 운영 요구사항을
인터뷰에서 정한 후 프레임워크와 구현 구조를 결정합니다.

## 설정 참고

- [uv 프로젝트 관리](https://docs.astral.sh/uv/guides/projects/)
- [Ruff 설정](https://docs.astral.sh/ruff/configuration/)
- [ty 설정](https://docs.astral.sh/ty/reference/configuration/)
- [uv GitHub Actions 연동](https://docs.astral.sh/uv/guides/integration/github/)
