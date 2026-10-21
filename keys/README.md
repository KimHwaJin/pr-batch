# 토큰 검증 공개키

pr-batch는 호출 서비스가 보낸 JWT(RS256)를 여기 있는 **공개키**로 검증한다.
설정 `AUTH_PUBLIC_KEY_FILE`(예: `keys/auth_public.pem`)이 이 폴더의 파일을 가리키며,
폴더는 이미지에 그대로 복사된다. 공개키는 비밀이 아니므로 커밋해도 된다.

```
auth_private.pem ──(안전한 경로로 전달)──▶ 호출 서비스들 (모두 같은 파일로 서명)
auth_public.pem  ──(이 폴더에 커밋)──────▶ pr-batch (검증)
```

## 키 쌍 만들기

환경(dev/stg/prd)마다 한 번씩 만든다. 도커는 필요 없고 아래 중 편한 방법을 쓴다. 결과물은 모두 같은
형식(RSA 2048비트, PEM)이라 어느 방법이든 pr-batch에서 그대로 쓸 수 있다.

**macOS·Linux (레포 + uv)**

```sh
uv sync --locked
uv run --locked python scripts/auth_keys.py ~/batch-keys/prd
```

**Windows PowerShell (레포 + uv)** — 보통 `make`가 없으므로 명령을 직접 실행한다.

```powershell
uv sync --locked
uv run --locked python scripts\auth_keys.py C:\batch-keys\prd
```

**레포 없이 Python만 있을 때** — `scripts/auth_keys.py` 파일 하나만 복사해 실행한다.

```powershell
pip install cryptography
python auth_keys.py C:\batch-keys\prd
```

**openssl (macOS·Linux 터미널, Windows는 Git for Windows의 Git Bash)**

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out auth_private.pem
openssl pkey -in auth_private.pem -pubout -out auth_public.pem
```

`scripts/auth_keys.py`는 대상 폴더에 키가 이미 있으면 덮어쓰지 않는다. 다시 만들려면 다른 폴더를 지정한다.

## 배치하기

1. `auth_public.pem`을 이 폴더에 넣는다. 환경마다 키가 다르면 `auth_public.prd.pem`처럼 이름을 나눈다.
2. 해당 환경의 `config.{APP_ENV}.yml`에 경로를 적는다.

   ```yaml
   AUTH_PUBLIC_KEY_FILE: keys/auth_public.prd.pem
   ```

3. 커밋하고 이미지를 다시 빌드한다. 파일이 없거나 RSA 2048비트 이상 공개키가 아니면 앱이 기동하지 않는다.
4. `auth_private.pem`은 호출 서비스들에 안전한 경로로 전달한다. **이 폴더나 git에 두지 않고**, 전달한 뒤에는
   만든 곳의 사본을 지운다.

## 키 교체

개인키 유출이 의심되거나 주기적으로 바꿀 때는 새 키 쌍을 만들고, 호출 서비스들의 개인키와 이 폴더의
공개키를 함께 바꾼다. pr-batch는 공개키를 하나만 받으므로 두 쪽 배포 시점을 맞춘다(교체 사이에는 401).

호출 쪽 사용법은 [호출 서비스 인증 가이드](../docs/client-auth.md)를 따른다.
로컬 개발용 키 쌍은 `make auth-keys`가 git 제외 폴더 `.local-keys/`에 만든다.
