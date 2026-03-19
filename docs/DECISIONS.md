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

## Don't add `--exclude-tests` convenience flag

**Decision:** Don't implement it. Keep `--exclude` as the only mechanism.

**Rationale:** Test file naming conventions vary across projects — `test_*.py`, `*_test.py`, `tests/`, `test/`, `spec/`, framework-specific patterns, etc. A hardcoded `--exclude-tests` bakes in assumptions that won't be universal. The existing `--exclude` flag is flexible enough (`--exclude 'test_*' --exclude 'tests/'`). If discoverability is a concern, better `--help` examples are the right fix, not a new flag.

## Don't add speculative checks without clear cross-file value

**Decision:** Don't implement `parallel-implementations`, `boolean-parameter-smell`, `stale-comments`, or `remainder-flags`.

**Rationale:**
- `parallel-implementations` — Functions with the same name/signature in different files. Hard to detect generically without a clear scope definition; too many legitimate cases (interface implementations, test doubles, overrides).
- `boolean-parameter-smell` — Functions with boolean params where the first statement is `if flag:`. Too noisy — many legitimate uses of boolean parameters. The interesting case (function should be split) is hard to distinguish from the common case (feature toggle).
- `stale-comments` — Comments referencing names that no longer exist. Comments aren't structured, so name matching produces false positives on partial matches, English words, etc. Fragile and low-confidence.
- `remainder-flags` — Argparse REMAINDER swallowing flags. Extremely niche — only relevant to CLI-heavy codebases using `argparse.REMAINDER`.
