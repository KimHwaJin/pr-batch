# 노트북 등록 API 실행과 연동

구현 범위는 사용자별 등록·목록·상세, 이름 변경, 원본 버전 업로드·다운로드,
커널·일정·실행 정책 저장이다. 새 등록은 비활성 상태로 생성한다.
수동 실행·Executor 제출·상태 확인은 [실행 API 안내](execution-api.md)를 따른다.
활성화·스케줄러는 [예약 실행 안내](scheduling-api.md)를 따른다.
보고서 생성은 아직 제공하지 않는다.
재시도 정책은 보류했으며 자동 재실행 로직이 없다.

## 로컬 실행

Python 3.12와 uv, PostgreSQL, 공유 PV가 필요하다. Redis는 사용하지 않는다.

```sh
uv sync --locked
cp .env.example .env
# .env의 DB URL, 공유 PV 절대경로, 인증 비밀키, 커널 목록 수정
uv run --locked alembic upgrade head
uv run --locked uvicorn pr_batch.app:create_app --factory
```

`BATCH_DATABASE_URL`은 `postgresql+psycopg://...` 형식이다.
migration은 자기 `batch` 스키마와 그 안의 Alembic 버전 테이블만 사용한다.
공유 DB의 다른 서비스 테이블을 변경하지 않는다. 스키마 생성 권한은 migration
실행 역할에 필요하며 일반 API 역할에는 해당 테이블의 접근 권한만 부여할 수 있다.
DB 초기화는 앱 기동과 분리한다. 되돌리기 명령은 등록 데이터를 삭제하므로
`alembic downgrade`는 보존할 데이터가 있는 환경에서 실행하지 않는다.

`BATCH_STORAGE_ROOT`는 미리 생성·마운트한 절대경로다. 원본과 셀 파일은
`batch-input/users/<owner-hash>/batches/<id>/versions/<version>/` 아래 저장한다.
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
| sub | 로그인 검증이 끝난 사용자 ID. 1~255자 |
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
회사 표준 서비스 인증이 정해지면 `auth.py` 경계에서 교체할 수 있다.

모든 쓰기는 `Idempotency-Key`를 요구한다. 동일 사용자·작업·키에 동일 내용이면
최초 상태 코드와 응답을 재전송한다. 내용이 다르면 409다. 네트워크 오류 후에는
같은 키·본문으로 재요청한다. 명령 재전송 응답은 최초 snapshot이므로 최신 상태는
상세 GET으로 읽는다. 멱등성 기록은 자동 삭제하지 않는다.

이름 및 일정 변경에는 상세 GET의 `ETag` 값을 `If-Match`에 넣는다.
현재 revision과 다르면 409다. 새 버전 업로드는 배치 행 잠금으로 번호를 할당하므로
If-Match 없이 사용할 수 있고, 실제 새 버전이 생성되면 revision이 증가한다.

## API 목록

공통 prefix는 `/internal/v1/batches`다. 경로의 UUID는 해당 사용자에게 속해야 하며
다른 사용자 자원은 존재 여부를 드러내지 않는 404를 반환한다.

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | 공통 prefix | multipart file + metadata로 비활성 등록. 201 |
| GET | 공통 prefix | 사용자별 목록, limit/cursor |
| GET | /{batch_id} | 현재 설정·선택 버전·revision과 ETag |
| PATCH | /{batch_id} | 이름 변경. Idempotency-Key와 If-Match 필수 |
| POST | /{batch_id}/versions | 새 원본 업로드. 새 버전 201, 같은 원본 200 |
| GET | /{batch_id}/versions | 버전 목록, limit/cursor |
| GET | /{batch_id}/versions/{version_id}/content | 해시를 검증한 등록 원본 다운로드 |
| PUT | /{batch_id}/schedule | 지정 버전·커널·일정의 불변 이력 추가 |

목록은 최신순으로 반환하고 limit은 기본 50, 최대 100이다. 응답은 `items`와
`next_cursor`를 갖는다. next_cursor가 null이면 마지막 페이지다.
원본 다운로드와 향후 Executor의 실행 결과 다운로드는 서로 다른 파일이다.

## 등록 예시

`metadata`는 아래 JSON을 문자열로 전달하는 multipart 필드다.
`file`은 `.ipynb` 파일이다. owner_id나 enabled를 넣으면 검증 오류다.

```json
{
  "name": "생산라인 일일 분석",
  "kernel_profile": "python3",
  "schedule": {
    "definition": {"kind": "CRON", "expression": "0 8 * * *"},
    "timezone": "Asia/Seoul",
    "overlap_policy": "QUEUE",
    "max_concurrent": 1,
    "max_pending": 100,
    "misfire_policy": "LATEST",
    "misfire_grace_seconds": 60,
    "catch_up_max_runs": 10,
    "catch_up_max_age_seconds": 86400
  }
}
```

이를 `registration.json`으로 저장한 호출 예시:

```sh
curl --fail-with-body http://127.0.0.1:8000/internal/v1/batches \
  -H "Authorization: Bearer $BATCH_SERVICE_TOKEN" \
  -H 'Idempotency-Key: register-line-1' \
  -F 'file=@analysis.ipynb' \
  -F 'metadata=<registration.json'
```

응답에는 id, name, enabled=false, revision, latest_version_number,
selected_schedule, created_at, updated_at이 있다. selected_schedule의
version_id로 처음 등록된 노트북 버전을 식별한다.

커널은 `BATCH_ALLOWED_KERNEL_PROFILES`의 이름과 일치해야 한다. 등록 시 Executor
상태를 조회하지 않으므로 허용 목록과 실제 BATCH 커널을 운영자가 맞춰야 한다.
노트북 메타데이터에 다른 언어가 명시되면 거절한다. 코드 문법은 서버에서 실행·변환하지
않으며 IPython 문법도 원문으로 보관한다. 실제 커널 호환성은 실행 연동 단계에서
검증한다. 외부 파일·데이터 접근 설정과 라이브러리는 업로드로 설치하지 않는다.

## 버전과 일정 변경

동일한 원본 바이트를 다시 업로드하면 기존 버전을 반환한다. 파일명이 같더라도
바이트가 다르면 새 버전이다. 과거 출력만 바뀐 경우도 원본 버전은 구분하고,
code_hash로 실제 실행 코드가 같음을 확인할 수 있다.
원본은 과거 출력을 포함한 그대로 보관하지만 실행용 `.py`에는 코드만 저장한다.
Markdown·raw 셀, 코드가 전혀 없는 파일, 정규화가 필요한 손상된 노트북은 거절한다.

새 버전을 올려도 현재 일정은 기존 버전에 고정된다. 아래 body로 schedule PUT을
호출해야 선택 버전이 바뀐다. 현재 Batch ETag를 If-Match로 전달한다.

```json
{
  "version_id": "00000000-0000-0000-0000-000000000001",
  "kernel_profile": "python3",
  "schedule": {
    "definition": {
      "kind": "CALENDAR",
      "frequency": "MONTHLY",
      "at": "08:00:00",
      "last_day": true
    }
  }
}
```

## 일정 저장 규칙

현재는 정의를 검증·저장하며 실행 예정 시각 계산과 실제 실행은 제공하지 않는다.

| kind | 필드와 규칙 |
| --- | --- |
| ONCE | at: 시간대가 포함된 시각 |
| INTERVAL | anchor: 시간대가 포함된 기준 시각, seconds: 60~31536000 |
| CALENDAR | frequency: DAILY/WEEKLY/MONTHLY, at: 시간대 없는 시각 |
| CRON | 숫자 기반 5필드 expression, 목록·범위·간격. 일/요일 OR 의미 |

WEEKLY는 weekdays가 필요하며 월요일=0, 일요일=6이다. MONTHLY는 month_days
(1~31) 또는 last_day=true를 받는다. 다른 주기에 적용되지 않는 필드는 거절한다.
Cron의 초 필드, 이름, L/W/# 확장은 받지 않고 존재하지 않는 날짜 조합도 거절한다.
시간대는 Asia/Seoul이다. start_at/end_at은 선택이며 시작 포함·끝 미포함이다.
ONCE는 기간 안에 있어야 한다. 비활성 초안에는 과거 시각을 저장할 수 있으며
활성화 기능에서 미래 시각을 검증할 예정이다.

겹침 옵션은 SKIP/QUEUE/PARALLEL, 누락 일정은 SKIP/LATEST/CATCH_UP이다.
생략 시 두 정책 모두 SKIP이며 응답에는 적용된 모든 기본값을 반환한다.
max_concurrent는 PARALLEL일 때만 1보다 크게 설정할 수 있다.
초기 상한은 동시 실행 100, 대기 10000, 보충 1000회이며 실행 처리량 보장이 아니다.
실제 예약 접수 한도는 [스케줄러 안내](scheduling-api.md)를 따른다.
운영 부하 검증에 따른 한도 조정은 별도다.

## 검증과 오류

업로드 기본 10 MiB, 코드 셀 수 100, 셀당 코드 1 MiB다. 설정으로 조정 가능하다.
HTTP 요청 전체는 업로드 상한 + 64 KiB를 넘으면 multipart 파싱 전에 413이다.
chunked 요청도 실제 수신 바이트로 제한한다. 요청 본문은 이 한도 안에서 메모리에
버퍼링하므로 운영 동시 요청 수와 프록시 제한을 함께 설정한다.

401은 서비스 토큰 오류, 403은 scope 오류, 404는 접근 불가능한 자원,
409는 멱등성·revision 충돌, 413은 크기 상한, 422는 입력 오류다.
DB·PV 사용 불가와 원본 해시 불일치는 503이며 내부 경로나 코드 본문을 반환하지 않는다.

실제 PostgreSQL 테스트는 별도의 삭제 가능한 DB에서 수행한다.

```sh
# pr_batch_test처럼 이름이 _test로 끝나는 별도 DB를 사용한다.
export BATCH_TEST_DATABASE_URL='postgresql+psycopg://USER:PASS@HOST/pr_batch_test'
make check
```

테스트는 batch 스키마에 migration을 적용하고 테스트마다 등록 테이블을 비운다.
변수를 생략하면 PostgreSQL 테스트는 skip하고 입력·인증·저장소 단위 검증만 실행한다.
CI에는 일회성 PostgreSQL을 연결해 통합 테스트 누락을 방지한다.

참고: [FastAPI 업로드](https://fastapi.tiangolo.com/tutorial/request-files/),
[SQLAlchemy PostgreSQL](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html),
[Alembic 스키마 필터](https://alembic.sqlalchemy.org/en/latest/autogenerate.html).
