# 에이전트 전달: 배치 실행 제어는 pr-batch 경로로만

대상: pr-agentic-service `src/api_service/routers/executor.py` (기준 커밋 `f5281e1`). 통합테스트 D8·D9 전에 반영한다.

## 요약

배치 실행의 제어 경로는 **에이전트 → pr-batch → Executor** 하나다(2026-11-01 결정). 지금 Executor 게이트웨이는
`trigger`를 보지 않으므로 key 호출자가 pr-batch를 거치지 않고 배치 실행을 만들거나 멈추거나 다시 돌릴 수 있다.
게이트웨이에서 이 호출들을 거절한다.

결과 조회는 이번 변경 대상이 아니다. pr-batch가 결과 노트북을 중계하지 않으므로 지금처럼 Executor 게이트웨이의
GET으로 받는다.

## 막을 호출

key 호출자의 `/api/v1/executor/{path}` 요청에 적용한다.

| 호출 | 판단 방법 | 지금 동작 (코드 기준, 실제 호출로는 미확인) | 정상 경로 |
| --- | --- | --- | --- |
| `POST /executions` | 본문 `trigger.type`이 `BATCH` | 그대로 전달. pr-batch 없는 배치 실행이 생기고, 우리 tenant에서 성공하면 리포트 대상도 된다 | `POST /api/v1/batch/batches/{id}/runs` |
| `POST /executions/{id}/cancel` | `GET /executions/{id}`의 `trigger.type`이 `BATCH` | 그대로 전달. Run이 `cancel_source=null`로 끝난다 | `POST /api/v1/batch/batches/{id}/runs/{run_id}/cancel` |
| `POST /executions/{id}/retry` | 위와 같음 | 그대로 전달. Run은 `FAILED`인데 Execution만 다시 돈다 | `POST /api/v1/batch/batches/{id}/runs`로 새로 실행 |

## 구현 시 지킬 것

- 위치는 `_for_caller`다. 쿠키 요청의 쓰기는 이미 404라 대상이 아니다.
- 조회는 호출자와 같은 `who`(tenant·사용자)로 한다. 404는 그대로 돌려줘 다른 tenant의 존재를 드러내지 않는다.
- 조회가 502·504 등으로 실패하면 전달하지 않고 그 오류를 돌려준다. 확인하지 못한 채 통과시키지 않는다.
- `POST /executions` 본문이 JSON이 아니거나 `trigger`가 없으면 지금처럼 전달해 Executor의 422를 따른다.
- 거절은 Executor로 보내기 전에 하고, 기존 쓰기 감사 로그(`executor_gateway_write … status=`)에 남긴다.
- 응답은 에이전트 오류 형식의 403을 제안한다. 코드 이름 예: `BATCH_CONTROL_USE_BATCH_API`. 메시지에 정상 경로를 적는다.

## 영향을 받지 않는 것

- pr-batch는 자기 key로 Executor를 직접 부르므로 제출·취소가 그대로 동작한다.
- 에이전트 내부 `ExecutorClient`도 게이트웨이를 거치지 않는다. 배치 리포트 저장은 그대로다.
- `trigger.type`이 `INTERACTIVE`인 실행의 제출·취소·재시도, 모든 GET.

## 구현 전 확인할 것

- `POST /executions/{id}/artifacts`: key 호출자가 배치 실행에 Artifact를 추가하는 것을 막을지 정한다. 실행 제어는 아니라서 위 표에 넣지 않았다.
- `POST /executions/{id}/operations`, `/finalize`: 배치는 SINGLE이라 Executor가 거절할 것으로 본다. D9 때 함께 확인한다.

## 확인 시나리오

| 시나리오 | 기대 결과 |
| --- | --- |
| trigger `BATCH`로 직접 제출 | 403, Execution 생성 없음 |
| 실행 중인 배치 실행을 직접 취소 | 403, 실행 계속됨 |
| 실패한 배치 실행을 직접 재시도 | 403, 새 Attempt 없음 |
| 다른 tenant의 배치 실행을 취소 | 404 |
| INTERACTIVE 실행 제출·취소·재시도 | 지금과 같음 |
| 배치 실행과 결과 노트북 GET | 200 |
| `/api/v1/batch/.../cancel`로 취소 | 202, `cancel_source=USER`로 `CANCELLED` |

단위 테스트는 `tests/test_executor_relay.py` 쪽에 두는 것을 제안한다. 통합 시나리오는
[통합테스트 시나리오 문서](scenarios.md)의 D8·D9다.
