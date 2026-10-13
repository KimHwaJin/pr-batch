.PHONY: setup check lint format typecheck test migrate serve worker scheduler

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

scheduler:
	uv run --locked python -m pr_batch.scheduler

# Docker stack; requires the Executor repo's shared-infra, executor-runtimes
# and executor Compose projects to be running first.
.PHONY: docker-up docker-down docker-smoke docker-logs

docker-up:
	docker compose up -d --build --wait api worker scheduler

docker-down:
	docker compose down

docker-smoke:
	docker compose --profile smoke run --rm smoke

docker-logs:
	docker compose logs -f api worker scheduler
