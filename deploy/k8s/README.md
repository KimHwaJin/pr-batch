# Kubernetes 배포

플랫폼 제약(1레포 1Deployment, 단일 컨테이너 파드, CPU HPA)에 맞춘 매니페스트다.
설계 근거는 [목표 아키텍처](../../docs/architecture.md).

| 파일 | 내용 |
| --- | --- |
| deployment.yaml | 컨테이너 1개가 API·스케줄러·Task dispatcher를 함께 실행 |
| service.yaml | 내부 Service (`prj_re`가 호출) |
| hpa.yaml | CPU 70%, min 1, max 4 |
| pdb.yaml | 노드 drain 등 자발적 중단 시 한 번에 Pod 하나만 내림 (`maxUnavailable: 1`, Pod 1개여도 drain을 막지 않음) |

- DB 역할 준비(한 번): `CREATE ROLE batch LOGIN PASSWORD '…' CONNECTION LIMIT 40;` (Pod당 풀 5 + 5, 최대 4 Pod)
  `CREATE DATABASE batch OWNER batch;` 스키마와 테이블은 앱이 시작할 때 만든다.
- 공유 PV는 Executor와 같은 RWX PVC(`executor-shared`)를 `/mnt/data`에 마운트한다. 이름은 환경에 맞게 바꾼다.
  `storage_root`는 프로젝트 루트(`/mnt/data/dtest`)이고 파일은 `batch/inputs/` 아래 저장한다.
  Executor는 `INPUT_STORAGE_ROOT=/mnt/data/dtest`, `SHARED_STORAGE_ROOT=/mnt/data/dtest/executor`.
- 설정은 ConfigMap·Secret 없이 이미지에 들어간 `config.{APP_ENV}.yml`로 준다. 저장소 루트의
  `config.prd.yml` 등에 계별 값을, `config.yml`에 공통값을 채우고, Deployment는 `APP_ENV`만 지정한다.
- 컨테이너는 설정의 `PORT`(기본 8000)로 뜬다. `config.yml`에서 바꾸면 `deployment.yaml`의
  `containerPort`도 같이 바꾼다(Service는 이름 `http`로 가리키므로 그대로 둔다).
- Pod 종료: `preStop`에서 5초 기다려 Service에서 빠진 뒤 SIGTERM을 받고, 진행 중인 호출을 40초 안에 마치고 끝낸다.
  플랫폼이 PDB를 받지 않으면 `pdb.yaml`은 빼도 된다.
- 배포 순서: Deployment·Service → HPA·PDB. 첫 Pod가 advisory lock을 잡고 migration을
  수행하는 동안 다른 Pod는 startupProbe에서 기다린다.
- migration은 롤링 배포 중 직전 버전과 호환되게(추가 → 전환 → 제거) 작성한다.
