# pysmelly

AST-based Python code smell detector for AI-assisted code review.

pysmelly finds **vestigial code patterns** — code that outlived the design that created it. It performs cross-file call-graph analysis to detect smells that single-file linters miss, then reports findings at three severity levels so an AI reviewer (or human) knows which to act on, which to review, and which to skim.

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
| `compat-shims` | `try/except ImportError` patterns left over from supporting older Python versions the project no longer targets. |
| `suspicious-fallbacks` | `.get()` on module-level constant dicts with non-trivial defaults. If the key should always exist, use `[]` indexing and fail fast. |
| `env-fallbacks` | `os.environ.get()` or `os.getenv()` with non-None defaults. Required config should fail fast, not silently fall back. |

### Medium severity — review each, fix what makes sense

| Check | What it finds |
|---|---|
| `constant-args` | Parameter always receives the same literal value from every caller. The value should be a default or constant. |
| `foo-equals-foo` | Single-use locals gathered into an object — suggests bundling into a dataclass or building directly. |
| `duplicate-blocks` | Structurally identical code blocks across functions (AST-normalized, so different variable names still match). |
| `duplicate-except-blocks` | Identical except handlers with same error messages across files. |
| `temp-accumulators` | `parts = []; parts.append(...); join(parts)` patterns replaceable with comprehensions. |
| `constant-dispatch-dicts` | Module-level `{"name": func, ...}` tables that can get out of sync — consider decorator registration. |
| `return-none-instead-of-raise` | Functions returning `None` on error where callers all guard against `None`. The function should raise instead. |
| `pass-through-params` | Parameters received by a function and only forwarded to another function. The intermediary's signature is vestigial. |
| `param-clumps` | Groups of 3+ parameters appearing together in 3+ function signatures — extract a dataclass. |
| `runtime-monkey-patch` | Function assigned to attribute of external object at module scope. |

### Low severity — informational

| Check | What it finds |
|---|---|
| `single-call-site` | Short public functions called exactly once — candidates for inlining. |
| `internal-only` | Public functions only called within their own file — candidates for `_private` naming. |
| `trivial-wrappers` | Functions whose body is a single return statement — candidates for inlining. |
| `stdlib-alternatives` | Stdlib modules where well-known third-party libraries are better, deprecated stdlib/third-party modules, and mixed stdlib/modern usage in the same codebase. |

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

The text output includes a guidance preamble that helps LLMs interpret findings in context. The three severity levels map to AI review actions:
- **High**: Auto-fix or flag for immediate attention
- **Medium**: Discuss in review, suggest specific refactoring
- **Low**: Mention in summary, don't block on these

## Requirements

- Python 3.12+
- No external dependencies (stdlib only)

## License

BSD-3-Clause
