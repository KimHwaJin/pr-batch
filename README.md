# pr-batch

Python 3.12 기반 배치 서비스 프로젝트입니다.
현재는 개발 환경만 구성했으며, 서비스 구현은 요구사항 인터뷰 후 시작합니다.

## 서비스 설계

인터뷰에서 합의한 범위와 구현 전 검토할 설계를 관리합니다.

- [확정 요구사항](docs/requirements.md)
- [API·데이터 모델·상태 흐름 설계 초안](docs/design.md)
- [Executor·prj_re 연동 계약과 확인 사항](docs/integrations.md)

설계 초안의 API와 세부 정책은 검토 제안이며 아직 구현되지 않았습니다.

## 개발 도구

- Python 3.12 (`>=3.12,<3.13`)
- uv: 가상환경 및 의존성 관리
- Ruff: 린트, import 정렬, 코드 포맷
- ty: 정적 타입 검사

설정은 `pyproject.toml`, Python 버전은 `.python-version`에서 관리합니다.
의존성의 실제 설치 버전은 `uv.lock`에 고정하고 함께 커밋합니다.
CI는 uv 0.11.32를 사용합니다.

## 시작하기

[uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라
uv를 설치한 뒤 저장소 루트에서 실행합니다.

```sh
uv python install
uv sync --locked
```

프로젝트 전용 `.venv`가 생성됩니다. `uv run`을 사용하면 가상환경을 별도로
활성화하지 않아도 됩니다. 현재 서비스 실행 명령은 없습니다.

## 품질 검사

```sh
make check
```

Make가 없는 환경에서는 아래 명령을 직접 실행합니다.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
```

자동 수정과 포맷 적용은 `make format`으로 실행합니다.
GitHub Actions에서도 push와 pull request마다 동일한 검사를 수행합니다.
현재 검사는 환경 및 기본 패키지 틀에 대한 검사이며, 서비스 동작 테스트는
구현과 함께 추가합니다.

## 의존성 변경

```sh
uv add <package>
uv add --dev <tool>
```

변경된 `pyproject.toml`과 `uv.lock`을 함께 커밋합니다.
`.venv`, 도구 캐시, `.env` 등 로컬 환경 파일은 커밋하지 않습니다.

## Git 작업 흐름

원격 저장소: [KimHwaJin/pr-batch](https://github.com/KimHwaJin/pr-batch)

```sh
# 작업 시작 전, 작업 트리가 깨끗한 상태에서 원격 변경 반영
git pull --ff-only

# 변경 후 검사 및 동기화
make check
git add <changed-files>
git commit -m "Describe the change"
git push
```

원격 이력이 분기되어 pull이 실패하면 차이를 확인한 뒤 통합합니다.
서비스 목적, 배치 실행 방식, 데이터 입출력, 재시도 및 운영 요구사항을
인터뷰에서 정한 후 프레임워크와 구현 구조를 결정합니다.

## 설정 참고

- [uv 프로젝트 관리](https://docs.astral.sh/uv/guides/projects/)
- [Ruff 설정](https://docs.astral.sh/ruff/configuration/)
- [ty 설정](https://docs.astral.sh/ty/reference/configuration/)
- [uv GitHub Actions 연동](https://docs.astral.sh/uv/guides/integration/github/)
