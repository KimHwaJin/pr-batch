# API 사용 변경 사항 (2026-10-21 → 2026-10-26)

> **2026-10-29 인증 변경**: 아래의 RS256 JWT 서명과 본문 `tenant_id`는 더 이상 쓰지 않는다. 호출 서비스는
> `X-Api-Key`와 `X-User-Id`(선택 `X-Project-Id`·`X-Tenant-Id`)를 보낸다. tenant·project는 등록 때 헤더로 정해지고
> 바꿀 수 없다(본문 `tenant_id`는 422, `TENANT_IMMUTABLE` 대신 403 `TENANT_MISMATCH`). 런타임 목록은 설정
> `BATCH_RUNTIMES`에서 오며 비어 있으면 503 `RUNTIMES_NOT_CONFIGURED`다. 자세한 내용은
> [호출 서비스 인증 가이드](client-auth.md)를 따른다.

[API별 호출 가이드](client-api.md)와 [호출 서비스 인증 가이드](client-auth.md)를 처음 전달한
2026-10-21 이후, 호출 서비스 입장에서 바뀐 점만 모았다. 최신 상세 규격은 두 가이드와 서버의
`/docs`(Swagger)를 따른다.

## 한눈에 보기

| # | 구분 | 변경 | 호출 코드 수정 |
| --- | --- | --- | --- |
| 1 | 인증 | HS256 공유 비밀키 → **RS256 개인키 서명** | **필수** |
| 2 | 인증 | 기본 `iss` `prj_re` → `batch-client`, `aud` `pr-batch` → `batch-service` | **필수** (배치팀이 알려준 값 사용) |
| 3 | 목록 | 실행 이력·생략 구간·버전 목록의 `cursor`가 불투명 문자열 | 직접 만든 cursor를 보냈다면 필수 |
| 4 | 배치 | 등록·수정·응답에 `tenant_id` 추가 | 이벤트를 자기 Stream으로 받으려면 필요 |
| 5 | 스크립트 | `type=PYTHON` 제거 | `PYTHON`을 보냈다면 필요 |
| 6 | 실행 | `QUEUED` 취소 응답이 `QUEUED` + `cancel_requested: true` | 응답만 보고 끝났다고 판단했다면 필요 |
| 7 | 실행 | `steps[].sequence`·`cell_list[].sequence`는 0부터 시작 | 1부터라고 가정했다면 필요 |
| 8 | 실행 | `failure_type`에 `ABANDONED` 추가 | 표시만 |
| 9 | 예약 | 생략 구간 `reason`에 `REPLACED_BY_LATEST` 추가, REPLACE는 최근 일정 하나만 실행 | 표시만 |
| 10 | 공통 | `Idempotency-Key` 기록 7일 보관 | 재시도는 7일 안에 |
| 11 | 공통 | 배치 등록·수정도 Executor가 없으면 503 (가이드 보완) | 503 재시도 처리 확인 |

## 1. RS256 개인키 서명 (필수)

- 토큰 헤더는 `{"alg": "RS256", "typ": "JWT"}`다. HS256 토큰은 이제 401이다.
- 배치팀이 환경(dev/stg/prd)마다 전달하는 **개인키 파일 `auth_private.pem`**으로 서명한다. 공유 비밀키(`AUTH_SECRET`)는 없어졌다.
- 모든 호출 서비스가 같은 개인키를 쓴다. 서버 안에만 보관하고 git·이미지·로그에 남기지 않는다.
- Python은 `pyjwt[crypto]`가 필요하다.

```python
PRIVATE_KEY = Path("/secure/path/auth_private.pem").read_bytes()
jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")  # 이전: HS256 + 공유 비밀키
```

Java 예시와 키 교체 절차는 [인증 가이드](client-auth.md)에 있다.

## 2. issuer·audience 기본값

| claim | 이전 기본 | 현재 기본 |
| --- | --- | --- |
| `iss` | `prj_re` | `batch-client` (모든 호출 서비스 공통) |
| `aud` | `pr-batch` | `batch-service` |

`sub`(사용자 UUID), `scope=batch:manage`, 유효 기간 300초 이하 규칙은 그대로다.

## 3. 불투명 cursor

- 대상: `GET /batches/{id}/runs`, `GET /batches/{id}/skipped-windows`, `GET /scripts/{id}/versions`
  (스크립트·배치 목록은 원래 불투명 문자열이었다).
- 이전에는 각각 UUID, UUID, 정수였다. 이제 모두 문자열이고 응답 `next_cursor`도 문자열이다.
- 받은 `next_cursor`를 그대로 `cursor`에 넣는다. 직접 만든 UUID·숫자는 422 `INVALID_CURSOR`다.

## 4. 배치 `tenant_id`

- `POST /batches`에 선택 필드 `tenant_id`가 생겼다(예: `"pr-agentic"`). 형식은
  `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`이고 `dlq`·`work`는 쓸 수 없다(422).
- 값이 있으면 이 배치 실행의 Executor 이벤트가 `executor.events.{tenant_id}`로만 가고, 없으면 공용
  `executor.events`로 간다. 이벤트의 `execution.metadata.run_id`가 pr-batch `run_id`다.
- `PATCH`로는 한 번만 정할 수 있다(null → 값, 기존 배치 백필용). 같은 값은 변경으로 치지 않고,
  다른 값은 409 `TENANT_IMMUTABLE`, `null`은 422다.
- `BatchView` 응답에 `tenant_id`가 추가됐다. 이미 접수된 실행은 접수 당시 값으로 제출된다.

## 5. `type=PYTHON` 제거

스크립트 업로드의 `type`은 `NOTEBOOK`만 받는다. `PYTHON`은 이전에 422 `UNSUPPORTED_SCRIPT_TYPE`이었고 이제
422 `VALIDATION_ERROR`다.

## 6. QUEUED 실행 취소

- `WAITING`은 이전처럼 바로 `CANCELLED`다.
- `QUEUED`는 응답이 `QUEUED` + `cancel_requested: true`로 온다. Executor에 닿기 전이면 곧
  `CANCELLED`가 되고, 이미 제출됐으면 Executor 종료를 확인한 뒤 `CANCELLED`가 된다.
- 최종 상태는 상세 GET으로 확인한다(`RUNNING`과 같은 방식). 가이드는 "바로 `CANCELLED`"로 잘못 적혀 있었다.

## 7. sequence는 0부터

`cell_list[].sequence`와 실행 상세 `steps[].sequence`는 0부터 시작한다. 서버 동작은 원래 그랬고 가이드 예시만
1부터로 잘못 적혀 있었다.

## 8. `failure_type: ABANDONED`

pr-batch가 Executor와 24시간 동안 한 번도 통신하지 못하면 실행을 `FAILED`(`failure_type=ABANDONED`)로 끝내고
다음 실행 자리를 푼다. Executor 쪽 실행은 남아 있을 수 있으므로 필요하면 `execution_id`로 확인한다.
이전에는 이런 실행이 `QUEUED`·`RUNNING`에 계속 머물렀다.

`COMPLETED`의 의미도 정리됐다. Executor는 결과 노트북이 준비된 뒤에만 성공을 보고하므로
`COMPLETED`면 `result.notebook_artifact_id`를 쓸 수 있다.

## 9. REPLACE 정책과 `REPLACED_BY_LATEST`

`policy.overlap.mode=REPLACE`에서 한 번에 여러 일정이 도래하면(장애 뒤 CATCH_UP 복구, 짧은 주기) 가장 최근
일정 하나만 실행하고 나머지는 생략 구간 `reason=REPLACED_BY_LATEST`로 기록한다. 이전에는 실행이 여러 개
만들어져 서로를 취소했다.

## 10. Idempotency-Key 보관 기간

쓰기 요청의 키 기록은 7일 보관된다. 7일이 지난 같은 키는 새 요청으로 처리된다. 같은 키 재전송은 Executor가
장애여도 최초 응답을 그대로 돌려준다.

## 11. 503 범위 (가이드 보완)

서버 동작은 그대로이고 가이드에 빠져 있던 내용이다. Executor 주소가 없거나 런타임 목록을 읽지 못하면 배치 등록·수정·활성화가 503
(`EXECUTOR_NOT_CONFIGURED`, `RUNTIME_CATALOG_UNAVAILABLE`)이다. 같은 `Idempotency-Key`로 잠시 뒤 재시도한다.

## 바뀌지 않은 것

엔드포인트 경로, `Idempotency-Key`·`If-Match` 사용법, 일정·정책 형식, 실행 상태 이름, 결과 노트북 다운로드
방식(Executor artifact API, pr-batch 중계 없음)은 그대로다.
