# 수동 실행과 실행 이력 API

현재 등록 API에 수동 실행 접수·목록·상세와 Executor 제출·상태 확인 워커를 추가했다.
예약 활성화와 스케줄러는 [예약 실행 안내](scheduling-api.md)를 따른다.
취소, 자동 코드 재시도, 보고서 생성 호출은 아직 제공하지 않는다.
등록의 `enabled=false`는 예약 비활성 상태를 뜻하며 수동 실행은 가능하다.

## 실행 준비

[등록 API 안내](registration-api.md)의 PostgreSQL·공유 PV·prj_re 인증 설정을 먼저
준비한다. API와 별도 워커가 같은 DB와 설정을 사용한다. Redis는 필요하지 않다.

```sh
uv sync --locked
# .env에 Executor의 내부 HTTP 주소 설정. /api/v1은 붙이지 않는다.
# BATCH_EXECUTOR_BASE_URL=http://executor-service:8000
make migrate
make serve
# 별도 터미널 또는 별도 컨테이너에서 실행
make worker
```

`BATCH_EXECUTOR_BASE_URL`을 생략하면 등록·조회는 가능하고 수동 실행 접수는
503 `EXECUTOR_NOT_CONFIGURED`로 거절한다. API 프로세스가 워커를 자동으로
실행하지 않는다. API만 실행하면 수동 요청은 DB에 접수되어 워커 시작까지 대기한다.
`/readyz`는 DB migration과 PV만 확인하며 Executor 및 워커 생존을 확인하지 않는다.
운영에서는 API와 워커 프로세스를 각각 감독하고 미처리 명령의 체류 시간을 관찰한다.

Executor의 `INPUT_STORAGE_ROOT`에서 Batch가 저장한
`batch-input/users/.../notebooks/.../versions/.../cells/*.py`를 같은 상대경로로
읽을 수 있어야 한다. 두 서비스의 절대 마운트 경로는 달라도 된다.
허용 커널 목록은 Executor BATCH 풀의 실제 프로필과 일치시킨다.
Executor HTTP 인증 헤더는 현재 계약에 없어 추가하지 않았다. 내부망 접근과 서비스
인증은 배포 환경에서 연결하며 사용자 JWT를 Executor에 그대로 전달하지 않는다.

`BATCH_EXECUTOR_POLL_SECONDS`는 기본 5초이며 1~300초로 설정할 수 있다.
워커 한 프로세스는 한 번에 한 요청을 처리한다. 처리량이 필요하면 같은 DB를 사용하는
워커를 추가할 수 있다. 여러 워커의 claim은 DB 잠금과 lease로 조정한다.
`uv run --locked python -m app.workers.executor_worker --once`는 처리 가능한 명령 하나를
검사하는 점검용이다. 지속 실행에는 `make worker`를 사용한다.

## HTTP 계약

공통 prefix는 `/api/v1/batches`이며 기존과 동일한 사용자 JWT가 필요하다.
다른 사용자의 배치·실행에는 404를 반환한다.

| Method | 경로 | 동작 |
| --- | --- | --- |
| POST | /{batch_id}/runs | 선택된 버전·커널·정책으로 수동 실행 접수 |
| GET | /{batch_id}/runs | 접수 시각 역순 이력, limit 1~100·cursor |
| GET | /{batch_id}/runs/{run_id} | 상태·snapshot·원본 셀/Step·결과 참조 |

POST에는 `Idempotency-Key`가 필수다. 본문은 생략하거나 `{}`를 보낸다.
버전·커널 등의 임의 override는 422로 거절한다. 원하는 버전은 배치 PATCH로 먼저
선택한다. 실행 접수 트랜잭션에서 현재 실행 설정(`config_number`)의 노트북 버전·커널·
정책을 고정하므로 접수 후 배치 설정을 바꿔도 대기 중인 실행에는 영향을 주지 않는다.
일정이 없는 배치도 수동 실행할 수 있다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/runs" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Idempotency-Key: manual-request-001"
```

응답의 `id`가 `run_id`다. 같은 사용자·배치·키를 재전송하면 최초 상태 코드와 응답을
반환한다. 상태 확인에는 GET을 사용한다. 별도 실행은 새 키를 사용한다.
예약 비활성 상태에서도 수동 실행할 수 있으며, 예약 기간·misfire 조건은 적용하지
않는다. 수동 실행은 사용자가 지금 선택한 고정 코드를 실행하는 명령이다.

| 접수 결과 | HTTP | dispatch_status | 사유 |
| --- | --- | --- | --- |
| 제출 자리를 확보 | 202 | PENDING | 워커의 Executor 제출 대기 |
| QUEUE/PARALLEL의 한도 초과 | 202 | WAITING_CAPACITY | 같은 배치의 앞선 실행 대기 |
| SKIP 정책에서 이미 점유 중 | 202 | SKIPPED | skip_reason=OVERLAP |
| 대기열 max_pending 초과 | 409 | SKIPPED | skip_reason=SKIPPED_CAPACITY |

SKIPPED도 이력을 남기며 Executor에는 제출하지 않는다. 409 용량 초과 응답도
실행 기록이며 같은 키를 재전송하면 같은 응답이다. 자리가 생긴 뒤 새 실행을 원하면
새 키를 사용한다. QUEUE는 접수 순서대로 하나씩, PARALLEL은 snapshot의
`max_concurrent`까지 제출한다. 이미 대기 중인 실행을 새 실행이 추월하지 않는다.

## 이력과 상태

`batch_runs`가 배치별 실행, `run_step_bindings`가 원본 셀과 Step의 대응,
`run_commands`가 재시작 후 이어갈 제출·조회 명령을 보관한다.
접수와 명령 저장은 같은 트랜잭션이고 Executor 호출은 커밋 이후에만 수행한다.
하나의 노트북을 SINGLE Execution의 순서 있는 Steps로 제출한다.
셀별 `.py` 경로와 SHA-256을 사용하며 코드 자체를 Batch DB에 복제하지 않는다.

| 필드 | 의미 |
| --- | --- |
| dispatch_status | PENDING, WAITING_CAPACITY, SUBMITTED, SKIPPED |
| execution_status | 미제출 시 null, 제출 후 Executor 원본 상태 |
| report_status | 초기 NOT_REQUESTED, 실행 실패·취소·생략은 NOT_APPLICABLE |
| result_status | PENDING, READY, UNAVAILABLE, NOT_APPLICABLE |
| notebook_status | Executor의 노트북 projection 상태 |
| notebook_id / notebook_version_id / config_number | 실행한 노트북 버전과 배치 설정 이력 번호 |
| snapshot | 접수 당시 노트북 버전, 원본·코드 해시, 일정(schedule), 정책(policy) |
| observed_version / observed_at | 마지막 유효한 Executor 상태 버전·확인 시각 |
| tracking_error | 통신·계약 오류. 코드 실패와 별개 |
| failure_type | Executor 코드 실행 실패 분류 |
| retry_info | Executor가 보고한 재시도 관련 정보. 재시도 명령이 아님 |
| steps | 원본 셀 인덱스, sequence, Step ID·결과 manifest/Attempt/fence 참조 |
| artifacts / attempts | Executor 결과 파일·실행 Attempt의 식별자와 메타데이터 |

GET 상세의 `steps`에는 출력 원문·표·이미지를 넣지 않는다. 상태 응답의 오류 메시지와
출력 요약도 복제하지 않는다. 근거는 `result_ref`와 Artifact ID를 통해 조회한다.
`result_status=READY`는 projection 성공과 AVAILABLE NOTEBOOK Artifact를
함께 확인했다는 뜻이며
보고서 완료를 의미하지 않는다. 현재 보고서 생성 연동은 구현 전이므로 성공 실행도
`report_status=NOT_REQUESTED`로 남고 전체 완료로 표시하지 않는다.

Executor의 `SUCCEEDED`, `FAILED`, `CANCELLED`는 코드 실행 종료다.
이때 다음 실행을 허용하며, 결과 파일 수집이나 향후 리포트 생성은 점유하지 않는다.
노트북 projection이 늦으면 상태와 결과 참조를 계속 조회한다. projection 실패를
확인하면 `UNAVAILABLE`로 남기고 자동 조회를 종료한다. 이미 존재하는 부분 결과의
Artifact 참조는 보존한다.
수동 복구 후 재수집하는 API는 후속 범위다.

AVAILABLE Artifact의 다운로드 계약은 Executor의
`GET /api/v1/artifacts/{artifact_id}/content`다. prj_re는 이력의 사용자 소유권을
확인하고 이 API를 중계하는 사용자 다운로드 경로를 제공해야 한다. Batch는 현재
Artifact ID만 반환하며 prj_re의 아직 없는 공개 URL을 만들어 저장하지 않는다.
PV 경로를 브라우저 URL로 사용하지 않는다. 자동 삭제는 없다.

## 장애 복구와 한계

명령은 `SUBMIT → POLL → RESULT → DONE`으로 처리한다. 결과 노트북이 아직
준비되지 않으면 POLL로 돌아가 확인한다. DB 시간 기준 90초 lease와 매 claim마다
새 UUID 토큰을 사용한다. lease 만료 후 다른 워커가 회수할 수 있고, 만료되거나
교체된 토큰의 응답은 DB에 반영하지 않는다. 프로세스 재시작 시 메모리 상태가 필요없다.

제출 응답이 유실되면 동일한 저장 payload와 `batch-run:<run_id>:submit` 키로
Executor 접수를 다시 확인한다. 새 Execution을 요구하거나 `/retry`를 호출하지 않는다.
이 중복 전달 방지는 Executor의 멱등성 계약에 의존하며 노트북 외부 부작용의
전역 exactly-once를 보장하지 않는다.

HTTP·계약 오류는 실행 실패로 간주하지 않는다. 오류 횟수에 따라 2초부터 최대
300초에 25% 이내 jitter를 더한 간격으로 다시 확인한다. 4xx도 마지막 제출의 성공
여부를 임의로 판단하지 않고 같은 명령을 유지한다. 인증·경로·커널 설정 오류나
404·409가 지속되면 운영자가 원인을 수정해야 한다. 자동 포기·취소 기능은 없으므로
불명확한 제출은 계속 점유할 수 있다. API에서 tracking_error와 마지막 관측 시각을
함께 표시한다. 본문·토큰·코드를 오류 로그에 출력하지 않는다.

요청별 HTTP timeout은 10초, 응답 처리 시간 상한은 30초와 진행 중인 마지막 읽기
timeout, 응답 크기는 4 MiB다. 크기를 넘으면 EXECUTOR_RESPONSE_LIMIT로 남는다.
현재 결과 bundle이 이 한도를 넘는 대형 실행은 별도 페이지 조회 방식의 확장이 필요하다.
낮은 Executor 상태 버전, 다른 실행·소유자·셀의 응답은 반영하지 않는다.
Batch 밖에서 같은 Execution을 재시도하면 이력·점유 계약을 깨뜨릴 수 있으므로
사용자 수동 재실행도 Batch의 새 run_id를 통해 접수한다.

## 검증 범위

실제 PostgreSQL에서 API·트랜잭션·동시 접수·대기열 승격·lease 회수·오래된 응답
거절을 검증한다. Executor HTTP는 계약에 맞춘 테스트 응답을 사용해 응답 유실,
통신 장애, 결과 projection 지연을 재현한다.

Executor commit `d7fa206cce6508e55e72ec9b7063ec88aac191ad`의 실제
`ExecutionSubmitRequest` 및 `ExecutionSpecResolver`로 생성 payload와 공유 경로
셀 파일도 검증했다. 운영 Executor·공유 PV·Jupyter까지 연결한 종단간 검증은
배포 환경 준비 후 수행해야 한다. prj_re gateway와 보고서 API는 아직 연결하지 않았다.
