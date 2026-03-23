# pysmelly - Claude Code Context

## Project Overview

AST-based Python code smell detector that acts as an **investigation dispatcher** for AI-assisted code review. Finds vestigial code patterns — code that outlived the design that created it — and reports them as **starting points for investigation**, not mandates. Performs cross-file call-graph analysis that single-file linters can't do, providing context like caller counts and blast radius so the AI reviewer (Claude Code) or human can apply judgment.

Target audience is AI-assisted code review (Claude Code), but output is useful for humans too.

## Key Files

- `src/pysmelly/cli.py` - CLI entry point, argparse setup
- `src/pysmelly/registry.py` - `@check` decorator, `Finding` dataclass, `Severity` enum
- `src/pysmelly/discovery.py` - File finding (git-aware), AST parsing
- `src/pysmelly/output.py` - Text formatter
- `src/pysmelly/checks/callers.py` - Cross-file call-graph checks (unused-defaults, dead-code, single-call-site, internal-only, pass-through-params, vestigial-params, constant-args, return-none-instead-of-raise, inconsistent-error-handling, dict-as-dataclass)
- `src/pysmelly/checks/patterns.py` - Pattern detection (foo-equals-foo, suspicious-fallbacks, temp-accumulators, constant-dispatch-dicts, fossilized-toggles, dead-constants, unreachable-after-return, isinstance-chain, boolean-param-explosion, exception-flow-control, arrow-code, hungarian-notation, inconsistent-returns, plaintext-passwords, getattr-strings, late-binding-closures, law-of-demeter)
- `src/pysmelly/checks/structure.py` - Structural checks (duplicate-blocks, duplicate-except-blocks, param-clumps, middle-man)
- `src/pysmelly/checks/dead.py` - Dead code extension checks (dead-exceptions, dead-dispatch-entries, orphaned-test-helpers, dead-abstraction, broken-backends)
- `src/pysmelly/checks/architecture.py` - Architectural checks (shared-mutable-module-state, write-only-attributes, temporal-coupling, feature-envy, anemic-domain)
- `src/pysmelly/checks/imports.py` - Import checks (compat-shims)
- `src/pysmelly/checks/recommendations.py` - Stdlib alternatives check with TOML catalog
- `src/pysmelly/checks/repetition.py` - Repetition checks (scattered-constants, scattered-isinstance, shotgun-surgery, repeated-string-parsing)
- `src/pysmelly/catalog.toml` - Pattern catalog for stdlib-alternatives (22 patterns)
- `src/pysmelly/checks/helpers.py` - Shared AST utilities (call finder, function index)
- `src/pysmelly/checks/history.py` - Git history checks (abandoned-code, blast-radius, change-coupling, growth-trajectory, churn-without-growth)
- `src/pysmelly/git_history.py` - Git log parser, CommitInfo/FileStats dataclasses, reviewed marker parsing, lazy numstat

## Common Commands

```bash
uv run pysmelly                                  # Analyze current directory
uv run pysmelly --check dead-code                # Run single check
uv run pysmelly --no-context src/               # Suppress LLM guidance preamble
uv run pysmelly git-history                      # Run git history checks
uv run pysmelly git-history --check blast-radius # Run single git check
uv run pysmelly git-history --window 1y          # Look back 1 year
uv run pysmelly git-history reviewed path/file.py # Acknowledge a finding
uv run pytest                                    # Run tests
make format                                      # Format with black + isort
make self-check                                  # Run pysmelly on itself
```

## Architecture

- **Minimal dependencies** — currently stdlib only, but not a hard constraint
- **Check registration** via `@check("name", severity=Severity.X, category="ast"|"git-history")` decorator
- **Each check** receives `AnalysisContext` (all parsed files + cached indices) and returns `list[Finding]`
- **Git history checks** run via `pysmelly git-history` subcommand; `category="git-history"` in `@check()`
- **File discovery** uses `git ls-files` when in a repo, falls back to rglob
- **Severity levels**: HIGH (fix), MEDIUM (fix unless specific reason not to), LOW (review and fix where it makes sense)

## Design Principles

- Cross-file analysis is the differentiator — don't reimplement what ruff/pylint already do well
- Findings should drive action, not analysis paralysis — the default is to fix, not to explain why it's OK
- Include cross-file context (caller counts, blast radius) so fixes can be applied across the codebase
- Grey areas exist, but Claude Code should lean toward fixing rather than defending the status quo
- Minimal dependencies preferred — don't add deps without clear justification
- Calls `git` via subprocess (list args, no shell) for file discovery and diff mode

## Complementary Tools

pysmelly does NOT replace these — recommend them in combination:
- **ruff** — single-file lint (formatting, style, bugs)
- **vulture** — dead code by name-matching (faster, different approach)
- **mypy** — type checking
- **bandit** — security
