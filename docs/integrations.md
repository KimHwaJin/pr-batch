# 기존 서비스 연동 계약 검토

2026-10-12 확인한 저장소 소스 기준이다. 배포 환경에서 호출한 결과를 뜻하지 않는다.
기존 기능과 새로 필요한 API를 구분한다. 관련 저장소 코드는 이번 단계에서 변경하지
않는다.

| 저장소 | 검토 기준 |
| --- | --- |
| Executor main | `d7fa206cce6508e55e72ec9b7063ec88aac191ad` |
| prj_re feature/refactor-base | `d19b3601d418ac77e65616bba56ee0c34bd8bc0c` |

## Executor 실행 제출

`POST /api/v1/executions`에 SINGLE Operation과 연속된 sequence의 Step을 보낸다.
노트북의 비어 있지 않은 코드 셀을 순서대로 하나씩 Step에 대응하는 안이다.
코드를 함수로 감싸거나 Python AST로 변환하지 않고 커널에 원문을 전달한다.
Jupyter driver는 `execute_request`를 사용하고 `allow_stdin=false`이므로 사용자
입력을 기다리는 셀은 지원하지 않는다. magic 등의 호환성은 실제 커널 검증이 필요하다.

프로젝트와 세션은 선택 필드이고 user_id와 task_id는 필수다.
`task_id`에 Batch의 run_id를 사용하고 존재하지 않는 프로젝트·세션을 만들지 않는다.
`runtime.profile`에 사용자가 선택한 kernelspec 이름을 전달한다.

다음은 형식을 보여주는 예시다. 경로와 SHA-256은 실제 생성 파일로 치환한다.

```json
{
  "idempotency_key": "batch-run:<run-id>:submit",
  "lifecycle": {"operation_mode": "SINGLE"},
  "trigger": {
    "type": "BATCH",
    "actor": {"type": "BATCH", "id": "<batch-id>"}
  },
  "runtime": {"type": "JUPYTER", "profile": "<kernel-profile>"},
  "context": {"user_id": "<owner-id>", "task_id": "<run-id>"},
  "operation": {
    "spec": {
      "schema_version": "1.0",
      "steps": [{
        "sequence": 0,
        "payload": {
          "type": "PYTHON_EXECUTE",
          "source": {
            "type": "PATH",
            "path": "batch-input/<batch-id>/<version-id>/cells/000000.py",
            "sha256": "<actual-64-character-sha256>"
          }
        }
      }]
    }
  },
  "metadata": {"batch_id": "<batch-id>", "notebook_version_id": "<version-id>"}
}
```

예시의 source.path는 축약형이다. 실제 경로는 [저장소 설계](design.md)의 소유자별
경로에서 생성한 입력 루트 기준 상대경로다. Executor는 `.ipynb` 입력을 직접 받지
않고 PATH의 `.py` 확장자·UTF-8·입력 루트 범위·SHA-256을 검증한다.
셀 수와 소스 크기에는 설정 상한이 있으므로 Batch 등록 상한과 맞춘다.
상한을 우회하려고 임의로 MULTI로 변경하지 않는다.

근거:
[제출 모델](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/interfaces/_contracts/execution_inputs.py),
[Step 계약](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/execution_specs.py),
[Jupyter 실행](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/infrastructure/_jupyter/execution.py).

## 재시도 조사 결과와 남은 결정

2026-10-12 후속 합의: 재시도 정책은 추후 결정한다. 초기 Batch는 Executor의
상태를 기록하고 자동 재실행하지 않는다. 아래 조사는 향후 결정의 근거로 유지한다.

사용자는 Executor의 기존 재시도를 활용하고 Batch에 중복 엔진을 두지 않기로 했다.
확인 커밋의 전체 서비스 소스와 README를 조사한 결과는 다음과 같다.

| 구분 | 현재 코드에서 확인한 내용 |
| --- | --- |
| 최초 실행 body | lifecycle, trigger, runtime, context, operation, metadata. retry 정책 필드 없음 |
| 실행 조회의 retry | strategy, count, from_sequence, retained_runtime_session_until 응답 필드 |
| 실패 처리 | 실패 유형에 따라 재시도 전략을 계산하고 FAILED 상태로 저장 |
| 재실행 시작 | REST `/executions/{id}/retry` 또는 MCP `execution_retry`에서 명령 접수 |
| 내부 호출 경로 | lifecycle.retry가 request_retry를 호출하고 retry_ready 작업을 기록 |
| lease 만료 복구 | FAILED 및 FROM_START를 기록. 이 경로가 즉시 코드를 재실행하지는 않음 |
| README | 자동 코드 재실행은 아직 활성화하지 않았다고 명시 |

즉, 확인한 main에서는 **재시도 방법을 Executor가 자동 결정하지만, 재실행 개시는
명시적인 retry 요청을 받는 구조**다. 응답의 retry 객체를 제출 body 옵션과 혼동하지
않아야 한다. Runtime 정리 재시도·미처리 메시지 회수는 실패한 코드의 자동 재실행과
별개다. 다른 브랜치나 배포 버전은 이 조사에 포함하지 않았다.

현재 계약을 유지한다면 누가 retry를 호출할지 추가 합의가 필요하다.
서비스 경계상 제안은 Executor에 횟수·간격·재시도 가능 오류를 포함한 자동 정책을
명시적으로 제공하고 Batch는 이를 전달·관찰하는 것이다. 이것은 **추가 제안**이며
이번 설계 작업에서 Executor를 변경하거나 Batch에 자동 호출 루프를 만들지 않는다.
다른 배포 버전에 이미 있다면 그 계약을 기준으로 이 부분을 갱신한다.

retry는 기존 Execution과 Operation을 유지하고 Executor가 FROM_FAILED_STEP 또는
FROM_START를 선택한다. Batch가 새 Execution으로 재시도를 흉내 내지 않는다.
동일 명령의 통신 응답 유실을 복구하는 멱등 재전송은 코드 재시도와 구분한다.

`COMPLETION_FAILED`는 코드 실행 전에도 발생할 수 있다. 이 분류만으로 셀 전체
성공을 추정하지 않는다. NOT_RETRYABLE의 409는 무한 재요청하지 않는다.

근거:
[retry API](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/dev_docs/post-execution-retry.md),
[명령 처리](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/application/_execution_service/lifecycle.py),
[실패 처리](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/infrastructure/execution_worker/run_finalizer.py),
[lease 복구](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/infrastructure/execution_worker/lease_recovery.py),
[응답 모델](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/interfaces/_contracts/executions.py),
[README](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/README.md).

## 결과와 리포트 저장

> 2026-10-19 결정으로 pr-batch는 리포트를 요청하지 않는다. pr-agentic-service가 Executor
> `execution.completed` 이벤트로 직접 작성한다. 이 절과 아래 prj_re 리포트 API 제안은 기록용이다.

`GET /api/v1/executions/{id}/result`에서 Operation·Step·Attempt·Artifact를 확인한다.
대용량 출력 본문은 manifest와 representation 참조에서 읽는다. 공유 결과 경로는
ID를 조합해 재구축하지 않고 반환된 참조의 범위·크기·해시·identity를 검증한다.

`POST /api/v1/executions/{id}/artifacts`는 REPORT 저장을 지원하며 현재 구현은
**Execution이 SUCCEEDED인 상태**를 요구한다. Batch가 마지막 셀 성공만 보고
리포트 처리를 시작하지 않고 Executor의 정식 성공을 기다린다.

```json
{
  "idempotency_key": "report:<report-job-id>:materialize",
  "type": "REPORT",
  "source": {"type": "INLINE", "content": "# 분석 결과\n\n..."},
  "name": "batch-report-<report-job-id>.md",
  "media_type": "text/markdown",
  "append_to_notebook": true,
  "actor": {"type": "AGENT", "id": "batch-report-service"}
}
```

`prj_re`는 생성한 Markdown과 해시를 먼저 영속화한다. Artifact 등록만 실패하면
같은 본문·이름·키로 재요청한다. 매번 LLM을 호출해 같은 키의 내용을 바꾸지 않는다.
기존 API는 동일 키의 Markdown 셀 추가를 중복시키지 않는다. 향후 별도 보고서의
재생성을 지원한다면 새로운 세대·이름·키를 사용한다.

`GET /executions/{id}/notebook`은 JSON 셀 조회이고 파일 다운로드와 다르다.
NOTEBOOK Artifact의 ID를 확인한 후
`GET /api/v1/artifacts/{artifact_id}/content`로 파일을 다운로드한다.
현행 다운로드는 현재 파일을 제공하므로 불변 등록 원본과 동일한 보존 보장을
가정하지 않는다. 생성 직후와 리포트 추가 후의 결과 파일 내용은 달라진다.

근거:
[Artifact 요청](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/interfaces/_contracts/artifacts.py),
[저장 처리](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/src/executor_service/infrastructure/materialized_artifacts.py),
[다운로드 검증](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/docs/artifact-download-validation.md),
[공유 PV 계약](https://github.com/KimHwaJin/executor/blob/d7fa206cce6508e55e72ec9b7063ec88aac191ad/docs/shared-pv-path-contract.md).

## prj_re 독립 리포트 API 제안

기존 리포트는 관측 결과의 근거 표와 LLM 해석을 분리하며, 텍스트 모델이 이미지를
봤다고 주장하지 않도록 구성되어 있다. 이 부분을 재사용 후보로 둔다.
다만 승인된 계획·Tool ID·구조화 관측을 전제로 하므로 임의 노트북과 바로 연결할 수
있다고 가정하지 않는다. 프로젝트·대화·승인을 요구하지 않는 별도 진입점과 일반적인
셀 출력을 근거로 바꾸는 어댑터가 필요하다.

아래는 **신설 제안**이며 현재 제공되는 API가 아니다.

| Method | 경로 | 의미 |
| --- | --- | --- |
| POST | /internal/v1/execution-reports | 비동기 접수. 202와 report_job_id |
| GET | /internal/v1/execution-reports/{id} | 생성·저장 상태와 Artifact 참조 |
| POST | /internal/v1/execution-reports/{id}/retry | 동일 근거로 실패 단계 재개 |

입력은 batch_run_id, execution_id, owner_id, 확정된 결과 identity와 Step별
manifest 참조, 출력 옵션이다. 사용자별 프롬프트·프로젝트는 받지 않는다.
필수 Idempotency-Key의 재전송은 같은 작업으로 연결하고 다른 내용이면 409다.
인증된 서비스 요청의 소유자와 Execution 소유자가 일치하는지 확인한다.

제안 상태는 QUEUED → COLLECTING → GENERATING → MATERIALIZING → SUCCEEDED.
FAILED에서 실패 단계를 보존한다. retry는 같은 작업을 재개하며 새 Execution을
만들지 않는다. 작성된 Markdown이 있으면 저장 단계만 재개한다.

응답에는 state, failed_stage, execution_id, evidence_identity, artifact_id,
notebook_appended, quality, limitations, updated_at을 포함한다.
저장과 노트북 추가가 확인되기 전에는 성공으로 표시하지 않는다. 근거 표만 제공하는
fallback을 채택한다면 quality=EVIDENCE_ONLY로 분석 완료와 구분한다.

초기 근거는 코드 snapshot, stream, text/plain, 표를 포함한 text/html과 구조화
출력이다. HTML을 실행하지 않고 표로 읽으며 전체 수치와 샘플·생략 표시를 구분한다.
코드나 출력의 지시문은 분석 데이터로 취급한다. 이미지에만 근거가 있다면 평가할 수
없다고 명시하며 이상 없음으로 결론 내리지 않는다. 대용량 결과는 분할·상한·참조로
처리하고 읽지 못한 범위는 limitations에 기록한다.

공통 구성은 특이사항, 근거, 해석, 추가 확인 사항이다. 수치는 근거에서 가져오고
원인 단정·미계산 지표·미실시 과거 비교는 생성하지 않는다. 독립 리포트 처리는
새 계산이나 노트북 코드 수정을 수행하지 않는다. 향후 VLM은 근거 수집과 모델
지원 여부를 확장해 연결하며 Batch API 변경은 최소화한다.

근거:
[기존 근거 표](https://github.com/KimHwaJin/prj_re/blob/d19b3601d418ac77e65616bba56ee0c34bd8bc0c/src/dtest/agent_service/agents/analysis/execution/report.py),
[기존 작성 지침](https://github.com/KimHwaJin/prj_re/blob/d19b3601d418ac77e65616bba56ee0c34bd8bc0c/src/dtest/agent_service/agents/analysis/agent_builders/execution_report/prompt.md).

## 구현 전 연동 확인

| 확인 | 담당 경계 |
| --- | --- |
| 코드 자동 재시도의 실제 제공 여부와 최종 실패 | Executor와 Batch |
| 서비스 인증·소유자 전달·다운로드 중계 | prj_re와 Batch와 Executor |
| 입력·결과·Jupyter PV 마운트 및 권한 | 각 서비스 운영 설정 |
| 원본 크기·Step 수·출력 크기 상한 정합성 | Batch 등록과 Executor 설정 |
| 일반 셀 출력에서 리포트 근거로의 변환 | prj_re |
| 리포트 추가 후 결과 파일 다운로드 | prj_re와 Executor |

이번 검토는 소스 조사다. 서비스 기동·DB 변경·실제 API 호출 검증은 수행하지 않았다.
