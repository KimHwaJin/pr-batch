.PHONY: setup check lint format typecheck test migrate serve worker

setup:
	uv sync --locked

check: lint typecheck test
	uv run --locked ruff format --check .

lint:
	uv run --locked ruff check .

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

typecheck:
	uv run --locked ty check

test:
	uv run --locked pytest -q

migrate:
	uv run --locked alembic upgrade head

serve:
	uv run --locked uvicorn pr_batch.app:create_app --factory

worker:
	uv run --locked python -m pr_batch.worker
