.PHONY: test format lint self-check

test:
	uv run pytest

format:
	uv run black src/ tests/
	uv run isort src/ tests/

lint:
	uv run black --check src/ tests/
	uv run isort --check src/ tests/

self-check:
	uv run pysmelly src/
