# 토큰 검증 공개키

pr-batch는 호출 서비스가 보낸 JWT(RS256)를 여기 있는 **공개키**로 검증한다.
설정 `AUTH_PUBLIC_KEY_FILE`(예: `keys/auth_public.pem`)이 이 폴더의 파일을 가리키며,
폴더는 이미지에 그대로 복사된다. 공개키는 비밀이 아니므로 커밋해도 된다.

키 쌍 만들기 (배치팀, 환경마다 한 번):

```sh
uv run --locked python scripts/auth_keys.py /안전한/경로
# → auth_private.pem: 호출 서비스들에 안전한 경로로 전달 (커밋 금지)
# → auth_public.pem : 이 폴더에 복사하고 커밋
```

개인키(`auth_private.pem`)는 이 폴더에 두지 않는다. 전달한 뒤에는 만든 곳의 사본을 지운다.
호출 쪽 사용법은 [호출 서비스 인증 가이드](../docs/client-auth.md)를 따른다.
