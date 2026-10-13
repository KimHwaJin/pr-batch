# 스크립트·배치 등록 API

스크립트 업로드·버전 관리와, 업로드한 스크립트 중 어떤 것을 언제·어떻게 돌릴지 정하는
배치 등록을 분리해 제공한다. 설계 근거는 [스크립트·배치 분리 설계](notebook-batch-design.md)와 [서비스 체계](domain-model.md)다.
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
# .env의 DB URL, 공유 PV 절대경로, 인증 비밀키, Executor 주소 수정
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:create_app --factory
```

`BATCH_DATABASE_URL`은 `postgresql+psycopg://...` 형식이다.
migration은 자기 `batch` 스키마와 그 안의 Alembic 버전 테이블만 사용한다.
공유 DB의 다른 서비스 테이블을 변경하지 않는다. 스키마 생성 권한은 migration
실행 역할에 필요하며 일반 API 역할에는 해당 테이블의 접근 권한만 부여할 수 있다.
DB 초기화는 앱 기동과 분리한다. 되돌리기 명령은 등록 데이터를 삭제하므로
`alembic downgrade`는 보존할 데이터가 있는 환경에서 실행하지 않는다.

`BATCH_STORAGE_ROOT`는 미리 생성·마운트한 절대경로다. 원본과 셀 파일은
`batch-input/users/<사용자 UUID>/scripts/<id>/versions/<version>/` 아래 저장한다.
사용자 UUID는 인증 토큰의 `sub`를 표준 형식(소문자, 하이픈)으로 바꾼 값이다.
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

## prj_re 인증 계약

이번 구현은 내부 호출용 HS256 JWT를 사용한다. 기존 `prj_re`에 자동 연결된 것이
아니며 gateway에서 아래 토큰을 발급·전달하는 연동이 필요하다.

| JWT claim | 값 |
| --- | --- |
| sub | 로그인 검증이 끝난 사용자 UUID (prj_re 사용자 ID). UUID가 아니면 401 |
| iss | 기본 `prj_re`, `BATCH_AUTH_ISSUER`로 설정 |
| aud | 기본 `pr-batch`, `BATCH_AUTH_AUDIENCE`로 설정 |
| iat | 발급 UTC Unix 초, 정수 |
| exp | 만료 UTC Unix 초, 정수. iat 이후 최대 300초 |
| scope | `batch:manage` |

`Authorization: Bearer <token>`으로 전달한다. 공유 비밀키
`BATCH_AUTH_SECRET`는 32바이트 이상 무작위 값으로 설정하고 사용자에게 전달하지
않는다. 요청 body의 사용자 ID나 `X-User-Id`는 인증 근거가 아니다.
외부 클라이언트가 서비스 인증 헤더를 그대로 전달하게 하지 않고 `prj_re`가 검증한
사용자로 새 토큰을 발급한다. 운영에서는 TLS와 내부 서비스 접근 제한을 적용한다.
회사 표준 서비스 인증이 정해지면 `src/app/core/security.py` 경계에서 교체할 수 있다.

모든 쓰기는 `Idempotency-Key`를 요구한다. 동일 사용자·작업·키에 동일 내용이면
최초 상태 코드와 응답을 재전송한다. 내용이 다르면 409다. 네트워크 오류 후에는
같은 키·본문으로 재요청한다. 명령 재전송 응답은 최초 snapshot이므로 최신 상태는
상세 GET으로 읽는다. 멱등성 기록은 자동 삭제하지 않는다.

스크립트 이름 변경, 배치 수정·활성화·비활성화에는 상세 GET의 `ETag` 값을
`If-Match`에 넣는다. 현재 revision과 다르면 409다. 새 버전 업로드는 스크립트 행
잠금으로 번호를 할당하므로 If-Match 없이 사용할 수 있고, 실제 새 버전이 생성되면
스크립트 revision이 증가한다.

## API 목록

경로의 UUID는 해당 사용자에게 속해야 하며 다른 사용자 자원은 존재 여부를 드러내지
않는 404를 반환한다. 목록은 최신순, limit 기본 50·최대 100이며 응답은 `items`와
`next_cursor`를 갖는다. 생성(201) 응답은 `Location` 헤더를 준다.

### 스크립트 `/api/v1/scripts`

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | / | multipart `file`, `type`(필수), 선택 `name`. 201 |
| GET | / | 내 스크립트 목록 |
| GET | /{script_id} | 상세 + 최신 버전 요약, ETag |
| PATCH | /{script_id} | `{"name"}` 변경. If-Match |
| POST | /{script_id}/versions | 수정본 업로드. 새 버전 201, 같은 원본 200 |
| GET | /{script_id}/versions | 버전 목록 (번호 역순, 번호 cursor) |
| GET | /{script_id}/versions/{version_id} | 버전 상세 + 셀 목록 |
| GET | /{script_id}/versions/{version_id}/content | 해시를 검증한 원본 다운로드 |

`type`은 스크립트 종류이며 등록 시 고정된다. 이후 버전은 같은 타입으로 파싱되고, 맞지 않으면 422다.

| type | 파일 | 실행 단위 | 상태 |
| --- | --- | --- | --- |
| NOTEBOOK | `.ipynb` | 비어 있지 않은 코드 셀 하나 = Step 하나 | 지원 |
| PYTHON | `.py` | 파일 전체 = Step 하나 | 예약. 422 `UNSUPPORTED_SCRIPT_TYPE` |

`name`을 생략하면 파일명에서 확장자를 뗀 값을 쓴다. 파일명은 표시용으로만
저장하고 저장 경로에는 쓰지 않는다. 응답(ScriptView)에는 `type`이 포함된다.

```sh
curl --fail-with-body http://127.0.0.1:8100/api/v1/scripts \
  -H "Authorization: Bearer $BATCH_SERVICE_TOKEN" \
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
  "policy": {"overlap": "QUEUE", "misfire": "LATEST"}
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

응답(BatchView)은 등록 요청과 같은 모양에 `script_version_number`, `enabled`,
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
- 새 스크립트 버전을 올려도 기존 배치는 바뀌지 않는다. 이미 접수된 실행은 접수 당시
  설정을 유지한다.

### 런타임 (`runtime`)

배치가 어떤 런타임에서 돌지는 Executor 실행 요청의 `runtime` 객체와 같은 모양으로 지정한다.

```json
"runtime": {"type": "JUPYTER", "profile": "default"}
```

- 쓸 수 있는 조합은 `GET /api/v1/runtimes`가 돌려준다. Executor `GET /api/v1/runtime-targets`에서 BATCH 풀의
  활성(enabled) 대상들이 지원하는 프로필을 모은 것이며 `BATCH_RUNTIME_CATALOG_TTL_SECONDS`(기본 30초) 동안 캐시한다.
  Executor에 커널이 추가되면 pr-batch 설정 변경 없이 바로 쓸 수 있다.
- 등록·런타임 변경·활성화 때 이 목록으로 검증한다. 없는 조합은 422 `UNKNOWN_RUNTIME`, 스크립트 타입이 돌 수 없는
  런타임은 422 `RUNTIME_NOT_SUPPORTED`(NOTEBOOK은 JUPYTER만), Executor를 조회할 수 없으면 503
  `RUNTIME_CATALOG_UNAVAILABLE`로 거절해 잘못된 런타임이 저장되지 않게 한다.
- 풀은 항상 BATCH라 지정하지 않는다. Executor에 런타임 종류나 옵션이 늘면 이 객체에 필드를 추가한다.

노트북 메타데이터에 Python 외 언어가 명시되면 거절한다. 코드 문법은 서버에서 실행·변환하지 않으며
IPython 문법도 원문으로 보관한다.

## 감사 필드 (모든 자원 응답 공통)

서비스 표준에 따라 Script, ScriptVersion, Batch, Run, Report, SkippedWindow 응답은 모두 아래 여섯 필드를 갖는다.

| 필드 | 의미 |
| --- | --- |
| `created_at`, `updated_at` | 생성·마지막 수정 시각 (UTC) |
| `created_by_type`, `updated_by_type` | `USER`(로그인 사용자) 또는 `SYSTEM`(서비스 자체) |
| `created_by`, `updated_by` | USER면 JWT `sub`(prj_re가 SSO로 확인한 사용자 ID), SYSTEM이면 `scheduler` 또는 `dispatcher` |

- Script·Batch처럼 사용자가 관리하는 자원은 항상 USER가 생성·수정한다. 스케줄러가 `next_run_at`을 옮기는 것은
  수정으로 보지 않아 `updated_*`가 바뀌지 않는다.
- Run은 수동 실행이면 USER, 예약 실행이면 `SYSTEM / scheduler`가 생성한다. 실행 진행(상태·결과 반영)은
  `SYSTEM / dispatcher`, 취소·리포트 재시도는 USER가 수정한 것으로 남는다. 새 정보가 없는 상태 조회는 수정이 아니다.
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

```json
"policy": {
  "overlap": {"mode": "QUEUE", "max_concurrent": 1, "max_queued": 100},
  "misfire": {"mode": "CATCH_UP", "grace_seconds": 60, "max_runs": 10, "max_age_seconds": 86400}
}
```

| 구역 | mode | 함께 쓰는 값 | 기본 |
| --- | --- | --- | --- |
| overlap (이전 실행이 아직 돌 때) | `SKIP` 건너뜀 / `QUEUE` 순서대로 대기 / `PARALLEL` 동시 실행 / `REPLACE` 실행 중인 것 취소 후 새로 실행 | `max_concurrent` 1~100(PARALLEL만 1 초과), `max_queued` 1~10000 | SKIP, 1, 100 |
| misfire (장애 등으로 놓친 일정) | `SKIP` 버림 / `LATEST` 가장 최근 1회 / `CATCH_UP` 보충 | `grace_seconds` 1~86400, `max_runs` 1~1000, `max_age_seconds` 60~31536000 | SKIP, 60, 10, 86400 |

- 수동 실행도 overlap 정책을 따른다. REPLACE에서는 대기 중인 실행은 취소되고, 실행 중인 것은 Executor에 취소를
  요청한 뒤 실제로 끝나면 새 실행이 시작된다.
- PATCH의 `policy`는 `overlap`·`misfire` 안에서 보낸 값만 바뀌며, 결과 조합이 잘못되면 422 `INVALID_POLICY`.

## 검증과 오류

업로드 기본 10 MiB, 실행할 코드 셀 100개, 셀당 코드 1 MiB다. 설정으로 조정 가능하다.
HTTP 요청 전체는 업로드 상한 + 64 KiB를 넘으면 본문 파싱 전에 413이다.
chunked 요청도 실제 수신 바이트로 제한한다.

JSON 본문 오류는 422 `VALIDATION_ERROR`와 `errors[]`의 `loc`, `type`, `msg`를
준다. 사용자가 보낸 값은 되돌려주지 않는다. 401은 서비스 토큰 오류, 403은 scope 오류,
404는 접근 불가능한 자원, 409는 멱등성·revision 충돌과 `SCHEDULE_REQUIRED`,
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

DB migration은 출시 전 정리로 `0001_baseline` 하나다. 이전 0001~0003을 적용한
로컬 DB는 `batch` 스키마를 지운 뒤 다시 migration한다.
