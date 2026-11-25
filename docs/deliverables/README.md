# pr-batch 공식 산출물

기준: pr-batch `main` 병합 커밋 `7fad2c8` (2026-11-22), DB revision `0005_audit_client`. 작성일 2026-11-25.

| # | 산출물 | 파일 |
| --- | --- | --- |
| 4 | 논리 ERD | `pr-batch_논리ERD_v1.0.drawio` (원본), `.png`, `.pdf` |
| 5 | 물리 ERD | `pr-batch_물리ERD_v1.0.drawio` (원본), `.png`, `.pdf` |
| 6 | Table 정의서 | `pr-batch_Table정의서_v1.0.xlsx` (원본), `.pdf` |

- `.drawio` 파일은 draw.io 데스크톱, app.diagrams.net, VS Code의 Draw.io Integration 확장에서 열어 고칠 수 있다.
- 테이블·컬럼·타입·키·제약·인덱스는 `src/app/models`의 SQLAlchemy 모델에서 추출했다. 모델이 바뀌면 세 산출물을 함께 갱신한다.
- VectorDB는 사용하지 않는다.
