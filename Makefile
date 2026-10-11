.PHONY: setup check lint format typecheck

setup:
	uv sync --locked

check: lint typecheck
	uv run --locked ruff format --check .

lint:
	uv run --locked ruff check .

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

typecheck:
	uv run --locked ty check
