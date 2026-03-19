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
pysmelly --skip lazy-imports --skip single-call-site

# JSON output (for LLM consumption)
pysmelly --format=json

# Verbose output
pysmelly -v
```

## Checks

### High severity — act on or justify

| Check | What it finds |
|---|---|
| `unused-defaults` | Parameter defaults to `None` but every caller always passes a value. The `Optional` is vestigial — make the param required. |
| `dead-code` | Public functions with zero callers anywhere in the codebase. Cross-references direct calls, imports, dict/list references, and callback passing. |
| `compat-shims` | `try/except ImportError` patterns left over from supporting older Python versions the project no longer targets. |
| `suspicious-fallbacks` | `.get()` on module-level constant dicts with non-trivial defaults. If the key should always exist, use `[]` indexing and fail fast. |

### Medium severity — review each, fix what makes sense

| Check | What it finds |
|---|---|
| `foo-equals-foo` | Constructor calls with 4+ `name=name` kwargs, suggesting the caller has too many mirrored local variables — bundle into a dataclass. |
| `duplicate-blocks` | Structurally identical code blocks across functions (AST-normalized, so different variable names still match). |
| `temp-accumulators` | `parts = []; parts.append(...); join(parts)` patterns replaceable with comprehensions. |
| `constant-dispatch-dicts` | Module-level `{"name": func, ...}` tables that can get out of sync — consider decorator registration. |
| `too-many-params` | Functions with 6+ parameters (excluding self/cls). |

### Low severity — informational

| Check | What it finds |
|---|---|
| `single-call-site` | Public functions called exactly once — candidates for inlining. |
| `internal-only` | Public functions only called within their own file — candidates for `_private` naming. |
| `lazy-imports` | Imports inside functions instead of at module level. |

## Output formats

**Text** (default) — grouped by check, one line per finding:

```
=== unused-defaults (2 finding(s)) ===
  src/myapp/deploy.py:45: deploy() param 'timeout' defaults to None but all 3 caller(s) always pass it
  src/myapp/config.py:12: load_config() param 'env' defaults to None but all 5 caller(s) always pass it
```

**JSON** (`--format=json`) — structured for programmatic consumption:

```json
{
  "total_files": 42,
  "total_findings": 2,
  "findings": [
    {
      "file": "src/myapp/deploy.py",
      "line": 45,
      "check": "unused-defaults",
      "message": "deploy() param 'timeout' defaults to None but all 3 caller(s) always pass it",
      "severity": "high"
    }
  ]
}
```

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

pysmelly is designed to work well as a code review tool invoked by Claude Code or similar AI assistants. A typical workflow:

```bash
# Run pysmelly, get structured findings
pysmelly --format=json src/ > /tmp/smells.json

# Or just run it directly — the text output is clear enough for LLMs
pysmelly src/
```

The three severity levels map to AI review actions:
- **High**: Auto-fix or flag for immediate attention
- **Medium**: Discuss in review, suggest specific refactoring
- **Low**: Mention in summary, don't block on these

## Requirements

- Python 3.12+
- No external dependencies (stdlib only)

## License

BSD-3-Clause
