# batch Table 정의서

스키마 `batch` · PostgreSQL · DB revision `0005_audit_client` · 기준 소스 pr-batch main `e71019e` (2026-11-25) · 작성 2026-11-27 · v1.1

| 항목 | 내용 |
|---|---|
| 시스템명 | batch (레포 pr-batch) |
| DBMS | PostgreSQL |
| 스키마 | batch |
| 테이블 수 | 9 |
| 컬럼 수 | 145 |
| DB revision | 0005_audit_client |
| 기준 소스 | pr-batch main e71019e (2026-11-25) |
| Vector DB | 사용하지 않음 |

## 1. 테이블 목록

| No | 테이블 ID | 테이블명 | 주제 영역 | 설명 | 삭제·보존 | 증가 요인 | 컬럼 수 |
|---|---|---|---|---|---|---|---|
| 1 | `scripts` | 스크립트 | 스크립트 | 사용자가 업로드한 스크립트(노트북). 이름·유형과 버전 목록의 주인 | soft delete (행은 남음) | 사용자 업로드 | 16 |
| 2 | `script_versions` | 스크립트 버전 | 스크립트 | 업로드한 원본 파일 하나에 해당하는 버전. 만든 뒤 바뀌지 않음(불변) | 삭제 없음 | 업로드·새 버전 등록 | 16 |
| 3 | `batches` | 배치 | 배치 | "이 스크립트 버전을 이 일정·정책으로 돌린다"는 등록. 일정·정책의 주인 | soft delete (행은 남음) | 사용자 등록 | 24 |
| 4 | `batch_configs` | 배치 설정 이력 | 배치 | 배치 실행 설정(버전·런타임·일정·정책)의 불변 스냅샷. 설정이 바뀔 때마다 다음 번호로 추가 | 삭제 없음 | 배치 생성·설정 변경 | 13 |
| 5 | `batch_runs` | 실행 이력 | 실행 | 배치의 실행 1회(Run). 접수 당시 설정과 executor 실행 결과 참조를 보존 | 삭제 없음 (영구 보존) | 예약·수동 실행마다 1행 | 36 |
| 6 | `run_step_bindings` | 실행 셀 단계 | 실행 | Run 안의 코드 셀 1개와 executor step의 연결, 셀 결과 참조 | 삭제 없음 (영구 보존) | Run당 코드 셀 수(최대 100) | 7 |
| 7 | `skipped_windows` | 생략 회차 기록 | 배치 | 실행하지 않고 넘긴 예약 구간. 구간과 건수 한 행으로 압축 | 삭제 없음 | 생략 발생 시 | 13 |
| 8 | `tasks` | 내부 작업 큐 | 내부 처리 | Run을 진행시키는 외부(executor) 호출 1건. 사용자에게 노출하지 않음 | DONE 후 30일 뒤 삭제 | Run당 3개 이상(제출·상태 조회·결과 수집, 취소 시 추가) | 12 |
| 9 | `idempotency_records` | 멱등성 기록 | 내부 처리 | 같은 Idempotency-Key로 재전송된 쓰기 요청에 첫 응답을 그대로 돌려주기 위한 기록 | 생성 후 7일 뒤 삭제 | 쓰기 요청마다 | 8 |

batch 스키마에는 Alembic이 관리하는 `alembic_version`(version_num) 테이블이 함께 있다. 현재 적용된 migration revision 한 행만 가지며 업무 데이터가 아니라 목록에서 뺐다.

## 2. 테이블 관계 (외래키)

| No | 부모 테이블 | 부모 컬럼 | 자식 테이블 | 자식 컬럼 | 관계 설명 | 제약명 |
|---|---|---|---|---|---|---|
| 1 | `scripts` | id | `script_versions` | script_id | 버전 (1 : 1..N) | (자동 부여) |
| 2 | `batches` | id | `batch_configs` | batch_id | 설정 이력 (1 : 1..N) | (자동 부여) |
| 3 | `script_versions` | script_id, id | `batch_configs` | script_id, script_version_id | 실행 대상 버전 (1 : 0..N) | fk_config_version_in_script |
| 4 | `batches` | id | `batch_runs` | batch_id | 실행 (1 : 0..N) | (자동 부여) |
| 5 | `batch_configs` | batch_id, number | `batch_runs` | batch_id, config_number | 접수 당시 설정 (1 : 0..N) | (자동 부여) |
| 6 | `script_versions` | script_id, id | `batch_runs` | script_id, script_version_id | 실행한 버전 (1 : 0..N) | (자동 부여) |
| 7 | `batch_runs` | id | `run_step_bindings` | run_id | 셀 단계 (1 : 0..100) | (자동 부여) |
| 8 | `batches` | id | `skipped_windows` | batch_id | 생략 회차 (1 : 0..N) | (자동 부여) |
| 9 | `batch_configs` | batch_id, number | `skipped_windows` | batch_id, config_number | 생략 당시 설정 (1 : 0..N) | (자동 부여) |
| 10 | `batch_runs` | id | `tasks` | run_id | 외부 호출 (0..1 : 0..N) | (자동 부여) |

## 3. 테이블 정의

NULL의 N은 NOT NULL, Y는 NULL 허용이다. 키의 PK는 기본키, FK는 외래키, UQ는 유일 제약에 포함된 컬럼이다.

### 3.1 scripts (스크립트)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.scripts` |
| 테이블명 | 스크립트 |
| 주제 영역 | 스크립트 |
| 설명 | 사용자가 업로드한 스크립트(노트북). 이름·유형과 버전 목록의 주인 |
| 삭제·보존 | soft delete (행은 남음) |
| 증가 요인 | 사용자 업로드 |
| 기본키 | id |
| 컬럼 수 | 16 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 스크립트 ID | UUID | N | PK |  | 스크립트 식별자. 앱에서 uuid4로 생성 |
| 2 | `tenant_id` | 테넌트 ID | VARCHAR(64) | N |  |  | 소유 tenant. 생성 시 고정 |
| 3 | `owner_id` | 소유자 ID | VARCHAR(255) | N |  |  | 소유 사용자(X-User-Id). 생성 시 고정 |
| 4 | `name` | 스크립트명 | VARCHAR(255) | N |  |  | 표시 이름. 변경 가능 |
| 5 | `type` | 스크립트 유형 | VARCHAR(16) | N |  |  | NOTEBOOK만 허용. 생성 시 고정 |
| 6 | `revision` | 리비전 | INTEGER | N |  |  | 낙관적 잠금 번호(1 이상). 수정 API의 If-Match와 비교 |
| 7 | `latest_version_number` | 최신 버전 번호 | INTEGER | N |  |  | 이 스크립트의 마지막 버전 번호 |
| 8 | `deleted_at` | 삭제 일시 | TIMESTAMPTZ | Y |  |  | soft delete 시각. NULL이면 삭제되지 않은 상태 |
| 9 | `deleted_by` | 삭제자 | VARCHAR(255) | Y |  |  | 삭제한 사용자(X-User-Id) |
| 10 | `deleted_by_client` | 삭제 클라이언트 | VARCHAR(64) | Y |  |  | 삭제 요청에 쓰인 API key의 client 이름 |
| 11 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 12 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 13 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 14 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 15 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 16 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| CHECK | script_revision_positive | `revision >= 1` |
| CHECK | script_type | `type IN ('NOTEBOOK')` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_scripts_owner_created` |  | tenant_id, owner_id, created_at, id |  | 사용자별 스크립트 목록(최신순, 커서 페이지) |

### 3.2 script_versions (스크립트 버전)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.script_versions` |
| 테이블명 | 스크립트 버전 |
| 주제 영역 | 스크립트 |
| 설명 | 업로드한 원본 파일 하나에 해당하는 버전. 만든 뒤 바뀌지 않음(불변) |
| 삭제·보존 | 삭제 없음 |
| 증가 요인 | 업로드·새 버전 등록 |
| 기본키 | id |
| 컬럼 수 | 16 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 버전 ID | UUID | N | PK |  | 스크립트 버전 식별자 |
| 2 | `script_id` | 스크립트 ID | UUID | N | FK, UQ |  | 소속 스크립트 (참조: scripts.id) |
| 3 | `version_number` | 버전 번호 | INTEGER | N | UQ |  | 스크립트 안 순번(v1, v2 …, 1 이상) |
| 4 | `filename` | 파일명 | VARCHAR(255) | N |  |  | 업로드 파일명(표시용) |
| 5 | `original_path` | 원본 경로 | VARCHAR | N |  |  | 공유 스토리지 안 원본 파일의 상대 경로 |
| 6 | `sha256` | 원본 해시 | VARCHAR(64) | N | UQ |  | 원본 파일의 SHA-256. 다운로드 시 무결성 확인, 같은 내용 중복 업로드 방지 |
| 7 | `code_hash` | 코드 해시 | VARCHAR(64) | N |  |  | 실행 코드만의 해시 |
| 8 | `size_bytes` | 파일 크기 | INTEGER | N |  |  | 원본 크기(바이트) |
| 9 | `cell_map` | 실행 셀 목록 | JSONB | N |  |  | executor에 제출할 코드 셀 목록. [{original_index, sequence, path, sha256}] |
| 10 | `cell_list` | 전체 셀 목록 | JSONB | N |  |  | 원본의 모든 셀. [{index, type, executed, …}] |
| 11 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 12 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 13 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 14 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 15 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 16 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| FK | (자동 부여) | (script_id) → scripts (id) |
| UQ | uq_version_script_id | script_id, id |
| UQ | uq_version_content | script_id, sha256 |
| UQ | uq_version_number | script_id, version_number |
| CHECK | version_number_positive | `version_number >= 1` |

인덱스는 기본키 인덱스만 있다.

### 3.3 batches (배치)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.batches` |
| 테이블명 | 배치 |
| 주제 영역 | 배치 |
| 설명 | "이 스크립트 버전을 이 일정·정책으로 돌린다"는 등록. 일정·정책의 주인 |
| 삭제·보존 | soft delete (행은 남음) |
| 증가 요인 | 사용자 등록 |
| 기본키 | id |
| 컬럼 수 | 24 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 배치 ID | UUID | N | PK |  | 배치 식별자. 앱에서 uuid4로 생성 |
| 2 | `tenant_id` | 테넌트 ID | VARCHAR(64) | N |  |  | 소유 tenant. 등록 시 고정. executor에 제출할 때의 tenant이기도 함 |
| 3 | `owner_id` | 소유자 ID | VARCHAR(255) | N |  |  | 소유 사용자(X-User-Id). 등록 시 고정 |
| 4 | `name` | 배치명 | VARCHAR(255) | N |  |  | 표시 이름 |
| 5 | `enabled` | 활성 여부 | BOOLEAN | N |  |  | 예약 실행 활성 여부. 등록 직후는 false |
| 6 | `next_run_at` | 다음 예약 시각 | TIMESTAMPTZ | Y |  |  | 다음 예약 회차 시각. 비활성이면 반드시 NULL |
| 7 | `revision` | 리비전 | INTEGER | N |  |  | 낙관적 잠금 번호(1 이상). 수정 API의 If-Match와 비교 |
| 8 | `schedule_failures` | 예약 접수 실패 횟수 | INTEGER | N |  | `0` | next_run_at 회차의 예약 접수 실패 횟수 |
| 9 | `schedule_retry_at` | 예약 접수 재시도 시각 | TIMESTAMPTZ | Y |  |  | 이 시각까지 스케줄러가 배치를 건너뜀 |
| 10 | `config_number` | 현재 설정 번호 | INTEGER | N |  |  | 현재 적용 중인 설정(batch_configs.number) |
| 11 | `project_id` | 프로젝트 ID | VARCHAR(255) | Y |  |  | 등록 시 X-Project-Id. 고정. NULL이면 프로젝트 범위 아님 |
| 12 | `client` | 등록 클라이언트 | VARCHAR(64) | Y |  |  | 등록한 API key의 client 이름 |
| 13 | `disabled_reason` | 비활성 사유 | VARCHAR(32) | Y |  |  | OWNER(소유자가 끔), USER_DEACTIVATED(사용자 일괄 비활성), TENANT_REVOKED(tenant 일괄 비활성). 활성이거나 켠 적 없으면 NULL |
| 14 | `disabled_at` | 비활성 일시 | TIMESTAMPTZ | Y |  |  | 비활성화된 시각 |
| 15 | `disabled_by` | 비활성 주체 | VARCHAR(64) | Y |  |  | 비활성화한 주체(client 이름) |
| 16 | `deleted_at` | 삭제 일시 | TIMESTAMPTZ | Y |  |  | soft delete 시각. NULL이면 삭제되지 않은 상태 |
| 17 | `deleted_by` | 삭제자 | VARCHAR(255) | Y |  |  | 삭제한 사용자(X-User-Id) |
| 18 | `deleted_by_client` | 삭제 클라이언트 | VARCHAR(64) | Y |  |  | 삭제 요청에 쓰인 API key의 client 이름 |
| 19 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 20 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 21 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 22 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 23 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 24 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| CHECK | batch_disabled_reason | `disabled_reason IS NULL OR disabled_reason IN ('OWNER', 'USER_DEACTIVATED', 'TENANT_REVOKED')` |
| CHECK | batch_revision_positive | `revision >= 1` |
| CHECK | deleted_is_disabled | `deleted_at IS NULL OR NOT enabled` |
| CHECK | disabled_has_no_next_run | `enabled OR next_run_at IS NULL` |
| CHECK | disabled_reason_is_disabled | `disabled_reason IS NULL OR NOT enabled` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_batches_due` |  | enabled, next_run_at |  | 스케줄러가 도래한 배치를 찾는 조회 |
| `ix_batches_owner_created` |  | tenant_id, owner_id, created_at, id |  | 사용자별 배치 목록 |

### 3.4 batch_configs (배치 설정 이력)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.batch_configs` |
| 테이블명 | 배치 설정 이력 |
| 주제 영역 | 배치 |
| 설명 | 배치 실행 설정(버전·런타임·일정·정책)의 불변 스냅샷. 설정이 바뀔 때마다 다음 번호로 추가 |
| 삭제·보존 | 삭제 없음 |
| 증가 요인 | 배치 생성·설정 변경 |
| 기본키 | batch_id, number |
| 컬럼 수 | 13 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `batch_id` | 배치 ID | UUID | N | PK, FK |  | 소속 배치 (참조: batches.id) |
| 2 | `number` | 설정 번호 | INTEGER | N | PK |  | 배치 안 설정 순번(1 이상) |
| 3 | `script_id` | 스크립트 ID | UUID | N | FK |  | 실행 대상 스크립트 (참조: script_versions.script_id) |
| 4 | `script_version_id` | 스크립트 버전 ID | UUID | N | FK |  | 실행 대상 버전. script_id와 묶어 그 스크립트의 버전임을 보장 (참조: script_versions.id) |
| 5 | `runtime` | 런타임 | JSONB | N |  |  | executor 런타임. {type, profile} |
| 6 | `schedule` | 일정 | JSONB | Y |  |  | 예약 일정. {definition, timezone, start_at, end_at}. NULL이면 수동 실행 전용 |
| 7 | `policy` | 정책 | JSONB | N |  |  | 겹침(overlap)·놓친 일정(misfire) 정책. {overlap, misfire} |
| 8 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 9 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 10 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 11 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 12 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 13 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | batch_id, number |
| FK | (자동 부여) | (batch_id) → batches (id) |
| FK | fk_config_version_in_script | (script_id, script_version_id) → script_versions (script_id, id) |
| CHECK | config_number_positive | `number >= 1` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_configs_script` |  | script_id |  | 스크립트 삭제 가능 여부 검사 |

### 3.5 batch_runs (실행 이력)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.batch_runs` |
| 테이블명 | 실행 이력 |
| 주제 영역 | 실행 |
| 설명 | 배치의 실행 1회(Run). 접수 당시 설정과 executor 실행 결과 참조를 보존 |
| 삭제·보존 | 삭제 없음 (영구 보존) |
| 증가 요인 | 예약·수동 실행마다 1행 |
| 기본키 | id |
| 컬럼 수 | 36 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 실행 ID | UUID | N | PK |  | run_id |
| 2 | `batch_id` | 배치 ID | UUID | N | FK, UQ |  | 소속 배치 (참조: batch_configs.batch_id, batches.id) |
| 3 | `owner_id` | 소유자 ID | VARCHAR(255) | N |  |  | 배치 소유자(접수 시 복사) |
| 4 | `script_id` | 스크립트 ID | UUID | N | FK |  | 실행한 스크립트 (참조: script_versions.script_id) |
| 5 | `script_version_id` | 스크립트 버전 ID | UUID | N | FK |  | 실행한 버전 (참조: script_versions.id) |
| 6 | `config_number` | 설정 번호 | INTEGER | N | FK, UQ |  | 접수 당시 설정 번호 (참조: batch_configs.number) |
| 7 | `runtime` | 런타임 | JSONB | N |  |  | 접수 당시 런타임. {type, profile} |
| 8 | `snapshot` | 접수 스냅샷 | JSONB | N |  |  | 접수 시 고정값. {script_id, script_version_id, sha256, code_hash, schedule, policy, tenant_id, project_id} |
| 9 | `trigger` | 실행 계기 | VARCHAR(16) | N |  |  | SCHEDULED(예약), MANUAL(수동) |
| 10 | `scheduled_for` | 예약 회차 시각 | TIMESTAMPTZ | Y | UQ |  | 예약 실행의 회차 시각. 수동 실행은 NULL |
| 11 | `status` | 상태 | VARCHAR(32) | N |  |  | WAITING, QUEUED, RUNNING, COLLECTING, COMPLETED, FAILED, CANCELLED, SKIPPED |
| 12 | `skip_reason` | 생략 사유 | VARCHAR(64) | Y |  |  | OVERLAP, SKIPPED_CAPACITY, OWNER_DEACTIVATED, TENANT_REVOKED |
| 13 | `cancel_requested` | 취소 요청 여부 | BOOLEAN | N |  |  | 취소가 요청됐는지 |
| 14 | `cancel_source` | 취소 요청 주체 | VARCHAR(16) | Y |  |  | USER(소유자), POLICY(겹침 정책 REPLACE) |
| 15 | `cancel_reason` | 취소 사유 | VARCHAR(255) | Y |  |  | executor에 보낸 취소 사유 |
| 16 | `execution_id` | executor 실행 ID | UUID | Y | UQ |  | executor가 발급한 execution ID |
| 17 | `operation_id` | executor 작업 ID | UUID | Y |  |  | executor operation ID |
| 18 | `execution_status` | executor 상태 | VARCHAR(32) | Y |  |  | executor 원본 상태. 종료 값은 SUCCEEDED, FAILED, CANCELLED |
| 19 | `observed_version` | 관찰 상태 버전 | INTEGER | Y |  |  | 마지막으로 본 executor 상태 버전 |
| 20 | `failure_type` | 실패 유형 | VARCHAR(128) | Y |  |  | SUBMIT_REJECTED, ABANDONED, 또는 executor가 준 실패 사유 코드 |
| 21 | `notebook_status` | 결과 노트북 상태 | VARCHAR(32) | Y |  |  | 결과 노트북 projection 상태 |
| 22 | `artifacts` | 산출물 목록 | JSONB | N |  |  | executor artifact 목록(응답 그대로). 기본 [] |
| 23 | `attempts` | 시도 목록 | JSONB | N |  |  | executor 시도 목록(응답 그대로). 기본 [] |
| 24 | `retry_info` | 재시도 정보 | JSONB | Y |  |  | executor 재시도 정보(응답 그대로) |
| 25 | `last_error` | 마지막 오류 | VARCHAR(64) | Y |  |  | 진행 중 마지막 통신·계약 오류 코드 |
| 26 | `submitted_at` | 제출 일시 | TIMESTAMPTZ | Y |  |  | executor에 제출한 시각 |
| 27 | `started_at` | 실행 시작 일시 | TIMESTAMPTZ | Y |  |  | executor가 보고한 실행 시작 시각 |
| 28 | `finished_at` | 실행 종료 일시 | TIMESTAMPTZ | Y |  |  | executor가 보고한 실행 종료 시각 |
| 29 | `observed_at` | 마지막 관찰 일시 | TIMESTAMPTZ | Y |  |  | executor 상태를 마지막으로 확인한 시각 |
| 30 | `completed_at` | 완료 일시 | TIMESTAMPTZ | Y |  |  | batch에서 Run이 끝난 시각 |
| 31 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 32 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 33 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 34 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 35 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 36 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| FK | (자동 부여) | (batch_id) → batches (id) |
| FK | (자동 부여) | (batch_id, config_number) → batch_configs (batch_id, number) |
| FK | (자동 부여) | (script_id, script_version_id) → script_versions (script_id, id) |
| UQ | uq_run_occurrence | batch_id, config_number, scheduled_for |
| UQ | (자동 부여) | execution_id |
| CHECK | run_cancel_source | `cancel_source IS NULL OR cancel_source IN ('USER', 'POLICY')` |
| CHECK | run_status | `status IN ('WAITING', 'QUEUED', 'RUNNING', 'COLLECTING', 'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED')` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_runs_batch_created` |  | batch_id, created_at, id |  | 배치별 실행 목록(최신순 커서), 대기 Run 승격 순서 |
| `ix_runs_batch_status` |  | batch_id, status |  | 배치별 동시 실행 수 계산 |
| `ix_runs_script_status` |  | script_id, status |  | 스크립트 삭제 시 진행 중 Run 확인 |

### 3.6 run_step_bindings (실행 셀 단계)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.run_step_bindings` |
| 테이블명 | 실행 셀 단계 |
| 주제 영역 | 실행 |
| 설명 | Run 안의 코드 셀 1개와 executor step의 연결, 셀 결과 참조 |
| 삭제·보존 | 삭제 없음 (영구 보존) |
| 증가 요인 | Run당 코드 셀 수(최대 100) |
| 기본키 | run_id, sequence |
| 컬럼 수 | 7 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `run_id` | 실행 ID | UUID | N | PK, FK |  | 소속 Run (참조: batch_runs.id) |
| 2 | `sequence` | 실행 순서 | INTEGER | N | PK |  | Run 안 실행 순서(0부터) |
| 3 | `original_index` | 원본 셀 위치 | INTEGER | N |  |  | 원본 노트북에서의 셀 위치 |
| 4 | `source_sha256` | 셀 코드 해시 | VARCHAR(64) | N |  |  | 셀 코드의 SHA-256 |
| 5 | `step_id` | executor step ID | UUID | Y | UQ |  | executor step ID. 제출 성공 후 채움 |
| 6 | `status` | 셀 결과 상태 | VARCHAR(32) | Y |  |  | 셀 실행 결과 상태. 결과 수집 후 채움 |
| 7 | `result_ref` | 결과 참조 | JSONB | Y |  |  | executor 셀 결과 참조. 결과 수집 후 채움 |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | run_id, sequence |
| FK | (자동 부여) | (run_id) → batch_runs (id) |
| UQ | (자동 부여) | run_id, step_id |

인덱스는 기본키 인덱스만 있다.

### 3.7 skipped_windows (생략 회차 기록)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.skipped_windows` |
| 테이블명 | 생략 회차 기록 |
| 주제 영역 | 배치 |
| 설명 | 실행하지 않고 넘긴 예약 구간. 구간과 건수 한 행으로 압축 |
| 삭제·보존 | 삭제 없음 |
| 증가 요인 | 생략 발생 시 |
| 기본키 | id |
| 컬럼 수 | 13 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 생략 기록 ID | UUID | N | PK |  | 생략 기록 식별자 |
| 2 | `batch_id` | 배치 ID | UUID | N | FK |  | 소속 배치 (참조: batch_configs.batch_id, batches.id) |
| 3 | `config_number` | 설정 번호 | INTEGER | N | FK |  | 생략 당시 설정 번호 (참조: batch_configs.number) |
| 4 | `from_at` | 구간 시작 | TIMESTAMPTZ | N |  |  | 생략 구간 시작 시각 |
| 5 | `until_at` | 구간 끝 | TIMESTAMPTZ | N |  |  | 생략 구간 끝 시각(시작보다 뒤) |
| 6 | `occurrences` | 생략 회차 수 | BIGINT | N |  |  | 구간 안 생략 회차 수(1 이상) |
| 7 | `reason` | 생략 사유 | VARCHAR(64) | N |  |  | MISFIRE_SKIP, MISFIRE_LATEST, MISFIRE_CATCH_UP, REPLACED_BY_LATEST, SCHEDULER_ERROR, DISABLED, SCHEDULE_REPLACED, DELETED, OWNER_DEACTIVATED, TENANT_REVOKED |
| 8 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 레코드 생성 시각 |
| 9 | `created_by` | 생성자 | VARCHAR(255) | Y |  |  | 생성한 사용자(X-User-Id). 백그라운드 작업(스케줄러·dispatcher)이 만든 경우 NULL |
| 10 | `created_by_client` | 생성 클라이언트 | VARCHAR(64) | Y |  |  | 생성 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |
| 11 | `updated_at` | 수정 일시 | TIMESTAMPTZ | N |  |  | 레코드 내용이 마지막으로 바뀐 시각. 스케줄러가 다음 예약 시각을 옮기는 것은 수정으로 보지 않음 |
| 12 | `updated_by` | 수정자 | VARCHAR(255) | Y |  |  | 마지막으로 수정한 사용자(X-User-Id). 백그라운드 작업은 NULL |
| 13 | `updated_by_client` | 수정 클라이언트 | VARCHAR(64) | Y |  |  | 마지막 수정 요청에 쓰인 API key의 client 이름. 백그라운드 작업은 NULL |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| FK | (자동 부여) | (batch_id) → batches (id) |
| FK | (자동 부여) | (batch_id, config_number) → batch_configs (batch_id, number) |
| CHECK | skipped_count_positive | `occurrences > 0` |
| CHECK | skipped_window_order | `from_at < until_at` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_skipped_batch_created` |  | batch_id, created_at, id |  | 배치별 생략 기록 목록 |

### 3.8 tasks (내부 작업 큐)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.tasks` |
| 테이블명 | 내부 작업 큐 |
| 주제 영역 | 내부 처리 |
| 설명 | Run을 진행시키는 외부(executor) 호출 1건. 사용자에게 노출하지 않음 |
| 삭제·보존 | DONE 후 30일 뒤 삭제 |
| 증가 요인 | Run당 3개 이상(제출·상태 조회·결과 수집, 취소 시 추가) |
| 기본키 | id |
| 컬럼 수 | 12 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `id` | 작업 ID | UUID | N | PK |  | Task 식별자 |
| 2 | `kind` | 작업 종류 | VARCHAR(32) | N |  |  | SUBMIT_EXECUTION, SYNC_EXECUTION, COLLECT_RESULT, CANCEL_EXECUTION |
| 3 | `run_id` | 실행 ID | UUID | Y | FK |  | 대상 Run (참조: batch_runs.id) |
| 4 | `payload` | 부가 정보 | JSONB | N |  |  | interval(다음 조회 간격), uncertain(제출 결과 불확실), abandoned(포기 후 1회 취소) |
| 5 | `status` | 상태 | VARCHAR(16) | N |  |  | PENDING, DONE |
| 6 | `run_at` | 실행 예정 시각 | TIMESTAMPTZ | N |  |  | 이 시각 이후 dispatcher가 가져감 |
| 7 | `attempts` | 연속 시도 횟수 | INTEGER | N |  |  | 연속 시도 횟수. 성공 시 0 |
| 8 | `lease_token` | lease 토큰 | UUID | Y |  |  | 현재 처리 중인 worker의 토큰. 늦게 끝난 옛 worker의 반영을 차단 |
| 9 | `lease_until` | lease 만료 시각 | TIMESTAMPTZ | Y |  |  | 지나면 다른 인스턴스가 가져감 |
| 10 | `last_error` | 마지막 오류 | VARCHAR(64) | Y |  |  | 마지막 오류 코드 |
| 11 | `failing_since` | 연속 실패 시작 시각 | TIMESTAMPTZ | Y |  |  | 마지막 성공 이후 첫 실패 시각. 포기 판단 기준 |
| 12 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | Task 생성 시각 |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | id |
| FK | (자동 부여) | (run_id) → batch_runs (id) |
| CHECK | task_status | `status IN ('PENDING', 'DONE')` |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_tasks_done_created` |  | created_at | `status = 'DONE'` | 보존 기간이 지난 Task 삭제 |
| `ix_tasks_due` |  | run_at | `status = 'PENDING'` | dispatcher가 처리할 Task를 가져오는 조회 |
| `ix_tasks_run` |  | run_id |  | Run별 Task 조회 |

### 3.9 idempotency_records (멱등성 기록)

| 항목 | 내용 |
|---|---|
| 테이블 ID | `batch.idempotency_records` |
| 테이블명 | 멱등성 기록 |
| 주제 영역 | 내부 처리 |
| 설명 | 같은 Idempotency-Key로 재전송된 쓰기 요청에 첫 응답을 그대로 돌려주기 위한 기록 |
| 삭제·보존 | 생성 후 7일 뒤 삭제 |
| 증가 요인 | 쓰기 요청마다 |
| 기본키 | tenant_id, owner_id, operation, key |
| 컬럼 수 | 8 |

**컬럼**

| No | 컬럼 ID | 컬럼명 | 데이터 타입 | NULL | 키 | 기본값 | 설명 |
|---|---|---|---|---|---|---|---|
| 1 | `tenant_id` | 테넌트 ID | VARCHAR(64) | N | PK |  | 요청 tenant. 같은 key라도 tenant가 다르면 별개 |
| 2 | `owner_id` | 소유자 ID | VARCHAR(255) | N | PK |  | 요청 사용자 |
| 3 | `operation` | 작업 | VARCHAR(128) | N | PK |  | 요청 작업 이름 |
| 4 | `key` | 멱등성 키 | VARCHAR(255) | N | PK |  | Idempotency-Key 헤더 값 |
| 5 | `request_hash` | 요청 지문 | VARCHAR(64) | N |  |  | 요청 본문 해시. 같은 key에 본문이 다르면 409 |
| 6 | `response` | 첫 응답 본문 | JSONB | Y |  |  | 처음 처리한 응답 본문 |
| 7 | `status_code` | 첫 응답 코드 | INTEGER | Y |  |  | 처음 처리한 응답의 HTTP 상태 코드 |
| 8 | `created_at` | 생성 일시 | TIMESTAMPTZ | N |  |  | 기록 생성 시각 |

**제약 조건**

| 종류 | 제약명 | 내용 |
|---|---|---|
| PK | (자동 부여) | tenant_id, owner_id, operation, key |

**인덱스**

| 인덱스명 | 유일 | 컬럼 | 조건 (부분 인덱스) | 용도 |
|---|---|---|---|---|
| `ix_idempotency_created` |  | created_at |  | 보존 기간이 지난 멱등성 기록 삭제 |
