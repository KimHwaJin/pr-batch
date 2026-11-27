# batch 공식 산출물

기준: pr-batch `main` `e71019e` (2026-11-25), DB revision `0005_audit_client`. 작성일 2026-11-27. 테이블 9개, 컬럼 145개.

| # | 산출물 | 파일 |
| --- | --- | --- |
| 4 | 논리 ERD | `pr-batch_논리ERD_v1.1.drawio` (원본), `.png`, `.pdf`, `.md`, `.pptx` |
| 5 | 물리 ERD | `pr-batch_물리ERD_v1.1.drawio` (원본), `.png`, `.pdf`, `.md`, `.pptx` |
| 6 | Table 정의서 | `pr-batch_Table정의서_v1.1.xlsx` (원본), `.pdf`, `.md`, `.pptx` |
| 8 | 운영자 매뉴얼 | `operator-manual/` |

- 모든 형식에 컬럼을 빠짐없이 적었다. 논리 ERD도 생성·수정·삭제 정보 같은 공통 속성을 묶지 않고 엔터티마다 적는다.
- `.md`의 ERD는 이미지 파일 없이 Mermaid 코드로 직접 적었고, 그 아래에 전체 컬럼 표가 있다. GitHub, GitLab, VS Code에서 그림으로 보인다.
- `.drawio` 파일은 draw.io 데스크톱, app.diagrams.net, VS Code의 Draw.io Integration 확장에서 열어 고칠 수 있다.
- 테이블·컬럼·타입·키·제약·인덱스는 `src/app/models`의 SQLAlchemy 모델에서 추출했다. 모델이 바뀌면 세 산출물을 함께 갱신한다.
- 서비스 표기는 batch, executor다. 레포와 파일 이름의 `pr-batch`는 그대로 둔다.
- VectorDB는 사용하지 않는다.
