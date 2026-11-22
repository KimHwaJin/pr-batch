# pr-batch 인수인계·운영 문서

작성일 2026-11-22 · 기준 pr-batch `main` (2026-11-13), DB revision `0005_audit_client`.
배포 플랫폼(Kubernetes) 운영은 플랫폼 팀 소관이라 제외하고, 소스코드와 서비스 관점에서 작성했다.

| 순서 | 문서 | 대상 |
| --- | --- | --- |
| 1 | [인수인계서](05-handover.md) | 인수 팀 리더. 여기서 시작 |
| 2 | [운영자 가이드](04-operator-guide.md) | 운영·당직. 주의점, 부하 지점, 장애 시 확인 위치 |
| 3 | [소스코드 설명서](01-source-code-guide.md) | 개발자 |
| 4 | [DB 논리 설계서](02-db-logical-design.md) | 개발자, 분석가 |
| 5 | [DB 물리 설계서 + ERD](03-db-physical-design.md) · [DDL](03-db-schema.sql) | DBA, 개발자, 운영자 |

ERD는 물리 설계서 2장의 Mermaid 다이어그램이다(GitLab, VS Code 등에서 그림으로 보임).
운영 환경: 사내 GitLab, 내부 SaaS 배포·DB, 로그는 내부 Kibana, 알림은 내부 메신저(CPU 급증, Pod 중단). 운영자 한 사람이 연동 서비스까지 함께 맡는다.
