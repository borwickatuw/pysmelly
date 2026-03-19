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
