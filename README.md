# pysmelly

AST-based Python code smell detector for AI-assisted code review.

pysmelly finds **vestigial code patterns** — code that outlived the design that created it. It performs cross-file call-graph analysis to detect smells that single-file linters miss, then reports findings as **investigation pointers** — signals that tell an AI reviewer (like [Claude Code](https://docs.anthropic.com/en/docs/claude-code)) or a human where to look, not what to do. Findings include cross-file context (caller counts, blast radius) so the reviewer can apply judgment.

## Install

```bash
# Run directly (no install)
uvx --from git+https://github.com/borwickatuw/pysmelly pysmelly

# Or install as a tool
uv tool install git+https://github.com/borwickatuw/pysmelly

# Or pip
pip install git+https://github.com/borwickatuw/pysmelly
```

## Usage

```bash
# Analyze current directory
pysmelly

# Analyze a specific directory
pysmelly src/

# Run a single check
pysmelly --check unused-defaults

# Skip specific checks
pysmelly --skip single-call-site --skip trivial-wrappers

# Only show findings in changed lines
pysmelly --diff HEAD

# Exclude test files
pysmelly --exclude 'test_*' --exclude 'tests/'

# Summary counts by check (no individual findings)
pysmelly --summary

# Verbose output
pysmelly -v

# Set up AI review guidance for a project
pysmelly init
```

## Checks

### High severity — act on or justify

| Check | What it finds |
|---|---|
| `unused-defaults` | Parameter defaults to `None` but every caller always passes a value. The `Optional` is vestigial — make the param required. |
| `dead-code` | Public functions with zero callers anywhere in the codebase. Cross-references direct calls, imports, dict/list references, and callback passing. |
| `dead-exceptions` | Custom exception classes never raised or caught anywhere. |
| `compat-shims` | `try/except ImportError` patterns left over from supporting older Python versions the project no longer targets. |
| `suspicious-fallbacks` | `.get()` on module-level constant dicts with non-trivial defaults. If the key should always exist, use `[]` indexing and fail fast. |
| `env-fallbacks` | `os.environ.get()` or `os.getenv()` with non-None defaults. Required config should fail fast, not silently fall back. |
| `unreachable-after-return` | Code after `return`/`raise` or exhaustive `if/else` branches — dead tail code from refactoring. |
| `plaintext-passwords` | `==`/`!=` comparison on password/secret/token variables — use `hmac.compare_digest()` or hash comparison. |

### Medium severity — review each, fix what makes sense

| Check | What it finds |
|---|---|
| `constant-args` | Parameter always receives the same literal value from every caller. The value should be a default or constant. |
| `vestigial-params` | Parameters declared but never referenced in the function body — with cross-file caller count showing blast radius. |
| `foo-equals-foo` | Single-use locals gathered into an object — suggests bundling into a dataclass or building directly. |
| `duplicate-blocks` | Structurally identical code blocks across functions (AST-normalized, so different variable names still match). |
| `duplicate-except-blocks` | Identical except handlers with same error messages across files. |
| `temp-accumulators` | `parts = []; parts.append(...); join(parts)` patterns replaceable with comprehensions. |
| `constant-dispatch-dicts` | Module-level `{"name": func, ...}` tables that can get out of sync — consider decorator registration. |
| `return-none-instead-of-raise` | Functions returning `None` on error where callers all guard against `None`. The function should raise instead. |
| `pass-through-params` | Parameters received by a function and only forwarded to another function. The intermediary's signature is vestigial. |
| `param-clumps` | Groups of 3+ parameters appearing together in 3+ function signatures — extract a dataclass. |
| `runtime-monkey-patch` | Function assigned to attribute of external object at module scope. |
| `fossilized-toggles` | UPPER_CASE boolean constants that make conditionals always-true/false (dead branches). |
| `dead-constants` | UPPER_CASE module-level constants never referenced anywhere — e.g. event name constants nobody uses. |
| `dead-abstraction` | ABCs with zero concrete implementations — speculative generality that never materialized. |
| `dead-dispatch-entries` | Dispatch dict entries whose key strings appear nowhere else in the codebase. |
| `middle-man` | Classes where 75%+ of methods just delegate to a single wrapped object. |
| `write-only-attributes` | `@dataclass` fields never read anywhere in the codebase — vestigial config accretion. |
| `isinstance-chain` | Functions with 5+ `isinstance()` checks — investigate for polymorphism or dispatch table. |
| `boolean-param-explosion` | Functions with 4+ boolean parameters — accumulated flags suggesting decomposition. |
| `exception-flow-control` | Custom exceptions raised and caught in the same `try/except` — used as goto, not error handling. |
| `inconsistent-error-handling` | Same function called with divergent error handling across callers. |
| `shared-mutable-module-state` | Module-level mutable containers mutated from other files at import time. |
| `orphaned-test-helpers` | Test helper functions and unused fixtures with zero callers. |
| `shadowed-method` | Diamond inheritance where multiple parents define the same method — MRO silently picks one. |
| `broken-backends` | Non-abstract classes where every method raises `NotImplementedError` — missing ABC base or broken backend. |
| `inconsistent-returns` | Functions returning 3+ distinct types across return paths — consider narrowing the return type. |
| `getattr-strings` | `getattr(obj, 'literal')` without default or `hasattr(obj, 'literal')` — stringly-typed attribute access. |
| `temporal-coupling` | Methods reading `self.x` only set by another non-`__init__` method — implicit call ordering. |
| `feature-envy` | Methods accessing 3+ attributes of another parameter, more than `self` — logic belongs elsewhere. |
| `anemic-domain` | Classes with 5+ `__init__` attributes but zero non-dunder methods — data bag with no behavior. |
| `shotgun-surgery` | Same `obj.attr` accessed in 4+ files — changes to that attribute require updating many files. |

### Low severity — informational

| Check | What it finds |
|---|---|
| `single-call-site` | Short public functions called exactly once — candidates for inlining. |
| `internal-only` | Public functions only called within their own file — candidates for `_private` naming. |
| `trivial-wrappers` | Functions whose body is a single return statement — candidates for inlining. |
| `stdlib-alternatives` | Stdlib modules where well-known third-party libraries are better, deprecated stdlib/third-party modules, and mixed stdlib/modern usage in the same codebase. |
| `scattered-constants` | Same string literal repeated across 3+ files — consider a named constant. |
| `scattered-isinstance` | Same `isinstance` type-check pattern repeated across 3+ files. |
| `large-class` | Classes with 20+ methods — review for single responsibility. |
| `long-function` | Functions spanning 100+ lines — review for decomposition. |
| `long-elif-chain` | 8+ branch if/elif chains comparing the same variable to literals — consider a dict or enum. |
| `arrow-code` | Functions with nesting depth 5+ (if/for/while/try/with pyramid) — consider extracting inner blocks. |
| `hungarian-notation` | Variables like `strName`, `intCount`, `lstItems` — use snake_case instead. |

## Output

Text output, grouped by check, one line per finding:

```
=== unused-defaults (2 finding(s)) ===
  src/myapp/deploy.py:45: deploy() param 'timeout' defaults to None but all 3 caller(s) always pass it
  src/myapp/config.py:12: load_config() param 'env' defaults to None but all 5 caller(s) always pass it
```

Output includes a guidance preamble for LLM consumers. Use `--no-context` to suppress it.

## What pysmelly is NOT

pysmelly intentionally does **not** cover:

- **Formatting** — use [black](https://github.com/psf/black) and [isort](https://github.com/PyCQA/isort)
- **Single-file lint rules** — use [ruff](https://github.com/astral-sh/ruff) (reimplements 900+ flake8/pylint rules, extremely fast)
- **Type checking** — use [mypy](https://github.com/python/mypy) or [pyright](https://github.com/microsoft/pyright)
- **Security** — use [bandit](https://github.com/PyCQA/bandit)
- **Dead code by name-matching** — use [vulture](https://github.com/jendrikseipp/vulture) (faster, scope-agnostic approach that complements pysmelly's call-graph analysis)
- **Dependency vulnerabilities** — use `pip-audit`

pysmelly focuses on the gap: **cross-file call-graph analysis** and **design-level code smells** that require understanding how functions are actually used across a codebase.

## Using with Claude Code

pysmelly is designed as an **investigation dispatcher** for AI code review. Its findings are starting points — signals that tell Claude Code where to look and what to investigate, with enough cross-file context to act on. Claude Code reads the findings, examines the actual code, and applies judgment about what to fix.

### Setup

Run `pysmelly init` in your project to set up AI review guidance:

```bash
pysmelly init                    # creates PYSMELLY.md + adds reference to CLAUDE.md
pysmelly init docs/PYSMELLY.md   # custom path for the guidance file
```

This creates a guidance file that tells Claude Code what pysmelly is, how to run it, and — critically — how to interpret findings without dismissing them. Re-running `pysmelly init` safely overwrites the guidance file without duplicating the CLAUDE.md reference.

### Review workflow

```bash
pysmelly src/
```

The text output includes a guidance preamble that tells Claude Code to **default to fixing findings**, not explaining why they're acceptable. The three severity levels guide priority:
- **High**: Fix these — dead code, unused defaults, unreachable code
- **Medium**: Fix unless there's a specific reason not to (framework convention, public API). State the reason if skipping.
- **Low**: Review and fix where it makes sense — investigation pointers, not mandates

### What makes findings actionable for AI review

pysmelly findings include **cross-file context** that single-file linters can't provide:

- *"format_type is declared but never used in parse_body() — 12 callers still pass it"* — Claude Code can trace the vestigial parameter through the call chain and remove it everywhere
- *"deploy() param 'timeout' defaults to None but all 3 callers always pass it"* — Claude Code can make the parameter required and simplify the callers
- *"TASK_BEFORE_EXECUTE = 'task:before\_execute' is never referenced anywhere"* — Claude Code can investigate whether the constant was superseded and delete it

## Requirements

- Python 3.12+
- No external dependencies (stdlib only)
- Calls `git` (via subprocess, not shell) for `.gitignore`-aware file discovery, `--diff` mode, and version detection. Falls back gracefully when `git` is not available.

## License

BSD-3-Clause
