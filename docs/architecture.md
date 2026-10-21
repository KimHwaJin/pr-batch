# pr-batch 목표 아키텍처 (배포 제약 반영)

상태: 구현됨(2026-10-13). 배포 환경 답변 반영. 기능 요건은 [요구사항](requirements.md),
API·데이터 계약은 [노트북·배치 분리 설계](notebook-batch-design.md)를 따른다.

## 1. 배포 제약과 그 의미

| 제약 (2026-10-13 확인) | 설계에 주는 의미 |
| --- | --- |
| 사내 온프렘 SaaS형 k8s | 표준 Deployment·Service·Secret·PVC 범위에서 동작해야 함 |
| 1레포 1서비스 (한 레포로 Deployment 하나) | API·스케줄러·워커를 **별도 Deployment로 나눌 수 없음**. 한 이미지가 모든 역할 수행 |
| 멀티컨테이너 파드 불가 | 사이드카·별도 워커 컨테이너·(아마) migration init container 불가 |
| CPU 사용률 기반 HPA 강제 | 레플리카 수가 1..N으로 **언제든 늘고 줄어듦**. 모든 레플리카가 같은 일을 해도 안전해야 하고, 스케일 인 시 작업이 중간에 끊겨도 복구돼야 함 |

결론적으로 지금 구조(API·Executor 워커·스케줄러를 각각 다른 프로세스로 띄우는 방식)는
그대로 배포할 수 없다. 한 컨테이너 안에 프로세스 여러 개를 띄우는 방식(supervisord 등)도
헬스체크와 HPA 신호가 프로세스 하나만 반영해 장애를 숨기므로 쓰지 않는다.

참고: 같은 플랫폼에 올라가는 Executor도 API와 워커를 한 프로세스 안에서 함께 돌리고,
시작 시 자동 migration(`DB_AUTO_MIGRATE`)을 한다.

## 2. 핵심 결정

**한 이미지, 한 프로세스, 대칭형 레플리카.** 각 Pod는 FastAPI(HTTP API)와 내장
백그라운드 런타임(스케줄러 루프 + 작업 처리기)을 같은 프로세스에서 돌린다. 모든 레플리카가
똑같고 리더가 없다. 레플리카 사이의 조정은 전부 PostgreSQL 잠금으로 한다.

```mermaid
flowchart LR
    subgraph Pod["Pod (레플리카 1..N, 모두 동일)"]
        direction TB
        API["FastAPI\n/api/v1 · /healthz · /readyz"]
        subgraph RT["Background runtime (lifespan에서 시작·종료)"]
            S["Scheduler loop\n도래한 일정 → Run·Task 생성"]
            D["Task dispatcher\nTask claim → 원격 호출 → 결과 반영"]
            H["Heartbeat · lease 복구"]
        end
    end
    U["prj_re 사용자 API"] -->|JWT| API
    API --> PG[(PostgreSQL\nbatch 스키마)]
    S --> PG
    D --> PG
    API --> PV[(공유 PV · RWX)]
    D -->|제출·조회| EX["Executor"]
    D -->|리포트 요청·조회| RE["prj_re 리포트 API"]
    EX --> PV
```

| 대안 | 판단 |
| --- | --- |
| API·스케줄러·워커 별도 Deployment | 1레포 1서비스 제약으로 불가 |
| Celery/RQ + Redis 브로커 | 워커를 별도 Deployment로 띄우는 구조가 전제. 같은 프로세스에서는 이점 없이 인프라만 추가 |
| APScheduler 같은 메모리 스케줄러 | 레플리카마다 같은 일정을 중복 실행. 분산 잠금을 따로 만들어야 하고 재시작 시 상태 유실 |
| k8s CronJob | 사용자별로 동적으로 생기는 일정에 맞지 않고, 레포당 리소스 제약에도 걸림 |
| **PostgreSQL 기반 내장 작업 큐 + 스케줄러 루프 (채택)** | 추가 인프라 없음, 레플리카 수와 무관하게 정확히 한 번 접수, 장애·스케일 인 복구가 DB lease로 해결 |

## 3. 런타임 구성

`uvicorn` 워커 1개(프로세스 1개)로 실행한다. 확장은 프로세스 수가 아니라 HPA 레플리카로 한다.

| 구성 요소 | 동작 |
| --- | --- |
| HTTP API | 등록·조회·수동 실행 접수. 원격 서비스를 직접 호출하지 않고 DB에 기록만 함 |
| Scheduler loop | 1초 주기. 도래한 배치 하나를 `FOR UPDATE SKIP LOCKED`로 잡아 Run과 첫 Task를 같은 트랜잭션으로 생성. 여러 레플리카가 동시에 돌아도 같은 예약을 두 번 만들지 않음 |
| Task dispatcher | 스레드 풀(Pod당 동시성 `BATCH_TASK_CONCURRENCY`, 기본 4). Task를 lease로 claim → DB 트랜잭션 밖에서 원격 호출 1회 → fence 확인 후 결과 반영 + 다음 Task 예약 |
| Heartbeat | 루프마다 마지막 활동 시각을 메모리에 기록. liveness가 이 값을 봄 |

현재 코드의 동기 SQLAlchemy·HTTP 클라이언트는 그대로 쓰고, 백그라운드 작업은 스레드에서
돌려 API 이벤트 루프를 막지 않는다. 처리량 규모(사용자 100~200명)에서 async 전환 이득보다
복잡도가 크다.

## 4. 실행 파이프라인과 상태 머신

지금은 실행 단계가 SUBMIT→POLL→RESULT로 하드코딩되고, 상태가 dispatch·execution·result·report
문자열 4개로 흩어져 있다. 이것을 **Run 상태 하나 + 범용 Task 큐**로 바꾼다.

```mermaid
stateDiagram-v2
    [*] --> QUEUED: 접수 (자리 있음)
    [*] --> WAITING: 동시 실행 한도 초과
    [*] --> SKIPPED: 겹침·대기열 초과
    WAITING --> QUEUED: 자리 생김
    QUEUED --> RUNNING: Executor 접수
    RUNNING --> COLLECTING: Executor 종료 확인
    COLLECTING --> REPORTING: 성공 + 결과 노트북 준비
    COLLECTING --> FAILED: 코드 실행 실패
    COLLECTING --> CANCELLED: 사용자 취소
    REPORTING --> COMPLETED: 리포트 저장 완료
    REPORTING --> REPORT_FAILED: 리포트 실패
    REPORT_FAILED --> REPORTING: 리포트만 재시도 (코드 재실행 없음)
```

- 겹침 판단의 "점유"는 QUEUED·RUNNING이다(요건: 코드 실행 종료 기준). 결과 수집·리포트는 다음 실행을 막지 않는다.
- 사용자 취소는 CANCELLED로 끝나며 리포트를 만들지 않는다.
- Executor 원본 상태, 실패 유형, 리포트 상태 등 세부 값은 별도 컬럼으로 보존하되,
  사용자에게 보이는 상태와 전이 규칙은 `run.status` 하나와 한 모듈(`domain/run_state.py`)에 둔다.

Task 종류:

| kind | 원격 호출 | 다음 |
| --- | --- | --- |
| SUBMIT_EXECUTION | Executor `POST /executions` (멱등성 키 `batch-run:<run_id>:submit`) | SYNC_EXECUTION |
| SYNC_EXECUTION | Executor 상태 조회 | 진행 중이면 자신을 backoff로 재예약, 종료면 COLLECT_RESULT |
| COLLECT_RESULT | Executor 결과·Artifact 조회 | 성공이면 REQUEST_REPORT |
| REQUEST_REPORT | prj_re 리포트 작업 접수 (멱등성 키 `batch-run:<run_id>:report:<generation>`) | SYNC_REPORT |
| SYNC_REPORT | prj_re 리포트 상태 조회 | 완료·실패 반영 |

| NOTIFY (향후) | 알림 API 호출 | 없음. 리포트와 독립적으로 병렬 진행 |

`tasks` 테이블: `id, kind, run_id(선택), payload, run_at, attempts, lease_owner, lease_until, fence, last_error, status`.
claim은 `FOR UPDATE SKIP LOCKED`, 반영은 `fence` 일치 시에만 한다(늦게 끝난 옛 Pod의 쓰기 차단).
통신 오류는 지수 backoff + jitter로 재시도하고, 계약 위반은 해당 Task를 멈추고 `last_error`에 남긴다.
지금의 `run_commands`가 이 테이블로 일반화된다.

**Run 상태와 Task의 역할 분담 (트랜잭션 outbox 방식)** — 2026-10-13 결정:
- **Run 상태가 유일한 진실**이다. "지금 어디까지 왔나"는 Run만 보고 판단한다.
- 상태를 바꾸는 트랜잭션 안에서 그 결과로 해야 할 외부 호출을 Task로 함께 넣는다.
  상태 변경과 Task 생성이 같이 커밋되거나 같이 롤백되므로 둘이 어긋나지 않는다.
- Task handler는 실행 전에 Run 상태를 확인해, 이미 지난 단계의 Task(재전송·중복)면 아무것도 하지 않고 끝낸다.
- 하나의 전이가 Task 여러 개를 낼 수 있다. 예: 실행 성공 → REQUEST_REPORT + NOTIFY. 알림처럼 별도 API를
  호출하는 부가 작업은 handler 하나를 추가하는 것으로 끝나고, 본 실행 흐름은 바뀌지 않는다.
- Run과 무관한 주기 작업(고아 파일 점검 등)도 `run_id` 없는 Task로 같은 큐에 넣을 수 있다.

## 5. HPA·스케일 인/아웃 대응

**CPU HPA는 이 서비스의 실제 부하를 잘 반영하지 못한다.** 노트북 실행은 Executor가 하고,
pr-batch의 백그라운드 일은 대부분 네트워크 대기라 CPU가 거의 오르지 않는다. 그래서:

- 처리량은 레플리카 수가 아니라 **Pod당 동시성 설정과 Executor·prj_re 쪽 한도**로 관리한다.
  레플리카가 늘면 처리량도 같이 늘지만, 늘지 않아도 정상 동작하도록 설계한다.
- HPA 제안값: `minReplicas: 2`(한 Pod 장애·배포 중에도 예약 접수 유지), `maxReplicas: 4`,
  CPU 목표 70%. 업로드 검증처럼 CPU를 쓰는 API 요청이 몰릴 때만 늘어나는 것이 정상이다.
- 레플리카가 늘어도 DB 부하가 폭주하지 않게 루프는 유휴 시 sleep, 조회는 backoff·jitter를 쓰고,
  커넥션 수는 아래 10장 예산을 넘지 않게 한다.

스케일 인·롤링 배포 시 종료 순서 (SIGTERM 수신):

1. readiness를 실패로 바꿔 새 요청 유입 차단.
2. 스케줄러·dispatcher가 새 claim을 멈춤.
3. 진행 중인 원격 호출(최대 HTTP timeout 30초)을 끝내고 반영.
4. 끝내지 못한 Task는 lease 만료 후 다른 Pod가 이어받음. Executor 제출은 멱등성 키라 중복 실행이 없음.

`terminationGracePeriodSeconds: 60`, lease 90초를 기본으로 한다.

## 6. DB migration

별도 Job·init container를 쓸 수 없고, 배포된 Pod 쉘에서 스크립트를 돌리는 방식은 지양해야
하므로 **앱 시작 시 자동 migration**으로 바꾼다(현재는 앱이 migration을 하지 않는 정책).
Pod 쉘에서 `alembic upgrade head`를 수동 실행하는 것은 장애 복구용 비상 수단으로만 문서화한다.

- `BATCH_DB_AUTO_MIGRATE=true`이면 시작 시 PostgreSQL advisory lock을 잡고 `alembic upgrade head`.
  여러 Pod가 동시에 떠도 한 Pod만 수행하고 나머지는 기다림.
- 롤링 배포 중에는 옛 버전과 새 버전 Pod가 잠시 공존하므로 migration은 "추가 → 코드 전환 →
  제거" 순서(expand/contract)로 나눠, 직전 버전 코드와 호환되게 작성한다.
- migration이 끝나기 전에는 readiness가 실패한다.

## 7. 헬스체크

| 엔드포인트 | 용도 | 검사 |
| --- | --- | --- |
| `/healthz` | liveness | 프로세스 응답 + 백그라운드 루프 heartbeat가 최근(예: 60초 이내)인지. 루프가 죽었으면 재시작되게 함 |
| `/readyz` | readiness | DB 연결, migration head, PV 마운트, 종료 중 아님 |

## 8. 외부 연동

- **Executor**: **이벤트 구독 없이 조회(polling)로 시작한다.** Executor Redis 접근은 가능하지만
  별도 consumer group을 만들 분명한 사유가 아직 없다.
  - 조회 대상은 등록된 배치 수가 아니라 **지금 실행 중인 Run 수**다. 조회 간격은 5초에서 시작해
    두 배씩 늘려 최대 60초(설정 가능)로 둔다. 배치 500개가 같은 시각에 10분짜리 실행을 시작하는
    최악의 경우에도 Run당 조회는 약 13회, 평균 초당 11건 수준이다.
  - Executor 상태 조회(`GET /executions/{id}`)는 기본키 한 행을 필요한 컬럼만 읽는 비동기 핸들러다
    (`executor/src/executor_service/infrastructure/_execution_queries/executions.py`). 초당 수십 건은 부하가 아니다.
  - pr-batch가 Executor에 동시에 보내는 요청은 `Pod 수 × dispatcher 동시성`(최대 4 × 4 = 16)을 넘지 않는다.
    Run이 아무리 많아도 Executor가 받는 동시 요청은 이 상한으로 묶인다.
  - 종료 감지 지연은 최대 조회 간격(60초) + 큐 대기다. 처리 용량(Pod당 초당 수십 건)이 필요량보다 훨씬 커서
    수십 분씩 밀리는 일은 없다.
  - 이벤트 구독은 Redis 의존성, consumer group 운영, 순서·누락 복구 로직(어차피 polling이 필요)을
    추가한다. 지금 얻는 것은 종료 감지가 최대 수십 초 빨라지는 것뿐이다.
  - 도입 기준을 미리 정해 둔다: 동시 RUNNING이 수백 개를 넘어 조회가 Executor·DB 부하가 되거나,
    "종료 후 몇 초 안에 리포트 시작" 같은 지연 요건이 생기면 pr-batch 전용 consumer group으로
    `executor.events`를 구독해 조회를 깨우는 신호로만 쓴다. 이때도 polling은 누락 복구용으로 남긴다.
- **prj_re 리포트**: 비동기 접수·조회 API(`/internal/v1/execution-reports` 제안)를 호출한다.
  리포트 실패 시 사용자의 재시도 요청은 REQUEST_REPORT를 새 generation으로 다시 넣는다.
- **공유 PV**: 모든 레플리카가 같은 볼륨을 쓰므로 **ReadWriteMany** PVC를 Executor와 공유한다
  (공유 가능 확인됨). Executor 입력 루트와 같은 상대경로가 보여야 하고, 두 서비스 모두 UID 1000.

## 9. DB 커넥션 예산

자원이 넉넉하지 않으므로 **원격 호출 중에는 DB 커넥션을 잡지 않는 것**을 원칙으로 한다.
Task 하나는 "짧은 claim 트랜잭션 → 커넥션 반납 → 원격 호출 → 짧은 반영 트랜잭션"으로 처리하므로
동시에 처리 중인 Task가 많아도 실제 커넥션 사용은 순간적이다.

| 항목 | 제안값 | 근거 |
| --- | --- | --- |
| Pod당 SQLAlchemy 풀 | `pool_size=3`, `max_overflow=3` (최대 6) | API 요청 + 스케줄러 1 + dispatcher의 짧은 트랜잭션 |
| Pod당 dispatcher 동시성 | 4 | 원격 호출 대기 중에는 커넥션 미사용 |
| 레플리카 | 평소 2, 최대 4 | HPA 제안값 |
| **pr-batch 전체 상한** | **24** (4 × 6), 평소 실사용 2~6 | DB role에 `CONNECTION LIMIT 24` 설정 권장 |

효율을 위한 장치:
- 유휴 시 스케줄러는 "다음 예약 시각까지" 또는 최대 5초까지 잠들고, dispatcher는 다음 Task
  예정 시각(`min(run_at)`)까지 잠든다. 할 일이 없을 때 초당 쿼리가 거의 없다.
- claim은 한 번에 Task 여러 개(동시성 여유만큼)를 가져와 쿼리 수를 줄인다.
- `LISTEN/NOTIFY`처럼 Pod마다 커넥션을 계속 점유하는 방식은 쓰지 않는다.
- 풀 크기·동시성은 환경변수로 바꿀 수 있게 하고, 부하 측정 후 조정한다.

## 10. 인증

| 구간 | 방식 |
| --- | --- |
| 사용자 → prj_re | SSO (필수, prj_re 책임) |
| prj_re → pr-batch | **prj_re가 SSO로 확인한 사용자 ID로 pr-batch 전용 단기 JWT를 발급**해 호출(현재 구현 유지). RS256(호출 서비스가 공유 개인키로 서명, pr-batch는 공개키 파일로 검증), `aud=pr-batch`, `scope=batch:manage`, 수명 5분 이하 |
| pr-batch → Executor | 인증 없음 (내부 서비스). 내부망·NetworkPolicy로 접근 제한 |
| pr-batch → prj_re 리포트 API | **pr-batch가 서비스 토큰을 발급**해 호출. `iss=pr-batch`, `aud=prj_re-report`, 실행 소유자 ID claim 포함, 수명 5분 이하 |

prj_re의 사용자별 내부 bearer token을 pr-batch까지 그대로 넘기는 방식은 택하지 않는다.
- 예약 실행은 사용자가 접속해 있지 않은 시각에 돌기 때문에, 그때 쓸 수 있는 사용자 토큰이 없다.
  pr-batch → prj_re 리포트 호출은 사용자 토큰과 무관한 서비스 인증이어야 한다.
- 사용자 토큰을 pr-batch가 검증하려면 prj_re에 매 요청 조회하거나 서명 키를 공유해야 해서 결합이 커진다.
- 서비스 간 단기 JWT는 prj_re 쪽 변경이 작고(서명 1회), pr-batch는 키 하나로 검증한다.
  나중에 사내 표준(mTLS, OIDC client credentials 등)이 정해지면 `app/core/security.py` 경계에서 교체한다.

prj_re에 요청할 변경: ① pr-batch 호출 시 사용자 bearer token 대신 pr-batch용 단기 JWT 발급,
② 리포트 API에서 pr-batch 서비스 토큰 검증.

## 11. 코드 구조 변화

현재 `src/app` 계층은 유지하고 아래만 바꾼다.

| 위치 | 변경 |
| --- | --- |
| `app/runtime/` (신규) | 백그라운드 런타임: 루프 실행·종료, heartbeat, dispatcher 스레드 풀 |
| `app/tasks/` (신규) | Task 종류별 handler (submit, sync, collect, report) |
| `app/domain/run_state.py` (신규) | Run 상태와 전이 규칙 |
| `app/integrations/report/` (신규) | prj_re 리포트 API 클라이언트 |
| `app/workers/` | 제거 |
| `app/main.py` | lifespan에서 migration(옵션) → 런타임 시작, 종료 시 정지 |
| `deploy/k8s/` (신규) | Deployment(단일 컨테이너), Service, HPA(CPU), PVC(RWX), Secret 예시 |
| `compose.yaml` | api·worker·scheduler 3개 서비스를 `app` 1개로 통합 (운영과 같은 형태로 테스트) |

## 12. 결정 사항 (2026-10-13 답변)

| 질문 | 답변 | 반영 |
| --- | --- | --- |
| init container | 금지. Pod 쉘 수동 스크립트는 가능하지만 지양 | 앱 시작 시 advisory lock 자동 migration (6장) |
| Executor Redis | 접근 가능. 구독하려면 별도 consumer group + 분명한 사유 필요 | 초기엔 구독 안 함, 도입 기준 명시 (8장) |
| HPA min/max | 지정 가능 | min 2, max 4, CPU 70% 제안 (5장) |
| PVC 공유 | 가능 | Executor와 RWX PVC 공유 (8장) |
| DB 커넥션 | 직접 설정 가능, 자원 넉넉하지 않음 | 전체 상한 24, 원격 호출 중 커넥션 미점유 (9장) |
| 서비스 인증 | Executor 없음. prj_re는 SSO 필수, 나머지는 변경 요청 가능 | 서비스 간 단기 JWT (10장) |

## 13. 여러 인스턴스 검증 (2026-10-13)

`make docker-multi`로 같은 이미지의 app 3개를 한 DB에 띄우고(lease 30초로 단축)
`scripts/multi_instance_check.py`로 확인했다. 로컬 Executor BATCH 풀에서 실제로 실행했다.

| 시나리오 | 결과 |
| --- | --- |
| 같은 시각 ONCE 예약 30건 | 배치마다 Run 1개, Executor 실행 30개(멱등성 키 중복 0). 3개 인스턴스가 제출을 10/11/9건 분담 |
| 제출 중 lease를 쥔 인스턴스를 멈춤(`docker pause`) 후 강제 종료(SIGKILL) | 멈춘 시점에 lease 8건 보유. 30초 뒤 다른 인스턴스가 회수해 30건 모두 COMPLETED, Executor 중복 실행 0 |
| 처리 중 인스턴스 정상 종료(SIGTERM) | 1초 안에 종료("Background runtime stopped"), 남은 lease 0, 30건 모두 COMPLETED |
| 빈 스키마에서 3개 동시 기동 | 1개만 migration 수행, 나머지는 advisory lock 대기 후 정상 기동 |
