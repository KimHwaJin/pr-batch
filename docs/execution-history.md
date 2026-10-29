# 배치 실행 이력 저장 계약

배치 실행 이력은 `pr-batch`의 PostgreSQL(`batch` 스키마)에 저장하고, 실행 결과 파일은
Executor가 관리한다. 배치 ID 아래에 실행별 기록을 쌓고 각 기록에서 결과 노트북
Artifact를 찾을 수 있도록 한다. 결과 리포트는 pr-agentic-service가 Executor 이벤트로
만들며 pr-batch는 리포트 상태를 저장하지 않는다. API는 [실행 API 안내](execution-api.md)를 따른다.

## 식별자와 소속

| 식별자 | 의미 |
| --- | --- |
| batch_id | 등록 작업의 고정 ID. 버전·일정을 변경해도 유지 |
| script_id / script_version_id | 실행한 스크립트와 불변 원본 버전의 ID |
| config_number | 접수 당시의 배치 설정 이력 번호 |
| run_id | 예약·수동 실행 한 번마다 pr-batch가 발급하는 ID |
| execution_id | 실제 코드 실행을 맡긴 Executor의 ID |
| operation_id | 제출한 Executor Operation의 ID |
| step_id | 원본 코드 셀과 대응하는 Executor Step의 ID |
| artifact_id | Executor가 관리하는 결과 노트북의 ID |

배치는 사용자 단위로 소유하며 프로젝트에 속하지 않는다. 실행 이력 조회에도 같은
소유권을 적용한다. Executor 제출 시 `metadata`에 batch_id, script_id, script_version_id,
run_id를 넣어 Executor 쪽 이벤트에서도 실행을 찾을 수 있게 한다.

## 실행별 저장 정보 (`batch_runs`)

| 구분 | 보존 내용 |
| --- | --- |
| 소유·소속 | run_id, batch_id, owner_id |
| 원본 | script_id, script_version_id, 선택한 런타임 |
| 접수 맥락 | trigger(SCHEDULED/MANUAL), 예약 시각, config_number |
| 실행 설정 | 접수 당시의 일정·정책·tenant_id·project_id 사본(`snapshot`) |
| 상태 | Run 상태, Executor 실행 상태와 version, 노트북 projection 상태 |
| 취소 | cancel_requested, 취소 출처(USER/POLICY)와 사유 |
| 시간 | 접수, 제출, 실행 시작·종료, 마지막 상태 확인, 종료 시각 |
| 실행 연결 | execution_id, operation_id, 원본 셀과 Step의 연결(`run_step_bindings`) |
| 근거 연결 | Step별 결과 manifest 참조(`result_ref`), Attempt 목록 |
| 결과 연결 | Executor Artifact 목록, 결과 노트북 Artifact ID |
| 오류 | failure_type(Executor 실패 유형, SUBMIT_REJECTED, ABANDONED), last_error 코드 |
| 생략 | SKIPPED 실행의 사유, 압축된 생략 구간(`skipped_windows`) |

실행 접수 시 선택한 버전·런타임·설정을 고정한다. 현재 배치 설정을 다시 조회해서
과거 실행 내용을 재구성하지 않는다. 이미 접수되어 대기 중인 실행에도 이 원칙을 적용한다.
다른 서비스 테이블을 직접 수정하거나 DB 외래키로 연결하지 않는다.

## 저장과 상태 갱신 순서

1. 실행 ID를 발급하고 실행 기록과 제출 Task를 같은 DB 트랜잭션에 저장한다.
2. Executor에 제출한 뒤 반환받은 실행·Operation·Step ID를 해당 기록에 연결한다.
3. 상태 조회 결과로 실행 상태와 시각을 갱신한다. 마지막 관측 시각도 기록한다.
4. Executor 종료를 확인하면 결과를 한 번 조회해 Step별 결과 참조와 Artifact를 저장하고 Run을 끝낸다.

Executor 제출 응답을 잃어도 새 실행 ID를 만들지 않고 같은 요청·멱등성 키로
접수 여부를 복구한다. 서비스 재시작 후에는 DB의 미완료 Task를 이어서 처리한다.
상태 조회의 통신 오류는 코드 실행 실패와 구분하며, 오래된 응답(낮은 version)으로 상태를
되돌리지 않는다. 원격 호출이 24시간 계속 실패하면 Run을 FAILED(`ABANDONED`)로 남긴다.

자동 코드 재시도는 하지 않는다. 사용자가 수동 실행을 요청하면 새 run_id로 구분하며,
실패한 과거 기록을 덮어쓰지 않는다.

## 보관

실행 이력(`batch_runs`, `run_step_bindings`, `skipped_windows`)은 자동 삭제하지 않는다.
내부 처리 기록은 보존 기간 뒤 지운다: 끝난 Task는 30일, 멱등성 기록은 7일
([아키텍처](architecture.md#41-보존-정리)).

원본 `.ipynb`와 셀별 실행용 `.py`는 공유 PV의 사용자·스크립트·버전 경로 아래에 보관한다.
실행 출력과 결과 노트북은 Executor의 저장 책임이다. pr-batch DB에는 코드·표·이미지·전체
파일을 복사하지 않고 식별자·상태·설정·참조만 저장한다. 내부 PV 경로를 브라우저 URL로
간주하지 않는다.
