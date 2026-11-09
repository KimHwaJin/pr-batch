# pr-batch API 문서

pr-batch를 호출하는 서비스를 위한 API 명세다. 내용은 `src/app/api/` 라우터와 `src/app/schemas/`가 실제로
받는 값·돌려주는 값을 기준으로 썼고(2026-11-09 기준), 쿠버네티스에 배포된 서비스를 호출하는 경우를 기준으로 한다.

| 문서 | 대상 |
| --- | --- |
| [00-common.md](00-common.md) | 인증·신원 헤더, Idempotency-Key, If-Match, 목록, 오류 형식 등 모든 API 공통 규칙 |
| [01-runtimes.md](01-runtimes.md) | 런타임 (1개) |
| [02-scripts.md](02-scripts.md) | 스크립트·버전 (9개) |
| [03-batches.md](03-batches.md) | 배치, 일정·정책, 운영 API (10개) |
| [04-runs.md](04-runs.md) | 실행, 결과 노트북, 완료 알림 (4개) |
| [pr-batch-for-agents.md](pr-batch-for-agents.md) | 코딩 에이전트용 단일 파일 요약. 클라이언트 코드를 만들 때 이 파일과 `openapi.json`을 함께 준다 |
| [openapi.json](openapi.json) | 전체 HTTP 명세. 코드에서 생성한 정본 |

key 발급·`AUTH_CLIENTS` 설정·감사 로그는 [호출 서비스 인증 가이드](../client-auth.md), 내부 처리 구조는
[실행 API](../execution-api.md), [예약 실행](../scheduling-api.md), [아키텍처](../architecture.md)를 본다.

## 엔드포인트 현황 (공개 24개 + 헬스체크 2개)

모든 공개 API는 `/api/v1` 아래에 있다. 표의 경로는 `/api/v1`을 뺀 값이다.

| 분류 | Method | 경로 | 성공 | `Idempotency-Key` | `If-Match` | `X-User-Id` |
| --- | --- | --- | --- | --- | --- | --- |
| 런타임 | GET | `/runtimes` | 200 | | | 불필요 |
| 스크립트 | POST | `/scripts` | 201 | 필수 | | 필수 |
| | GET | `/scripts` | 200 | | | 선택 |
| | GET | `/scripts/{script_id}` | 200 | | | 선택 |
| | PATCH | `/scripts/{script_id}` | 200 | 필수 | 필수 | 필수 |
| | DELETE | `/scripts/{script_id}` | 204 | 필수 | 필수 | 필수 |
| 버전 | POST | `/scripts/{script_id}/versions` | 201 / 200 | 필수 | | 필수 |
| | GET | `/scripts/{script_id}/versions` | 200 | | | 선택 |
| | GET | `/scripts/{script_id}/versions/{version_id}` | 200 | | | 선택 |
| | GET | `/scripts/{script_id}/versions/{version_id}/content` | 200 | | | 선택 |
| 배치 | POST | `/batches` | 201 | 필수 | | 필수 |
| | GET | `/batches` | 200 | | | 선택 |
| | GET | `/batches/{batch_id}` | 200 | | | 선택 |
| | PATCH | `/batches/{batch_id}` | 200 | 필수 | 필수 | 필수 |
| | DELETE | `/batches/{batch_id}` | 204 | 필수 | 필수 | 필수 |
| | POST | `/batches/{batch_id}/enable` | 200 | 필수 | 필수 | 필수 |
| | POST | `/batches/{batch_id}/disable` | 200 | 필수 | 필수 | 필수 |
| | GET | `/batches/{batch_id}/skipped-windows` | 200 | | | 선택 |
| 배치 운영 (admin key) | POST | `/batches:disable-by-owner` | 200 | | | 불필요 |
| | POST | `/batches:disable-by-tenant` | 200 | | | 불필요 |
| 실행 | POST | `/batches/{batch_id}/runs` | 202 (409) | 필수 | | 필수 |
| | GET | `/batches/{batch_id}/runs` | 200 | | | 선택 |
| | GET | `/batches/{batch_id}/runs/{run_id}` | 200 | | | 선택 |
| | POST | `/batches/{batch_id}/runs/{run_id}/cancel` | 202 | 필수 | | 필수 |
| 헬스체크 | GET | `/healthz` (경로 앞 `/api/v1` 없음) | 200 | | | 인증 없음 |
| | GET | `/readyz` (경로 앞 `/api/v1` 없음) | 200 | | | 인증 없음 |

`X-User-Id` 선택: 보내면 그 사용자 것만, 빼면 tenant 전체를 조회한다([조회 범위](00-common.md#조회-범위-읽기)).

## openapi.json

[openapi.json](openapi.json)은 코드에서 생성한 정본이다. 배포된 서비스는 Swagger와 `/openapi.json`을 열지 않으므로
요청·응답 스키마는 이 파일로 확인한다. 라우터나 스키마가 바뀌면 이 파일도 함께 갱신된다.
