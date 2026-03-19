# pysmelly — Development Plan

## Current State

15 checks, zero dependencies, installable via `uvx`. 121 tests passing. See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work, [DECISIONS.md](DECISIONS.md) for design decisions.

## Next up — from deployer real-world feedback

### `pass-through-params` (upgraded)

Parameter received by function A and threaded unchanged through a call chain (A → B → C) with no use in the intermediary. Example: `environment_type` passed through 3 functions unchanged. The intermediary's signature is vestigial — the caller should pass the value directly to the function that uses it.

This was already in the ideas list but the deployer feedback provided concrete examples. Complex to implement (requires tracing params through call graph) and potentially noisy — stretch goal.

## Other potential checks

| Source | Proposed check |
|---|---|
| Refactoring history | **`param-clumps`** — detect groups of 3+ parameters that appear together in multiple function signatures. Strong signal for "extract a dataclass." Cross-file, leverages existing function index. |

Single-file checks (`stdlib-shadow`, `function-level-loggers`, `write-only-variables`, `immediately-overwritten`, `remainder-flags`) deliberately excluded — see [DECISIONS.md](DECISIONS.md). Speculative checks (`parallel-implementations`, `boolean-parameter-smell`, `stale-comments`) moved to [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md).

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
