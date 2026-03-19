# pysmelly - Claude Code Context

## Project Overview

AST-based Python code smell detector. Finds vestigial code patterns that survive after design changes — the kind of cruft that accumulates as code evolves. Performs cross-file call-graph analysis that single-file linters can't do.

Target audience is AI-assisted code review (Claude Code), but output is useful for humans too.

## Key Files

- `src/pysmelly/cli.py` - CLI entry point, argparse setup
- `src/pysmelly/registry.py` - `@check` decorator, `Finding` dataclass, `Severity` enum
- `src/pysmelly/discovery.py` - File finding (git-aware), AST parsing
- `src/pysmelly/output.py` - Text formatter
- `src/pysmelly/checks/callers.py` - Cross-file call-graph checks (unused-defaults, dead-code, single-call-site, internal-only)
- `src/pysmelly/checks/patterns.py` - Pattern detection (foo-equals-foo, suspicious-fallbacks, temp-accumulators, constant-dispatch-dicts)
- `src/pysmelly/checks/structure.py` - Structural checks (too-many-params, duplicate-blocks)
- `src/pysmelly/checks/imports.py` - Import checks (lazy-imports, compat-shims)
- `src/pysmelly/checks/helpers.py` - Shared AST utilities (call finder, function index)

## Common Commands

```bash
uv run pysmelly                        # Analyze current directory
uv run pysmelly --check dead-code      # Run single check
uv run pysmelly --no-context src/     # Suppress LLM guidance preamble
uv run pytest                          # Run tests
make format                            # Format with black + isort
make self-check                        # Run pysmelly on itself
```

## Architecture

- **Zero dependencies** — stdlib `ast` module only
- **Check registration** via `@check("name", severity=Severity.X)` decorator
- **Each check** receives `dict[Path, ast.Module]` (all parsed files) and returns `list[Finding]`
- **File discovery** uses `git ls-files` when in a repo, falls back to rglob
- **Severity levels**: HIGH (act on it), MEDIUM (review it), LOW (informational)

## Design Principles

- Cross-file analysis is the differentiator — don't reimplement what ruff/pylint already do well
- Findings should be actionable, not just informative
- Grey areas are fine — the consumer (Claude Code) can apply judgment
- No external dependencies — this runs via `uvx` with zero setup

## Complementary Tools

pysmelly does NOT replace these — recommend them in combination:
- **ruff** — single-file lint (formatting, style, bugs)
- **vulture** — dead code by name-matching (faster, different approach)
- **mypy** — type checking
- **bandit** — security
