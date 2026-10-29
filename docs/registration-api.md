# 스크립트·배치 등록 API

스크립트 업로드·버전 관리와, 업로드한 스크립트 중 어떤 것을 언제·어떻게 돌릴지 정하는
배치 등록을 분리해 제공한다. 설계 근거는 [서비스 체계](domain-model.md)와 [목표 아키텍처](architecture.md)다.
새 배치는 비활성 상태로 생성한다.
수동 실행·Executor 제출·상태 확인은 [실행 API 안내](execution-api.md)를 따른다.
활성화·스케줄러는 [예약 실행 안내](scheduling-api.md)를 따른다.
보고서 생성은 아직 제공하지 않는다.
재시도 정책은 보류했으며 자동 재실행 로직이 없다.

## 로컬 실행

Python 3.12와 uv, PostgreSQL, 공유 PV가 필요하다. Redis는 사용하지 않는다.

```sh
uv sync --locked
cp .env.example .env
# .env의 DB URL, 공유 PV 절대경로, Executor 주소·key 수정
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:create_app --factory
```

`DATABASE_URL`은 `postgresql+psycopg://...` 형식이다.
migration은 자기 `batch` 스키마와 그 안의 Alembic 버전 테이블만 사용한다.
공유 DB의 다른 서비스 테이블을 변경하지 않는다. 스키마 생성 권한은 migration
실행 역할에 필요하며 일반 API 역할에는 해당 테이블의 접근 권한만 부여할 수 있다.
DB 초기화는 앱 기동과 분리한다. 되돌리기 명령은 등록 데이터를 삭제하므로
`alembic downgrade`는 보존할 데이터가 있는 환경에서 실행하지 않는다.

`STORAGE_ROOT`는 미리 생성·마운트한 절대경로다. 원본과 셀 파일은
`batch/inputs/tenants/<tenant>/users/<사용자 ID>/scripts/<id>/versions/<version>/` 아래 저장한다
(Executor workspace와 같은 `tenants/…/users/…` 구조).
tenant는 `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, 사용자 ID는 `X-User-Id`를 소문자로 바꾼 값이며 `^[a-z0-9][a-z0-9._-]{0,63}$`만
허용하고, 저장 직전에 다시 검사하므로 경로를 벗어날 수 없다.
사용자 파일명은 경로로 사용하지 않는다. 버전 디렉터리는 0750, 파일은 0640이며
Executor가 읽을 공유 그룹·UID/GID·마운트 권한을 운영 환경에서 맞춘다.
서비스는 자기 저장 영역에 쓰기 권한이 필요하다. 다른 사용자 코드가 이 등록
영역을 수정할 수 있도록 마운트하지 않는다.

파일을 임시 디렉터리에 작성한 뒤 원자적으로 이동하고 DB 트랜잭션을 커밋한다.
파일 작성 실패 시 DB 등록은 롤백된다. 이동 직후 DB 실패나 프로세스 중단이 발생하면
DB에서 참조하지 않는 버전 디렉터리가 남을 수 있다. API에는 노출되지 않으며 자동
삭제하지 않는다. 현재 자동 고아 파일 복구·정리 도구는 없고, 운영 시 DB의
`original_path`와 비교해 조사한다. 로컬 파일시스템 테스트가 실제 공유 PV의 장애
내구성을 보장하지는 않으므로 배포 전 동일 PV에서 확인한다.

`/healthz`는 프로세스 생존, `/readyz`는 DB migration 버전과 저장 루트 존재를
확인한다. `/docs`와 `/openapi.json`에서 현재 HTTP 명세를 볼 수 있다.

## 호출 서비스 인증 계약

헤더·key 발급·오류 대응은 [호출 서비스 인증 가이드](client-auth.md),
엔드포인트별 호출 예시는 [API별 호출 가이드](client-api.md)에 정리했다.

- 호출 서비스는 `X-Api-Key`와 `X-User-Id`를 보낸다. pr-batch는 key의 SHA-256만 설정 `AUTH_CLIENTS`에 둔다.
- 사용자 ID는 형식(UUID·사번 등)을 묻지 않고 소문자로 바꿔 저장·비교한다.
- 호출 서비스는 자기가 확인한 사용자로 헤더를 새로 만든다. 외부에서 받은 신원 헤더를 그대로 넘기지 않는다.
회사 표준 서비스 인증이 정해지면 `src/app/core/security.py` 경계에서 교체할 수 있다.

모든 쓰기는 `Idempotency-Key`를 요구한다. 동일 사용자·작업·키에 동일 내용이면
최초 상태 코드와 응답을 재전송한다. 내용이 다르면 409다. 네트워크 오류 후에는
같은 키·본문으로 재요청한다. 명령 재전송 응답은 최초 snapshot이므로 최신 상태는
상세 GET으로 읽는다. 멱등성 기록은 자동 삭제하지 않는다.

스크립트 이름 변경·삭제, 배치 수정·활성화·비활성화·삭제에는 상세 GET의 `ETag` 값을
`If-Match`에 넣는다. 현재 revision과 다르면 409다. 새 버전 업로드는 스크립트 행
잠금으로 번호를 할당하므로 If-Match 없이 사용할 수 있고, 실제 새 버전이 생성되면
스크립트 revision이 증가한다.

## API 목록

경로의 UUID는 해당 사용자에게 속해야 하며 다른 사용자 자원은 존재 여부를 드러내지
않는 404를 반환한다. 목록은 최신순, limit 기본 50·최대 100이며 응답은 `items`와
`next_cursor`를 갖는다. 다음 페이지는 `next_cursor`를 그대로 `cursor`로 보낸다(불투명 문자열,
잘못된 값은 422 `INVALID_CURSOR`). 생성(201) 응답은 `Location` 헤더를 준다.

### 스크립트 `/api/v1/scripts`

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | multipart `file`, `type`(필수), 선택 `name`. 201 |
| GET | / | 내 스크립트 목록 |
| GET | /{script_id} | 상세 + 최신 버전 요약, ETag |
| PATCH | /{script_id} | `{"name"}` 변경. If-Match |
| DELETE | /{script_id} | 삭제. If-Match, 204 ([삭제](#삭제)) |
| POST | /{script_id}/versions | 수정본 업로드. 새 버전 201, 같은 원본 200 |
| GET | /{script_id}/versions | 버전 목록 (번호 역순) |
| GET | /{script_id}/versions/{version_id} | 버전 상세 + 셀 목록 |
| GET | /{script_id}/versions/{version_id}/content | 해시를 검증한 원본 다운로드 |

`type`은 스크립트 종류이며 등록 시 고정된다. 이후 버전은 같은 타입으로 파싱되고, 맞지 않으면 422다.

| type | 파일 | 실행 단위 | 상태 |
| --- | --- | --- | --- |
| NOTEBOOK | `.ipynb` | 비어 있지 않은 코드 셀 하나 = Step 하나 | 지원 |

`name`을 생략하면 파일명에서 확장자를 뗀 값을 쓴다. 파일명은 표시용으로만
저장하고 저장 경로에는 쓰지 않는다. 응답(ScriptView)에는 `type`이 포함된다.

```sh
curl --fail-with-body http://127.0.0.1:8100/api/v1/scripts \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H 'Idempotency-Key: upload-daily-1' \
  -F 'file=@daily.ipynb' -F 'type=NOTEBOOK' -F 'name=생산라인 일일 분석'
```

응답의 `latest_version.cells`는 `{total, executable, skipped}`이고, 버전 상세의
`cell_list`는 원본 셀마다 `{index, type, executed, sequence}`를 준다.

### 배치 `/api/v1/batches`

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | JSON으로 비활성 등록. 201 |
| GET | / | 내 배치 목록. 선택 필터 `script_id` |
| GET | `/api/v1/runtimes` | 등록 가능한 런타임 `{type, profile}` 목록 (Executor BATCH 풀 기준) |
| GET | /{batch_id} | 상세, ETag |
| PATCH | /{batch_id} | 보낸 필드만 변경. If-Match |
| DELETE | /{batch_id} | 삭제. If-Match, 204 ([삭제](#삭제)) |
| POST | /{batch_id}/enable, /disable | [예약 안내](scheduling-api.md) |
| POST·GET | /{batch_id}/runs | [실행 안내](execution-api.md) |
| GET | /{batch_id}/skipped-windows | 생략된 예약 구간 |

등록 예시:

```json
{
  "name": "A라인 매일 8시",
  "script_id": "00000000-0000-0000-0000-000000000001",
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "schedule": {"definition": {"kind": "CRON", "expression": "0 8 * * *"}},
  "policy": {"overlap": {"mode": "QUEUE"}, "misfire": {"mode": "LATEST"}}
}
```

| 필드 | 필수 | 규칙 |
| --- | --- | --- |
| name | O | 1~255자 |
| script_id | O | 내 스크립트 |
| script_version_number | X | 버전 번호(v1=1, v2=2 …). 생략하면 등록 시점 최신 버전으로 고정(이후 새 버전을 올려도 따라가지 않음) |
| runtime | O | Executor 실행 요청의 `runtime`과 같은 `{type, profile}`. `GET /api/v1/runtimes` 목록 중 하나 |
| schedule | X | 없으면 수동 실행 전용. 활성화하려면 필요 |
| policy | X | 보낸 값만 적용하고 나머지는 기본값 |

tenant와 project는 본문이 아니라 헤더로 정한다(본문 `tenant_id`는 422). 스크립트와 배치는 모두 tenant + 사용자 소유이며,
배치는 같은 tenant·사용자의 스크립트만 쓸 수 있다(아니면 404).

| 값 | 출처 | 규칙 |
| --- | --- | --- |
| `tenant_id` | 중계자 key의 `X-Tenant-Id`(허용 목록 안), 없으면 key의 기본 tenant | Executor 규칙 `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, `dlq`·`work` 불가(위반 400). 배치 실행 이벤트는 `executor.events.{tenant_id}`로 간다 |
| `project_id` | `X-Project-Id` | 선택. `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 불가 |

응답(BatchView)은 등록 요청과 같은 모양에 `script_version_number`, `tenant_id`, `project_id`, `enabled`,
`disabled_reason`, `disabled_at`,
`next_run_at`, `preview_runs`, `config_number`, `revision`, 생성·수정 시각을 더한다.
응답을 고쳐 그대로 PATCH 본문으로 쓸 수 있다.

### 배치 수정 규칙

- 보낸 필드만 바뀐다. 빈 본문과 `name`·`script_id`·`runtime`·`policy`의
  null은 422다.
- `name`만 바꾸면 실행 설정 이력(`config_number`)은 늘지 않는다.
- `script_id`만 바꾸면 그 스크립트의 최신 버전으로, `script_version_number: null`이면
  현재 스크립트의 최신 버전으로 다시 고정한다.
- `schedule: null`은 일정 제거다. 활성 배치에서는 409 `SCHEDULE_REQUIRED`.
- `policy`는 보낸 하위 필드만 바뀌며, 결과 조합이 잘못되면 422 `INVALID_POLICY`.
- tenant와 project는 바꿀 수 없다. 수정 요청의 `X-Project-Id`는 무시한다. tenant는 소유 범위이므로 다른 tenant로
  부르면(relay key가 `X-Tenant-Id`를 뺀 경우 포함) 404다. 실행 이벤트 수신자도 바뀌지 않는다.
- 새 스크립트 버전을 올려도 기존 배치는 바뀌지 않는다. 이미 접수된 실행은 접수 당시
  설정을 유지한다.

### 런타임 (`runtime`)

배치가 어떤 런타임에서 돌지는 Executor 실행 요청의 `runtime` 객체와 같은 모양으로 지정한다.

```json
"runtime": {"type": "JUPYTER", "profile": "default"}
```

- 쓸 수 있는 조합은 `GET /api/v1/runtimes`가 돌려준다. pr-batch 설정 `BATCH_RUNTIMES`의 목록이다.
  Executor의 runtime 조회 API는 운영자(admin key) 전용이라 묻지 않는다. Executor BATCH 풀에 커널을 추가하면
  이 설정도 같이 고친다.
- 등록·런타임 변경·활성화 때 이 목록으로 검증한다. 없는 조합은 422 `UNKNOWN_RUNTIME`, 스크립트 타입이 돌 수 없는
  런타임은 422 `RUNTIME_NOT_SUPPORTED`(NOTEBOOK은 JUPYTER만), 목록이 비어 있으면 503
  `RUNTIMES_NOT_CONFIGURED`로 거절해 잘못된 런타임이 저장되지 않게 한다.
- 풀은 항상 BATCH라 지정하지 않는다. Executor에 런타임 종류나 옵션이 늘면 이 객체에 필드를 추가한다.

노트북 메타데이터에 Python 외 언어가 명시되면 거절한다. 코드 문법은 서버에서 실행·변환하지 않으며
IPython 문법도 원문으로 보관한다.

### 삭제

스크립트와 배치는 **soft delete**다. 요건상 원본·버전·실행 이력을 지우지 않으므로 행과 PV 파일은 그대로 두고
`deleted_at`·`deleted_by_type`·`deleted_by`만 기록한다. 삭제한 자원은 상세·목록·하위 경로(버전, 원본
다운로드, 실행, 생략 구간)가 모두 404이고, 새 배치가 가리킬 수도 없다. 되살리는 API는 없다.

| 대상 | 막히는 경우 (409) | 삭제 시 함께 일어나는 일 |
| --- | --- | --- |
| 배치 | `BATCH_HAS_ACTIVE_RUNS`: WAITING·QUEUED·RUNNING·COLLECTING 실행이 있음. 취소하거나 끝나길 기다린다 | 활성 배치는 비활성화된다. 이미 지난 미접수 예약은 `DELETED` 생략 구간으로 남는다 |
| 스크립트 | `SCRIPT_IN_USE`: 삭제되지 않은 배치의 현재 설정이 이 스크립트를 가리킴. 배치를 지우거나 다른 스크립트로 바꾼다 | 없음 |
| 스크립트 | `SCRIPT_HAS_ACTIVE_RUNS`: 이 스크립트 버전으로 진행 중인 실행이 있음(배치가 다른 스크립트로 옮겨간 뒤에도) | |

- 배치의 과거 설정 이력이나 삭제된 배치가 가리키던 것은 스크립트 삭제를 막지 않는다.
- 배치 삭제는 실행 접수와 같은 배치 행 잠금을, 스크립트 삭제는 배치 등록·수정이 거는 스크립트 공유 잠금을
  기다리므로 동시에 요청해도 "실행 중인 배치 삭제"나 "삭제된 스크립트를 가리키는 배치"가 생기지 않는다.
- 같은 `Idempotency-Key`로 재시도하면 다시 204, 다른 키로 다시 삭제하면 404다.

## 감사 필드 (모든 자원 응답 공통)

서비스 표준에 따라 Script, ScriptVersion, Batch, Run, SkippedWindow 응답은 모두 아래 여섯 필드를 갖는다.

| 필드 | 의미 |
| --- | --- |
| `created_at`, `updated_at` | 생성·마지막 수정 시각 (UTC) |
| `created_by_type`, `updated_by_type` | `USER`(로그인 사용자) 또는 `SYSTEM`(서비스 자체) |
| `created_by`, `updated_by` | USER면 `X-User-Id`(호출 서비스가 확인한 사용자 ID, 소문자), SYSTEM이면 `scheduler` 또는 `dispatcher` |

- Script·Batch처럼 사용자가 관리하는 자원은 항상 USER가 생성·수정한다. 스케줄러가 `next_run_at`을 옮기는 것은
  수정으로 보지 않아 `updated_*`가 바뀌지 않는다.
- Run은 수동 실행이면 USER, 예약 실행이면 `SYSTEM / scheduler`가 생성한다. 실행 진행(상태·결과 반영)은
  `SYSTEM / dispatcher`, 취소는 USER가 수정한 것으로 남는다. 새 정보가 없는 상태 조회는 수정이 아니다.
- 버전·설정 이력처럼 바뀌지 않는 자원은 `updated_*`가 생성값과 같다.

## NOTEBOOK 처리 규칙

동일한 원본 바이트를 다시 업로드하면 기존 버전을 반환한다. 파일명이 같더라도
바이트가 다르면 새 버전이다. 과거 출력만 바뀐 경우도 원본 버전은 구분하고,
code_hash로 실제 실행 코드가 같음을 확인할 수 있다.

- 원본은 과거 출력과 Markdown·raw 셀을 포함해 바이트 그대로 보관한다.
- 실행용 `.py`에는 비어 있지 않은 코드 셀만 저장한다. Markdown·raw 셀과 빈 코드
  셀은 실행에서만 제외한다.
- cell id 누락처럼 정규화가 필요한 노트북은 복사본을 정규화해 검증하고 받는다.
- 실행할 코드 셀이 없으면 422 `EMPTY_NOTEBOOK`.

## 일정과 정책

### 일정 (`schedule`)

| kind | 필드 | 예 |
| --- | --- | --- |
| ONCE | `at`(시간대 포함 시각) | 2026-11-01T09:00+09:00 한 번 |
| INTERVAL | `seconds`(60~31536000), `anchor`(선택) | 30분마다 |
| CALENDAR | `frequency` DAILY/WEEKLY/MONTHLY, `at`(시간대 없는 시각) | 평일 8시, 매월 말일, 매월 첫째 월요일 |
| CRON | `expression` 5필드 또는 단축어 | `*/10 9-17 * * MON-FRI`, `@daily` |

- **WEEKLY**: `weekdays`에 요일 이름 `MON`~`SUN`.
- **MONTHLY**: `month_days`(1~31, 그 날짜가 없는 달은 건너뜀), `last_day: true`(말일),
  `nth_weekdays: [{"weekday": "MON", "nth": 1}]`(n번째 요일, `nth`는 1~5 또는 -1=마지막) 중 하나 이상.
- **INTERVAL `anchor`**: "이 시각부터 N초마다"의 기준. 생략하면 `start_at`, 없으면 설정을 저장한 시각으로 채워 응답에 보여준다.
- **CRON**: 숫자·범위·목록·간격, 요일·월 이름(`MON`, `JAN`), 단축어 `@hourly`, `@daily`(=`@midnight`), `@weekly`,
  `@monthly`, `@yearly`(=`@annually`). 일·요일을 둘 다 제한하면 OR. 초 단위와 `L/W/#`는 받지 않는다(월말·n번째 요일은 CALENDAR).
- `timezone`은 Asia/Seoul만 허용한다. `start_at`/`end_at`은 선택이며 시작 포함·끝 미포함이다.

### 정책 (`policy`)

각 구역은 `mode`를 고르고, **그 모드가 쓰는 값만** 보낸다. 모드에 없는 값을 보내면 422다.
값을 생략하면 기본값이 채워져 응답에 보인다. 자세한 설명과 예시는 정책 안내 문서를 따른다.

```json
// overlap: 이전 실행이 아직 돌 때
{"mode": "SKIP"}                                               // 기본: 이번 건 생략
{"mode": "QUEUE", "max_queued": 100}                            // 순서대로 대기
{"mode": "PARALLEL", "max_concurrent": 3, "max_queued": 100}    // 동시 실행, 넘치면 대기
{"mode": "REPLACE"}                                            // 실행 중인 것 취소 후 새로

// misfire: 장애 등으로 grace_seconds 넘게 놓친 일정
{"mode": "SKIP", "grace_seconds": 300}                           // 기본: 버림
{"mode": "LATEST", "grace_seconds": 300}                         // 가장 최근 1회
{"mode": "CATCH_UP", "grace_seconds": 300, "max_runs": 10, "max_age_seconds": 86400}
```

| 값 | 쓰는 모드 | 범위 | 기본 |
| --- | --- | --- | --- |
| `max_concurrent` | PARALLEL (필수) | 1~100 | — |
| `max_queued` | QUEUE, PARALLEL | 1~10000 | 100 |
| `grace_seconds` | misfire 모든 모드 | 1~86400 | 300 (재배포·Pod 재시작 지연을 정상으로 보도록 5분) |
| `max_runs`, `max_age_seconds` | CATCH_UP | 1~1000, 60~31536000 | 10, 86400 |

- 수동 실행도 overlap 정책을 따른다.
- PATCH: 같은 모드면 보낸 값만 바뀌고, 다른 모드를 보내면 그 구역은 새 모드의 기본값에서 다시 시작한다.
  잘못된 조합은 422 `INVALID_POLICY`(어느 값이 문제인지 메시지에 포함).

## 검증과 오류

업로드 기본 10 MiB, 실행할 코드 셀 100개, 셀당 코드 1 MiB다. 설정으로 조정 가능하다.
HTTP 요청 전체는 업로드 상한 + 64 KiB를 넘으면 본문 파싱 전에 413이다.
chunked 요청도 실제 수신 바이트로 제한한다.

JSON 본문 오류는 422 `VALIDATION_ERROR`와 `errors[]`의 `loc`, `type`, `msg`를
준다. 사용자가 보낸 값은 되돌려주지 않는다. 400은 신원 헤더 오류, 401은 key 오류, 403은 허용되지 않은
tenant·admin 전용 API, 404는 다른 tenant·사용자의 자원이거나 삭제된 자원, 409는 멱등성·revision 충돌, `SCHEDULE_REQUIRED`와 삭제 차단 사유,
413은 크기 상한이다. DB·PV 사용 불가와 원본 해시 불일치는 503이며 내부 경로나
코드 본문을 반환하지 않는다.

실제 PostgreSQL 테스트는 별도의 삭제 가능한 DB에서 수행한다.

```sh
# pr_batch_test처럼 이름이 _test로 끝나는 별도 DB를 사용한다.
export BATCH_TEST_DATABASE_URL='postgresql+psycopg://USER:PASS@HOST/pr_batch_test'
make check
```

테스트는 batch 스키마에 migration을 적용하고 테스트마다 테이블을 비운다.
변수를 생략하면 PostgreSQL 테스트는 skip하고 입력·인증·저장소 단위 검증만 실행한다.

DB migration은 운영 전이라 `0001_initial` 하나로 합쳐 두었다. 이전 migration(0001~0004)을 적용한
로컬·개발 DB는 `batch` 스키마를 지운 뒤 다시 migration한다.
