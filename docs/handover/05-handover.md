# pr-batch 인수인계서

작성일 2026-11-22 · 기준 pr-batch `main` (2026-11-22 커밋 `7cf5439`), DB revision `0005_audit_client`.
운영 환경: 소스는 사내 GitLab, 빌드·배포·DB는 내부 SaaS 플랫폼이 제공한다. 인수자 한 사람이 pr-batch와 연동 서비스(Executor, pr-agentic-service, prj_re)를 함께 운영하므로 담당자·연락 체계는 따로 두지 않는다.

---

## 1. 서비스 요약

| 항목 | 내용 |
| --- | --- |
| 서비스 | pr-batch (Notebook Batch Service) |
| 목적 | 사용자가 올린 Jupyter 노트북을 일정 또는 수동으로 Executor에서 실행하고 실행 이력을 남김 |
| 사용자 진입점 | 분석 시스템 `prj_re` → pr-agentic-service → pr-batch 내부 API |
| 규모 | 초기 사용자 100~200명. 배치 수·실행량은 운영하며 확인 |
| 상태 | 구현 완료, **prd 배포 완료**. 환경은 dev와 prd 두 개이고 stg는 없다 |
| 범위 밖 | 노트북 실행(Executor), 결과 리포트(pr-agentic-service), 로그인(SSO), 알림, 자동 재실행, 배치 전용 화면 |

## 2. 구성 요소와 연동

| 대상 | 관계 | 계약 |
| --- | --- | --- |
| pr-agentic-service | pr-batch를 호출하는 주 서비스 (relay·admin key) | `X-Api-Key` + `X-User-Id` (+ `X-Tenant-Id`, `X-Project-Id`). 레포 `docs/client-auth.md` |
| Executor | pr-batch가 실행 제출·상태 조회·결과 조회·취소를 호출 | `EXECUTOR_API_KEY` + Run에 고정된 신원 헤더. 폴링 방식(이벤트 구독 안 함) |
| PostgreSQL | 모든 상태 저장, 인스턴스 간 조정 | DB·스키마·계정 모두 `batch` |
| 공유 스토리지 | 노트북 원본·셀 파일. Executor가 같은 경로로 읽음 | `STORAGE_ROOT`(예 `/mnt/data/dtest`) 아래 `batch/inputs/…`, UID 1000 |

## 3. 문서 지도

| 문서 | 내용 |
| --- | --- |
| [01 소스코드 설명서](01-source-code-guide.md) | 실행 구조, 모듈 지도, 핵심 흐름, 인증, API 규칙·오류 코드, Executor 연동, 설정, 테스트, 변경 가이드 |
| [02 DB 논리 설계서](02-db-logical-design.md) | 엔터티, 관계, 코드값, JSONB 구조, 이력·삭제 규칙 |
| [03 DB 물리 설계서 + ERD](03-db-physical-design.md) | 테이블·인덱스·제약 정의, 잠금, 용량 추정, migration 이력. DDL은 [03-db-schema.sql](03-db-schema.sql) |
| [04 운영자 가이드](04-operator-guide.md) | **운영 주의점(3장), 부하 지점(4장), 장애 시 확인 위치·로그·진단 SQL(5장)**, 정기 점검, 비상 조치 |
| 레포 `README.md` | 로컬 실행, Docker 연계 테스트, 품질 검사 |
| 레포 `docs/requirements.md` | 확정 요구사항(2026-10-12 인터뷰) |
| 레포 `docs/architecture.md` | 설계 근거와 대안 비교 |
| 레포 `docs/api/` | API 종류별 명세, `openapi.json`, 코딩 에이전트용 요약 |
| 레포 `docs/integration-test/scenarios.md` | 에이전트 → pr-batch → Executor 통합테스트 시나리오 |

프로젝트 공유 폴더의 `batch-api-docs/`, `batch-api-guide/`, `auth-guide/`, `_old-docs/`는 레포 문서보다 **오래된 사본**이다. 기준은 레포 `docs/`다.

## 4. 주요 설계 결정과 이유

| 결정 | 이유 |
| --- | --- |
| 한 프로세스에서 API + 스케줄러 + 작업 처리기 | 배포 플랫폼이 레포당 서비스 하나, 컨테이너 하나만 허용. 모든 인스턴스가 같은 일을 하고 DB 잠금으로 나눔 |
| PostgreSQL 기반 내장 작업 큐(Celery·Redis·메모리 스케줄러 미사용) | 추가 인프라 없이 인스턴스 수와 무관하게 정확히 한 번 접수, 장애 시 lease로 복구 |
| Executor 상태를 폴링(이벤트 구독 안 함) | 지금 규모에선 구독의 이점(종료 감지 수십 초 단축)보다 운영 비용이 큼. 도입 기준은 architecture.md 8장 |
| 시작 시 자동 migration(advisory lock) | 별도 Job·init container 불가 |
| 서비스 인증을 JWT에서 API key + 신원 헤더로 변경 (2026-10-29) | 비밀이 어차피 레포 설정에 들어가 JWT의 보안 이점이 작고, 호출 서비스 추가가 쉬워야 함. Executor·pr-agentic-service와 같은 형식 |
| 소유는 tenant + 사용자 (2026-10-29) | Executor와 같은 체계 |
| 실행 이력 자동 삭제 없음 | 요건 |
| 실행 재시도 정책 보류 | 요건. 상태만 기록 |
| 결과 리포트를 pr-batch 범위에서 제외 | pr-agentic-service가 Executor 이벤트로 작성 |

## 5. 운영 환경과 자산

| 자산 | 위치·방법 |
| --- | --- |
| 설정·비밀값 | 레포 `config.{dev,prd}.yml` (DB 비밀번호, `EXECUTOR_API_KEY`, 호출자 key 해시). **레포 접근 = 비밀 접근** (`config.stg.yml`은 stg 환경이 없어 쓰지 않는다) |
| 호출자 API key 원문 | pr-batch에는 해시만 있다. 원문은 호출 서비스(pr-agentic-service) 설정에 있다 |
| Executor key | Executor 설정에서 발급·교체한다. 같은 운영자가 관리하므로 교체 시 Executor와 pr-batch 설정을 함께 바꾼다 (운영자 가이드 3.2) |
| DB | 내부 SaaS PostgreSQL. DB·스키마·계정 모두 `batch`. 전담 DBA가 없으므로 백업·복구는 운영자가 챙긴다 (DB 물리 설계서 8장 권장안) |
| 로그 | 내부 Kibana. 검색어는 운영자 가이드 5.4의 로그 메시지 표 |
| 알림 | CPU 사용량 급증, Pod 중단 시 내부 메신저로 알림이 온다. 그 밖의 이상(멈춘 Run, 예약 생략 등)은 알림이 없어 정기 점검(운영자 가이드 6장)으로 확인한다 |
| 장애 대응 | 운영자가 직접 판단·조치한다. 순서는 운영자 가이드 5장(확인) → 7장(비상 조치) |

## 6. 알려진 이슈·개선 후보

| 구분 | 내용 | 영향 |
| --- | --- | --- |
| 관측성 | 메트릭·요청 ID 없음. 알림은 CPU·Pod 중단뿐 | 적체·지연은 운영자 가이드 5.5 SQL과 정기 점검으로 확인 |
| 데이터 | `batch_runs`, `run_step_bindings`, `skipped_windows`, 공유 스토리지 원본 영구 보존 | 장기 증가. 운영 1년 시점에 보존 정책 재논의 |
| 성능(추정) | tenant 전체 목록 조회는 기존 인덱스로 정렬 못 할 수 있음 | tenant의 배치가 많아지면 목록 지연. 실행 계획으로 확인 필요 |
| 연동 | `BATCH_RUNTIMES`와 Executor 커널 목록을 수동으로 맞춤 | 어긋나면 제출 거절(FAILED `SUBMIT_REJECTED`) |
| 연동 | Executor 호출이 24시간 실패하면 Run을 포기(ABANDONED) | Executor 쪽 실행이 남을 수 있음 |
| 문서 | 레포 `docs/architecture.md` 일부 수치·설명이 코드와 다름(소스코드 설명서 9.1) | 문서 수정 필요 |
| 문서 | 레포 `docs/client-auth.md`의 "Executor가 header 모드일 때" 문장이 현재와 다름 | 문서 수정 필요 |
| 통합테스트 | 게이트웨이 경유 시 `Location`·`Content-Disposition` 헤더 문제 등 (레포 `docs/integration-test/scenarios.md` "설계 리뷰") | 게이트웨이 쪽 확인 필요 |
| 배포 | Run 상태 변경 로그(커밋 `7cf5439`)는 main에 병합됨. prd에 배포돼야 Kibana에서 보임 | 다음 배포 때 반영 |
| 테스트 | 레포 폴더에 `.env`(EXECUTOR_BASE_URL)가 있으면 테스트 2개가, `config.dev.yml`의 `DOCS_ENABLED`를 true로 두면 1개가 실패 | 로컬 환경 문제. 깨끗한 체크아웃에서는 254개 통과 |
| 보류 | 실행 재시도 정책, 노트북 파라미터, DAG, 알림 | 요건상 초기 범위 밖 |

## 7. 인수 체크리스트

인수 담당자가 직접 해 보고 체크한다.

- [ ] GitLab 레포 clone, `uv sync --locked`, `make check` 통과
- [ ] 로컬 PostgreSQL로 `make serve` 기동, `/readyz` 200 확인
- [ ] Executor Compose와 함께 `make docker-up` → `make docker-smoke` 성공 (업로드 → 배치 등록 → 수동 실행 → 결과)
- [ ] 소스코드 설명서 3장의 흐름(업로드, 예약 접수, Task 처리)을 코드에서 따라가 봄
- [ ] dev 환경에서 배치 하나를 등록·활성화하고 예약 실행이 COMPLETED까지 가는 것을 DB(`batch_runs`, `tasks`)로 확인
- [ ] 운영자 가이드 5.5의 진단 SQL을 dev DB에서 실행해 봄
- [ ] 호출자 key 교체 절차(운영자 가이드 3.2)를 dev에서 1회 수행
- [ ] Executor를 잠시 멈췄다 살려서 Run이 재시도로 복구되는 것 확인 (`last_error`, `Task failed` 로그)
- [ ] 설정 변경(예: `TASK_CONCURRENCY`) → 배포 → 반영 확인 1회
- [ ] Kibana에서 운영자 가이드 5.4의 로그 메시지 검색해 봄
- [ ] DB 백업 설정 확인과 복구 리허설 1회 (DB 물리 설계서 8장)
