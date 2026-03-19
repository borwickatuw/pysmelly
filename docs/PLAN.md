# pysmelly — Development Plan

## Current State

Extracted from deployer's `bin/lint-review.py` into a standalone package. 12 checks implemented, zero dependencies, installable via `uvx`. Basic text and JSON output.

## Phase 1: Foundation (before first release)

### Remove checks better handled by other tools

- [ ] **Remove `lazy-imports`** — Pylint's `import-outside-toplevel` (C0415) covers this. pysmelly should focus on cross-file analysis, not single-file rules.
- [ ] **Remove `too-many-params`** — Ruff's PLR0913 and Pylint's R0913 already do this. Unless we add caller-aware context (e.g., "and 3 of 6 callers pass the same values"), this is just duplicating existing tools.
- [ ] **Evaluate `compat-shims`** — Simple pattern match; could be a Semgrep rule. Keep for now since no standard tool flags it specifically.

### Polish CLI

- [ ] **`--help` should be LLM-aware** — The epilog should explain how to use pysmelly findings in a code review workflow, what each severity means in terms of action, and list complementary tools (vulture, ruff, mypy, bandit) with what they cover. An LLM reading `--help` should understand both how to invoke the tool and what to do with the output.
- [ ] **Exit codes** — 0 = clean, 1 = findings. Consider: exit 0 for low-only findings?
- [ ] **`--min-severity`** — Filter output to only show findings at or above a severity level.
- [ ] **Relative paths in output** — Currently uses absolute paths from `Path.resolve()`. Use paths relative to the target directory for cleaner output.

### Tests

- [ ] Write tests for each check using small synthetic AST fixtures
- [ ] Test CLI (argparse, exit codes, output format selection)
- [ ] `make self-check` should pass (pysmelly analyzing itself)

## Phase 2: New Checks (informed by real refactoring history)

These are based on patterns observed in the deployer project, where Claude Code ran pysmelly and then performed the suggested refactorings. The git history shows which findings led to real improvements.

### Checks inspired by actual refactoring commits

| Pattern observed in git history | Proposed check |
|---|---|
| `def0e34` "Make vestigial Optional params required" — unused-defaults found these, but a related pattern is **params that are always the same value** across all callers. | **`constant-args`** — param always receives the same literal value from every caller. Suggests the value should be a default or constant. |
| `a390d5b` "Refactor 6 functions to accept DeploymentContext instead of 8-13 params" — too-many-params found these, but the real signal was that **the same N params were passed together** across multiple functions. | **`param-clumps`** — detect groups of 3+ parameters that appear together in multiple function signatures. Strong signal for "extract a dataclass." |
| `5547656` "Prefix internal-only functions with underscore" — direct result of `internal-only` check. | (Already covered by `internal-only`.) |
| `f3ab3d2` "Remove 6 dead functions with zero callers" — direct result of `dead-code` check. | (Already covered.) |
| `be88fa0` "Move lazy imports to module level" — direct result of `lazy-imports` check. | (Will be removed — better covered by pylint.) |
| `7babbd9` "Remove trivial config getter functions, inline at call sites" — functions that just returned a dict lookup or attribute access. | **`trivial-wrappers`** — functions whose body is a single return of a dict lookup, attribute access, or simple expression. Candidates for inlining. |
| `96728f8` "Unify aws/ecs.py to boto3, remove CLI subprocess fallbacks" — two code paths doing the same thing (subprocess vs SDK). | **`parallel-implementations`** — (hard to detect generically, but a variant: functions with the same name/signature in different files, or if/else branches that both produce the same type) |
| `b93dbb7` "Remove tomllib fallback and move lazy imports to module level" — `compat-shims` found this one. | (Already covered.) |
| `21c56ba` "Simplify ServiceMetrics construction" — `foo-equals-foo` found this. | (Already covered.) |
| `c30caa1` "Use canonical FARGATE_VALID_MEMORY, fail fast on unknown CPU value" — `suspicious-fallbacks` found this. | (Already covered.) |

### Checks inspired by PYTHON.md best practices

| Best practice | Proposed check |
|---|---|
| "Fail fast for required configuration" — `os.environ.get("KEY", "default-value")` where the default hides a missing config | **`env-fallbacks`** — detect `os.environ.get()` or `os.getenv()` calls with non-None defaults. Fail-fast principle says required config should raise, not fall back. |
| "Use modern type hints (3.10+ syntax)" — `from typing import List, Dict, Optional` | **`legacy-type-hints`** — detect imports from `typing` for types available as builtins (List, Dict, Set, Tuple, Optional, Union). |
| "Don't shadow stdlib module names" — files named `secrets.py`, `logging.py` | **`stdlib-shadow`** — detect Python files whose names shadow stdlib modules. |
| "Module-level loggers" — `logging.getLogger()` called inside functions | **`function-level-loggers`** — detect `logging.getLogger()` or `logging.basicConfig()` inside functions instead of at module level. |
| "argparse.REMAINDER swallows flags" — REMAINDER with flags defined after it | **`remainder-flags`** — detect argparse patterns where REMAINDER is used alongside flags that will be swallowed. |

### Other new check ideas

- **`write-only-variables`** — Variable assigned but never read in the same scope. Different from unused imports.
- **`pass-through-params`** — Parameter received by function A and passed unchanged to exactly one function B, with no other use in A. Suggests the caller should call B directly.
- **`boolean-parameter-smell`** — Functions with boolean parameters where the first statement is `if flag:` — suggests the function should be two functions.
- **`stale-comments`** — Comments referencing function/variable names that no longer exist in the codebase.
- **`immediately-overwritten`** — `x = "default"` immediately followed by `x = compute()`. The first assignment is dead.

## Phase 3: Better Output for LLMs

- [ ] **`--diff` mode** — Only report findings on files/functions changed in a git diff. Critical for PR review workflows where the LLM doesn't need 100 findings about the whole codebase.
- [ ] **Code context in JSON output** — Include the actual source lines for each finding so the LLM can reason about whether to fix without additional file reads.
- [ ] **Suggestion field** — Each finding includes a concrete suggestion (e.g., "Remove the `= None` default and update callers at lines X, Y, Z").
- [ ] **SARIF output** — For IDE integration (VS Code, GitHub Advanced Security).
- [ ] **Inline suppression** — `# pysmelly: ignore[check-name]` comments to acknowledge findings without removing them.

## Phase 4: Configuration

- [ ] **`pyproject.toml` support** — `[tool.pysmelly]` section for thresholds, exclusions, enabled checks.
- [ ] **Threshold overrides** — e.g., `--foo-equals-foo-threshold=5`.
- [ ] **Per-file exclusions** — `exclude = ["tests/**", "migrations/**"]`.
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
