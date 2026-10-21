.PHONY: setup check lint format typecheck test migrate serve

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
	# API + scheduler + task dispatcher in one process (as deployed).
	# Port comes from settings: BATCH_PORT in .env (8100 locally) or PORT in config.yml.
	uv run --locked pr-batch

# Docker stack; requires the Executor repo's shared-infra, executor-runtimes
# and executor Compose projects to be running first.
.PHONY: docker-up docker-down docker-smoke docker-logs docker-multi dev-token auth-keys

# Local RSA pair in .local-keys/ (kept if present): the stack verifies with
# the public half, dev-token and the smoke checks sign with the private one.
auth-keys:
	@uv run --locked python scripts/auth_keys.py

docker-up: auth-keys
	docker compose up -d --build --wait --remove-orphans app

docker-down:
	docker compose down

docker-smoke:
	docker compose --profile smoke run --rm smoke

docker-logs:
	docker compose logs -f app

# Three identical app replicas on one database (HPA-like); see
# scripts/multi_instance_check.py for the scheduling/recovery checks.
docker-multi: auth-keys
	docker compose -f compose.yaml -f compose.multi.yaml up -d --build \
		--wait --remove-orphans app app-2 app-3

# 5-minute JWT for Swagger (/docs → Authorize) against the Docker stack.
dev-token: auth-keys
	@uv run --locked python scripts/dev_token.py
