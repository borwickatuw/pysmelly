.PHONY: test test-cov format lint security self-check

test:
	uv run pytest

test-cov:
	uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=70

format:
	uv run black src/ tests/
	uv run isort src/ tests/

lint:
	uv run black --check src/ tests/
	uv run isort --check src/ tests/

security:
	uv run bandit -c pyproject.toml -r src/ -ll
	uv run pip-audit

self-check:
	uv run pysmelly src/
