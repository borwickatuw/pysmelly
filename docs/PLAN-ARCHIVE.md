# pysmelly — Completed Work

## Phase 1: Foundation

### Remove checks better handled by other tools

- [x] **Remove `lazy-imports`** — Pylint's `import-outside-toplevel` (C0415) covers this.
- [x] **Remove `too-many-params`** — Ruff's PLR0913 and Pylint's R0913 already do this.
- [x] **Evaluate `compat-shims`** — Keep it. No standard tool flags this pattern. See [DECISIONS.md](DECISIONS.md).

### Polish CLI

- [x] **`--help` should be LLM-aware** — Epilog includes severity definitions, complementary tools (vulture, ruff, pylint, mypy, bandit), exit codes, install instructions, and JSON guidance.
- [x] **`--list-checks`** — Prints each check with severity and one-line description.
- [x] **Exit codes** — 0 = clean, 1 = findings.
- [x] **`--min-severity`** — Filter output to only show findings at or above a severity level.
- [x] **Relative paths in output** — Paths are now relative to the target directory.
- [x] **`--exclude`** — Exclude files by glob pattern (pulled forward from Phase 4).
- [x] **Multiple targets** — Accept multiple directories for cross-directory analysis.
- [x] **`--version`** — Uses git describe with package metadata fallback.
- [x] **`__main__` entry point detection** — Functions called from `if __name__ == "__main__":` blocks are excluded from caller-aware checks.

### Tests

- [x] Write tests for each check using small synthetic AST fixtures
- [x] Test CLI (argparse, exit codes, output format selection)
- [x] `make self-check` should pass (pysmelly analyzing itself)

## Phase 2: New Checks

### Checks inspired by actual refactoring commits

| Pattern observed in git history | Check |
|---|---|
| `def0e34` "Make vestigial Optional params required" — params that are always the same value across all callers. | **`constant-args`** — param always receives the same literal value from every caller. Suggests the value should be a default or constant. |
| `7babbd9` "Remove trivial config getter functions, inline at call sites" — functions that just returned a dict lookup or attribute access. | **`trivial-wrappers`** — functions whose body is a single return of a dict lookup, attribute access, or simple expression. Candidates for inlining. |

### Checks inspired by PYTHON.md best practices

| Best practice | Check |
|---|---|
| "Fail fast for required configuration" — `os.environ.get("KEY", "default-value")` where the default hides a missing config | **`env-fallbacks`** — detect `os.environ.get()` or `os.getenv()` calls with non-None defaults. Fail-fast principle says required config should raise, not fall back. |

### Already covered by existing checks (no new work needed)

| Commit | Covered by |
|---|---|
| `5547656` "Prefix internal-only functions with underscore" | `internal-only` |
| `f3ab3d2` "Remove 6 dead functions with zero callers" | `dead-code` |
| `be88fa0` "Move lazy imports to module level" | Removed — covered by pylint C0415 |
| `b93dbb7` "Remove tomllib fallback and move lazy imports to module level" | `compat-shims` |
| `21c56ba` "Simplify ServiceMetrics construction" | `foo-equals-foo` |
| `c30caa1` "Use canonical FARGATE_VALID_MEMORY, fail fast on unknown CPU value" | `suspicious-fallbacks` |

## Phase 3: Better Output for LLMs

- [x] **`--diff` mode** — Only report findings in lines changed since a git ref.
- [x] **Code context in JSON output** — Each finding includes a `source` field with the source line.
- [x] **Inline suppression** — `# pysmelly: ignore` and `# pysmelly: ignore[check-name]` comments.

## Phase 4: Deployer Real-World Feedback

Checks identified from running pysmelly on a real production codebase.

- [x] **`return-none-instead-of-raise`** — Functions with mixed returns (None + value) where 2+ callers guard against None. The function should raise instead of pushing error handling to every call site. Caller-aware check in `callers.py`.
- [x] **`duplicate-except-blocks`** — Identical except handlers across files — same exception type, same error messages, same structure. Higher confidence than `duplicate-blocks` by including string literals and exception type in signature. Cross-file only (same-file handled by `duplicate-blocks`). Structural check in `structure.py`.
