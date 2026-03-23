# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work (Phases 1-10), [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Phase 11: Git-History Signal-to-Noise

Validated git-history checks against real-world open source projects
(Flask, requests, FastAPI, Celery, Poetry, sentry-python) and local
repos (outscience, havoc). Top checks (bug-magnet, fix-follows-feature,
test-erosion) found confirmed real issues. But overall signal-to-noise
needs improvement — on havoc, 82% of findings came from two noisy checks.

### Phase 11a: Noise reduction

Five concrete fixes identified from validation:

- [ ] **knowledge-silo team gate** — Skip check entirely if project has
  < 3 distinct authors in the window. Currently the #1 noise source
  (74 findings on a solo-developer project).

- [ ] **blast-radius relative threshold** — Current flat threshold of
  median-5 co-changes is barely above project baseline for active repos
  (havoc: 54 findings). Change to `max(8, ~2.5× project median commit
  size)` so the threshold scales with project commit style.

- [ ] **abandoned-code min-lines filter** — Skip files under ~20 lines.
  Files like `exceptions.py`, `__main__.py`, `consts.py` are
  definitional and *should* be stable. Larger files (like a real
  abandoned `cli.py`) are the useful findings.

- [ ] **Emoji conventional commit support** — FastAPI uses 🐛/✨/♻️
  prefixes. Our classifier misses them (64 of 104 FastAPI commits
  unclassified). Add emoji-to-category mapping in `classify_commit()`.

- [ ] **Bulk commit filter** — Commits touching 30+ files are likely
  mechanical (formatting, type annotations, linter sweeps). Downweight
  or exclude from per-file metrics. Caveat: some developers make
  legitimately large commits, so this may need to be a heuristic
  (e.g., only exclude if commit message matches chore/style patterns
  AND touches 30+ files).

### Phase 11b: Evaluate underperforming checks

Several checks produced zero or near-zero findings across all tested
projects. Decide whether to improve, demote, or drop:

- [ ] **conscious-debt** — Zero findings on all projects. Nobody writes
  "HACK" or "workaround" in commit messages. Consider broadening
  patterns or accepting this is too niche.
- [ ] **divergent-change** — Zero findings. Requires conventional commit
  scopes `feat(scope):` which few projects use consistently. May need
  a different approach to detect "one file serving multiple concerns."
- [ ] **fix-propagation** — Only fired once (Celery 1y window, between
  two test files). Threshold of 3 co-fixes may be too high for 6m
  windows, or the pattern is genuinely rare.

### Phase 11c: Multi-signal convergence

When multiple checks flag the same file (e.g., sentry's anthropic.py
hit by bug-magnet + fix-follows-feature + stabilization-failure),
confidence is much higher. Consider surfacing "hotspot" files where
3+ checks converge, as a summary section in output.

## Phase 12: Broader Validation

After Phase 11 noise fixes are implemented, re-validate against both
the original test projects and new actively-developed ones.

### Phase 12a: Re-run original projects

Re-run sentry-python, Poetry, havoc, outscience after Phase 11 fixes.
Measure finding count reduction and confirm true positives are retained.

### Phase 12b: New project validation

Test against more actively-developed projects to stress-test:

- [ ] **Airflow** — notoriously complex, many contributors, lots of issues
- [ ] **Pydantic** — active v2 migration aftermath
- [ ] **Home Assistant core** — extreme commit volume, integration churn
- [ ] **Scrapy** — known architectural complexity

### Phase 12c: Validate against issue trackers

For projects with good issue tracking, attempt to correlate findings
with actual bug reports. Best candidates are projects that reference
issue numbers in commit messages (`Fixes #123`).

### Future ideas (not yet planned)

- **Semver patch-release signal** — If a project uses semantic versioning
  and tags, files changed between v1.2.0 and v1.2.1 are emergency fixes.
  Could detect bug-magnet files via release tags instead of commit messages.
  Avoids time-based analysis; uses version semantics instead.

- **Co-change test mapping** — `_find_test_file()` only matches
  `test_{stem}.py` naming. Falls down when tests are in differently-named
  files (e.g., `test_span_streaming.py` for `tracing_utils.py`). A
  co-change heuristic (files frequently committed together where one is
  a test) could supplement the name-based mapping.
