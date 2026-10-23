# 호출 서비스 인증 가이드

pr-batch API를 호출하는 서비스(prj_re gateway 등)가 요청 헤더에 넣을 토큰을 만드는 방법이다.
내용은 `src/app/core/security.py`가 실제로 검증하는 항목만 적었다. API별 호출 방법은
[API별 호출 가이드](client-api.md)를 따른다.

## 요약

- 모든 `/api/v1/*` 요청에 `Authorization: Bearer <JWT>` 헤더를 넣는다. `/healthz`, `/readyz`만 예외다.
- JWT는 **RS256**으로, 배치팀에서 받은 **개인키 파일(`auth_private.pem`)로 서명**한다.
  pr-batch는 짝이 되는 **공개키 파일**로 검증만 하므로 pr-batch 쪽에는 토큰을 만들 수 있는 비밀이 없다.
- 필수 claim은 `sub`(사용자 UUID), `iss`, `aud`, `iat`, `exp`, `scope=batch:manage`다.
- 유효 기간은 `exp - iat`가 **1~300초**(기본값)여야 한다. 요청마다, 또는 만료 전에 새로 발급한다.
- 토큰의 `sub`가 그 요청의 사용자(자원 소유자)다. 사용자마다 따로 발급한다.

## 키 구조

```
배치팀: scripts/auth_keys.py 로 환경마다 키 쌍 생성
   ├─ auth_private.pem ──(안전한 경로로 전달)──▶ 호출 서비스 A, B, C … (모두 같은 파일로 서명)
   └─ auth_public.pem  ──(keys/ 에 커밋, 이미지에 포함)──▶ pr-batch (검증)
```

- 호출 서비스들은 **같은 개인키 파일 하나**를 쓴다. 서비스가 늘어도 pr-batch 설정은 바뀌지 않는다.
- 개인키는 서명에만 쓰는 비밀이다. 호출 서비스 서버 안에만 두고 git·이미지·로그·사용자 단말에 남기지 않는다.
- 공개키는 비밀이 아니다. pr-batch 레포 `keys/`에 커밋하고 설정 `AUTH_PUBLIC_KEY_FILE`로 가리킨다
  ([keys/README.md](../keys/README.md)).
- 개인키가 유출되면 키 쌍을 새로 만들어 모든 호출 서비스의 개인키와 pr-batch의 공개키를 함께 바꾼다.
  여러 서비스가 한 개인키를 공유하므로 한 곳의 유출이 전체 교체로 이어진다는 점을 감안해 보관한다.

### 키 쌍 만들기

배치팀이 환경마다 한 번 만든다. 도커 없이 macOS·Linux·Windows 어디서나 만들 수 있다.

```sh
uv run --locked python scripts/auth_keys.py <출력 폴더>      # macOS·Linux
uv run --locked python scripts\auth_keys.py C:\batch-keys\prd # Windows PowerShell
```

레포 없이 Python만 있거나 openssl(Git Bash 포함)로 만드는 방법, 공개키 배치와 키 교체 절차는
[keys/README.md](../keys/README.md)를 따른다.

## 배치팀에서 받아야 하는 값

| 값 | pr-batch 설정 키 | 기본값 | 비고 |
| --- | --- | --- | --- |
| 서명 개인키 파일 | (pr-batch는 짝이 되는 `AUTH_PUBLIC_KEY_FILE`만 가짐) | — | `auth_private.pem`, RSA 2048비트 이상, PEM. 환경(dev/stg/prd)마다 다르다 |
| issuer | `AUTH_ISSUER` | `batch-client` | 토큰 `iss`와 정확히 같아야 한다. 모든 호출 서비스가 같은 값을 쓴다 |
| audience | `AUTH_AUDIENCE` | `batch-service` | 토큰 `aud`와 같아야 한다 |
| 최대 유효 기간 | `AUTH_MAX_LIFETIME_SECONDS` | `300` | `exp - iat` 상한. 설정 가능 범위 30~3600초 |
| 서비스 주소 | — | 클러스터 안 `http://pr-batch:8000` | 배포 환경에 맞는 주소를 확인한다 |

pr-batch는 이 값을 `config.yml`과 `config.{APP_ENV}.yml`(키 대소문자 무관)에서 읽고, 같은 이름의
환경변수(예: `AUTH_ISSUER`)가 있으면 그 값이 우선한다.

## 헤더

```
Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOi...
```

- 스킴은 `Bearer`(대소문자 무관), 공백 하나 뒤에 토큰을 붙인다.
- 토큰 길이는 4096자 이하여야 한다.
- 요청 본문의 사용자 ID나 `X-User-Id` 같은 헤더는 인증에 쓰지 않는다. 사용자는 오직 토큰 `sub`로 정한다.

## JWT 규격

헤더: `{"alg": "RS256", "typ": "JWT"}`. 다른 알고리즘(HS256, RS512, ES256, none 등)은 401이다. `kid`는 보지 않는다.

| claim | 필수 | 값 | 검증 내용 |
| --- | --- | --- | --- |
| `sub` | O | 로그인 검증이 끝난 사용자 UUID 문자열 | UUID가 아니면 401. 대소문자는 구분하지 않으며 소문자 표준형으로 저장한다 |
| `iss` | O | 배치팀이 알려준 issuer (기본 `batch-client`) | 정확히 일치해야 한다(대소문자·공백 포함) |
| `aud` | O | 배치팀이 알려준 audience (기본 `batch-service`) | 문자열, 또는 이 값을 포함한 배열 |
| `iat` | O | 발급 시각, UTC Unix 초, **정수** | 실수(1734567890.5)나 문자열이면 401. 서버 시각보다 미래면 401 |
| `exp` | O | 만료 시각, UTC Unix 초, **정수** | 지났으면 401. `0 < exp - iat <= 300` 이어야 한다 |
| `scope` | O | 정확히 문자열 `batch:manage` | 없거나 다르면 **403**. 공백으로 여러 scope를 이어 붙인 값(`"batch:manage other"`)이나 배열도 403 |
| `nbf` | X | 넣는다면 발급 시각 이하 | 넣으면 검증한다. 미래면 401 |

그 밖의 claim(`jti`, 이름 등)은 넣어도 무시한다.

### 시계 오차

서버는 시각 오차 허용(leeway)을 두지 않는다. 호출 서비스의 시계가 pr-batch보다 몇 초라도 빠르면 `iat`가
미래로 보여 401이 난다. 서버 시각을 NTP로 맞추고, 필요하면 `iat`를 현재보다 몇 초 앞당겨 발급한다.
이때도 `exp - iat`는 300초 이하로 유지한다(예: `iat = now - 5`, `exp = iat + 300`).

## 토큰 만들기

### Python (PyJWT)

```python
import time
import uuid
from pathlib import Path

import jwt  # pip install "pyjwt[crypto]>=2.8"

# 배치팀에서 받은 개인키 파일. 프로세스 시작 시 한 번 읽어 둔다.
PRIVATE_KEY = Path("/secure/path/auth_private.pem").read_bytes()
BATCH_AUTH_ISSUER = "batch-client"  # 배치팀에서 받은 issuer
BATCH_AUTH_AUDIENCE = "batch-service"  # 배치팀에서 받은 audience


def batch_token(user_id: str, ttl_seconds: int = 300) -> str:
    """로그인 검증이 끝난 사용자(user_id)로 pr-batch 호출용 JWT를 만든다."""
    now = int(time.time())
    claims = {
        "sub": str(uuid.UUID(user_id)),  # 사용자 UUID
        "iss": BATCH_AUTH_ISSUER,
        "aud": BATCH_AUTH_AUDIENCE,
        "scope": "batch:manage",
        "iat": now,
        "exp": now + ttl_seconds,  # iat 이후 1초 ~ 300초
    }
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")
```

호출 예:

```python
import httpx

token = batch_token("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
response = httpx.get(
    "http://pr-batch:8000/api/v1/batches",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
```

### Java (jjwt 0.12)

```java
// auth_private.pem(PKCS#8 PEM)을 읽어 PrivateKey로 변환
String pem = Files.readString(Path.of("/secure/path/auth_private.pem"))
    .replaceAll("-----(BEGIN|END) PRIVATE KEY-----", "")
    .replaceAll("\\s", "");
PrivateKey key = KeyFactory.getInstance("RSA")
    .generatePrivate(new PKCS8EncodedKeySpec(Base64.getDecoder().decode(pem)));

long now = Instant.now().getEpochSecond();
String token = Jwts.builder()
    .subject(userId)                    // 사용자 UUID
    .issuer("batch-client")
    .audience().add("batch-service").and()
    .claim("scope", "batch:manage")
    .issuedAt(Date.from(Instant.ofEpochSecond(now)))
    .expiration(Date.from(Instant.ofEpochSecond(now + 300)))
    .signWith(key, Jwts.SIG.RS256)
    .compact();
```

jjwt는 `iat`, `exp`를 정수 초로 직렬화한다. 다른 라이브러리를 쓸 때는 두 값이 소수점 없는 정수인지 확인한다.
`scripts/auth_keys.py`가 만드는 개인키는 PKCS#8(`-----BEGIN PRIVATE KEY-----`) 형식이다.

### curl로 확인

```sh
TOKEN=$(python mint_token.py)   # 위 batch_token()을 출력하는 스크립트
curl --fail-with-body http://pr-batch:8000/api/v1/runtimes \
  -H "Authorization: Bearer $TOKEN"
```

로컬 개발에서는 `make dev-token`(= `scripts/dev_token.py`)이 로컬 키 쌍(`.local-keys/`, `make auth-keys`로
자동 생성)으로 5분짜리 토큰을 출력한다. 로컬 키 쌍은 공유 환경에 쓰지 않는다.

배포 환경(dev/stg 등)의 Swagger를 테스트할 때는 그 환경의 개인키를 지정하고, 기본값과 다르면 issuer·audience도 지정한다.
`--user`를 생략하면 고정 테스트 사용자(`00000000-0000-4000-8000-000000000de0`)로 발급된다.

```sh
uv run --locked python scripts/dev_token.py \
  --private-key <그 환경 auth_private.pem> --user <사용자 UUID> --issuer <그 환경 AUTH_ISSUER> --audience <그 환경 AUTH_AUDIENCE>
```

Swagger(`/docs`)에서는 **Authorize**에 토큰만(`Bearer` 없이) 붙여넣는다. 5분이 지나면 다시 발급한다.

## 인증 오류

| HTTP | 본문 | 원인 |
| --- | --- | --- |
| 401 | `{"code": "UNAUTHENTICATED", "message": "Valid service token required."}` | 헤더 없음, `Bearer`가 아닌 스킴, 다른 키로 서명(환경별 키 혼동 포함), RS256이 아닌 알고리즘, `iss`/`aud` 불일치, 필수 claim 누락, 만료, `iat` 미래, `iat`/`exp`가 정수가 아님, 유효 기간 300초 초과, `sub`가 UUID 아님 |
| 403 | `{"code": "FORBIDDEN", "message": "Missing batch:manage scope."}` | 토큰은 유효하지만 `scope`가 `batch:manage`가 아님 |

401 응답에는 `WWW-Authenticate: Bearer` 헤더가 붙는다. 보안상 서버는 401의 세부 원인을 알려주지 않으므로
위 표의 항목을 차례로 확인한다. 가장 흔한 원인은 다른 환경의 개인키를 쓰거나, issuer·audience가 맞지 않거나,
유효 기간을 300초보다 길게 잡았거나, 시계가 앞선 경우다.

## 운영 원칙

- 개인키는 호출 서비스 서버에만 두고 브라우저·모바일 앱 등 사용자 단말로 보내지 않는다.
- 외부 사용자의 인증 헤더를 pr-batch에 그대로 넘기지 않는다. 호출 서비스가 사용자를 검증한 뒤 그
  사용자의 UUID로 새 토큰을 발급한다.
- 다른 사용자의 자원은 404로 보인다. 엉뚱한 `sub`로 발급하면 401이 아니라 404·빈 목록이 나오므로
  `sub` 값부터 확인한다.
- 운영에서는 TLS와 내부망 접근 제한을 함께 적용한다.
