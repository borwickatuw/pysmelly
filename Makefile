# pysmelly Makefile
#
# Usage: make [target]
# Run `make` or `make help` to see available targets.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

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
	@uv audit

.PHONY: security-updates
security-updates: ## CVE scan + outdated package report
	@echo "=== CVE + adverse-status scan ==="
	@uv audit
	@echo ""
	@echo "=== Outdated packages ==="
	@uv pip list --outdated

.PHONY: security-secrets
security-secrets: ## Check tracked files for secrets not in .secrets.baseline
	@test -f .secrets.baseline || { echo "Error: .secrets.baseline missing. Bootstrap with 'make security-secrets-init' and review before committing."; exit 1; }
	@uv run detect-secrets-hook --baseline .secrets.baseline $$(git ls-files)

.PHONY: security-secrets-init
security-secrets-init: ## Bootstrap/regenerate .secrets.baseline (review the diff before committing)
	@uv run detect-secrets scan > .secrets.baseline

# =============================================================================
# Documentation
# =============================================================================

.PHONY: format-docs
format-docs: ## Format markdown files
	@command -v mdformat >/dev/null 2>&1 || { echo "Error: mdformat not found. Install with: uv tool install mdformat --with mdformat-gfm"; exit 1; }
	@git ls-files -coz --exclude-standard '*.md' | xargs -0 mdformat

.PHONY: format-docs-check
format-docs-check: ## Check markdown formatting without modifying
	@command -v mdformat >/dev/null 2>&1 || { echo "Error: mdformat not found. Install with: uv tool install mdformat --with mdformat-gfm"; exit 1; }
	@git ls-files -coz --exclude-standard '*.md' | xargs -0 mdformat --check

# =============================================================================
# Cleanup
# =============================================================================

.PHONY: clean
clean: ## Remove build artifacts
	@rm -rf dist build .pytest_cache .coverage htmlcov
