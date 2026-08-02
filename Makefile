.PHONY: install check test lint format audit serve demo

install:
	uv sync --all-extras --dev

check: lint test

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/auxris

format:
	uv run ruff check --fix .
	uv run ruff format .

audit:
	uv run pip-audit

serve:
	uv run auxris serve --db ./data/auxris.sqlite3

demo:
	uv run python examples/create_demo.py

