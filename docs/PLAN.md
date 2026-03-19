# pysmelly — Development Plan

## Current State

18 checks (including `stdlib-alternatives`), zero dependencies, installable via `uvx`. 186 tests passing. See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work, [DECISIONS.md](DECISIONS.md) for design decisions.

## Other potential checks

No cross-file checks currently queued. Single-file checks (`stdlib-shadow`, `function-level-loggers`, `write-only-variables`, `immediately-overwritten`, `remainder-flags`) deliberately excluded — see [DECISIONS.md](DECISIONS.md). Speculative checks, configuration ideas, and output improvements in [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md).

## Non-Goals

Things pysmelly should NOT do (use the right tool instead):

- Formatting (black, isort, ruff format)
- Single-file lint rules (ruff — reimplements 900+ rules, extremely fast)
- Type checking (mypy, pyright)
- Security scanning (bandit)
- Dead code by name-matching (vulture — faster, scope-agnostic, complements pysmelly)
- Dependency vulnerabilities (pip-audit)
- Complexity metrics (radon — pysmelly cares about *patterns*, not *numbers*)
