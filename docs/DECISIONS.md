# pysmelly — Design Decisions

## Keep `compat-shims` check

**Decision:** Keep it.

**Context:** `compat-shims` detects `try/except ImportError` patterns — compatibility shims for older Python versions. The question was whether to remove it (like `lazy-imports` and `too-many-params`) since it's a simple single-file pattern match.

**Rationale:**
- No standard tool flags this. Ruff, pylint, and bandit all miss it.
- Found a real issue in the deployer project (`b93dbb7` "Remove tomllib fallback").
- Small amount of code, low maintenance burden.
- The pattern is specifically about vestigial code after `requires-python` changes — squarely in pysmelly's wheelhouse.

## Remove `lazy-imports` check

**Decision:** Remove it.

**Rationale:** Pylint's `import-outside-toplevel` (C0415) covers this. pysmelly should focus on cross-file analysis, not single-file rules that existing tools handle well.

## Remove `too-many-params` check

**Decision:** Remove it.

**Rationale:** Ruff's PLR0913 and Pylint's R0913 already do this. The check becomes interesting only with caller-aware context (e.g., `param-clumps` detecting the same params passed together), which is a separate check.

## Don't add `legacy-type-hints` check

**Decision:** Don't implement it.

**Rationale:** Ruff's UP006 and UP007 already detect `typing.List`, `typing.Optional`, etc. and auto-fix them. pysmelly should not reimplement what ruff does well.

## Don't add single-file checks that overlap with existing tools

**Decision:** Don't implement `stdlib-shadow`, `function-level-loggers`, `write-only-variables`, `immediately-overwritten`, or `remainder-flags`.

**Rationale:** These are all single-file pattern matches that existing tools already handle or could trivially handle:
- `stdlib-shadow` — ruff A005
- `function-level-loggers` — pylint W1203/W1201 territory
- `write-only-variables` — pylint W0612, vulture
- `immediately-overwritten` — pylint W0128 (self-assigning-variable), ruff territory

pysmelly's differentiator is cross-file call-graph analysis. Adding single-file lint rules dilutes that focus, increases maintenance surface, and competes with tools (ruff, pylint) that do single-file analysis better and faster. The `compat-shims` exception stands because no other tool flags that pattern.
