# 02. 스크립트·버전 API (9개)

라우터: `src/app/api/v1/scripts.py` · 태그 `scripts` · 공통 규칙은 [00-common.md](00-common.md)

| Method | 경로 | 설명 |
| --- | --- | --- |
| POST | `/api/v1/scripts` | 노트북 업로드 (스크립트 + 버전 1 생성) |
| GET | `/api/v1/scripts` | 스크립트 목록 |
| GET | `/api/v1/scripts/{script_id}` | 스크립트 상세 |
| PATCH | `/api/v1/scripts/{script_id}` | 이름 변경 |
| DELETE | `/api/v1/scripts/{script_id}` | 삭제 (soft delete) |
| POST | `/api/v1/scripts/{script_id}/versions` | 새 버전 업로드 |
| GET | `/api/v1/scripts/{script_id}/versions` | 버전 목록 |
| GET | `/api/v1/scripts/{script_id}/versions/{version_id}` | 버전 상세 (셀 목록 포함) |
| GET | `/api/v1/scripts/{script_id}/versions/{version_id}/content` | 원본 노트북 다운로드 |

모든 요청에 `X-Api-Key`, `X-User-Id`가 필요하다. 예시의 `$BATCH_URL`은 Base URL, `$BATCH_API_KEY`는 호출 서비스 key,
`$USER_ID`는 대신하는 사용자다.

## 응답 모델

### ScriptView

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | UUID | 스크립트 ID |
| `name` | string | 이름 (1~255자) |
| `type` | `NOTEBOOK` | 스크립트 타입 |
| `revision` | int | `If-Match`에 쓰는 값. 이름 변경·새 버전 생성·삭제 때 오른다 |
| `latest_version` | VersionView | 가장 최근 버전 |
| 감사 필드 | | [공통 응답 필드](00-common.md#7-공통-응답-필드) |

### VersionView

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | UUID | 버전 ID |
| `script_id` | UUID | 스크립트 ID |
| `version_number` | int | 1부터 오르는 번호. 배치의 `script_version_number`에 쓴다 |
| `filename` | string | 업로드한 파일명 |
| `sha256` | string | 원본 바이트의 SHA-256 |
| `code_hash` | string | 실행 코드 셀 내용의 해시 (마크다운만 바뀌면 같다) |
| `size_bytes` | int | 원본 크기 |
| `cells.total` / `cells.executable` / `cells.skipped` | int | 전체 셀 / 실행할 코드 셀 / 실행하지 않는 셀(마크다운·raw·빈 코드 셀) |

### VersionDetail

VersionView에 `cell_list`가 추가된다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `cell_list[].index` | int | 원본 노트북의 셀 위치 (0부터) |
| `cell_list[].type` | string | `code`, `markdown`, `raw` |
| `cell_list[].executed` | bool | 실행 단위(step)인지 |
| `cell_list[].sequence` | int \| null | 실행 순서 (0부터). 실행하지 않는 셀은 `null` |

## 노트북 검사 규칙

업로드와 새 버전 업로드에 같은 검사가 적용된다. 기본값은 서버 설정으로 바뀔 수 있다.

| 규칙 | 기본 한도 | 위반 시 |
| --- | --- | --- |
| 파일명이 `.ipynb`로 끝남 | — | 422 `INVALID_NOTEBOOK` |
| 파일 크기 | 10 MiB (`UPLOAD_MAX_BYTES`) | 413 `UPLOAD_TOO_LARGE` (요청 전체가 크면 `REQUEST_TOO_LARGE`) |
| nbformat v4 JSON, 스키마 유효 | — | 422 `INVALID_NOTEBOOK` |
| 전체 셀 수 (마크다운·raw 포함) | 1,000 (`MAX_NOTEBOOK_CELLS`) | 422 `TOO_MANY_CELLS` (`type: notebook_cell_limit_exceeded`) |
| 코드 셀 하나 크기 | 1 MiB (`CELL_MAX_BYTES`) | 422 `CELL_TOO_LARGE` (`location: ["body","file","cells",<index>]`) |
| 비어 있지 않은 코드 셀 수 | 100 (`MAX_CODE_CELLS`, Executor 한도) | 422 `TOO_MANY_CELLS` (`type: code_cell_limit_exceeded`) |
| 실행할 코드 셀이 1개 이상 | — | 422 `EMPTY_NOTEBOOK` |
| `language_info.name`·`kernelspec.language`가 있으면 `python` | — | 422 `UNSUPPORTED_LANGUAGE` |

공백만 있는 코드 셀은 실행하지 않는다(`executed: false`). 원본 바이트는 그대로 보관한다.

---

## POST /api/v1/scripts — 업로드

노트북을 올려 스크립트와 버전 1을 만든다.

### 요청

`multipart/form-data`.

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | 요청마다 새 값 |
| 폼 | `file` | O | `.ipynb` 파일 |
| 폼 | `type` | O | `NOTEBOOK` |
| 폼 | `name` | X | 1~255자, 제어문자 불가, 앞뒤 공백 제거. 생략하면 파일명에서 확장자를 뺀 값 |

```sh
curl "$BATCH_URL/api/v1/scripts" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'file=@daily.ipynb' -F 'type=NOTEBOOK' -F 'name=생산라인 일일 분석'
```

### 응답 201 — `ScriptView`

헤더 `Location: /api/v1/scripts/{id}`, `ETag: "1"`.

```json
{
  "id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
  "name": "생산라인 일일 분석",
  "type": "NOTEBOOK",
  "revision": 1,
  "latest_version": {
    "id": "0b9a7e52-2f4d-4c33-8a51-6f0e9d2c7b10",
    "script_id": "8d3c2f0e-6c1b-4a8e-9f6e-2d7b1c0a4e11",
    "version_number": 1,
    "filename": "daily.ipynb",
    "sha256": "9f86d08…",
    "code_hash": "2c26b46…",
    "size_bytes": 18432,
    "cells": {"total": 12, "executable": 8, "skipped": 4}
  }
}
```

### 오류

[노트북 검사 규칙](#노트북-검사-규칙)의 오류와 다음.

| HTTP | code | 의미 |
| --- | --- | --- |
| 422 | `VALIDATION_ERROR` | `file`·`type` 누락, `type`이 `NOTEBOOK`이 아님, `name` 형식 오류 |
| 503 | `STORAGE_UNAVAILABLE` | 저장소 장애. 같은 키로 재시도 |

---

## GET /api/v1/scripts — 목록

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 쿼리 | `limit` | X | 1~100, 기본 50 |
| 쿼리 | `cursor` | X | 이전 응답의 `next_cursor` |

응답 200 `{"items": [ScriptView, ...], "next_cursor": string | null}`. 최신순. 삭제된 스크립트는 나오지 않는다.

```sh
curl "$BATCH_URL/api/v1/scripts?limit=20" -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

---

## GET /api/v1/scripts/{script_id} — 상세

응답 200 `ScriptView`, 헤더 `ETag: "<revision>"`. 이름 변경·삭제 전에 호출해 `ETag`를 얻는다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 없음, 다른 tenant·사용자, 삭제됨 |
| 422 | `VALIDATION_ERROR` | `script_id`가 UUID가 아님 |

---

## PATCH /api/v1/scripts/{script_id} — 이름 변경

### 요청

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | |
| 헤더 | `If-Match` | O | 상세의 `ETag` |
| 본문 (JSON) | `name` | O | 1~255자. 다른 필드는 422 |

```sh
curl -X PATCH "$BATCH_URL/api/v1/scripts/$SCRIPT_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: "1"' \
  -H 'Content-Type: application/json' \
  -d '{"name": "A라인 일일 분석"}'
```

### 응답 200 — `ScriptView`

변경된 ScriptView와 새 `ETag`. 이름은 배치·실행에 영향을 주지 않는다.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | |
| 409 | `REVISION_CONFLICT` | 그 사이 바뀜. 다시 GET |
| 422 | `INVALID_REVISION`, `VALIDATION_ERROR` | `If-Match` 형식 오류·누락, `name` 오류 |

---

## DELETE /api/v1/scripts/{script_id} — 삭제

soft delete이며 되살리는 API는 없다. 삭제 후 스크립트·버전 조회는 404다.

| 구분 | 이름 | 필수 |
| --- | --- | --- |
| 헤더 | `Idempotency-Key` | O |
| 헤더 | `If-Match` | O |

```sh
curl -X DELETE "$BATCH_URL/api/v1/scripts/$SCRIPT_ID" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" -H 'If-Match: "2"'
```

응답 204, 본문 없음.

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | |
| 409 | `REVISION_CONFLICT` | |
| 409 | `SCRIPT_IN_USE` | 삭제되지 않은 배치가 이 스크립트를 쓰고 있음. 배치를 지우거나 다른 스크립트로 바꾼다 |
| 409 | `SCRIPT_HAS_ACTIVE_RUNS` | 이 스크립트로 끝나지 않은 실행이 있음 |

---

## POST /api/v1/scripts/{script_id}/versions — 새 버전 업로드

같은 스크립트에 노트북을 새 버전으로 올린다. 스크립트의 `type`으로 파싱한다.

### 요청

`multipart/form-data`. `If-Match`는 필요 없다.

| 구분 | 이름 | 필수 | 값 |
| --- | --- | --- | --- |
| 헤더 | `Idempotency-Key` | O | |
| 폼 | `file` | O | `.ipynb` 파일 |

```sh
curl "$BATCH_URL/api/v1/scripts/$SCRIPT_ID/versions" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F 'file=@daily.ipynb'
```

### 응답

| HTTP | 경우 | 본문 |
| --- | --- | --- |
| 201 | 새 버전 생성. `Location: /api/v1/scripts/{script_id}/versions/{id}` | VersionView |
| 200 | 이미 올린 어떤 버전과 바이트가 같음. 새로 만들지 않음 | 그 기존 VersionView |

- 기존 배치는 새 버전을 자동으로 따라가지 않는다. 배치 PATCH로 `script_version_number`를 바꾸거나
  `script_version_number: null`로 최신에 다시 고정한다.
- 201이면 스크립트의 `revision`이 1 오른다(`latest_version`이 바뀜). 이름 변경·삭제 전에는 상세를 다시 읽어 새 `ETag`를 쓴다. 200이면 바뀌지 않는다.

오류는 업로드와 같고 404 `NOT_FOUND`(스크립트 없음)가 추가된다.

---

## GET /api/v1/scripts/{script_id}/versions — 버전 목록

쿼리 `limit`, `cursor`. 응답 200 `{"items": [VersionView, ...], "next_cursor": ...}`. 버전 번호 역순.

---

## GET /api/v1/scripts/{script_id}/versions/{version_id} — 버전 상세

응답 200 `VersionDetail`.

```json
{
  "id": "0b9a7e52-…", "script_id": "8d3c2f0e-…", "version_number": 1,
  "filename": "daily.ipynb", "sha256": "…", "code_hash": "…", "size_bytes": 2048,
  "cells": {"total": 3, "executable": 2, "skipped": 1},
  "cell_list": [
    {"index": 0, "type": "markdown", "executed": false, "sequence": null},
    {"index": 1, "type": "code", "executed": true, "sequence": 0},
    {"index": 2, "type": "code", "executed": true, "sequence": 1}
  ]
}
```

실행 상세의 `steps[].sequence`·`original_index`가 여기의 `sequence`·`index`와 대응한다.

---

## GET /api/v1/scripts/{script_id}/versions/{version_id}/content — 원본 다운로드

업로드한 원본 바이트를 그대로 돌려준다.

```sh
curl -o daily.ipynb "$BATCH_URL/api/v1/scripts/$SCRIPT_ID/versions/$VERSION_ID/content" \
  -H "X-Api-Key: $BATCH_API_KEY" -H "X-User-Id: $USER_ID"
```

응답 200 헤더:

| 헤더 | 값 |
| --- | --- |
| `Content-Type` | `application/x-ipynb+json` |
| `Content-Disposition` | `attachment; filename="{version_id}.ipynb"` |
| `Cache-Control` | `private, no-store` |
| `X-Content-Type-Options` | `nosniff` |

| HTTP | code | 의미 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 스크립트·버전 없음 |
| 503 | `STORAGE_UNAVAILABLE` | 저장소 장애 |
| 503 | `STORAGE_INTEGRITY_ERROR` | 저장된 파일의 해시가 맞지 않음. pr-batch 담당자에게 알림 |
