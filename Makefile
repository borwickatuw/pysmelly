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
format: ## Format code with ruff
	@uv run ruff format .

.PHONY: lint
lint: ## Check linting and formatting with ruff
	@uv run ruff check .
	@uv run ruff format --check .

.PHONY: self-check
self-check: ## Run pysmelly on itself (loads [tool.pysmelly] config from pyproject.toml)
	@uv run pysmelly .

.PHONY: pysmelly
pysmelly: self-check ## Alias for self-check (cross-repo convention)

.PHONY: check
check: lint test self-check ## All checks (lint + test + self-check)

# =============================================================================
# Security
# =============================================================================

.PHONY: security
security: security-bandit security-deps security-secrets ## Run all security checks
	@echo "=== Security Checks Complete ==="

.PHONY: security-bandit
security-bandit: ## Run bandit security linter
	@uv run bandit -c pyproject.toml -r src/ -ll

.PHONY: security-deps
security-deps: ## Check dependency vulnerabilities
	@uv run pip-audit

.PHONY: security-secrets
security-secrets: ## Scan for hardcoded secrets
	@uv tool run detect-secrets scan --baseline .secrets.baseline 2>/dev/null || \
		uv tool run detect-secrets scan > .secrets.baseline

# =============================================================================
# Documentation
# =============================================================================

.PHONY: format-docs
format-docs: ## Format markdown files
	@command -v mdformat >/dev/null 2>&1 || { echo "Error: mdformat not found. Install with: uv tool install mdformat --with mdformat-gfm"; exit 1; }
	@mdformat .

# =============================================================================
# Cleanup
# =============================================================================

.PHONY: clean
clean: ## Remove build artifacts
	@rm -rf dist build .pytest_cache .coverage htmlcov
