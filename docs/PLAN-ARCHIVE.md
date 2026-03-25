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

## Phase 7: Dead Code Extensions

Three new checks in `checks/dead.py` building on existing dead-code infrastructure.

- [x] **`dead-exceptions`** (HIGH) — Custom exception classes defined but never raised, caught, imported, subclassed, or referenced anywhere. Accumulate after error handling refactors.
- [x] **`dead-dispatch-entries`** (MEDIUM) — Entries in dispatch dicts whose key strings appear nowhere else in the codebase. Extension of `constant-dispatch-dicts`.
- [x] **`orphaned-test-helpers`** (MEDIUM) — Utility functions and unused fixtures in test files with zero callers. Pytest fixture detection via parameter name matching.

## Phase 8: Cross-file Repetition

Two new checks in `checks/repetition.py` using collect-group-flag pattern. Moved `build_parent_map` from patterns.py to helpers.py as shared utility.

- [x] **`scattered-constants`** (LOW) — Same literal value in 3+ files in assignment/comparison/subscript contexts. Filters trivial values, docstrings, raise messages, log calls, `__all__` entries. 117 findings on havoc.
- [x] **`scattered-isinstance`** (MEDIUM) — isinstance/issubclass checks for project-defined types scattered across 3+ non-test files. Skips stdlib types and ambiguously-defined classes. Anchored at class definition.

### Also completed (not in original plan)

- [x] **`--summary` flag** — Shows finding counts per check without individual findings. Sorted by severity (high first), then count descending.
- [x] **`pysmelly init`** — Creates AI review guidance file (PYSMELLY.md) + CLAUDE.md reference for Claude Code adoption.
- [x] **`temp-accumulators` consumer naming** — When an accumulator's only consumer is a dict key or attribute assignment, the message names the target (e.g., "only to populate manifest['metadata']").

## Phase 9: Architectural Smells

Two cross-file architectural checks, plus 10 new checks from the antipatterns corpus analysis.

### Phase 9a: Original architectural checks

- [x] **`inconsistent-error-handling`** (MEDIUM) — Same function called from 3+ sites with divergent error handling: some catch specific exceptions, some catch broad `Exception`, some don't catch at all. Caller-aware check in `callers.py`.
- [x] **`shared-mutable-module-state`** (MEDIUM) — Module-level mutable variables mutated from other files at module scope. Tracks star imports, direct imports, module attribute access. Covers `.append()`, `.extend()`, `[key]=`, `+=`, etc. Architectural check in `architecture.py`.
- [x] **`write-only-attributes`** (MEDIUM) — `@dataclass` fields never read anywhere in the codebase. Architectural check in `architecture.py`.
- ~~`circular-imports`~~ — Dropped. Pylint R0401 and pycycle cover this.

### Phase 9b: Antipatterns corpus checks (10 new checks)

Added from running pysmelly against the antipatterns corpus and real-world codebases:

- [x] **`arrow-code`** (LOW) — Functions with nesting depth 5+ (if/for/while/try/with pyramid). Pattern check in `patterns.py`.
- [x] **`hungarian-notation`** (LOW) — Variables using Apps Hungarian (strName, lstItems) or Systems Hungarian (szName, lpBuffer, dwFlags) prefixes. Pattern check in `patterns.py`.
- [x] **`inconsistent-returns`** (MEDIUM) — Functions returning 3+ distinct types across return paths. Pattern check in `patterns.py`.
- [x] **`plaintext-passwords`** (HIGH) — `==`/`!=` comparison on password/secret/token variables. Pattern check in `patterns.py`.
- [x] **`getattr-strings`** (MEDIUM) — `getattr(obj, 'literal')` without default or `hasattr(obj, 'literal')`. Includes cross-file shotgun surgery detection. Pattern check in `patterns.py`.
- [x] **`broken-backends`** (MEDIUM) — Non-abstract classes where every method raises NotImplementedError. Dead code check in `dead.py`.
- [x] **`temporal-coupling`** (MEDIUM) — Methods reading `self.x` only set by another non-`__init__` method. Architectural check in `architecture.py`.
- [x] **`feature-envy`** (MEDIUM) — Methods accessing 3+ attributes of another parameter, more than `self`. Architectural check in `architecture.py`.
- [x] **`anemic-domain`** (MEDIUM) — Classes with 5+ `__init__` attributes but zero non-dunder methods, with cross-file feature-envy evidence. Architectural check in `architecture.py`.
- [x] **`shotgun-surgery`** (MEDIUM) — Same `obj.attr` accessed in 4+ files. Only flags attributes defined in project classes (self.X assignments or annotations) — framework/stdlib attributes are automatically excluded. Repetition check in `repetition.py`.

### Phase 9c: Antipatterns corpus second pass (3 more checks)

- [x] **`late-binding-closures`** (HIGH) — Lambda/closure in loop captures loop variable by reference instead of value. Detects both lambdas and nested function defs; correctly handles default-arg capture pattern (x=x). Pattern check in `patterns.py`.
- [x] **`law-of-demeter`** (LOW) — Attribute chains 4+ deep (order.user.address.city). Skips fluent API method chains, stdlib module access, and AST/IR node navigation. Pattern check in `patterns.py`.
- [x] **`dict-as-dataclass`** (MEDIUM) — Functions returning dict literals with 4+ string keys. Cross-file evidence (callers accessing keys via subscript) enhances the message. Caller-aware check in `callers.py`.
- [x] **`repeated-string-parsing`** (MEDIUM) — `.split(delim)[N]` patterns in 3+ locations, or same delimiter with 3+ different indices. Detects both direct chaining and intermediate variable patterns. Repetition check in `repetition.py`.

### Phase 9d: Outscience feedback iterations

False-positive reductions based on real-world Django codebase feedback:

- [x] **`plaintext-passwords`** — Removed truthiness checks (`if SECRET_KEY:` is config presence, not secret comparison)
- [x] **`scattered-constants`** — Removed dict subscript key context (`d["deleted"]` is an API contract)
- [x] **`compat-shims`** — Skip `manage.py` (Django auto-generated boilerplate)
- [x] **`getattr-strings`** — Skip `hasattr(self, ...)` (legitimate introspection, Django reverse OneToOneField)
- [x] **`inconsistent-returns`** — Skip `@wraps`-decorated functions (decorators/middleware legitimately return different types)
- [x] **`temporal-coupling`** — Treat `setUp`/`setUpClass` as `__init__` for TestCase subclasses
- [x] **`feature-envy`** — Skip known framework hook methods; skip `request`/`response` params
- [x] **`shotgun-surgery`** — Rewritten to only flag project-defined attributes (framework/stdlib automatically excluded)

## Output pacing (`--more-please`)

- [x] Default output capped at top 10 highest-confidence findings. Ranking: severity desc, then check hit-count asc (fewer hits = higher signal). `--more-please` shows all findings. Footer: "Showing top 10. Run with --more-please for all N." Summary mode always shows full counts.

## Phase 10: Git history analysis

Mine `git log` for evolutionary signals that static analysis alone cannot
detect. Inspired by Adam Tornhill's "Your Code as a Crime Scene."

### Phase 10a: Infrastructure

- [x] `CommitInfo` dataclass and git log parser (`git_history.py`)
- [x] `GitHistory` cache class with lazy population on `AnalysisContext`
- [x] Message quality auto-detection (sample 50 commits, check for conventional prefixes)
- [x] `pysmelly git-history` subcommand with `--window`, `--commit-messages`, `--check`
- [x] `abandoned-code` (LOW) — files on disk with no commits while peers evolve

### Phase 10b: Structural checks (Tier 1 — any repo)

- [x] `blast-radius` (MEDIUM) — files whose changes drag many other files along
- [x] `change-coupling` (MEDIUM) — files that always change together with no import relationship
- [x] `growth-trajectory` (LOW) — files growing rapidly within the window
- [x] `churn-without-growth` (LOW) — many commits but stable/shrinking line count
- [x] `expected-coupling` config — suppress known coupling pairs via pyproject.toml

### Phase 10c: Semantic checks (Tier 2 — structured messages)

- [x] Commit classifier (`classify_commit()` — fix/feature/refactor/debt/emergency)
- [x] `bug-magnet` (MEDIUM) — files where majority of commits are fixes
- [x] `fix-propagation` (MEDIUM) — files that co-change in fix commits
- [x] `conscious-debt` (LOW) — commits with workaround/hack/temporary markers
- [x] `divergent-change` (MEDIUM) — one file in commits with 4+ different scopes

### Phase 10e: Churn pattern checks

- [x] `yo-yo-code` (MEDIUM) — high gross churn (write/delete/rewrite)
- [x] `fix-follows-feature` (MEDIUM) — feature→fix temporal sequences (time-slice aware)
- [x] `stabilization-failure` (LOW) — repeated activity bursts separated by gaps

### Phase 10f: Organizational checks

- [x] Author data infrastructure (`%an` in git log, `authors_for_file` index)
- [x] `knowledge-silo` (MEDIUM) — one author dominates 80%+ of commits

### Phase 10g: AST + history hybrid checks

- [x] `emergency-hotspots` (LOW) — files with disproportionate emergency/hotfix activity
- [x] `test-erosion` (LOW) — source files changing much more often than their tests
- [x] `hotspot-acceleration` (MEDIUM) — commit frequency increasing over time
- [x] `no-refactoring` (LOW) — heavy fix/feature activity but zero refactoring

### Phase 10 infrastructure

- [x] `TimeSlice` dataclass with auto-calibrated periods (~26 slices per window)
- [x] Commit granularity detection (`commits_per_slice`, `is_coarse_grained`)
- [x] `FileStats` with lazy numstat parsing
- [x] `pysmelly git-history reviewed` subcommand for acknowledging findings

### Phase 10 — dropped checks (with reasons)

- `growing-import-fan-out` — needs historical AST snapshots we don't have
- `conflict-prone` — `--no-merges` excludes merge commits; proxy unreliable
- `repeated-similar-changes` — overlaps bug-magnet + no-refactoring
- `shotgun-surgery-temporal` — weekly time slices too coarse for causal inference
- `responsibility-drift` — noisier version of divergent-change
- `same-change-multiple-files` (10d stretch) — requires per-commit diff parsing → SOMEDAY-MAYBE
- `growing-signatures` (10d stretch) — requires historical function signature parsing → SOMEDAY-MAYBE

## Phase 11: Git-History Signal-to-Noise

Validated against 10 projects (Flask, requests, FastAPI, Celery, Poetry,
sentry-python, havoc, outscience, Pydantic, Scrapy, Airflow).

### 11a: Noise reduction
- [x] knowledge-silo team gate (skip < 3 distinct authors)
- [x] blast-radius relative threshold (max(8, median_commit_size * 2.5))
- [x] abandoned-code min-lines filter (skip < 20 lines), test-file skip
- [x] abandoned-code active-majority threshold raised to 67%
- [x] Emoji conventional commit support (12 emoji→category mappings)
- [x] Bulk commit filter (30+ .py files excluded from per-file checks)

### 11b: Underperforming check improvements
- [x] divergent-change directory fallback (infer scope from co-changed dirs)
- [x] divergent-change structural dir filter + own-dir exclusion + test skip
- conscious-debt and fix-propagation: kept as-is (sound but niche)

### 11c: Multi-signal convergence
- [x] Convergence hotspots section in output (3+ checks on same file)
- [x] Hotspots shown first (before per-check details)

## Phase 12: Validation + Real-World Feedback

### 12a: Re-validation
- [x] Re-ran all 10 original projects after Phase 11 fixes
- [x] Confirmed true positives retained (sentry anthropic.py 5-check convergence)
- [x] Added Airflow (6887 files), Pydantic, Scrapy as new validation targets
- [x] conscious-debt fired for first time (10 findings on Airflow)

### 12b: Bug fixes from real-world usage (havoc Claude Code instance)
- [x] Fixed relative import detection in change-coupling
- [x] Package-level collapsing for blast-radius/change-coupling (havoc: 60→20)
- [x] `reviewed` resets analysis window for ALL git-history checks
- [x] `--all` alias for `--more-please`
- [x] Directory support for `reviewed` command
- [x] Updated PYSMELLY.md guidance for git-history findings
- [x] Clarified `reviewed` scope in docs
