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

### Checks inspired by real-world refactoring patterns

| Pattern observed | Check |
|---|---|
| Vestigial Optional params that every caller always passes the same value. | **`constant-args`** — param always receives the same literal value from every caller. Suggests the value should be a default or constant. |
| Trivial config getter functions that just returned a dict lookup or attribute access. | **`trivial-wrappers`** — functions whose body is a single return of a dict lookup, attribute access, or simple expression. Candidates for inlining. |

### Checks inspired by PYTHON.md best practices

| Best practice | Check |
|---|---|
| "Fail fast for required configuration" — `os.environ.get("KEY", "default-value")` where the default hides a missing config | **`env-fallbacks`** — detect `os.environ.get()` or `os.getenv()` calls with non-None defaults. Fail-fast principle says required config should raise, not fall back. |

### Already covered by existing checks (no new work needed)

| Pattern | Covered by |
|---|---|
| Prefixing internal-only functions with underscore | `internal-only` |
| Removing dead functions with zero callers | `dead-code` |
| Moving lazy imports to module level | Removed — covered by pylint C0415 |
| Removing compatibility fallbacks (e.g., tomllib) | `compat-shims` |
| Simplifying object construction with many name=name kwargs | `foo-equals-foo` |
| Using canonical constants and failing fast on unknown values | `suspicious-fallbacks` |

## Phase 3: Better Output for LLMs

- [x] **`--diff` mode** — Only report findings in lines changed since a git ref.
- [x] **Code context in JSON output** — Each finding includes a `source` field with the source line.
- [x] **Inline suppression** — `# pysmelly: ignore` and `# pysmelly: ignore[check-name]` comments.

## Phase 4: Real-World Feedback

Checks identified from running pysmelly on a production codebase.

- [x] **`return-none-instead-of-raise`** — Functions with mixed returns (None + value) where 2+ callers guard against None. The function should raise instead of pushing error handling to every call site. Caller-aware check in `callers.py`.
- [x] **`duplicate-except-blocks`** — Identical except handlers across files — same exception type, same error messages, same structure. Higher confidence than `duplicate-blocks` by including string literals and exception type in signature. Cross-file only (same-file handled by `duplicate-blocks`). Structural check in `structure.py`.

## Phase 5: Cross-File Parameter Checks

- [x] **`pass-through-params`** — Parameters that a function receives but only forwards to other known functions in the codebase. The intermediary's signature is vestigial — the caller should pass directly to the consumer, or a context/config object should be used. Caller-aware check in `callers.py`.
- [x] **`param-clumps`** — Groups of 3+ parameters appearing together in 3+ function signatures. Strong signal for "extract a dataclass." Broader than `build_function_index` (includes methods, private, decorated functions). Filters noise params (verbose, debug, etc.). Structural check in `structure.py`.

## Phase 6: Stdlib Alternatives

- [x] **`stdlib-alternatives`** — Shipped TOML catalog (`catalog.toml`) of 22 patterns across four categories: unconditional alternatives (urllib, xml.minidom, configparser, xmlrpc, ftplib), conditional "already using the better thing" (os.path+pathlib, unittest+pytest, logging+structlog, sqlite3+sqlalchemy, threading+concurrent.futures), deprecated stdlib removed in 3.12/3.13 (cgi, imp, distutils, telnetlib, nntplib), and deprecated third-party (pkg_resources, nose, mock, six). One finding per catalog pattern (aggregated across files). LOW severity.
- [x] **`condition_fn` support** — Catalog entries can name a Python-side function for AST-level condition checking. Used by `argparse-to-click` to only flag complex argparse usage (subcommands, mutually exclusive groups, or 5+ arguments).
