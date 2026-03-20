# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work (Phases 1-9), [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Phase 10: Git history analysis

Mine `git log` for evolutionary signals that static analysis alone cannot
detect. Inspired by Adam Tornhill's "Your Code as a Crime Scene" — the
idea that version control history reveals the actual coupling, hotspots,
and organizational patterns in a codebase better than any snapshot can.

pysmelly already calls `git` via subprocess for file discovery and diff
mode. This phase extends that to analyze commit history.

### Why git history?

Static analysis sees the code as it is now. Git history reveals:
- Which files *actually* change together (regardless of import graphs)
- Which files accumulate the most churn (complexity magnets)
- Which files change for many unrelated reasons (too many responsibilities)
- Which code stopped evolving while its neighbors kept changing (abandoned)

These signals complement pysmelly's existing cross-file AST analysis.
The static `shotgun-surgery` check flags `obj.attr` accessed in 4+ files;
git change-coupling catches the cases where files change together for
reasons that don't show up in the AST at all.

### Check 1: `change-coupling` (MEDIUM severity)

Files that always change in the same commit but have no direct import
relationship. Hidden shotgun surgery — the coupling is behavioral, not
structural.

#### Algorithm

1. Run `git log --format="%H" --since="6 months"` (configurable window)
2. For each commit, collect the set of changed Python files
3. For each pair of files (A, B) that co-change:
   - Count co-change commits
   - Count total commits touching A, count total touching B
   - Compute coupling ratio: `co_changes / min(changes_A, changes_B)`
4. Flag pairs where:
   - Co-change count >= 5 (they change together often enough to matter)
   - Coupling ratio >= 0.7 (most changes to the less-active file involve the other)
   - No direct import between them (if A imports B or vice versa, the coupling is explicit and expected)
5. Group into clusters when A↔B and B↔C both fire

#### Message format

`api/views.py and billing/invoice.py changed together in 8/10 commits
(last 6 months) with no import relationship — hidden coupling`

#### Noise suppression

- Skip test files co-changing with their source (expected)
- Skip `__init__.py` files (change for any reason)
- Skip migration files
- Configurable time window (default 6 months)
- Minimum commit threshold (default 5)

---

### Check 2: `hotspot` (LOW severity)

Files with high churn (many commits) AND high complexity (measured by
existing pysmelly findings or line count). These are the files most
likely to contain bugs and most painful to work in.

#### Algorithm

1. Count commits per file in the time window
2. Measure complexity per file — one of:
   - Line count (simple, always available)
   - Sum of pysmelly findings for that file (richer but circular)
   - Cyclomatic-ish: count of `if/for/while/try/except` nodes
3. Rank files by `churn * complexity` (Tornhill's hotspot formula)
4. Flag top N files (or files above a threshold)

#### Message format

`services/payment.py is a hotspot: 47 commits (top 5%) with 320 lines
and nesting depth 7 — high-churn, high-complexity code`

---

### Check 3: `divergent-change` (MEDIUM severity)

One file appearing in commits with very different purposes — too many
responsibilities. The file is a magnet for unrelated changes.

#### Algorithm

1. For each file, collect commit messages from the time window
2. Cluster messages by topic (simple keyword/prefix matching: "fix",
   "feat", "refactor", "auth", "billing", etc.)
3. Flag files appearing across 4+ distinct topic clusters
   with 3+ commits each

#### Message format

`models/user.py appears in commits for 5 different concerns (auth,
billing, notifications, reporting, admin) — consider splitting
responsibilities`

#### Noise suppression

- Skip files under 50 lines (small files naturally participate in many changes)
- Skip `__init__.py`, `conftest.py`, config files
- Keyword clustering is inherently fuzzy — err toward false negatives

---

### Check 4: `abandoned-code` (LOW severity)

Files untouched for a long time while their neighboring files (same
directory) keep evolving. Likely dead, rotting, or forgotten.

#### Algorithm

1. For each file, find its last commit date
2. For each directory, compute the median last-commit date of its files
3. Flag files whose last commit is > 12 months older than their
   directory's median

#### Message format

`utils/legacy_parser.py last modified 2023-01-15, but utils/ median is
2024-11-20 — abandoned while neighbors evolved`

---

### Implementation notes

- All checks go in a new `src/pysmelly/checks/git_history.py` module
- Gate behind `--git-history` flag (opt-in) since it's slower than AST checks
  and requires meaningful git history
- Time window configurable: `--git-window 6m` (default 6 months)
- Use `git log --format` and `git show --name-only` — same subprocess
  pattern as existing file discovery
- Cache parsed git log (it's the same data for all checks)
- Add to CLI help and `--list-checks`

### Implementation sequence

1. Git log parser infrastructure (shared cache)
2. `change-coupling` — highest-value signal
3. `hotspot` — straightforward once churn data exists
4. `divergent-change` — needs commit message clustering
5. `abandoned-code` — simplest, uses only last-commit dates

### Open questions

- Should `--git-history` be opt-in or on-by-default? Leaning opt-in:
  it's slower, needs real history, and the signals are different enough
  that users should consciously choose to include them.
- Commit message clustering for `divergent-change` — keyword-based or
  something smarter? Start with keywords, iterate.
- Should `hotspot` use pysmelly's own findings as the complexity metric?
  Creates a dependency on running the other checks first, but gives a
  richer signal than line count alone.

