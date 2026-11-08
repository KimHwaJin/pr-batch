# 00. 공통 규칙

모든 pr-batch API에 적용되는 주소, 인증, 헤더, 쓰기 규칙, 목록, 오류 형식이다. 엔드포인트별 문서는 이 규칙을
반복하지 않는다.

## 1. 서비스 개요

Jupyter 노트북을 올려 두고, 정해진 일정이나 수동 요청으로 Executor에서 실행하는 서비스다. 실행 자체는 Executor가
하고, pr-batch는 스크립트 보관, 일정 관리, 실행 접수와 추적을 맡는다.

| 개념 | 설명 |
| --- | --- |
| Tenant / User | 모든 자원의 주인. Executor와 같은 tenant / user 체계다. tenant는 key로 정해지고, user는 `X-User-Id`다 |
| Script | 업로드한 노트북. 이름과 버전 목록을 가진다. 현재 타입은 `NOTEBOOK` 하나다 |
| Version | 노트북 원본 한 벌. 바이트가 같으면 새로 만들지 않는다. 비어 있지 않은 코드 셀 하나가 실행 단위(step) 하나다 |
| Batch | "어떤 스크립트 버전을, 어떤 런타임에서, 언제, 어떤 정책으로" 돌릴지 정한 설정. 등록하면 비활성 상태다 |
| Run | 배치의 실행 한 번. 수동 실행이나 예약 시각에 만들어지고 Executor의 Execution 하나와 1:1이다 |
| Skipped window | 정책·중지 때문에 실행하지 않고 건너뛴 예약 구간 |

일반적인 흐름:

1. `GET /runtimes`로 고를 수 있는 런타임 확인
2. `POST /scripts`로 노트북 업로드
3. `POST /batches`로 배치 등록 (비활성 상태로 생성)
4. `POST /batches/{id}/runs`로 수동 실행하거나, `POST /batches/{id}/enable`로 예약 활성화
5. `GET /batches/{id}/runs/{run_id}`로 상태 확인, 끝나면 결과 노트북을 Executor에서 받음

## 2. 주소와 환경

| 환경 | Base URL | Swagger(`/docs`) |
| --- | --- | --- |
| 클러스터 안 (dev·stg·prd) | `http://pr-batch:8000` (환경별 주소는 배치팀에 확인) | 꺼짐 (내부망 규정, `DOCS_ENABLED: false`) |
| 로컬 Docker Compose | `http://127.0.0.1:8100` | 켜짐 (`/docs`, `/redoc`, `/openapi.json`) |

- 모든 공개 API는 `/api/v1` 아래에 있다.
- `/healthz`(프로세스 생존), `/readyz`(DB·저장소 준비)는 `/api/v1` 밖에 있고 인증 없이 부른다.
- 공유 환경에서는 서버의 OpenAPI가 꺼져 있으므로 [openapi.json](openapi.json)을 쓴다.
- **모든 환경에서 API key가 필요하다.** key 없이 신원 헤더만으로 부르는 방식은 없다.
- 로컬 Compose에는 로컬 전용 key `bat_local_agent_0123456789abcdef0123456789ab`가 등록돼 있다
  (tenant `dtest-agent-service`, relay, admin).

key 발급·교체·`AUTH_CLIENTS` 설정은 [호출 서비스 인증 가이드](../client-auth.md)를 본다.

## 3. 헤더

| 헤더 | 필수 | 형식 | 설명 |
| --- | --- | --- | --- |
| `X-Api-Key` | 모든 `/api/v1` 요청 | 1~512자 | 호출 서비스 key. 없거나 모르는 key는 401 |
| `X-User-Id` | `GET /runtimes`, `disable-by-owner`, `disable-by-tenant` 외 전부 | 소문자로 바꾼 뒤 `^[a-z0-9][a-z0-9._-]{0,63}$` | 대신하는 사용자(SSO 사번). `E12345`와 `e12345`는 같은 사용자 |
| `X-Project-Id` | 선택 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 불가 | 배치 등록 때만 저장한다. 다른 요청에서는 무시 |
| `X-Tenant-Id` | 선택 (`disable-by-tenant`는 필수) | `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, `dlq`·`work` 불가 | relay key만, 허용된 tenant 안에서 쓸 수 있다 |
| `X-Session-Id` | — | — | 보내도 무시한다 |
| `Idempotency-Key` | 운영 API를 뺀 모든 쓰기(POST·PATCH·DELETE) | 1~255자, 공백 없는 ASCII 출력 문자(`^[!-~]+$`) | 요청마다 새 값(UUID 권장). 재시도는 같은 값 |
| `If-Match` | 스크립트 이름 변경·삭제, 배치 수정·삭제·활성화·비활성화 | `"<양의 정수>"`, 예: `"3"` | 상세 GET 응답의 `ETag`를 따옴표까지 그대로 |

### 신원 규칙

- 헤더 값은 호출 서비스가 **자기가 확인한 사용자로 새로 만든다.** 브라우저나 상위 호출자가 보낸 값을 그대로
  넘기지 않는다. pr-batch는 key를 가진 호출 서비스가 보증한 사용자를 그대로 믿는다.
- **모든 자원(스크립트, 버전, 배치, 실행, 생략 구간)은 (tenant, user) 소유다.** 둘 다 같아야 보인다.
  같은 `X-User-Id`라도 tenant가 다르면 다른 사용자로 보고, 상세·수정·삭제·실행은 404, 목록은 빈 결과다.
- 요청의 tenant는 이렇게 정한다.
  - 일반 key: key에 등록된 기본 tenant. `X-Tenant-Id`를 보내면 403.
  - relay key: `X-Tenant-Id`(허용 목록 안, `allowed_tenants: ["*"]`면 형식에 맞는 모든 tenant), 없으면 key의 기본 tenant.
  - 본문으로는 보낼 수 없다(본문의 `tenant_id`는 모르는 필드라 422).
- relay key로 다른 tenant의 자원을 만들었다면, **그 자원을 조회·수정·실행할 때도 같은 `X-Tenant-Id`를 계속 보낸다.**
  빼면 기본 tenant로 보아 404가 난다.
- tenant는 배치 실행 이벤트를 받을 Executor 스트림(`executor.events.<tenant>`)이기도 하다.
- project는 소유 기준이 아니라 배치의 속성이다. 등록할 때 `X-Project-Id`로 정하고 바꿀 수 없다.

## 4. 쓰기 규칙

### Idempotency-Key

- 같은 tenant·같은 사용자·같은 작업·같은 키로 **같은 본문**을 다시 보내면 처리하지 않고 최초 응답(상태 코드 포함)을
  그대로 돌려준다.
- 네트워크 오류·타임아웃·503 뒤에는 **같은 키와 같은 본문**으로 재시도한다.
- 같은 키에 다른 본문을 보내면 409 `IDEMPOTENCY_CONFLICT`다. 새 작업에는 새 키를 쓴다.
- 재전송 응답은 최초 시점의 값이다. 최신 상태는 GET으로 읽는다.
- 키 기록은 7일(`IDEMPOTENCY_RETENTION_DAYS`) 보관된다.

### If-Match (낙관적 잠금)

- 상세 GET 응답과 모든 쓰기 응답에 `ETag: "<revision>"`이 붙는다. 본문의 `revision`과 같은 값이다.
- 자원의 `revision`이 그 사이 바뀌었으면 409 `REVISION_CONFLICT`다. 다시 GET해서 새 `ETag`로 보낸다.
- 형식이 틀리면(따옴표 누락 등) 422 `INVALID_REVISION`, 헤더가 없으면 422 `VALIDATION_ERROR`다.
- 연속으로 수정할 때는 직전 쓰기 응답의 `ETag`를 쓰면 된다.

### 생성 응답

생성(201) 응답에는 `Location: /api/v1/<collection>/<id>`와 `ETag`가 붙는다.

## 5. 목록 조회

- 쿼리 `limit`(1~100, 기본 50), `cursor`(최대 1024자).
- 응답은 `{"items": [...], "next_cursor": "..." | null}`. `next_cursor`가 `null`이면 마지막 페이지다.
- 다음 페이지는 받은 `next_cursor`를 그대로 `cursor`에 넣는다. 내용을 해석하거나 만들어 보내지 않는다
  (잘못된 값은 422 `INVALID_CURSOR`).
- 정렬은 최신순(생성 시각 역순)이다. 버전 목록은 버전 번호 역순이다.

## 6. 오류 형식

모든 오류는 Executor API와 같은 한 가지 모양이다. `details`는 비어 있을 수 있다.

```json
{"error": {"code": "REVISION_CONFLICT", "message": "Reload the resource.",
  "details": [{"location": ["header", "If-Match"], "type": "revision_conflict",
               "context": {"expected": 1, "current": 2}}]}}
```

- `error.code`: 기계가 읽는 오류 코드. **분기는 이 값으로 한다.** `message`는 바뀔 수 있다.
- `details[].location`: 어느 값인지 (`["body", "schedule", ...]`, `["body", "file", "cells"]` 등).
- `details[].type`: 왜인지 (`missing`, `notebook_cell_limit_exceeded` 등).
- `details[].context`: 근거 값(한도, 개수 등). 보낸 값(코드, key, 입력값)은 되돌려주지 않는다.
- 요청 형식 오류(422 `VALIDATION_ERROR`)는 항목마다 사람이 읽는 `message`도 붙는다.

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed.",
  "details": [{"location": ["body", "name"], "type": "missing", "context": {}, "message": "Field required"}]}}
```

### 공통 오류 코드

| HTTP | code | 의미 | 재시도 |
| --- | --- | --- | --- |
| 400 | `INVALID_IDENTITY` | `X-User-Id` 없음·형식 오류, `X-Project-Id`·`X-Tenant-Id` 형식 오류, `disable-by-tenant`의 `X-Tenant-Id` 없음 | 아니오 |
| 401 | `UNAUTHENTICATED` | key 없음, 등록되지 않은 key, 512자 초과 | 아니오 |
| 403 | `FORBIDDEN` | relay가 아닌 key의 `X-Tenant-Id`, 허용 목록 밖 tenant, admin이 아닌 key의 운영 API | 아니오 |
| 404 | `NOT_FOUND` | 없는 자원, 다른 tenant·사용자 자원, 삭제된 자원, 없는 경로 | 아니오 |
| 405 | `METHOD_NOT_ALLOWED` | 경로는 있으나 메서드가 다름 | 아니오 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 키에 다른 본문 | 새 키로 |
| 409 | `REVISION_CONFLICT` | `If-Match`가 현재 revision과 다름 | GET 후 새 `ETag`로 |
| 409 | 그 밖 (`SCRIPT_IN_USE` 등) | 지금 상태에서는 불가. 엔드포인트별 문서 참고 | 상태를 바꾼 뒤 |
| 413 | `REQUEST_TOO_LARGE`, `UPLOAD_TOO_LARGE` | 요청 본문·업로드 파일 크기 초과(기본 10 MiB) | 아니오 |
| 422 | `VALIDATION_ERROR` | 필드 누락·형식·범위 오류, 모르는 필드 | 아니오 |
| 422 | `INVALID_REVISION`, `INVALID_CURSOR` 등 | 헤더·쿼리 값 형식 오류 | 아니오 |
| 500 | `INTERNAL_ERROR` | DB 제약 위반 등 서버 쪽 오류 | 아니오. `details[0].context`(`error_id` 등)를 pr-batch 담당자에게 전달 |
| 503 | `DATABASE_UNAVAILABLE`, `STORAGE_UNAVAILABLE`, `INCONSISTENT_STATE` | 일시 장애 | 예, 같은 `Idempotency-Key`로 잠시 뒤 |
| 503 | `EXECUTOR_NOT_CONFIGURED`, `RUNTIMES_NOT_CONFIGURED` | 서버 설정 누락 | 설정이 고쳐진 뒤 |

DB 오류 응답(503 `DATABASE_UNAVAILABLE`, 500 `INTERNAL_ERROR`)은 `details`에 `location: ["database"]` 항목 하나가
붙는다(`context`: `error_id`, `type`, `driver_error`, `sqlstate`, `table`, `column`, `constraint` 중 있는 것).
`error_id`로 서버 로그를 찾는다. SQL 문이나 요청 값은 들어가지 않는다.

## 7. 공통 응답 필드

스크립트·버전·배치·실행·생략 구간 응답에는 감사 필드가 있다. 엔드포인트별 예시에서는 생략한다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `created_at`, `updated_at` | string (date-time) | UTC ISO 8601 |
| `created_by_type`, `updated_by_type` | `USER` \| `SYSTEM` | 사용자 요청이면 `USER`, 스케줄러·운영 API면 `SYSTEM` |
| `created_by`, `updated_by` | string | 사용자 ID 또는 시스템 이름(호출 서비스 이름 등) |

모든 ID는 UUID 문자열이다. 시각은 응답에서 모두 UTC다.

## 8. 헬스체크

| Method | 경로 | 응답 |
| --- | --- | --- |
| GET | `/healthz` | 200 `{"status": "ok"}`. 백그라운드 런타임이 멈췄으면 503 `{"status": "stalled"}` |
| GET | `/readyz` | 200 `{"status": "ready"}`. 기동·종료 중 503 `NOT_READY`, DB 스키마가 낮으면 503 `MIGRATION_REQUIRED`, 저장소 없음 503 `STORAGE_UNAVAILABLE` |

두 경로 모두 인증이 없고 OpenAPI에 나오지 않는다. Kubernetes probe용이다.
