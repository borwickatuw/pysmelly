# pysmelly — Development Plan

## Current State

13 checks, zero dependencies, installable via `uvx`. 76 tests passing. See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work, [DECISIONS.md](DECISIONS.md) for design decisions.

## Potential new checks

| Source | Proposed check |
|---|---|
| Refactoring history | **`param-clumps`** — detect groups of 3+ parameters that appear together in multiple function signatures. Strong signal for "extract a dataclass." |
| Refactoring history | **`parallel-implementations`** — (hard to detect generically, but a variant: functions with the same name/signature in different files, or if/else branches that both produce the same type) |
| PYTHON.md | **`stdlib-shadow`** — detect Python files whose names shadow stdlib modules. |
| PYTHON.md | **`function-level-loggers`** — detect `logging.getLogger()` or `logging.basicConfig()` inside functions instead of at module level. |
| PYTHON.md | **`remainder-flags`** — detect argparse patterns where REMAINDER is used alongside flags that will be swallowed. |
| Ideas | **`write-only-variables`** — Variable assigned but never read in the same scope. Different from unused imports. |
| Ideas | **`pass-through-params`** — Parameter received by function A and passed unchanged to exactly one function B, with no other use in A. Suggests the caller should call B directly. |
| Ideas | **`boolean-parameter-smell`** — Functions with boolean parameters where the first statement is `if flag:` — suggests the function should be two functions. |
| Ideas | **`stale-comments`** — Comments referencing function/variable names that no longer exist in the codebase. |
| Ideas | **`immediately-overwritten`** — `x = "default"` immediately followed by `x = compute()`. The first assignment is dead. |

## Better Output for LLMs

- [ ] **Suggestion field** — Each finding includes a concrete suggestion (e.g., "Remove the `= None` default and update callers at lines X, Y, Z").
- [ ] **SARIF output** — For IDE integration (VS Code, GitHub Advanced Security).

## Configuration

- [ ] **`pyproject.toml` support** — `[tool.pysmelly]` section for thresholds, exclusions, enabled checks.
- [ ] **Threshold overrides** — e.g., `--foo-equals-foo-threshold=5`.
- [ ] **Entry-point plugins** — Allow third-party packages to register checks via `[project.entry-points."pysmelly.checks"]`.

## Non-Goals

Things pysmelly should NOT do (use the right tool instead):

- Formatting (black, isort, ruff format)
- Single-file lint rules (ruff — reimplements 900+ rules, extremely fast)
- Type checking (mypy, pyright)
- Security scanning (bandit)
- Dead code by name-matching (vulture — faster, scope-agnostic, complements pysmelly)
- Dependency vulnerabilities (pip-audit)
- Complexity metrics (radon — pysmelly cares about *patterns*, not *numbers*)
