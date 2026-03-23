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
- Which code stopped evolving while its neighbors kept changing (abandoned)
- Where effort is being wasted on repeated fixes
- Which files are growing out of control before they become unmaintainable

These signals complement pysmelly's existing cross-file AST analysis.
The static `shotgun-surgery` check flags `obj.attr` accessed in 4+ files;
git change-coupling catches the cases where files change together for
reasons that don't show up in the AST at all.

### Two tiers of analysis

Git history checks split into two tiers based on what data they need:

**Tier 1 — Structural** checks use only file-level change data: which
files changed in which commits, how often, how many lines. These work
on any repository regardless of commit message quality.

**Tier 2 — Semantic** checks use commit message content to classify
commits (fix vs. feature, topic clustering, debt markers). These produce
high-quality results when commit messages are structured — which is
reliably the case for Claude Code projects. On repos with messy commit
history ("wip", "stuff", "fix fix fix"), semantic checks produce noise.

#### Auto-detection of message quality

Rather than requiring a CLI flag, pysmelly detects whether commit
messages are structured by sampling the last 50 commits and checking
for:
- Conventional commit prefixes (`fix:`, `feat:`, `refactor:`, etc.)
- `Co-Authored-By:` lines mentioning Claude or other AI tools
- Descriptive messages (> 10 words, no single-word messages)

If >= 50% of sampled commits pass these heuristics, Tier 2 checks run.
Otherwise they are skipped with a note:

`Skipped semantic checks (bug-magnet, fix-propagation, conscious-debt,
divergent-change) — commit messages don't appear structured enough.
Use --commit-messages=structured to force.`

Override with `--commit-messages=structured|unstructured|auto` (default:
auto).

---

### Phase 10a: Infrastructure

Build the git history parser and caching layer that all subsequent
checks depend on.

#### Git log cache

A `GitHistory` object that lazily parses `git log` output once and
provides pre-computed views for all checks. Lives in
`src/pysmelly/checks/git_history.py`.

```
GitHistory:
    commits: list[CommitInfo]        # all commits in the window
    files_in_commit: dict[str, list[str]]   # commit hash → files changed
    commits_for_file: dict[str, list[CommitInfo]]  # file → commits touching it
    message_quality: float           # 0.0–1.0, fraction of structured messages
```

Each `CommitInfo` is a lightweight dataclass:
```
CommitInfo:
    hash: str
    date: datetime
    message: str
    files: list[str]
    insertions: int    # total lines added
    deletions: int     # total lines deleted
```

Populated via a single `git log` call:
```
git log --format="%H%x00%aI%x00%s" --name-only --since="6 months" -- "*.py"
```

This gives hash, ISO date, subject, and changed files in one pass.
For checks that need per-file line counts (churn-without-growth), a
second call:
```
git log --format="%H" --numstat --since="6 months" -- "*.py"
```

Both are cached on `AnalysisContext` (or a new `HistoryContext`) so
parsing happens once regardless of how many checks run.

#### CLI integration

- `--git-history` flag enables git history checks (opt-in — they're
  slower and require meaningful history)
- `--git-window PERIOD` sets the time window (default: `6m` for 6
  months; supports `3m`, `1y`, etc.)
- `--commit-messages=auto|structured|unstructured` overrides message
  quality detection
- Git history checks appear in `--list-checks` with a `[git]` marker
- All checks register via the existing `@check()` decorator

#### Implementation sequence

1. `CommitInfo` dataclass and git log parser
2. `GitHistory` cache class with lazy population
3. Message quality auto-detection
4. Wire into CLI (`--git-history`, `--git-window`)
5. One simple check (abandoned-code) to validate the infrastructure

---

### Phase 10b: Structural checks (Tier 1 — any repo)

These checks use only file-level change data. They work on any
git repository regardless of commit message quality.

#### Check: `blast-radius` (MEDIUM)

When a file is modified, how many other files typically change in the
same commit? Measures encapsulation quality from observed behavior.

**Algorithm:**
1. For each Python file, find all commits in the window that touch it
2. For each such commit, count the other Python files also changed
3. Compute the median co-change count for the file
4. Flag files where median >= 5 other files per commit

**Message format:**
`services/payment.py — changes to this file touch a median of 7 other
files per commit (23 commits in last 6 months) — poor encapsulation`

**Noise suppression:**
- Skip commits touching 20+ files (bulk refactors, renames, formatting)
- Skip `__init__.py` (changes for structural reasons)
- Skip merge commits
- Minimum 5 commits in the window (need enough data)

---

#### Check: `change-coupling` (MEDIUM)

Files that always change in the same commit but have no direct import
relationship. Hidden shotgun surgery — the coupling is behavioral,
not structural.

**Algorithm:**
1. For each commit, collect the set of changed Python files
2. For each pair of files (A, B) that co-change:
   - Count co-change commits
   - Count total commits touching A, count total touching B
   - Compute coupling ratio: `co_changes / min(changes_A, changes_B)`
3. Flag pairs where:
   - Co-change count >= 5
   - Coupling ratio >= 0.7
   - No direct import between them (checked via AST import index)
4. Group into clusters when A↔B and B↔C both fire

**Message format:**
`api/views.py and billing/invoice.py changed together in 8/10 commits
(last 6 months) with no import relationship — hidden coupling`

**Noise suppression:**
- Skip test files co-changing with their source (expected)
- Skip `__init__.py` files
- Skip migration files
- Minimum 5 co-changes

---

#### Check: `growth-trajectory` (LOW)

Files growing rapidly — catching the "chonky file in the making" before
it becomes unmaintainable. The AST sees a 400-line file today; git
history sees it was 80 lines three months ago.

**Algorithm:**
1. For each file, get current line count
2. Get line count at start of window via `git show <earliest-hash>:<path>`
   (or compute from cumulative insertions/deletions in the numstat data)
3. Compute growth: `current_lines - start_lines`
4. Flag files that grew by >= 200 lines AND >= 2x in the window

**Message format:**
`models/user.py grew from 120 to 380 lines (+217%) in the last 6
months — accelerating accumulation of responsibilities`

**Noise suppression:**
- Skip new files (didn't exist at start of window) — growth is expected
- Skip files under 100 lines currently (small files growing is fine)
- Handle renames (git log --follow for the specific file)

---

#### Check: `churn-without-growth` (LOW)

Files with many commits but stable or shrinking line count. The code is
being rewritten, not extended — a design instability signal. The
abstractions aren't right, so every new requirement means reworking
existing code.

**Algorithm:**
1. Count commits per file in the window
2. Compute net line change: `total_insertions - total_deletions`
3. Flag files where:
   - Commits >= 10 in the window
   - Net growth <= 10% of file size (churn without meaningful growth)

**Message format:**
`utils/parser.py — 18 commits but only +12 net lines (280 lines total)
in the last 6 months — code is being rewritten, not extended`

**Noise suppression:**
- Skip files under 50 lines (small files churn naturally)
- Skip test files (tests legitimately get rewritten as code changes)
- Minimum 10 commits (need enough signal)

---

#### Check: `abandoned-code` (LOW)

Files untouched for a long time while their neighboring files (same
directory) keep evolving. Likely dead, rotting, or forgotten.

**Algorithm:**
1. For each file, find its last commit date
2. For each directory, compute the median last-commit date
3. Flag files whose last commit is > 12 months older than their
   directory's median

**Message format:**
`utils/legacy_parser.py last modified 2023-01-15, but utils/ median
is 2024-11-20 — abandoned while neighbors evolved`

**Noise suppression:**
- Skip directories with < 3 files (not enough peers to compare)
- Skip `conftest.py`, `__init__.py` (infrastructure files)
- Use the full git history for this check, not just the window
  (we need to know about files that *haven't* been touched)

---

### Phase 10c: Semantic checks (Tier 2 — structured messages)

These checks classify commits by their message content. They produce
strong signals on Claude Code projects and conventional-commit repos.
They are auto-skipped when message quality is low (see auto-detection
above).

#### Commit classification

All semantic checks share a commit classifier that categorizes each
commit based on message content:

- **fix**: message starts with `fix:`, `bugfix:`, or contains "fix",
  "bug", "patch", "correct", "repair" as a word
- **feature**: starts with `feat:`, or contains "add", "implement",
  "introduce", "support"
- **refactor**: starts with `refactor:`, or contains "refactor",
  "restructure", "reorganize", "simplify", "clean up"
- **debt**: contains "workaround", "hack", "temporary", "TODO",
  "FIXME", "quick fix", "stopgap"
- **other**: none of the above

A commit can match multiple categories. The classifier is intentionally
simple — conventional commit prefixes get exact matches; non-prefixed
messages get keyword matching with word boundaries to avoid false hits.

---

#### Check: `bug-magnet` (MEDIUM)

Files where a disproportionate fraction of commits are fixes. If Claude
Code keeps coming back to fix a file, there's a structural problem that
the fixes aren't addressing. Directly answers "where is effort being
wasted on re-work?"

**Algorithm:**
1. For each file, classify its commits as fix vs. non-fix
2. Compute fix ratio: `fix_commits / total_commits`
3. Flag files where:
   - Total commits >= 5
   - Fix ratio >= 0.5 (majority of changes are fixes)

**Message format:**
`services/billing.py — 8 of 12 commits (67%) are fixes — recurring
problems suggest a structural issue worth redesigning`

**Noise suppression:**
- Minimum 5 commits
- Skip test files (test fixes are expected when source changes)
- Don't count refactors as fixes

---

#### Check: `fix-propagation` (MEDIUM)

When a fix commit touches file A, which other files also get fixed?
Like change-coupling but filtered to fix commits — a stronger signal
because it specifically captures fragile coupling. File B breaks
whenever file A gets fixed.

**Algorithm:**
1. Filter to fix commits only (from commit classifier)
2. Run the same co-change pair analysis as `change-coupling`
3. Flag pairs where:
   - Co-change in fix commits >= 3
   - Fix coupling ratio >= 0.6

**Message format:**
`api/views.py and middleware/auth.py — 5 of 7 fix commits touch
both files — fixing one tends to break the other`

**Noise suppression:**
- Same exclusions as change-coupling
- Higher significance threshold than raw coupling (fixes are
  a stronger signal, so fewer co-occurrences are meaningful)

---

#### Check: `conscious-debt` (LOW)

Commits whose messages explicitly acknowledge technical debt.
Claude Code uses terms like "workaround" and "temporary" honestly —
these mark places where a shortcut was taken deliberately. Unlike
scanning source for `# TODO`, this catches debt introduced knowingly
at commit time that may not have left a breadcrumb in the code.

**Algorithm:**
1. Scan commit messages for debt markers: "workaround", "hack",
   "temporary", "TODO", "FIXME", "quick fix", "stopgap"
2. Report the file(s) touched by that commit, with the commit
   message as context

**Message format:**
`models/cache.py — commit a1b2c3d "Add temporary workaround for
rate limiting" (2024-09-15) — acknowledged debt, is it still needed?`

**Noise suppression:**
- Skip if the file has been substantially modified since the debt
  commit (the workaround may have been replaced)
- Group multiple debt commits to the same file

---

#### Check: `divergent-change` (MEDIUM)

One file appearing in commits with very different purposes — too many
responsibilities. The file is a magnet for unrelated changes.

**Algorithm:**
1. For each file, collect its commits and classify by topic
2. Use conventional commit scopes when available (`feat(auth):`,
   `fix(billing):`) — these are explicit topic markers
3. Fall back to keyword clustering on the message body
4. Flag files appearing across 4+ distinct topic clusters
   with 3+ commits each

**Message format:**
`models/user.py appears in commits for 5 different concerns (auth,
billing, notifications, reporting, admin) — consider splitting
responsibilities`

**Noise suppression:**
- Skip files under 50 lines
- Skip `__init__.py`, `conftest.py`, config files
- Err toward false negatives — only flag clear cases

---

### Phase 10d (stretch): Diff-level checks

These checks analyze the actual diff content, not just which files
changed. More expensive and complex to implement.

#### Check: `same-change-multiple-files` (MEDIUM)

When a commit's diff contains structurally similar hunks across several
files — same parameter added, same import added, same error handling
pattern — that's a DRY violation caught in the act.

**Algorithm:**
1. For commits touching 3+ Python files, extract the diff hunks
2. Normalize hunks (strip whitespace, variable names)
3. Find commits where 3+ files have hunks with > 80% similarity
4. Report the pattern and the files

**Message format:**
`commit a1b2c3d added similar error handling to 4 files
(api/views.py, api/admin.py, api/serializers.py, api/filters.py) —
consider extracting a shared pattern`

This is the hardest check to implement well. The normalization and
similarity comparison need careful tuning to avoid false positives
from boilerplate (imports, `__init__` methods). Defer until the
simpler checks are proven.

---

#### Check: `growing-signatures` (LOW)

Function parameter lists growing over time. The AST sees 7 parameters
today; git history sees it started with 2 and has been accumulating
parameters steadily. Catches the trajectory before it becomes a
param-clumps finding.

**Algorithm:**
1. For functions currently having 4+ parameters, get historical diffs
2. Parse the function signature from earlier versions
3. Flag functions that gained 3+ parameters in the window

**Message format:**
`services/email.py:send_notification — grew from 3 to 7 parameters
in the last 6 months — consider a config object or splitting concerns`

This requires parsing function signatures from diff output, which is
fragile. Consider using `git show <hash>:<path>` and AST-parsing the
historical version instead.

---

### Commit style awareness and time slicing

Git history checks must work across different commit styles:

- **Claude Code / granular**: many small commits, well-scoped, one
  change per commit
- **Conventional commits**: reasonably granular, typed prefixes
- **Daily squash**: one commit per day, everything lumped together
- **PR-squash**: one commit per PR or feature branch
- **Messy**: "wip", "stuff", undifferentiated blobs

Some checks are **commit-count sensitive** — their meaning changes
with commit granularity. "3 fix commits in a row" means very different
things at 20 commits/day vs 1 commit/week. Others are naturally
**commit-count insensitive** — "file exists on disk but has no commits
in the window" works regardless of how people commit.

**Time slicing** addresses this: divide the window into uniform
periods (e.g., weeks) and analyze patterns per period rather than per
commit. Instead of "fix commit follows feature commit within 3
commits," ask "in weeks where this file had feature activity, did fix
activity follow within 2 weeks?" This makes checks robust to commit
granularity.

#### Infrastructure: `TimeSlice`

Lazy-built on `GitHistory`, triggered when any time-slice-aware check
runs:

```
GitHistory.time_slices(period="1w") -> list[TimeSlice]

TimeSlice:
    start: datetime
    end: datetime
    commits: list[CommitInfo]
    files_touched: set[str]
    files_by_category: dict[str, set[str]]  # "fix" -> {files...}
    authors: set[str]
```

Period is derived from the window: `--window 6m` → 1-week slices
(~26 slices). `--window 1y` → 2-week slices (~26 slices). The goal
is roughly 20-30 slices regardless of window size.

#### Commit granularity detection

Auto-detect to calibrate thresholds and skip checks that can't
produce meaningful results:

- `median_commits_per_week`: < 2 suggests coarse commits
- `median_files_per_commit`: > 5 suggests squash-heavy workflow
- `commit_regularity`: coefficient of variation of inter-commit
  time — high means bursty, low means steady

Checks that depend on commit ordering (fix-follows-feature,
shotgun-surgery-temporal) should auto-skip or fall back to time-slice
mode when commit granularity is too coarse. Log a note like:

`Skipped fix-follows-feature — commit granularity too coarse for
sequence analysis (median 1.2 commits/week). Consider more frequent
commits or use --commit-messages=structured to enable time-slice
fallback.`

---

### Phase 10e: Churn pattern checks

These checks analyze the *character* of change — not just what changed
or who changed it, but *how* the changes relate to each other over time.

#### Check: `yo-yo-code` (MEDIUM)

Files with high gross churn (insertions + deletions) relative to their
size. The net change might be zero, but the code is being written,
deleted, rewritten. The abstractions are wrong and people keep trying
different approaches.

Different from `churn-without-growth` (which looks at `insertions -
deletions`); this looks at `insertions + deletions` relative to file
size — total movement regardless of direction.

**Algorithm:**
1. For each file, get total_insertions + total_deletions from numstat
2. Get current line count from AST
3. Compute churn ratio: `(insertions + deletions) / current_lines`
4. Flag if churn ratio >= 3.0 AND file >= 100 lines AND commits >= 5
5. Skip test files

**Commit style sensitivity:** Low — uses aggregate numstat totals, not
commit ordering.

**Message format:**
`utils/parser.py — 850 lines churned across 280 lines of code (3.0x
turnover) in last 6m — abstractions are being reworked repeatedly`

---

#### Check: `fix-follows-feature` (MEDIUM)

When feature work touches a file, does fix work follow shortly after?
Files that consistently go feature→fix→feature→fix have a design that
makes them hard to modify correctly. Different from `bug-magnet` (which
counts fix ratio) — this captures the *sequence* and temporal
relationship.

**Algorithm (time-slice aware):**
1. For each file, build a per-slice activity log:
   - Which slices had "feature" commits touching this file?
   - Which slices had "fix" commits touching this file?
2. Count "feature-then-fix" pairs: a feature slice followed by a fix
   slice within 2 periods
3. Flag if >= 3 feature-then-fix pairs in the window

**Commit style sensitivity:** High — requires commits to be scoped
enough that "fix" and "feat" are separate commits. With PR-squash, a
feature and its fix might be in the same commit. Falls back to
time-slice mode for coarse commit styles.

**Message format:**
`services/billing.py — feature changes followed by fixes in 4 of 6
cycles — design makes this file hard to modify correctly`

---

#### Check: `stabilization-failure` (LOW)

Files with repeated bursts of activity separated by quiet periods.
The design keeps failing to settle — each burst is another attempt
to get it right.

**Algorithm (time-slice based):**
1. For each file, mark which time slices have commits
2. Identify "bursts": consecutive active slices (2+ slices)
3. Identify "gaps": consecutive inactive slices (3+ slices)
4. Flag files with 3+ distinct bursts separated by gaps

**Commit style sensitivity:** Low — uses time-based activity windows,
not commit ordering.

**Message format:**
`models/cache.py — 3 bursts of activity (weeks 1-3, 12-14, 20-22)
separated by quiet periods — design keeps failing to stabilize`

---

### Phase 10f: Organizational checks

These checks use author information to detect knowledge distribution
and collaboration patterns. They require adding `%an` (author name)
to the git log format string.

#### Infrastructure: author data

Add author to `CommitInfo`:
```
CommitInfo:
    hash: str
    date: datetime
    message: str
    author: str          # NEW
    files: list[str]
```

Git log format becomes: `--format=%H%x00%aI%x00%an%x00%s`

Build per-file author index on `GitHistory`:
```
authors_for_file: dict[str, dict[str, int]]  # file -> {author: commit_count}
```

#### Check: `knowledge-silo` (MEDIUM)

Files where 80%+ of commits come from one author. If that person
leaves or is unavailable, nobody understands the file. Static analysis
can never see this.

**Algorithm:**
1. For each file, get author commit counts
2. Compute dominance: `max_author_commits / total_commits`
3. Flag if dominance >= 0.8 AND total commits >= 5
4. Skip files with only 1-2 total commits (not enough history)
5. Skip test files (test ownership concentration is less risky)

**Commit style sensitivity:** Low — author attribution works regardless
of commit granularity. Squash-merge can obscure the actual author, but
that's a workflow issue, not a commit style issue.

**Message format:**
`services/billing.py — 12 of 14 commits (86%) by alice@example.com —
knowledge concentration risk`

**Privacy note:** Shows author names from git log, which is already
public in the repository. Not introducing new information, just
surfacing what's already there.

---

### Phase 10g: AST + history hybrid checks

These checks combine current AST analysis with git history to detect
trajectories that neither analysis alone can see.

#### Check: `growing-import-fan-out` (LOW)

A file's import list is growing over the window. The static snapshot
shows 15 imports (maybe fine); the trajectory shows it had 8 imports
six months ago and is accelerating dependency accumulation.

**Algorithm:**
1. For each file, count current imports from AST
2. Get insertions/deletions from numstat
3. Approximate start-of-window imports: run `git show` on the earliest
   commit's version and count `import`/`from` lines, OR use a simpler
   heuristic based on net import-line additions in the numstat (fragile)
4. Flag if imports grew by >= 5 AND >= 50% in the window
5. Skip `__init__.py` (re-exports grow naturally)

**Alternative:** For accuracy, `git show <earliest-hash>:<path>` and
AST-parse the historical version. Expensive (one subprocess per file)
but accurate. Could be lazy — only run for files that pass a cheap
pre-filter (high overall growth).

**Commit style sensitivity:** None — uses aggregate data.

**Message format:**
`models/user.py — imports grew from 8 to 15 (+88%) in last 6m —
accelerating dependency accumulation`

---

#### Check: `test-erosion` (MEDIUM)

The ratio of test changes to source changes is declining for a module.
Tests aren't keeping up with code changes. The snapshot might look
adequate (plenty of test files exist), but the trajectory shows test
coverage is eroding.

**Algorithm:**
1. Group files by module (top-level directory or package)
2. For each module, split files into test vs source
3. Divide the window into time slices
4. For each slice, compute: test_lines_changed / source_lines_changed
5. Flag modules where the ratio declined by >= 50% from the first half
   of the window to the second half
6. Need >= 3 slices with activity in each half

**Commit style sensitivity:** Low — uses aggregate line counts per time
slice, not commit ordering.

**Message format:**
`oslist/ — test-to-source change ratio dropped from 1.2x to 0.4x over
the last 6 months — test coverage is eroding`

---

#### Check: `emergency-hotspots` (LOW)

Files disproportionately touched in commits with "hotfix", "urgent",
"emergency", "revert", "rollback" in the message. These are the places
where things break in production. Static analysis sees the code as it
is now; this reveals which code causes production fires.

**Algorithm:**
1. Classify commits as "emergency" based on message keywords
2. For each file, compute: emergency_commits / total_commits
3. Flag if emergency ratio >= 0.3 AND emergency commits >= 3
4. Skip if overall emergency commit rate is high (the whole repo is
   fire-fighting, not specific files)

**Commit style sensitivity:** Medium — depends on emergency commits
being distinguishable from normal commits. Works well with conventional
commits and Claude Code. May miss emergencies in repos where hotfixes
aren't labeled differently.

**Message format:**
`services/payment.py — 4 of 10 commits are emergency fixes (hotfix,
revert) — production instability hotspot`

---

### Phase 10g-extra: Additional pattern checks

These fill gaps identified after reviewing the full check catalog.

#### Check: `hotspot-acceleration` (MEDIUM)

A file's commit *frequency* is increasing even if its size is stable.
We have `growth-trajectory` for lines, but this measures *attention*.
A file that went from 1 commit/month to 5 commits/month is becoming a
problem — it's attracting more and more developer time regardless of
whether it's growing.

**Algorithm (time-slice based):**
1. For each file, count commits per time slice
2. Compare commit frequency in the first half vs second half of window
3. Flag if second-half frequency >= 2x first-half AND >= 3 commits in
   the second half
4. Skip files with < 5 total commits (not enough data)
5. Skip test files

**Commit style sensitivity:** Medium — commit frequency is inherently
commit-style-dependent, but the *ratio* (acceleration) is more robust
than absolute counts. A developer who commits once a day doubling to
twice a day is the same signal as one who commits 10x/day going to
20x/day.

**Message format:**
`services/billing.py — commit frequency increased from ~1/week to
~4/week in the second half of the window — becoming a hotspot`

---

#### Check: `no-refactoring` (LOW)

A file with many feature and fix commits but zero refactoring commits.
It's accumulating change after change without anyone ever stepping back
to restructure. Uses the commit classifier we already have.

**Algorithm:**
1. For each file, classify commits into fix/feature/refactor
2. Flag if total commits >= 8 AND refactor count == 0 AND
   (fix + feature) >= 6
3. Skip test files
4. Skip files < 50 lines

**Commit style sensitivity:** Medium — requires commits to be typed
(conventional prefixes or keyword-detectable). Gated behind message
quality like other semantic checks.

**Message format:**
`models/user.py — 12 commits (7 feature, 5 fix, 0 refactor) in last
6m — accumulating changes without restructuring`

---

#### Check: `conflict-prone` (MEDIUM)

Files that frequently appear in merge conflicts. We currently use
`--no-merges` and skip merge data entirely, but merge conflicts are
strong signal: a file where multiple concerns collide.

**Infrastructure:** Separate `git log --merges --name-only` call to
identify files appearing in merge commits. A file in a merge commit
isn't necessarily conflicted, but files that appear in *many* merge
commits disproportionate to their change frequency likely are.

**Algorithm:**
1. Run `git log --merges --name-only` for the window
2. For each file, count merge-commit appearances
3. Compare to non-merge commit count from existing data
4. Flag if merge appearances >= 5 AND merge ratio
   (merges / total commits) >= 0.3
5. Skip files with < 3 merge appearances

**Commit style sensitivity:** Only meaningful in repos that use merge
commits (not rebase-only workflows). Auto-skip if zero merge commits
found in the window.

**Message format:**
`api/views.py — appears in 8 merge commits (40% of its commits are
merges) — frequent integration conflicts suggest competing concerns`

---

#### Check: `repeated-similar-changes` (LOW)

The same *type* of modification is being applied to a file repeatedly.
Commit messages are structurally similar: "Add column to X", "Add field
to X", "Add handler for X". This suggests a missing abstraction or
configuration mechanism — the code should be data-driven rather than
requiring manual additions.

**Algorithm:**
1. For each file, collect commit messages
2. Tokenize and normalize messages (lowercase, strip numbers, remove
   common prefixes like "fix:", "feat:")
3. Compute pairwise similarity between messages (Jaccard on word sets)
4. Flag if 4+ commit messages have >= 0.5 Jaccard similarity to each
   other
5. Skip if the file has < 6 commits

**Commit style sensitivity:** High — requires descriptive commit
messages. Gated behind message quality.

**Message format:**
`models/report.py — 5 commits with similar messages ("add X field",
"add Y field", "add Z field") — consider a data-driven approach
instead of manual additions`

---

### Phase 10h: Temporal cascade checks

These are the most sophisticated checks — they analyze *sequences* of
changes across time and files. They require time slicing and commit
classification to work well.

#### Check: `shotgun-surgery-temporal` (MEDIUM)

Changing file A leads to changes in files B, C within the *next few
time slices* — not the same commit (that's `change-coupling`), but a
delayed cascade. You change something, and over the next few days
realize you broke or missed things elsewhere.

**Algorithm (time-slice based):**
1. For each file A, find time slices where A was modified
2. For each such slice, look at the next 2 slices
3. Track which other files B appear in those follow-up slices
4. Count how often B follows A (directional — A→B, not B→A)
5. Flag if B follows A in >= 50% of A's active slices, >= 4 times
6. Filter out pairs already caught by `change-coupling` (same-commit)

**Commit style sensitivity:** High — fundamentally depends on changes
being spread across multiple commits/time periods. Auto-skip with a
note when commit granularity is too coarse.

**Message format:**
`services/billing.py — changes here are followed by changes to
api/views.py within 2 weeks in 5 of 8 cases — delayed cascade`

---

#### Check: `responsibility-drift` (LOW)

The conventional commit scopes touching a file are *changing over time*.
Six months ago it was `feat(auth)` commits; now it's `fix(billing)` and
`feat(notifications)`. The file's purpose is shifting, which is a
different problem from `divergent-change` (which looks at breadth across
the whole window, not temporal shift).

**Algorithm (time-slice based):**
1. For each file, collect scopes per time slice
2. Compare scopes in the first third of the window vs last third
3. Flag if the dominant scope changed AND the file has >= 8 commits

**Commit style sensitivity:** High — requires scoped conventional
commits. Auto-skip without structured messages.

**Message format:**
`models/user.py — dominant concern shifted from auth (months 1-3) to
billing (months 4-6) — responsibility is migrating into this file`

---

### Implementation notes

- All checks go in `src/pysmelly/checks/history.py`
- `GitHistory` cache lives on `AnalysisContext` — populated lazily on
  first access, like existing indices
- `pysmelly git-history` subcommand (not a flag on the main command)
- Time window via `--window PERIOD` (default `6m`)
- Use `git log --format` and `git log --numstat` — same subprocess
  pattern as existing file discovery, list args, no shell
- All checks register via `@check()` decorator with
  `category="git-history"`
- Findings point to the file (line 1) since there's no specific source
  line for history-based findings — the message carries the context

### Implementation sequence

**Done:**

1. **10a**: GitHistory infrastructure, `pysmelly git-history` subcommand,
   message quality detection, `abandoned-code`
2. **10b**: `blast-radius`, `change-coupling`, `growth-trajectory`,
   `churn-without-growth`, `expected-coupling` config
3. **10c**: Commit classifier, `bug-magnet`, `fix-propagation`,
   `conscious-debt`, `divergent-change`

**Next:**

4. **10e**: `yo-yo-code` (easiest — reuses existing numstat)
5. **10e**: Time-slice infrastructure, commit granularity detection
6. **10e**: `fix-follows-feature`, `stabilization-failure`
7. **10f**: Author data infrastructure, `knowledge-silo`
8. **10g**: `growing-import-fan-out`, `test-erosion`, `emergency-hotspots`
9. **10g-extra**: `hotspot-acceleration`, `no-refactoring`,
   `conflict-prone`, `repeated-similar-changes`
10. **10h**: `shotgun-surgery-temporal`, `responsibility-drift`
    (only if earlier checks prove valuable)

Phases 10d (`same-change-multiple-files`, `growing-signatures`) remain
stretch goals requiring diff-level parsing.

### Open questions

- **Time slice period**: Should we auto-calibrate slice size from the
  window, or let users specify? Auto-calibrating to ~26 slices feels
  right — enough granularity without noise.
- **Author privacy**: `knowledge-silo` shows author names. Should we
  offer an option to hash or anonymize them? The data is already in
  `git log`, but surfacing it in a report feels different.
- **Squash-merge workflows**: PR-squash attributes all work to the
  merger, not the author. Should we detect this and warn that
  author-based checks may be unreliable?
- **Cross-check correlation**: When the same file appears in both
  static and git-history findings (e.g., `long-function` + `growth-
  trajectory`), should we boost its priority or note the convergence?

### Existing tools in this space

No open-source tool combines git history analysis with AST-level code
smell detection. The landscape splits into two non-overlapping camps:

- **code-maat** (Clojure) — temporal coupling, churn, age analysis from
  git logs. Language-agnostic, no code parsing. The closest tool to what
  we're building, but operates purely on file-level statistics.
- **CodeScene** (commercial) — Adam Tornhill's productization of code-maat
  ideas. The only tool bridging history + code analysis. Closed source.
- **git-of-theseus** (Python) — code survival/decay visualization.
  Metrics, not smell detection.
- **gilot** (Python) — hotspot detection, co-change network visualization.
- **git-code-debt** (Python, by Anthony Sottile) — custom metric tracking
  over git history with a web dashboard.
- **PyDriller** (Python) — framework for extracting commit data. A
  potential dependency but we'd rather keep zero deps and use subprocess
  directly, consistent with existing file discovery.

pysmelly's differentiator: combining these evolutionary signals with
AST-level findings and presenting them as actionable code smells, not
dashboards or metrics. The import-awareness (filtering change-coupling
by actual import relationships) is unique.
