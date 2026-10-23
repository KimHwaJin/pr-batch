# 스크립트·배치 분리 설계

> 2026-10-13 이후: 업로드 자원 이름을 Notebook에서 **Script**로 바꾸고(`/api/v1/scripts`, `script_id`,
> `script_version_id`), 등록 시 `type`(NOTEBOOK, 추후 PYTHON)을 받는다. 아래 본문의 "노트북" 자원은 Script로 읽는다.
> 실행 파이프라인과 Run 상태는 [서비스 체계](domain-model.md), [목표 아키텍처](architecture.md)가 최신이다.

상태: 2026-10-13 사용자와 합의한 방향의 상세 설계. 이 문서 기준으로 구현한다.

## 1. 개념

사용자는 노트북 파일을 여러 개 올려 둔다. 배치 등록은 올려 둔 노트북 중 **어떤 것**을
**언제**(일정), **어떻게**(커널, 겹침·누락 정책) 돌릴지만 정한다.

| 자원 | 의미 | 주요 속성 |
| --- | --- | --- |
| Notebook | 업로드한 노트북. 사용자별 여러 개 | 이름, 버전 목록 |
| ScriptVersion | 노트북의 한 업로드 원본. 불변 | 파일명, 원본 해시, 실행할 코드 셀 목록 |
| Batch | 정기 실행 등록. 노트북 한 버전을 가리킴 | 이름, 노트북·버전, 커널, 일정(선택), 정책, 활성 여부 |
| Run | 배치의 실행 한 번 (예약 또는 수동) | 접수 당시 설정 사본, Executor 상태·결과 |

```
POST /notebooks ─▶ Notebook ─┬─ v1
POST /scripts/{id}/versions └─ v2 …
                                ▲ 버전 고정 참조
POST /batches ─▶ Batch(노트북·버전 + 커널 + 일정 + 정책, 비활성)
                   ├─ POST /enable ─▶ 스케줄러가 일정마다 Run 생성 ─▶ worker ─▶ Executor
                   └─ POST /runs  ─▶ 수동 Run
```

결정 사항:

- 노트북 하나를 여러 배치에서 재사용할 수 있다. 배치 하나는 노트북 하나만 실행한다.
- 배치는 노트북의 **특정 버전을 고정**한다. 새 버전을 올려도 기존 배치는 바뀌지 않고,
  배치 PATCH로 `script_version_id`를 바꿔야 적용된다.
- 배치 없이 노트북만 한 번 실행하는 API는 두지 않는다. 수동 실행은 배치에서 한다.
- 일정은 선택이다. 일정이 없는 배치는 수동 실행 전용이며 활성화할 수 없다.
- 운영 데이터가 없으므로 DB migration은 새 기준(baseline) 하나로 다시 만든다.
  기존 로컬 DB는 `batch` 스키마를 지우고 다시 migration한다.

## 2. 공통 규칙

기존 규칙을 유지한다.

- Prefix `/api/v1`. `Authorization: Bearer <JWT>`(scope `batch:manage`), 소유자는 JWT `sub`.
- 다른 사용자의 자원은 404.
- 모든 쓰기에 `Idempotency-Key`. 같은 키·같은 내용은 최초 응답 재전송, 다른 내용은 409.
- 수정(PATCH)·활성화·비활성화에 `If-Match: "<revision>"`. 상세 GET의 `ETag` 사용.
- 목록은 `limit`(1~100, 기본 50)·`cursor`, 응답 `{items, next_cursor}`.

변경 사항:

- JSON 본문 검증 오류는 항상 `422 VALIDATION_ERROR`와 `errors[].loc`, `errors[].type`,
  `errors[].msg`를 준다. 사용자가 보낸 값은 되돌려주지 않는다. (`msg`는 우리가 정의한 검증
  문구라 입력값이 들어가지 않는다.)
- 생성 응답(201)에 `Location` 헤더를 준다.

## 3. Notebook API

| Method | 경로 | 동작 | 성공 |
| --- | --- | --- | --- |
| POST | /api/v1/scripts | multipart `file`(.ipynb), 선택 `name` | 201 NotebookView |
| GET | /api/v1/scripts | 내 노트북 목록 (최신 생성순) | 200 NotebookPage |
| GET | /api/v1/scripts/{script_id} | 상세 + 최신 버전 요약, `ETag` | 200 NotebookView |
| PATCH | /api/v1/scripts/{script_id} | `{"name"}` 변경. If-Match | 200 NotebookView |
| POST | /api/v1/scripts/{script_id}/versions | multipart `file` 새 버전 | 201 새 버전 / 200 동일 원본 |
| GET | /api/v1/scripts/{script_id}/versions | 버전 목록 (번호 역순) | 200 VersionPage |
| GET | /api/v1/scripts/{script_id}/versions/{version_id} | 버전 상세 + 셀 목록 | 200 VersionDetail |
| GET | /api/v1/scripts/{script_id}/versions/{version_id}/content | 원본 다운로드 | 200 파일 |

`name`을 생략하면 업로드 파일명에서 `.ipynb`를 뗀 값을 쓴다.
새 버전 업로드는 노트북의 `revision`을 올리지만 If-Match는 요구하지 않는다(번호는 행 잠금으로 할당).

### 노트북 검증 규칙

| 규칙 | 기존 | 변경 |
| --- | --- | --- |
| Markdown·raw 셀 | 422 거절 | **실행에서만 제외**, 원본에는 보존 |
| 빈 코드 셀 | 제외 | 제외 (동일) |
| cell id 누락 등 정규화 필요 | 422 거절 | **복사본을 정규화해 검증**, 원본 바이트는 그대로 저장 |
| 셀 수 한도 | 전체 셀 수 | **실행할 코드 셀 수** 기준 (`MAX_CODE_CELLS`) |
| 확장자·크기·nbformat 4·셀 크기·Python 언어 | 동일 | 동일 |
| 실행할 코드 셀 없음 | 422 `EMPTY_NOTEBOOK` | 동일 |

### 응답

```json
NotebookView {
  "id": "…", "name": "생산라인 일일 분석", "revision": 1,
  "latest_version": VersionView,
  "created_at": "…", "updated_at": "…"
}

VersionView {
  "id": "…", "script_id": "…", "version_number": 1,
  "filename": "daily.ipynb", "sha256": "…", "code_hash": "…", "size_bytes": 2048,
  "cells": {"total": 12, "executable": 7, "skipped": 5},
  "created_at": "…"
}

VersionDetail = VersionView + {
  "cell_list": [{"index": 0, "type": "markdown", "executed": false},
                {"index": 1, "type": "code", "executed": true, "sequence": 0}, …]
}
```

`filename`은 표시용으로만 저장하고 저장 경로에는 쓰지 않는다.

## 4. Batch API

| Method | 경로 | 동작 | 성공 |
| --- | --- | --- | --- |
| POST | /api/v1/batches | JSON 등록. 항상 비활성 | 201 BatchView |
| GET | /api/v1/batches | 내 배치 목록. 선택 필터 `script_id` | 200 BatchPage |
| GET | /api/v1/batches/{batch_id} | 상세, `ETag` | 200 BatchView |
| PATCH | /api/v1/batches/{batch_id} | 바꿀 필드만. If-Match | 200 BatchView |
| POST | /api/v1/batches/{batch_id}/enable | 예약 활성화. If-Match | 200 BatchView |
| POST | /api/v1/batches/{batch_id}/disable | 예약 비활성화. If-Match | 200 BatchView |
| POST | /api/v1/batches/{batch_id}/runs | 수동 실행 | 202 RunView (대기열 초과 409) |
| GET | /api/v1/batches/{batch_id}/runs | 실행 이력 | 200 RunPage |
| GET | /api/v1/batches/{batch_id}/runs/{run_id} | 실행 상세 + 셀별 Step | 200 RunDetail |
| GET | /api/v1/batches/{batch_id}/skipped-windows | 누락·생략 구간 이력 | 200 SkippedPage |

### 등록 요청

```json
{
  "name": "A라인 매일 8시",
  "script_id": "…",
  "script_version_id": "…",
  "kernel_profile": "default",
  "schedule": {
    "definition": {"kind": "CRON", "expression": "0 8 * * *"},
    "timezone": "Asia/Seoul",
    "start_at": null,
    "end_at": null
  },
  "policy": {
    "overlap": "SKIP",
    "max_concurrent": 1,
    "max_pending": 100,
    "misfire": "SKIP",
    "misfire_grace_seconds": 60,
    "catch_up_max_runs": 10,
    "catch_up_max_age_seconds": 86400
  }
}
```

| 필드 | 필수 | 규칙 |
| --- | --- | --- |
| name | O | 1~255자 |
| script_id | O | 내 노트북 |
| script_version_id | X | 생략 시 등록 시점의 최신 버전으로 **고정**. 지정 시 그 노트북의 버전이어야 함 |
| kernel_profile | O | 허용 커널 목록 |
| schedule | X | 생략·null이면 수동 실행 전용. definition 4종은 기존과 동일 (ONCE/INTERVAL/CALENDAR/CRON) |
| policy | X | 생략하면 아래 기본값. 일부만 보내면 나머지는 기본값 |

정책 기본값과 범위는 기존과 같다: overlap `SKIP|QUEUE|PARALLEL`(기본 SKIP),
max_concurrent 1~100(PARALLEL만 1 초과), max_pending 1~10000(100),
misfire `SKIP|LATEST|CATCH_UP`(SKIP), misfire_grace_seconds 1~86400(60),
catch_up_max_runs 1~1000(10), catch_up_max_age_seconds 60~31536000(86400).
수동 실행에도 겹침 정책이 적용되므로 일정이 없어도 policy는 의미가 있다.

### 수정 (PATCH)

등록 요청과 같은 필드를 쓰되 모두 선택이다. 보낸 필드만 바뀐다.

- `name`만 바꾸면 실행 설정 이력은 늘지 않는다.
- `script_id`만 바꾸면 그 노트북의 최신 버전으로 고정한다.
- `schedule: null`은 일정 제거다. 활성 상태에서는 409 `SCHEDULE_REQUIRED` (먼저 비활성화).
- `policy`는 보낸 하위 필드만 바뀐다.
- 활성 상태에서 일정을 바꾸면 기존과 같이 미래 일정부터 다시 계산하고, 다음 실행이 없으면 422.
- 이미 접수된 실행은 접수 당시 설정을 그대로 쓴다.

### 응답

```json
BatchView {
  "id": "…", "name": "…",
  "script_id": "…", "script_version_id": "…", "script_version_number": 3,
  "kernel_profile": "default",
  "schedule": {…} | null,
  "policy": {…},
  "enabled": false,
  "next_run_at": null,
  "preview_runs": ["…", "…"],
  "config_number": 1,
  "revision": 1,
  "created_at": "…", "updated_at": "…"
}
```

- 요청과 같은 모양이라 응답을 그대로 고쳐 PATCH에 쓸 수 있다.
- `next_run_at`은 활성일 때 실제 다음 예약. `preview_runs`는 "활성화하면 돌 시각" 미리보기(최대 5개, 일정 없으면 빈 배열).
- 일정이 끝나 더 돌 것이 없으면 `enabled=true`, `next_run_at=null`.
- `config_number`는 실행 설정(노트북 버전·커널·일정·정책) 이력 번호. Run이 이 번호를 가리킨다.

활성화 규칙: 일정이 있어야 하고(없으면 409 `SCHEDULE_REQUIRED`), 커널이 허용 목록에 있어야 하며,
활성화 시각 이후 다음 실행이 있어야 한다(없으면 422 `NO_FUTURE_OCCURRENCE`). Executor 미설정이면 503.

### Run

경로와 동작은 기존과 같다. 필드 변경:

- `schedule_number` → `config_number`
- `script_id`, `script_version_id` 추가 (`version_id` 대체)
- `snapshot`은 `{script_id, script_version_id, sha256, code_hash, schedule, policy}`

## 5. DB 모델 (`batch` 스키마, 새 baseline)

| 테이블 | 컬럼 | 제약 |
| --- | --- | --- |
| notebooks | id, owner_id, name, revision, latest_version_number, created_at, updated_at | idx(owner_id, created_at, id) |
| script_versions | id, script_id, version_number, filename, original_path, sha256, code_hash, size_bytes, cell_map(실행 셀), cell_list(전체 셀 요약), created_at | unique(script_id, version_number), unique(script_id, sha256), unique(script_id, id) |
| batches | id, owner_id, name, enabled, next_run_at, config_number, revision, created_at, updated_at | enabled OR next_run_at IS NULL, idx(enabled, next_run_at), idx(owner_id, created_at, id) |
| batch_configs | (batch_id, number) PK, script_id, script_version_id, kernel_profile, schedule JSONB NULL, policy JSONB, created_at | FK (script_id, script_version_id) → script_versions |
| batch_runs | 기존 + script_id, script_version_id, config_number | FK (batch_id, config_number) → batch_configs, unique(batch_id, config_number, scheduled_for) |
| run_step_bindings, run_commands, idempotency_records | 기존과 동일 | |
| skipped_windows | 기존, schedule_number → config_number | FK (batch_id, config_number) |

- 배치 목록의 `script_id` 필터는 현재 설정(`batch_configs` join)으로 거른다.
- 원본 저장 경로: `batch/inputs/users/<owner-hash>/scripts/<notebook-id>/versions/<version-id>/`.
  Executor에는 기존과 같이 셀 파일 상대경로와 SHA-256으로 제출한다.

## 6. 바뀌지 않는 부분

일정 계산(ScheduleClock), 스케줄러의 misfire·보충 처리, 실행 접수 시 겹침·대기 판단, worker의 Executor 제출·상태·결과 수집,
멱등성·ETag, 인증은 그대로 쓴다. 정책 값의 위치만 `schedule`에서 `policy`로 옮긴다.

## 7. 에러 코드 추가·변경

| HTTP | code | 상황 |
| --- | --- | --- |
| 404 | NOT_FOUND | 노트북·버전이 없거나 남의 것 (배치 등록·수정 포함) |
| 409 | SCHEDULE_REQUIRED | 일정 없는 배치 활성화, 활성 배치의 일정 제거 |
| 422 | VALIDATION_ERROR | JSON 본문 오류. `errors[].loc/type/msg` |
| 삭제 | NON_CODE_CELL, INVALID_METADATA | 더 이상 쓰지 않음 |
