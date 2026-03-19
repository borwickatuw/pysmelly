.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

# =============================================================================
# Testing
# =============================================================================

.PHONY: test
test: ## Run tests
	@uv run pytest

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	@uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=70

# =============================================================================
# Code Quality
# =============================================================================

.PHONY: format
format: ## Format code with black and isort
	@uv run black src/ tests/
	@uv run isort src/ tests/

.PHONY: lint
lint: ## Check formatting without modifying
	@uv run black --check src/ tests/
	@uv run isort --check src/ tests/

.PHONY: self-check
self-check: ## Run pysmelly on itself
	@uv run pysmelly src/

.PHONY: check
check: lint test self-check ## All checks (lint + test + self-check)

# =============================================================================
# Security
# =============================================================================

.PHONY: security
security: ## Run security checks (bandit + pip-audit)
	@uv run bandit -c pyproject.toml -r src/ -ll
	@uv run pip-audit

# =============================================================================
# Cleanup
# =============================================================================

.PHONY: clean
clean: ## Remove build artifacts
	@rm -rf dist build .pytest_cache .coverage htmlcov
