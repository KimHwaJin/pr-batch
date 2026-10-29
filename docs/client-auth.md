# 호출 서비스 인증 가이드

pr-batch API를 호출하는 서비스(pr-agentic-service 등)가 요청에 넣을 헤더와 key 발급 방법이다.
내용은 `src/app/core/security.py`가 실제로 검사하는 항목만 적었다. API별 호출 방법은
[API별 호출 가이드](client-api.md)를 따른다. Executor도 같은 헤더 규칙을 쓴다.

## 요약

- 모든 `/api/v1/*` 요청에 `X-Api-Key`(호출 서비스 key)와 `X-User-Id`(대신하는 사용자)를 넣는다.
  `/healthz`, `/readyz`만 예외다. `GET /runtimes`와 `POST /batches:disable-by-owner`는 `X-User-Id`가 필요 없다.
- pr-batch는 key 원문을 갖지 않는다. 설정 `AUTH_CLIENTS`에 key의 **SHA-256 해시**만 두고 상수 시간으로 비교한다.
- tenant(Executor 이벤트를 받을 스트림)는 key로 정해진다. 본문에 `tenant_id`를 보내면 422다.
- 로컬과 dev는 `AUTH_MODE: header`로 key 없이 `X-User-Id`만 보내도 된다. stg·prd에서 이 모드면 기동하지 않는다.

## 헤더

| 헤더 | 필수 | 형식 | 설명 |
| --- | --- | --- | --- |
| `X-Api-Key` | stg·prd 필수 | 512자 이하 | 호출 서비스 key. 모르는 key는 401 |
| `X-User-Id` | 필수 (위 두 API 제외) | 소문자로 바꾼 뒤 `^[a-z0-9][a-z0-9._-]{0,63}$` | 대신하는 사용자. UUID·사번 등 형식은 묻지 않는다. `ABC`와 `abc`는 같은 사용자다 |
| `X-Project-Id` | 선택 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 불가 | 배치 등록 때만 저장한다. 수정 요청에서는 무시한다 |
| `X-Tenant-Id` | 선택 | Executor tenant 규칙 | 중계자(`relay`) key만, `allowed_tenants` 안의 값만 쓸 수 있다 |
| `X-Session-Id` | — | — | 보내도 무시한다 |

- 헤더 값은 호출 서비스가 **자기 세션에서 확인한 사용자로 새로 만든다.** 브라우저나 상위 호출자가 보낸 값을 그대로 넘기지 않는다.
- tenant는 등록할 때 정해지고 바꿀 수 없다. 수정 요청에서 `X-Tenant-Id`를 빼면 저장된 값을 그대로 쓰고,
  저장된 값과 다른 값을 보내면 403 `TENANT_MISMATCH`다.
- 다른 사용자의 자원은 404로 보인다. 엉뚱한 `X-User-Id`를 보내면 401이 아니라 404·빈 목록이 나온다.

## 설정 (`config.{APP_ENV}.yml`)

```yaml
AUTH_MODE: api_key            # local·dev는 header 가능
AUTH_CLIENTS:
  - name: pr-agentic-service  # 로그·감사(disabled_by 등)에 남는 이름
    key_sha256:               # 교체 기간에는 여러 개
      - "9f2c…"
    tenant: pr-agentic-service  # 기본 tenant
    relay: true               # X-Tenant-Id로 다른 tenant를 대신할 수 있음
    allowed_tenants: [pr-agentic-service, upper-agent]
    admin: true               # POST /batches:disable-by-owner 허용
```

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `name` | — | 호출자 이름. 목록 안에서 겹치면 기동 실패 |
| `key_sha256` | — | 64자리 소문자 hex 목록(1개 이상). 다른 호출자와 겹치면 기동 실패 |
| `tenant` | — | `X-Tenant-Id`가 없을 때 쓰는 tenant |
| `relay` | `false` | 중계자 여부. `allowed_tenants`는 `relay: true`일 때만 쓸 수 있다 |
| `allowed_tenants` | `[]` | relay key가 `X-Tenant-Id`로 쓸 수 있는 tenant. 자기 기본 tenant도 명시해 보내려면 목록에 넣는다. 비우면 `X-Tenant-Id`를 쓸 수 없다 |
| `admin` | `false` | 운영 API 허용 |

## key 발급과 교체

1. key를 만든다: `python -c "import secrets; print('bat_live_' + secrets.token_urlsafe(32))"`
2. key 원문은 **호출 서비스 레포의 설정**에만 둔다.
3. 해시를 구해 pr-batch `config.{env}.yml`의 `AUTH_CLIENTS`에 추가한다:
   `python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <key>`
4. 환경(dev·stg·prd)마다 다른 key를 쓴다.
5. 교체할 때는 새 해시를 `key_sha256`에 먼저 추가해 배포하고, 호출 서비스를 새 key로 바꾼 뒤 옛 해시를 지운다.

로그에는 key 원문이 남지 않는다. `bat_…`·`exe_…` 형태의 값은 로그에서 `[REDACTED]`로 가린다.

## 호출 예

```sh
curl --fail-with-body "$BATCH_URL/api/v1/batches" \
  -H "X-Api-Key: $BATCH_API_KEY" \
  -H "X-User-Id: e12345"
```

```python
import httpx

BATCH_API_KEY = "bat_live_…"  # 호출 서비스 설정에서 읽는다


def batch_headers(
    user_id: str, project_id: str | None = None
) -> dict[str, str]:
    headers = {"X-Api-Key": BATCH_API_KEY, "X-User-Id": user_id}
    if project_id:
        headers["X-Project-Id"] = project_id
    return headers


response = httpx.get(
    "http://pr-batch:8000/api/v1/batches", headers=batch_headers("e12345")
)
response.raise_for_status()
```

Swagger(`/docs`)에서는 **Authorize**에 key를 넣고, 각 API의 `X-User-Id` 칸을 채운다.

dev(`AUTH_MODE: header`)에서는 `X-Api-Key` 없이 `X-User-Id`만 보내도 된다. 이때 tenant는 `X-Tenant-Id`, 없으면 `dev`다.

## 사용자 비활성

퇴사 등으로 사용자가 비활성되면 admin key로 그 사용자의 배치 일정을 멈춘다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches:disable-by-owner" \
  -H "X-Api-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"owner_id": "e12345", "reason": "USER_DEACTIVATED"}'
# {"disabled": 3}
```

- 켜져 있던 배치를 끄고 `disabled_reason=USER_DEACTIVATED`를 남긴다. 다시 불러도 결과가 같다(`{"disabled": 0}`).
- 이미 Executor에 제출된 실행은 계속 돈다. 슬롯을 기다리던 run은 `SKIPPED`(`OWNER_DEACTIVATED`)가 된다.
- 이렇게 꺼진 배치는 수동 실행도 409 `OWNER_DEACTIVATED`다. 자동으로 되살리지 않으며, 사용자가 직접 켜면
  `disabled_reason`이 지워진다.

## 인증 오류

| HTTP | 코드 | 원인 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | `api_key` 모드에서 key 없음, 등록되지 않은 key, 512자 초과 |
| 400 | `INVALID_IDENTITY` | `X-User-Id` 없음·형식 오류, `X-Project-Id`·`X-Tenant-Id` 형식 오류 |
| 403 | `FORBIDDEN` | relay가 아닌 key의 `X-Tenant-Id`, `allowed_tenants` 밖의 tenant, admin이 아닌 key의 운영 API |
| 403 | `TENANT_MISMATCH` | 저장된 tenant와 다른 `X-Tenant-Id`로 배치 수정 |
| 422 | `VALIDATION_ERROR` | 본문에 `tenant_id` 등 모르는 필드 |

## 알고 가는 위험

- key 원문이 호출 서비스 레포에 있으므로, 그 레포를 읽을 수 있는 사람은 아무 사용자로든 배치를 등록할 수 있다.
  레포 접근 권한을 필요한 사람으로 좁힌다.
- `AUTH_MODE: header`는 신원을 확인하지 않는다. local·dev 외에는 켜지지 않게 막혀 있다.
