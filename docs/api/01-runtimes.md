# 01. 런타임 API (1개)

라우터: `src/app/api/v1/runtimes.py` · 태그 `runtimes` · 공통 규칙은 [00-common.md](00-common.md)

| Method | 경로 | 설명 |
| --- | --- | --- |
| GET | `/api/v1/runtimes` | 배치에 지정할 수 있는 런타임 목록 |

---

## GET /api/v1/runtimes — 런타임 목록

배치 등록·수정 때 `runtime`에 넣을 수 있는 조합 목록이다. pr-batch 설정 `BATCH_RUNTIMES`(Executor BATCH 풀의 커널)에서 읽는다.

### 요청

| 구분 | 이름 | 필수 | 설명 |
| --- | --- | --- | --- |
| 헤더 | `X-Api-Key` | O | 호출 서비스 key |
| 헤더 | `X-User-Id` | X | 필요 없다 |

본문·쿼리 없음.

```sh
curl "$BATCH_URL/api/v1/runtimes" -H "X-Api-Key: $BATCH_API_KEY"
```

### 응답 200 — `RuntimeList`

```json
{"items": [{"type": "JUPYTER", "profile": "default"}, {"type": "JUPYTER", "profile": "3102311"}]}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `items[].type` | `JUPYTER` | 런타임 종류. 현재 `JUPYTER`만 있다 |
| `items[].profile` | string (1~128자) | 커널 이름. Executor 실행의 `runtime.profile`과 같다 |

배치에는 이 목록의 객체 하나를 **그대로** `runtime`에 넣는다. 목록에 없는 조합은 422 `UNKNOWN_RUNTIME`이다.

### 오류

| HTTP | code | 의미 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | key 오류 |
| 503 | `RUNTIMES_NOT_CONFIGURED` | `BATCH_RUNTIMES`가 비어 있음 |
