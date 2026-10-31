# Batch API 변경 사항 (인증·tenant 업데이트)

적용 커밋: `2f93fda` (API key 인증), `b27d764` (Swagger 헤더 표시), `7f56e1d` (`X-Project-Id` 규칙),
`1ee668e` (tenant + 사용자 소유 범위)

엔드포인트별 상세는 [API별 호출 가이드](client-api.md), key 발급·설정은 [호출 서비스 인증 가이드](client-auth.md)를 본다.
로컬에서 `DOCS_ENABLED=true`로 띄운 서버의 `/docs`(Swagger)와 `/openapi.json`이 항상 최신 명세다.
공유 환경(dev·stg·prd)에서는 내부망 규정으로 꺼져 있다.

## 오류 응답 형식 변경 (Executor와 통일)

모든 오류 응답이 `{"error": {"code", "message", "details": [...]}}` 한 가지 모양으로 바뀌었다.
자세한 내용은 [API별 호출 가이드의 오류 형식](client-api.md#오류-형식).

| 이전 | 이후 |
|---|---|
| `body.code`, `body.message` | `body.error.code`, `body.error.message` |
| 422 `body.errors[].loc` / `type` / `msg` | `body.error.details[].location` / `type` / `message` |
| DB 오류 `body.detail` | `body.error.details[0].context` |
| 없는 경로 404, 405 `{"detail": "Not Found"}` | `error.code` `NOT_FOUND`, `METHOD_NOT_ALLOWED` |

`error.code` 값 자체는 바뀌지 않았다. 한도 초과, revision 충돌 등에는 `details[].context`에 한도와 실제 값이 붙는다.

## 한눈에 보기: 환경별 차이

| 항목 | local / dev | stg / prd |
|---|---|---|
| 인증 모드 | `AUTH_MODE: header` | `AUTH_MODE: api_key` (header 모드로는 기동하지 않음) |
| `X-Api-Key` | 없어도 됨. **보내면 검사하고, 틀리면 `401`** | 필수. 없거나 틀리면 `401` |
| tenant 결정 | `X-Tenant-Id` 값을 그대로 쓰고, 없으면 `dev` | key의 기본 tenant. relay key만 허용 목록 안에서 `X-Tenant-Id`로 지정할 수 있고, 허용 밖이면 `403` |
| 운영 API (`POST /batches:disable-by-owner`) | 누구나 호출 가능 | admin key만 호출 가능. 아니면 `403` |
| Base URL | 환경별 주소 (클러스터 안 `http://pr-batch:8000`, 로컬 `http://127.0.0.1:8100`) | 환경별 주소 |

key는 환경(dev·stg·prd)마다 따로 발급한다. 아래 1~6절의 규칙은 **모든 환경에 똑같이** 적용된다.

## 1. 신원 헤더

| 헤더 | 규칙 |
|---|---|
| `X-User-Id` | `^[a-z0-9][a-z0-9._-]{0,63}$`, 소문자로 바꿔 저장. `GET /runtimes`, `disable-by-owner` 외 **모든 요청(조회 포함)에 필수** |
| `X-Project-Id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$` (Executor와 같음), `unscoped`는 금지. 배치 등록 때만 저장 |
| `X-Tenant-Id` | `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, `dlq`·`work` 불가. 위 표의 환경별 규칙을 따름 |
| `X-Session-Id` | 보내도 무시한다. 배치는 session 단위가 아니다 |

형식이 틀린 헤더와 `X-User-Id` 누락은 `400 INVALID_IDENTITY`다. Body에 `tenant_id`를 보내면 `422`다.

## 2. 소유 범위: tenant + 사용자

- 스크립트, 버전, 배치, 실행, 생략 구간은 모두 **(tenant, 사용자)** 소유다. Executor의 tenant / user 체계와 같다.
  project는 소유 범위가 아니라 배치의 속성이다.
- 요청의 tenant(1절)와 `X-User-Id`가 자원의 것과 같아야 한다. 다르면 존재 여부를 숨기고 `404`, 목록은 빈 결과다.
  같은 `X-User-Id`라도 tenant가 다르면 별개의 사용자다.
- **relay key는 자원을 만들 때 보낸 `X-Tenant-Id`를 이후 모든 호출에 똑같이 보낸다.** 빼면 key의 기본 tenant가 되어
  그 자원이 `404`로 보인다.
- `Idempotency-Key`도 (tenant, 사용자)별로 기록된다. 다른 tenant에서 같은 키를 쓰면 새 요청이다.

## 3. 등록과 수정

- `POST /scripts`: 요청의 tenant·사용자 소유로 만든다.
- `POST /batches`: `script_id`는 같은 tenant·사용자의 스크립트여야 한다. 아니면 `404`.
  요청의 tenant가 배치의 `tenant_id`가 되고, 실행 이벤트를 받을 Executor tenant(`executor.events.{tenant_id}`)이기도 하다.
  `X-Project-Id`를 보내면 `project_id`로 저장한다.
- 배치의 `tenant_id`·`project_id`는 등록 후 바꿀 수 없다. `PATCH` 등에서 다른 tenant로 부르면 **`404`**다
  (이전의 `403 TENANT_MISMATCH`는 없어졌다). 수정 요청의 `X-Project-Id`는 무시한다.
- 응답 형식은 바뀌지 않았다. BatchView에 `tenant_id`, `project_id`가 그대로 있다.

## 4. 운영 API: `POST /api/v1/batches:disable-by-owner`

- 본문 `{"owner_id": "e12345", "reason": "USER_DEACTIVATED"}`, 응답 `{"disabled": n}`은 그대로다.
- **요청의 tenant 안의 배치에만** 적용한다. tenant는 admin key의 기본 tenant이고, relay admin key는 `X-Tenant-Id`로
  지정한다. 다른 tenant의 같은 사용자는 tenant마다 따로 부른다. `X-User-Id`는 필요 없다.

**stg/prd 예시**

```http
POST /api/v1/batches:disable-by-owner
X-Api-Key: <admin key>
X-Tenant-Id: agent3
Content-Type: application/json

{"owner_id": "e12345", "reason": "USER_DEACTIVATED"}
```

## 5. Executor 호출과 저장 경로

- pr-batch는 Executor에 항상 `X-Api-Key`(pr-batch의 Executor key), `X-Tenant-Id`(배치의 `tenant_id`),
  `X-User-Id`(배치 소유자)를 보낸다. 배치에 `project_id`가 있으면 `X-Project-Id`도 보낸다. tenant·project는 실행을 만들 때
  고정되므로 재제출·조회·취소도 같은 값이다.
- 결과 노트북은 pr-batch가 중계하지 않는다. 호출 서비스가 pr-batch로 실행을 조회해 소유를 확인한 뒤, **자기 Executor key**와
  같은 `X-User-Id`·`X-Tenant-Id`로 Executor `GET /api/v1/artifacts/{result.notebook_artifact_id}/content`를 호출한다.
- 스크립트 저장 경로: `batch/inputs/tenants/{tenant}/users/{user}/scripts/{script_id}/versions/{version}/`
  (Executor workspace와 같은 `tenants/…/users/…` 구조).

## 6. 에러 코드

| 코드 | HTTP | 경우 | 발생 환경 |
|---|---|---|---|
| `UNAUTHENTICATED` | 401 | API key가 없거나 틀림 (dev는 key를 보냈는데 틀린 경우만) | 모든 환경 |
| `FORBIDDEN` | 403 | 허용되지 않은 `X-Tenant-Id`, admin이 아닌 key의 운영 API | stg/prd |
| `INVALID_IDENTITY` | 400 | 헤더 형식이 틀림, `X-User-Id` 없음 | 모든 환경 |
| `NOT_FOUND` | 404 | 없는 자원, **다른 tenant·사용자의 자원**, 삭제된 자원, 다른 tenant의 스크립트로 배치 등록 | 모든 환경 |
| ~~`TENANT_MISMATCH`~~ | ~~403~~ | 삭제됨. 다른 tenant로 수정하면 `404` | — |

## 7. Swagger

로컬에서 `DOCS_ENABLED=true`로 띄운 서버의 `/docs`에서 **Authorize**에 key를 넣고, 각 API의 `X-User-Id`(필요하면 `X-Project-Id`, `X-Tenant-Id`) 칸을 채운다.
dev는 key 없이 `X-User-Id`만 채워도 된다. 공유 환경(dev·stg·prd)에서는 내부망 규정으로 `/docs`가 꺼져 있다.

## 8. 배포 시 주의

- **DB와 PV**: tenant 컬럼과 저장 경로가 바뀌었다. 기존 baseline migration을 고쳤으므로 dev, stg, prd 모두 pr-batch DB
  (`batch` 스키마)와 PV의 `batch/` 폴더를 리셋하고 배포한다. Executor도 같은 이유로 리셋하므로 함께 진행한다.
- **환경별 설정** (`config.{dev,stg,prd}.yml`): `DATABASE_URL` 비밀번호, `EXECUTOR_API_KEY`(그 환경 Executor가 발급),
  `AUTH_CLIENTS`의 key 해시를 실제 값으로 바꾼다. `EXECUTOR_BASE_URL`, `STORAGE_ROOT`가 환경마다 다르면 함께 바꾼다.
- **런타임**: `BATCH_RUNTIMES`는 그 환경 Executor BATCH 대상이 지원하는 profile과 같아야 한다(로컬은 `default`,
  `3102311`). 다르면 실행이 `422 UNSUPPORTED_RUNTIME_PROFILE`로 거절된다. 환경마다 다르면 env 파일로 옮긴다.
- **호출 서비스**: pr-agentic-service는 헤더를 보내도록 바꾸고, relay로 다른 tenant를 대신할 때는 같은 자원에 항상 같은
  `X-Tenant-Id`를 보낸다.
