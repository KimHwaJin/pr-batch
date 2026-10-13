# 예약 활성화와 스케줄러

등록된 일정에 맞춰 실행 이력과 Executor 제출 명령을 생성한다. 코드를 실행하는
역할은 기존 [Executor 워커](execution-api.md)가 유지한다. 예약 등록·활성화는
사용자 단위이며 Redis나 별도 외부 스케줄링 시스템은 사용하지 않는다.

## 기동

API·스케줄러·Executor 워커를 같은 DB와 설정으로 각각 실행한다.
`BATCH_EXECUTOR_BASE_URL`과 공유 PV·커널 설정은 실행 API 안내를 따른다.

```sh
uv sync --locked
make migrate
make serve
# 각각 별도 터미널/컨테이너에서 실행
make scheduler
make worker
```

스케줄러만 실행하면 예약 접수까지 처리하며 실제 제출은 워커가 담당한다.
API는 두 프로세스를 자동 실행하지 않는다. 스케줄러는 유휴 상태에서 기본 1초마다
확인한다. `BATCH_SCHEDULER_POLL_SECONDS`로 1~60초 범위에서 조절할 수 있다.
`uv run --locked python -m app.workers.scheduler --once`는 도래한 배치 하나를
처리하고 종료한다. DB 오류나 누락된 Executor 설정은 실패로 종료한다.

배치는 비활성으로 등록되며 예약 프로세스를 배포한 것만으로 활성화되지 않는다.

## 활성화·중지 API

공통 prefix는 `/api/v1/batches`다. prj_re가 기존 사용자 JWT를 전달한다.

| Method | 경로 | 기능 |
| --- | --- | --- |
| POST | /{batch_id}/enable | 미래 일정부터 예약 활성화 |
| POST | /{batch_id}/disable | 신규 예약 접수 중지 |
| GET | /{batch_id} | 활성화 상태·다음 미처리 예약·일정 미리보기 |
| GET | /{batch_id}/runs | 실행별 예약 시각과 실행 결과 |
| GET | /{batch_id}/skipped-windows | 생략한 일정의 구간·건수·사유 |

enable/disable은 `Idempotency-Key`와 `If-Match`가 필수다. 성공하면 변경된 배치와
ETag를 반환한다. 실제 활성화 상태를 바꿀 때 revision을 증가시킨다. 이미 활성화된
배치에 enable을 요청해도 예약 포인터를 초기화하지 않는다. 멱등성 키 재전송은
최초 응답을 돌려주므로 현재 상태는 GET으로 읽는다.

```sh
curl -X POST "$BATCH_URL/api/v1/batches/$BATCH_ID/enable" \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  -H "Idempotency-Key: enable-request-001" \
  -H 'If-Match: "1"'
```

첫 활성화와 재활성화는 DB의 현재 시각보다 **뒤인** 첫 유효 일정을 선택한다.
사용자가 중지한 동안의 일정은 소급 실행하지 않는다. 이미 지난 ONCE, 종료 기간이
지난 일정처럼 앞으로 실행할 시각이 없으면 422 `NO_FUTURE_OCCURRENCE`다.
일정이 없는 배치는 409 `SCHEDULE_REQUIRED`, Executor 주소가 없으면 503,
선택 커널이 허용 목록에 없으면 422로 거절한다.

중지는 이미 접수·대기·실행 중인 작업을 취소하지 않는다. 중지할 때 아직 접수하지
못한 과거 예약은 `DISABLED` 생략 구간으로 보존한다. 중지 이후의 예약은 생성하지
않으며 재활성화 시에도 복구 대상에 포함하지 않는다.

활성화 상태에서 배치 PATCH로 일정을 바꾸면 새 실행 설정(`config_number`)은 변경
시각 이후의 첫 일정부터 적용한다. 옛 설정의 아직 접수되지 않은 과거 일정은
`SCHEDULE_REPLACED` 구간으로 기록한다. 버전·커널·정책만 바꾸면 예약 포인터는
그대로이고 다음 접수부터 새 설정을 쓴다. 접수된 실행은 원래 snapshot을 유지한다.
새 일정에 미래 발생 시각이 없으면 변경 전체를 거절하고 기존 예약을 유지한다.
활성 상태에서는 일정을 제거(`schedule: null`)할 수 없다.

## 배치 응답의 예약 필드

| 필드 | 의미 |
| --- | --- |
| enabled | 사용자가 선택한 예약 활성화 상태 |
| next_run_at | 처리할 다음 예약 시각. 장애 시 과거일 수 있음 |
| preview_runs | 조회 시각 이후의 유효 일정 최대 5개. 일정이 없으면 빈 배열 |

preview_runs는 "활성화하면 돌 시각" 미리보기이며 실행 접수나 성공을 보장하지 않는다.
ONCE 처리 완료나 종료 기간 도달 후에는 enabled=true, next_run_at=null로 남는다.
enabled만 보고 앞으로 실행이 남아 있다고 판단하지 않는다.

예약 포인터 이동은 설정 revision을 증가시키지 않는다. ETag는 설정 변경 충돌을
막는 용도이며 예약 진행 상황을 새로 읽으려면 GET을 사용한다.

## 일정 계산

시간대는 Asia/Seoul이다. 응답과 DB 시각은 UTC로 정규화하며 화면에서는 Seoul로
표시한다. `start_at`은 포함하고 `end_at`은 제외한다.

| 종류 | 의미 |
| --- | --- |
| ONCE | 지정 시각 1회 |
| INTERVAL | anchor + N × seconds. N은 0 이상의 정수이며 최소 간격 60초 |
| CALENDAR DAILY | 매일 지정 시·분·초 |
| CALENDAR WEEKLY | 선택 요일의 지정 시각. 월요일=0, 일요일=6 |
| CALENDAR MONTHLY | 선택 날짜 또는 월말. 없는 날짜는 건너뜀 |
| CRON | 분·시·일·월·요일의 숫자 기반 5필드 |

Cron의 일과 요일을 모두 제한하면 OR다. 일요일은 0 또는 7이다. 초 Cron과
`L/W/#` 확장은 받지 않는다. 월말은 CALENDAR의 last_day로 표현한다.
월말과 31일을 함께 선택한 경우 같은 시각을 한 번만 생성한다.
고정 간격은 이전 실행 종료 시각에 영향을 받지 않는다.

## 누락 일정 복구

예약 시각이 `현재 시각 - policy.misfire_grace_seconds`보다 **앞이면** 누락 일정이다.
경계와 같으면 정상 접수 대상이다. 누락 일정 복구를 먼저 처리한 뒤 정상 구간을
예약 시각 순서로 접수한다.

| policy.misfire | 복구 동작 |
| --- | --- |
| SKIP | 늦은 일정 전체 생략 |
| LATEST | 늦은 일정 중 가장 최근 1개 접수 |
| CATCH_UP | 나이 제한 안의 최근 N개를 선택해 오래된 순서로 접수 |

CATCH_UP은 `catch_up_max_age_seconds`와 `catch_up_max_runs`를 모두 적용한다.
LATEST는 나이 제한을 적용하지 않는다. 정상 유예 구간의 일정은 복구 개수 N에
포함하지 않는다. 선택된 복구 실행에도 SKIP·QUEUE·PARALLEL 겹침 정책을 적용한다.
따라서 CATCH_UP으로 여러 실행을 모두 대기시키려면 QUEUE 또는 적절한 PARALLEL
설정을 사용한다. SKIP에서는 이미 점유한 실행 때문에 뒤의 복구 실행이 생략될 수 있다.
대기열 한도 초과 예약도 `SKIPPED_CAPACITY` 실행 이력으로 남긴다.

고정 코드의 보충 실행은 과거 데이터를 자동 재현하지 않는다. 노트북이 현재 시각을
읽으면 보충 실행에서도 실행 시점의 현재 시각을 읽는다. 예약 시각 파라미터 주입은
제공하지 않는다. 자동 코드 재시도도 계속 보류한다.

## 이력·복구 보장 범위

예약 실행은 `trigger=SCHEDULED`, `scheduled_for=<예약 시각>`으로 기존 실행
이력에 저장한다. 수동 실행은 `trigger=MANUAL`, scheduled_for=null이다.
`batch_id + config_number + scheduled_for`에 DB 고유 제약을 둔다.

스케줄러는 도래한 배치 행을 `FOR UPDATE SKIP LOCKED`로 잠근다. 실행 접수,
제출 명령, 생략 구간, 다음 예약 포인터를 한 트랜잭션으로 커밋한다. 실행 도중
프로세스가 중단되면 전체 롤백되고 다음 스케줄러가 같은 예약부터 처리한다.
복제 스케줄러끼리 같은 예약을 중복 접수하지 않으며 Executor 네트워크 호출은
스케줄러 트랜잭션 안에서 수행하지 않는다.

장기간 생략된 예약을 실행 행으로 모두 만들지 않는다. skipped-windows 응답은
`from_at` 포함·`until_at` 미포함 범위와 정확한 `occurrences`, `config_number`,
reason을 반환한다. 사유는 MISFIRE_SKIP, MISFIRE_LATEST, MISFIRE_CATCH_UP,
DISABLED, SCHEDULE_REPLACED다. limit 1~100, UUID cursor로 페이지를 넘긴다.
소유자가 다른 배치와 구간은 조회할 수 없다. 기존 실행 목록과 생략 구간 목록은
별도 API다.

한 트랜잭션의 정상 접수는 최대 100개다. 남은 일정은 다음 tick에서 이어간다.
CATCH_UP 복구는 설정상 최대 1,000개, 기본 10개이며 여기에 정상 접수 한도가
더해진다. 생략 건수는 고정 간격의 경우 산술식, 달력/Cron은 날짜 단위로 계산한다.
분 단위 예약을 수년간 놓쳐도 생략된 모든 실행을 메모리나 DB에 펼치지 않는다.

`/readyz`는 migration과 PV 상태만 검사한다. 스케줄러·워커 프로세스 감독,
과거 next_run_at의 체류 시간, 실행의 tracking_error 관찰은 운영 배포에서 연결해야
한다. 이 단계에서 운영 환경 부하나 실제 Executor/Jupyter까지의 종단간 실행을
검증한 것은 아니다. PostgreSQL 동시성·롤백·복구와 테스트 Executor 연결을 검증했다.
