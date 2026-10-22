"""Git history analysis — commit parsing, file-level indices, line-level queries."""

from __future__ import annotations

import ast
import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from pathlib import Path
from statistics import median

from pysmelly.context import DEFAULT_COMMIT_MESSAGES
from pysmelly.discovery import run_git


@dataclass
class FileStats:
    """Aggregate line-change stats for a single file."""

    total_insertions: int = 0
    total_deletions: int = 0
    commit_count: int = 0


# Conventional commit prefixes (case-insensitive match before colon)
_CONVENTIONAL_PREFIXES = frozenset(
    {
        "fix",
        "feat",
        "refactor",
        "docs",
        "test",
        "tests",
        "chore",
        "style",
        "perf",
        "ci",
        "build",
        "revert",
    }
)

_WINDOW_RE = re.compile(r"^(\d+)([dmy])$")
_REVIEWED_RE = re.compile(r"^pysmelly:\s*reviewed\s+(.+)$", re.MULTILINE)

# Commit classification keywords (word-boundary matched)
_FIX_WORDS = re.compile(r"\b(fix|bug|bugfix|patch|correct|repair|resolve|hotfix)\b", re.IGNORECASE)
_FEATURE_WORDS = re.compile(r"\b(add|implement|introduce|support|create|new)\b", re.IGNORECASE)
_REFACTOR_WORDS = re.compile(
    r"\b(refactor|restructure|reorganize|simplify|clean\s*up|extract|inline|rename|move)\b",
    re.IGNORECASE,
)
_DEBT_WORDS = re.compile(
    r"\b(workaround|hack|temporary|todo|fixme|quick\s*fix|stopgap|kludge)\b",
    re.IGNORECASE,
)
_EMERGENCY_WORDS = re.compile(
    r"\b(hotfix|urgent|emergency|revert|rollback|cherry.?pick)\b",
    re.IGNORECASE,
)

# The label classify_commit gives a bug fix. The bug checks and the project
# trends' fix ratio all count it, so they name it rather than repeat the string.
FIX_CATEGORY = "fix"
# The label it gives new behaviour (a feature commit).
FEATURE_CATEGORY = "feature"

# Conventional commit prefix → category
_PREFIX_CATEGORIES = {
    "fix": "fix",
    "feat": FEATURE_CATEGORY,
    "refactor": "refactor",
}

# Emoji conventional commit prefixes (e.g., FastAPI / gitmoji style)
_EMOJI_CATEGORIES = {
    "\U0001f41b": "fix",  # 🐛
    "\U0001f6a8": "fix",  # 🚨
    "\u2705": "fix",  # ✅
    "\U0001f527": "fix",  # 🔧
    "\u2728": FEATURE_CATEGORY,  # ✨
    "\U0001f680": FEATURE_CATEGORY,  # 🚀
    "\U0001f389": FEATURE_CATEGORY,  # 🎉
    "\u267b\ufe0f": "refactor",  # ♻️
    "\U0001f3a8": "refactor",  # 🎨
    "\U0001f4a1": "refactor",  # 💡
    "\U0001f525": "emergency",  # 🔥
    "\u23ea": "emergency",  # ⏪ (revert)
}


def _emoji_category(message: str) -> str | None:
    """The category a leading gitmoji declares, or None without one."""
    for emoji, cat in _EMOJI_CATEGORIES.items():
        if message.startswith(emoji):
            return cat
    return None


def _conventional_type(message: str) -> str | None:
    """The conventional-commit type before the colon, or None without one.

    ``fix(auth): ...``, ``Fix: ...`` and ``feat!: ...`` give ``fix``, ``fix``
    and ``feat``. A prefix that is not a conventional type
    (``canary: ...``, ``S7a: ...``) is not one.
    """
    colon_pos = message.find(":")
    if colon_pos == -1:
        return None
    prefix = message[:colon_pos].strip().lower()
    paren_pos = prefix.find("(")
    if paren_pos != -1:
        prefix = prefix[:paren_pos]
    prefix = prefix.removesuffix("!")
    return prefix if prefix in _CONVENTIONAL_PREFIXES else None


def classify_commit(message: str) -> set[str]:
    """Classify a commit message into categories: fix, feature, refactor, debt.

    A commit can match multiple categories. Returns empty set for unclassified.

    A declared type (a conventional prefix or a gitmoji) decides whether the
    commit is a fix: ``docs: fix a typo`` and ``chore: fix lint`` are not,
    ``fix(lint): ...`` is. The fix keywords count only when the subject
    declares no type. The rule was chosen on 50 hand-labelled fleet subjects
    (DECISIONS.md, "Fix classification", Phase 31-3).
    """
    categories: set[str] = set()

    emoji_category = _emoji_category(message)
    if emoji_category is not None:
        categories.add(emoji_category)

    conventional_type = _conventional_type(message)
    if conventional_type in _PREFIX_CATEGORIES:
        categories.add(_PREFIX_CATEGORIES[conventional_type])

    declares_type = emoji_category is not None or conventional_type is not None
    if not declares_type and _FIX_WORDS.search(message):
        categories.add(FIX_CATEGORY)
    if _FEATURE_WORDS.search(message):
        categories.add(FEATURE_CATEGORY)
    if _REFACTOR_WORDS.search(message):
        categories.add("refactor")
    if _DEBT_WORDS.search(message):
        categories.add("debt")
    if _EMERGENCY_WORDS.search(message):
        categories.add("emergency")

    return categories


_WINDOW_UNITS = {
    "d": "days",
    "m": "months",
    "y": "years",
}


def _parse_window(window: str) -> str:
    """Convert compact window format (6m, 1y, 90d) to git --since format."""
    match = _WINDOW_RE.match(window)
    if not match:
        raise ValueError(
            f"Invalid git-window format: {window!r}. "
            f"Expected format like 6m, 1y, 90d (number + d/m/y)"
        )
    amount, unit = match.group(1), match.group(2)
    return f"{amount} {_WINDOW_UNITS[unit]} ago"


def describe_window(window: str, today: date) -> str:
    """Say which commits a run looked at, in a form a record can quote.

    The window is relative to the day the command runs, so a count taken on
    one day does not reproduce on another with no code change in between.
    Naming both the window and the date is what makes a git-history count
    reproducible: ``6m (git --since '6 months ago', as of 2026-09-22)``.
    ``today`` is the date the run counts from.
    """
    return f"{window} (git --since '{_parse_window(window)}', as of {today.isoformat()})"


def _is_quality_message(message: str) -> bool:
    """Check if a commit message meets basic quality criteria."""
    if len(message) <= 10:
        return False
    if _emoji_category(message) is not None or _conventional_type(message) is not None:
        return True
    # Check for Co-Authored-By (multi-line, but we only have subject line)
    # Any message > 10 chars that isn't a known low-quality pattern
    low_quality = {"wip", "stuff", "fix", "update", "changes", "tmp", "temp", "asdf"}
    return message.strip().lower() not in low_quality


@dataclass
class CommitInfo:
    """A single commit with its metadata and affected files."""

    hash: str
    date: datetime
    message: str
    author: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def python_files(self) -> list[str]:
        """The ``.py`` paths among ``files``, in commit order."""
        return [path for path in self.files if path.endswith(".py")]

    def postdates(self, moment: datetime | None) -> bool:
        """Whether the commit was made strictly after ``moment``; with no
        moment (a file never reviewed), every commit counts."""
        return moment is None or self.date > moment


@dataclass
class TimeSlice:
    """A uniform time period within the analysis window."""

    start: datetime
    end: datetime
    commits: list[CommitInfo] = field(default_factory=list)
    files_touched: set[str] = field(default_factory=set)
    files_by_category: dict[str, set[str]] = field(default_factory=dict)


def _window_to_days(window: str) -> int:
    """Convert compact window format to approximate number of days."""
    match = _WINDOW_RE.match(window)
    if not match:
        return 180  # fallback
    amount, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return amount
    if unit == "m":
        return amount * 30
    return amount * 365  # years


@dataclass(frozen=True)
class _NameStatus:
    """What one ``git log --name-status`` over the window says about files' lifecycles."""

    lifecycle: dict[str, tuple[int, int]]
    renames: list[tuple[str, str]]


class GitHistoryQueryError(Exception):
    """Raised when a git query behind a history view cannot be run or read."""


def _read_name_status(git_root: Path, window: str) -> _NameStatus:
    """Run the created/deleted/renamed query: count A and D lines per commit, list R lines.

    Raises:
        GitHistoryQueryError: git could not be run. A failure is never
            read as "nothing was created or deleted".
    """
    try:
        result = run_git(
            [
                "log",
                "--format=%x00%H",
                "--name-status",
                "--diff-filter=ADR",
                "--find-renames",
                "--no-merges",
                f"--since={_parse_window(window)}",
                "--",
                "*.py",
            ],
            git_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        msg = f"cannot list created and deleted files (git log --name-status): {err}"
        raise GitHistoryQueryError(msg) from err
    lifecycle: dict[str, tuple[int, int]] = {}
    renames: list[tuple[str, str]] = []
    current_hash = ""
    for line in result.stdout.split("\n"):
        if line.startswith("\x00"):
            current_hash = line[1:]
            continue
        status, *paths = line.split("\t")
        created, deleted = lifecycle.get(current_hash, (0, 0))
        if status == "A":
            lifecycle[current_hash] = (created + 1, deleted)
        elif status == "D":
            lifecycle[current_hash] = (created, deleted + 1)
        elif status.startswith("R"):
            old, new = paths
            renames.append((old, new))
    return _NameStatus(lifecycle, renames)


class GitHistory:
    """Parsed git history with file-level indices.

    Runs a single git log command and builds reverse indices for
    efficient lookup by file path.
    """

    def __init__(
        self,
        git_root: Path,
        window: str = "6m",
        commit_messages: str = DEFAULT_COMMIT_MESSAGES,
    ) -> None:
        self.git_root = git_root
        self.window = window
        self.commit_messages = commit_messages
        self._parsed = False
        self._commits: list[CommitInfo] = []
        self.commits_for_file: dict[str, list[CommitInfo]] = {}
        self.last_modified: dict[str, datetime] = {}
        self.reviewed_at: dict[str, datetime] = {}
        self._message_quality: float | None = None
        self._numstat_parsed: bool = False
        self._file_stats: dict[str, FileStats] = {}
        self._post_review_file_stats: dict[str, FileStats] = {}
        self.authors_for_file: dict[str, dict[str, int]] = {}
        self._time_slices: list[TimeSlice] | None = None
        self._commits_per_slice: float | None = None
        self._distinct_authors: int | None = None
        self._median_commit_size: float | None = None
        self._parse()

    @cached_property
    def line_history(self) -> LineHistory:
        """The line-level queries (hunks, ``git log -L``, blame) for this repo.

        Building it runs no git; each query runs on first use and is cached.
        """
        return LineHistory(self.git_root, self.window)

    def _parse(self) -> None:
        """Run git log and build indices."""
        if self._parsed:
            return
        self._parsed = True

        try:
            since = _parse_window(self.window)
        except ValueError:
            return

        try:
            result = run_git(
                [
                    "log",
                    "--format=%H%x00%aI%x00%an%x00%s",
                    "--name-only",
                    "--no-merges",
                    f"--since={since}",
                    "--",
                    "*.py",
                ],
                self.git_root,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return

        if not result.stdout.strip():
            return

        # Parse git log output: header lines are \x00-delimited,
        # followed by blank-line-separated filename lines. Lines are split
        # on \n alone: a subject or author name may hold a \r.
        current_commit: CommitInfo | None = None
        for line in result.stdout.split("\n"):
            if not line:
                # Blank line separates commit header from file list,
                # or separates file list from next commit
                continue
            if "\x00" in line:
                # This is a commit header line
                parts = line.split("\x00", 3)
                if len(parts) == 4:
                    commit_hash, date_str, author, message = parts
                    try:
                        date = datetime.fromisoformat(date_str)
                    except ValueError:
                        continue
                    current_commit = CommitInfo(
                        hash=commit_hash, date=date, message=message, author=author
                    )
                    self._commits.append(current_commit)
            elif current_commit is not None:
                # This is a filename line
                filepath = line.strip()
                if filepath:
                    current_commit.files.append(filepath)

        # Build reverse indices
        for commit in self._commits:
            for filepath in commit.files:
                self.commits_for_file.setdefault(filepath, []).append(commit)
                existing = self.last_modified.get(filepath)
                if existing is None or commit.date > existing:
                    self.last_modified[filepath] = commit.date
                # Track author contributions per file
                if commit.author:
                    author_counts = self.authors_for_file.setdefault(filepath, {})
                    author_counts[commit.author] = author_counts.get(commit.author, 0) + 1

        self._parse_reviewed(since)

    def _parse_reviewed(self, since: str) -> None:
        """Parse 'pysmelly: reviewed <path>' markers from commit messages.

        A commit message containing 'pysmelly: reviewed path/to/file.py'
        resets the last-modified clock for that file, even if the commit
        didn't touch it. This lets teams acknowledge persistent findings
        (like abandoned-code) without modifying the source file.
        """
        try:
            result = run_git(
                [
                    "log",
                    "--format=%H%x00%aI%x00%B%x00",
                    "--grep=pysmelly: reviewed",
                    f"--since={since}",
                ],
                self.git_root,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return

        if not result.stdout.strip():
            return

        # Split on the record separator (each commit ends with \x00\n)
        for record in result.stdout.split("\x00\n"):
            record = record.strip()
            if not record or "\x00" not in record:
                continue
            parts = record.split("\x00", 2)
            if len(parts) < 3:
                continue
            _commit_hash, date_str, body = parts
            try:
                date = datetime.fromisoformat(date_str)
            except ValueError:
                continue

            for match in _REVIEWED_RE.finditer(body):
                filepath = match.group(1).strip()
                if not filepath:
                    continue
                # Update last_modified so the file appears recently touched
                existing = self.last_modified.get(filepath)
                if existing is None or date > existing:
                    self.last_modified[filepath] = date
                # Track review date separately for audit
                existing_review = self.reviewed_at.get(filepath)
                if existing_review is None or date > existing_review:
                    self.reviewed_at[filepath] = date

    @property
    def commits(self) -> list[CommitInfo]:
        """Every commit in the window, newest first.

        Returns:
            The parsed commit list. Empty when the window holds no commits.
        """
        return self._commits

    @property
    def message_quality(self) -> float:
        """Fraction of commits with quality messages (0.0-1.0).

        Respects commit_messages override: "structured" -> 1.0, "unstructured" -> 0.0.
        """
        if self._message_quality is not None:
            return self._message_quality

        if self.commit_messages == "structured":
            self._message_quality = 1.0
        elif self.commit_messages == "unstructured":
            self._message_quality = 0.0
        else:
            # Auto-detect from sample
            sample = self._commits[:50]
            if not sample:
                self._message_quality = 0.0
            else:
                quality_count = sum(1 for c in sample if _is_quality_message(c.message))
                self._message_quality = quality_count / len(sample)

        return self._message_quality

    @property
    def distinct_authors(self) -> int:
        """Number of distinct commit authors in the window."""
        if self._distinct_authors is None:
            authors = {c.author for c in self._commits if c.author}
            self._distinct_authors = len(authors)
        return self._distinct_authors

    @property
    def median_commit_size(self) -> float:
        """Median number of .py files per commit."""
        if self._median_commit_size is None:
            sizes = [len([f for f in c.files if f.endswith(".py")]) for c in self._commits]
            self._median_commit_size = median(sizes) if sizes else 1.0
        return self._median_commit_size

    def commits_since_review(self, filepath: str) -> list[CommitInfo]:
        """Commits for a file, filtered to after reviewed_at if set."""
        commits = self.commits_for_file.get(filepath, [])
        review_date = self.reviewed_at.get(filepath)
        if review_date is None:
            return commits
        return [c for c in commits if c.date > review_date]

    def file_stats_since_review(self, filepath: str) -> FileStats | None:
        """File stats filtered to commits after reviewed_at if set.

        Falls back to full-window stats if no review date is set.
        For reviewed files, re-computes stats from only post-review commits
        using the numstat data.
        """
        review_date = self.reviewed_at.get(filepath)
        if review_date is None:
            return self.file_stats.get(filepath)

        # Re-compute from post-review numstat
        if not self._numstat_parsed:
            self._parse_numstat()
        post_review = self._post_review_file_stats.get(filepath)
        return post_review

    @property
    def file_stats(self) -> dict[str, FileStats]:
        """Lazy-parsed per-file insertion/deletion/commit stats from numstat."""
        if not self._numstat_parsed:
            self._parse_numstat()
        return self._file_stats

    def _parse_numstat(self) -> None:
        """Run git log --numstat and build per-file stats."""
        self._numstat_parsed = True

        try:
            since = _parse_window(self.window)
        except ValueError:
            return

        try:
            result = run_git(
                [
                    "log",
                    "--format=%H",
                    "--numstat",
                    "--no-merges",
                    f"--since={since}",
                    "--",
                    "*.py",
                ],
                self.git_root,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return

        if not result.stdout.strip():
            return

        # Build a hash→date index for post-review filtering
        commit_dates = {c.hash: c.date for c in self._commits}

        # Track which files appeared in each commit for commit_count
        current_commit_files: set[str] = set()
        current_hash: str = ""

        def _flush() -> None:
            for filepath in current_commit_files:
                self._file_stats.setdefault(filepath, FileStats()).commit_count += 1
                # Also count for post-review stats if this commit is after the review
                review_date = self.reviewed_at.get(filepath)
                if review_date is not None:
                    commit_date = commit_dates.get(current_hash)
                    if commit_date is not None and commit_date > review_date:
                        self._post_review_file_stats.setdefault(
                            filepath, FileStats()
                        ).commit_count += 1

        for line in result.stdout.split("\n"):
            line = line.strip()
            if not line:
                # Blank line — flush current commit's files
                _flush()
                current_commit_files = set()
                continue
            # Hash line (40-char hex)
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                # Flush previous commit
                _flush()
                current_commit_files = set()
                current_hash = line
                continue
            # Numstat line: insertions\tdeletions\tfilepath
            parts = line.split("\t", 2)
            if len(parts) == 3:
                ins_str, del_str, filepath = parts
                # Skip binary files (shown as - - path)
                if ins_str == "-" or del_str == "-":
                    continue
                try:
                    insertions = int(ins_str)
                    deletions = int(del_str)
                except ValueError:
                    continue
                stats = self._file_stats.setdefault(filepath, FileStats())
                stats.total_insertions += insertions
                stats.total_deletions += deletions
                current_commit_files.add(filepath)
                # Also accumulate for post-review stats
                review_date = self.reviewed_at.get(filepath)
                if review_date is not None:
                    commit_date = commit_dates.get(current_hash)
                    if commit_date is not None and commit_date > review_date:
                        post_stats = self._post_review_file_stats.setdefault(filepath, FileStats())
                        post_stats.total_insertions += insertions
                        post_stats.total_deletions += deletions

        # Flush last commit
        _flush()

    def window_start(self) -> datetime:
        """The instant the window opens: the cutoff git applies for ``--since``.

        Asked of git itself (``git rev-parse --since=...`` prints
        ``--max-age=<epoch>``) rather than recomputed, because ``6 months ago``
        is calendar arithmetic that ``_window_to_days`` only approximates.

        Raises:
            ValueError: the window is malformed.
            GitHistoryQueryError: git could not be run, or answered in a
                shape other than ``--max-age=<epoch>``.
        """
        since = _parse_window(self.window)
        try:
            result = run_git(["rev-parse", f"--since={since}"], self.git_root)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            msg = f"cannot read the window start (git rev-parse --since={since!r}): {err}"
            raise GitHistoryQueryError(msg) from err
        answer = result.stdout.strip()
        if not answer.startswith("--max-age="):
            msg = f"git rev-parse --since={since!r} answered {answer!r}, not --max-age=<epoch>"
            raise GitHistoryQueryError(msg)
        return datetime.fromtimestamp(int(answer.removeprefix("--max-age=")), tz=UTC)

    @property
    def lifecycle_by_commit(self) -> dict[str, tuple[int, int]]:
        """Python files each commit created and deleted, keyed by commit hash.

        Lazily runs one ``git log --name-status --diff-filter=ADR``. Rename
        detection is forced on, so a moved file is neither created nor
        deleted. A commit that created and deleted nothing is absent.
        """
        return self._name_status.lifecycle

    @property
    def renames(self) -> list[tuple[str, str]]:
        """Every Python file renamed in the window, as ``(old, new)``, newest commit first.

        Read from the same ``git log --name-status`` run as
        ``lifecycle_by_commit``, so asking for both costs one git call.
        """
        return self._name_status.renames

    @cached_property
    def _name_status(self) -> _NameStatus:
        return _read_name_status(self.git_root, self.window)

    @property
    def time_slices(self) -> list[TimeSlice]:
        """Uniform time periods dividing the analysis window."""
        if self._time_slices is None:
            self._build_time_slices()
        return self._time_slices  # type: ignore[return-value]

    @property
    def commits_per_slice(self) -> float:
        """Median commits per active time slice."""
        if self._commits_per_slice is None:
            # Trigger build if needed
            _ = self.time_slices
        return self._commits_per_slice or 0.0

    @property
    def is_coarse_grained(self) -> bool:
        """True if commit granularity is too coarse for sequence checks."""
        return self.commits_per_slice < 2.0

    def _build_time_slices(self) -> None:
        """Divide the analysis window into uniform time periods."""
        self._time_slices = []
        self._commits_per_slice = 0.0

        if not self._commits:
            return

        # Determine period from window string
        total_days = _window_to_days(self.window)
        period_days = max(7, total_days // 26)
        # Round to whole weeks
        period_days = max(7, (period_days // 7) * 7)
        period = timedelta(days=period_days)

        # Use the actual commit date range
        dates = [c.date for c in self._commits]
        earliest = min(dates)
        latest = max(dates)

        # Build slices from earliest to latest
        slice_start = earliest
        while slice_start <= latest:
            slice_end = slice_start + period
            self._time_slices.append(TimeSlice(start=slice_start, end=slice_end))
            slice_start = slice_end

        # Assign commits to slices
        for commit in self._commits:
            for ts in self._time_slices:
                if ts.start <= commit.date < ts.end:
                    ts.commits.append(commit)
                    for filepath in commit.files:
                        ts.files_touched.add(filepath)
                    categories = classify_commit(commit.message)
                    for cat in categories:
                        ts.files_by_category.setdefault(cat, set())
                        for filepath in commit.files:
                            ts.files_by_category[cat].add(filepath)
                    break
            else:
                # Commit falls on or after the last slice end — add to last slice
                if self._time_slices:
                    last = self._time_slices[-1]
                    last.commits.append(commit)
                    for filepath in commit.files:
                        last.files_touched.add(filepath)
                    categories = classify_commit(commit.message)
                    for cat in categories:
                        last.files_by_category.setdefault(cat, set())
                        for filepath in commit.files:
                            last.files_by_category[cat].add(filepath)

        # Compute median commits per active slice
        active_counts = [len(ts.commits) for ts in self._time_slices if ts.commits]
        self._commits_per_slice = median(active_counts) if active_counts else 0.0


# --- Line-level history -------------------------------------------------------
#
# The queries below are finer than the file-level window parse: one commit's
# hunks, one function's line-range history, one set of lines' blame. Each is
# too expensive to run over a whole repository, so nothing here runs until a
# check asks about a candidate it has already found, and every git answer is
# cached by its exact argument list in LineHistory._query.

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Extended header lines git prints between ``diff --git`` and the first hunk.
# Anything else there is a format this parser does not know, and is an error.
_EXTENDED_HEADER_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
)

# The flags every patch-producing query passes, so that user config
# (diff.noprefix, diff.mnemonicPrefix, color.ui, external diff drivers)
# cannot change the shape the parser reads.
_PATCH_FLAGS = ("--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/")

_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _unquote_path(raw: str) -> str:
    """A path as git prints it, with git's C-style quoting undone.

    Even with ``core.quotepath=off`` git quotes a name that holds a tab,
    newline, double quote or backslash, escaping bytes as ``\\ooo`` octal.
    """
    if not (len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            i += 1
            continue
        escape = body[i + 1 : i + 2]
        if escape in _C_ESCAPES:
            out.extend(_C_ESCAPES[escape].encode("utf-8"))
            i += 2
            continue
        octal = body[i + 1 : i + 4]
        if len(octal) != 3 or any(c not in "01234567" for c in octal):
            msg = f"cannot unquote git path {raw!r}"
            raise GitHistoryQueryError(msg)
        out.append(int(octal, 8))
        i += 4
    return out.decode("utf-8")


def _diff_path(raw: str, prefix: str) -> str | None:
    """The repo path on a ``---``/``+++`` line, or None for ``/dev/null``.

    Git appends a tab to an unquoted name that contains a space.
    """
    path = _unquote_path(raw.removesuffix("\t"))
    if path == "/dev/null":
        return None
    if not path.startswith(prefix):
        msg = f"diff path {raw!r} lacks the {prefix!r} prefix"
        raise GitHistoryQueryError(msg)
    return path.removeprefix(prefix)


@dataclass(frozen=True)
class Hunk:
    """One hunk of a diff, as git's ``@@ -old_start,old_count +new_start,new_count @@``.

    A zero count means the hunk removes nothing (old) or adds nothing (new);
    git then gives the line *before* the change as the start. ``body`` keeps
    the diff lines with their `` ``/``-``/``+`` prefixes, in order.
    """

    old_file: str | None
    new_file: str | None
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: tuple[str, ...]

    @property
    def file(self) -> str:
        """The path after the change, or before it when the change deleted the file."""
        path = self.new_file if self.new_file is not None else self.old_file
        if path is None:
            msg = "a hunk names neither an old nor a new file"
            raise GitHistoryQueryError(msg)
        return path

    @property
    def old_lines(self) -> range:
        """Line numbers the hunk covers before the change; empty when it removed nothing."""
        return range(self.old_start, self.old_start + self.old_count)

    @property
    def new_lines(self) -> range:
        """Line numbers the hunk covers after the change; empty when it added nothing."""
        return range(self.new_start, self.new_start + self.new_count)

    @property
    def added(self) -> list[str]:
        """The lines the change added, without the ``+`` prefix."""
        return [line[1:] for line in self.body if line.startswith("+")]

    @property
    def new_side(self) -> list[str]:
        """The hunk's lines after the change (context and added), without prefixes."""
        return [line[1:] for line in self.body if line[:1] in {" ", "+"}]

    @property
    def old_side(self) -> list[str]:
        """The hunk's lines before the change (context and removed), without prefixes."""
        return [line[1:] for line in self.body if line[:1] in {" ", "-"}]

    @property
    def removed(self) -> list[str]:
        """The lines the change removed, without the ``-`` prefix."""
        return [line[1:] for line in self.body if line.startswith("-")]


def _read_hunk(
    lines: list[str],
    start: int,
    header: re.Match[str],
    old_file: str | None,
    new_file: str | None,
) -> tuple[Hunk, int]:
    """Consume one hunk's body, counted from its header; return it and the next index."""
    old_start, old_count, new_start, new_count = (
        int(value) if value is not None else 1 for value in header.groups()
    )
    old_left, new_left = old_count, new_count
    body: list[str] = []
    i = start
    while old_left or new_left:
        if i >= len(lines):
            msg = f"diff ended inside the hunk {header.group(0)!r}"
            raise GitHistoryQueryError(msg)
        line = lines[i]
        i += 1
        tag = line[:1]
        if tag == "\\":
            continue  # "\ No newline at end of file"
        takes_old = tag in {" ", "-"}
        takes_new = tag in {" ", "+"}
        if (
            not (takes_old or takes_new)
            or (takes_old and not old_left)
            or (takes_new and not new_left)
        ):
            msg = f"line {line!r} does not fit the hunk {header.group(0)!r}"
            raise GitHistoryQueryError(msg)
        old_left -= takes_old
        new_left -= takes_new
        body.append(line)
    hunk = Hunk(old_file, new_file, old_start, old_count, new_start, new_count, tuple(body))
    return hunk, i


def _parse_patch(text: str) -> list[Hunk]:
    """Every hunk in git's patch output, in order.

    Lines are split on ``\\n`` only: ``str.splitlines`` would also split on
    form feeds and Unicode separators inside source lines. A combined
    (merge) diff, or any line this parser does not recognize, raises
    GitHistoryQueryError rather than being skipped.
    """
    lines = text.split("\n")
    # Every hunk body line carries a " ", "-", "+" or "\" prefix, so a line
    # starting "diff " can only open a file's section.
    starts = [i for i, line in enumerate(lines) if line.startswith("diff ")]
    stray = [line for line in lines[: starts[0] if starts else len(lines)] if line]
    if stray:
        msg = f"unexpected line before any file diff: {stray[0]!r}"
        raise GitHistoryQueryError(msg)
    if not starts:
        # A commit git lists with no diff: ``git log -L`` prints one when a
        # tracked range's history passes through a commit that left it as is.
        return []
    hunks: list[Hunk] = []
    for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        hunks.extend(_parse_file_diff(lines[start:end]))
    return hunks


def _parse_file_diff(lines: list[str]) -> list[Hunk]:
    """The hunks of one file's section, from its ``diff --git`` line on."""
    if not lines[0].startswith("diff --git "):
        msg = f"unsupported diff section {lines[0]!r} (a merge's combined diff?)"
        raise GitHistoryQueryError(msg)
    hunks: list[Hunk] = []
    old_file: str | None = None
    new_file: str | None = None
    i = 1
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("@@"):
            header = _HUNK_HEADER_RE.match(line)
            if header is None:
                msg = f"unrecognized hunk header {line!r}"
                raise GitHistoryQueryError(msg)
            hunk, i = _read_hunk(lines, i, header, old_file, new_file)
            hunks.append(hunk)
        elif line.startswith("--- "):
            old_file = _diff_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new_file = _diff_path(line[4:], "b/")
        elif line and not line.startswith(("\\", *_EXTENDED_HEADER_PREFIXES)):
            msg = f"unrecognized line in a file diff: {line!r}"
            raise GitHistoryQueryError(msg)
    return hunks


@dataclass(frozen=True)
class FunctionRevision:
    """One commit that changed a function's line range, and the hunks it made there.

    ``git log -L`` prints the whole tracked range with context, so the
    hunks' new side is the function as that commit left it.
    """

    sha: str
    hunks: tuple[Hunk, ...]

    @property
    def added(self) -> list[str]:
        """The lines this commit added to the function."""
        return [line for hunk in self.hunks for line in hunk.added]

    @property
    def source(self) -> str:
        """The function's text as this commit left it."""
        return "\n".join(line for hunk in self.hunks for line in hunk.new_side)


def _patch_records(output: str, query: str) -> list[tuple[str, list[Hunk]]]:
    """Split ``--format=%x00%H`` patch output into each commit's sha and hunks, in order.

    ``query`` names the git command in the error raised when anything but
    whitespace precedes the first commit.
    """
    preamble, *records = output.split("\x00")
    if preamble.strip():
        msg = f"unexpected output before the first commit of git {query}: {preamble!r}"
        raise GitHistoryQueryError(msg)
    parsed = []
    for record in records:
        sha, _, patch = record.partition("\n")
        parsed.append((sha, _parse_patch(patch)))
    return parsed


_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def qualified_definitions(tree: ast.Module) -> Iterable[tuple[str, _Definition]]:
    """Every function and class definition with its dotted qualified name."""
    pending: list[tuple[str, ast.AST]] = [("", tree)]
    while pending:
        prefix, node = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _Definition):
                qualname = f"{prefix}{child.name}"
                yield qualname, child
                pending.append((f"{qualname}.", child))
            else:
                pending.append((prefix, child))


def _line_ranges(lines: list[int]) -> list[tuple[int, int]]:
    """Sorted, distinct line numbers collapsed into inclusive runs."""
    runs: list[tuple[int, int]] = []
    for line in lines:
        if runs and runs[-1][1] == line - 1:
            runs[-1] = (runs[-1][0], line)
        else:
            runs.append((line, line))
    return runs


@dataclass(frozen=True)
class BlamedLine:
    """The commit ``git blame`` holds responsible for one line.

    ``written`` is that commit's author time and ``summary`` its subject line.
    """

    sha: str
    written: datetime
    summary: str


def _parse_line_porcelain(text: str) -> dict[int, BlamedLine]:
    """Read ``git blame --line-porcelain`` into each final line's commit.

    Each blamed line is a ``<sha> <orig-line> <final-line>[ <count>]``
    header, ``key value`` lines (``--line-porcelain`` repeats them for
    every line), then the line's content behind a tab.
    """
    blamed: dict[int, BlamedLine] = {}
    header: list[str] | None = None
    author_time: str | None = None
    summary: str | None = None
    for line in text.split("\n"):
        if header is None:
            if not line:
                continue
            header = line.split(" ")
            if len(header) not in {3, 4}:
                msg = f"unrecognized blame header {line!r}"
                raise GitHistoryQueryError(msg)
            author_time = None
            summary = None
        elif line.startswith("\t"):
            if author_time is None or summary is None:
                msg = f"blame record {' '.join(header)!r} has no author-time or summary"
                raise GitHistoryQueryError(msg)
            written = datetime.fromtimestamp(int(author_time), tz=UTC)
            blamed[int(header[2])] = BlamedLine(header[0], written, summary)
            header = None
        elif line.startswith("author-time "):
            author_time = line.removeprefix("author-time ")
        elif line.startswith("summary "):
            summary = line.removeprefix("summary ")
    return blamed


@dataclass(frozen=True)
class CommitRef:
    """A commit as a finding cites it: git's abbreviated sha and its subject line, as written.

    The subject is the committer's text, so anything that prints it goes
    through ``display_text``.
    """

    sha: str
    subject: str


# The longest commit subject (or other outside text) a provenance line quotes, in characters.
MAX_SUBJECT_WIDTH = 60

# Unicode categories never printed from outside text: control and format
# characters (escape sequences, carriage returns, bidirectional overrides)
# and the line and paragraph separators.
_UNPRINTED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def display_text(text: str) -> str:
    """Text from outside the analysis, such as a commit subject, made safe to print in a finding.

    Commit subjects are untrusted input. Every character in
    ``_UNPRINTED_CATEGORIES`` is dropped, and the rest is cut to
    ``MAX_SUBJECT_WIDTH`` characters with ``...`` marking the cut.
    """
    clean = "".join(
        char for char in text if unicodedata.category(char) not in _UNPRINTED_CATEGORIES
    ).strip()
    if len(clean) > MAX_SUBJECT_WIDTH:
        return clean[: MAX_SUBJECT_WIDTH - 3] + "..."
    return clean


def whole_word_pattern(token: str) -> re.Pattern[str]:
    """``token`` as a whole word: no identifier character directly before or after it."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class TokenCommit:
    """A commit ``git log -S`` lists for a token, with its hunks where the count changed."""

    commit: CommitRef
    hunks: tuple[Hunk, ...]


def token_delta(token: str, hunk: Hunk) -> int:
    """How many whole-word occurrences of ``token`` a hunk added, less how many it removed.

    ``git log -S`` matches the token as a plain substring, so it lists a
    commit that changed ``total`` in ``subtotal``; counting whole words here
    keeps that commit's hunk at zero.
    """
    pattern = whole_word_pattern(token)
    added = sum(len(pattern.findall(line)) for line in hunk.added)
    removed = sum(len(pattern.findall(line)) for line in hunk.removed)
    return added - removed


@dataclass(frozen=True)
class Provenance:
    """Where a name came from, and the commit that took its last production reader away.

    ``added`` is the commit that last took the name's count from none to
    some. ``last_removed`` is the commit that removed its last production
    reader, and is None unless that is proven: the name has no reader at
    HEAD, and the commit left none behind where its parent had some.
    """

    added: CommitRef
    last_removed: CommitRef | None

    def describe(self) -> str:
        """``added in <sha> (<subject>)``, then the removal when there is one."""
        text = f"added in {self.added.sha} ({display_text(self.added.subject)})"
        if self.last_removed is not None:
            removed = self.last_removed
            text += (
                f", last production reader removed in {removed.sha} "
                f"({display_text(removed.subject)})"
            )
        return text


@dataclass(frozen=True)
class TokenTrace:
    """When a token was added, and every commit that changed its count since, newest first."""

    added: CommitRef
    later: tuple[TokenCommit, ...]


def trace_addition(
    token: str, commits: list[TokenCommit], counts: Callable[[str], bool]
) -> TokenTrace | None:
    """When ``token`` was added, from ``token_commits``' answer (newest first).

    Only hunks in a file ``counts`` accepts (a git-root-relative path) are
    counted. Walking from the oldest commit, the running count starts at
    none, as the repository did; the addition is the last commit that took
    it from none to some. None when no commit did: git has no answer.

    A falling count says nothing about readers (a docstring mention, or two
    reads rewritten as one), so which later commit removed a reader is not
    decided here: the caller reads the code each one left.
    """
    added: int | None = None
    count = 0
    oldest_first = list(reversed(commits))
    for index, token_commit in enumerate(oldest_first):
        delta = sum(token_delta(token, hunk) for hunk in token_commit.hunks if counts(hunk.file))
        if count <= 0 < count + delta:
            added = index
        count += delta
    if added is None:
        return None
    later = tuple(reversed(oldest_first[added + 1 :]))
    return TokenTrace(oldest_first[added].commit, later)


class GitQueries:
    """Git queries for one repository, run lazily and cached by their exact arguments.

    Building it runs nothing. Paths are relative to the git root, as git
    prints them. Every git call goes through ``_query``, which caches the
    output by its argument list and raises GitHistoryQueryError when git
    fails; a failure is never read as "no history". The provenance queries
    live here because they read the whole history, not a window; the
    window-bound line queries are ``LineHistory``, which adds the window.
    """

    def __init__(self, git_root: Path) -> None:
        self.git_root = git_root
        self._outputs: dict[tuple[str, ...], str] = {}

    def _query(self, args: list[str], empty_exit: int | None = None) -> str:
        """Git's stdout for ``args``, run once per distinct argument list.

        ``empty_exit`` names an exit status that means "no output" rather than
        failure (``git grep`` exits 1 when nothing matched); it counts only
        when git wrote nothing to stderr.
        """
        key = tuple(args)
        if key not in self._outputs:
            try:
                result = run_git(args, self.git_root)
            except subprocess.CalledProcessError as err:
                if err.returncode == empty_exit and not err.stderr:
                    self._outputs[key] = ""
                    return ""
                msg = f"git {' '.join(args)} failed: {(err.stderr or '').strip()}"
                raise GitHistoryQueryError(msg) from err
            except (FileNotFoundError, UnicodeDecodeError) as err:
                msg = f"git {' '.join(args)} could not be run or read: {err}"
                raise GitHistoryQueryError(msg) from err
            self._outputs[key] = result.stdout
        return self._outputs[key]

    def blob(self, rev: str, path: str) -> str:
        """The text of ``path`` as ``rev`` holds it.

        Raises:
            GitHistoryQueryError: git failed (``rev`` has no such file included).
        """
        return self._query(["cat-file", "blob", f"{rev}:{path}"])

    def files_with_word(self, rev: str, token: str, pathspecs: tuple[str, ...]) -> list[str]:
        """The files ``rev`` holds, within ``pathspecs``, that contain ``token`` as a whole word.

        One ``git grep``; it narrows which files a caller reads, and may match
        more than the caller counts (a mention in a string or a comment).

        Raises:
            GitHistoryQueryError: git failed.
        """
        output = self._query(
            ["grep", "-z", "-l", "-I", "-w", "-F", "-e", token, rev, "--", *pathspecs],
            empty_exit=1,
        )
        return [entry.removeprefix(f"{rev}:") for entry in output.split("\0") if entry]

    def lines_at(self, rev: str, paths: Iterable[str]) -> dict[str, list[str]]:
        """The text of several files as ``rev`` holds them, as lines, from one ``git grep``.

        ``blob`` costs a git call per file; this costs one for all of them.
        A path ``rev`` lacks, or holds as an empty or binary file, is absent
        from the result. Lines are split on ``\\n`` only.

        Raises:
            ValueError: no path was given.
            GitHistoryQueryError: git failed, or printed a line it was not asked for.
        """
        pathspecs = [f":(literal){path}" for path in paths]
        if not pathspecs:
            msg = "lines_at needs at least one path"
            raise ValueError(msg)
        output = self._query(
            ["grep", "-z", "-n", "-I", "--no-color", "-e", "", rev, "--", *pathspecs],
            empty_exit=1,
        )
        found: dict[str, list[str]] = {}
        for record in output.split("\n")[:-1]:
            name, number, text = record.split("\0", 2)
            path = name.removeprefix(f"{rev}:")
            lines = found.setdefault(path, [])
            if int(number) != len(lines) + 1:
                msg = f"git grep gave {path} line {number} after line {len(lines)}"
                raise GitHistoryQueryError(msg)
            lines.append(text)
        return found

    def has_complete_history(self) -> bool:
        """Whether HEAD has history that reaches back to the first commit.

        False before the first commit (HEAD names nothing) and in a shallow
        clone, whose history is cut short: neither can say when anything began.
        """
        if not self._query(["rev-parse", "--verify", "--quiet", "HEAD"], empty_exit=1):
            return False
        return self._query(["rev-parse", "--is-shallow-repository"]).strip() == "false"

    def token_commits(
        self, token: str, pathspecs: tuple[str, ...], *, follow: bool
    ) -> list[TokenCommit]:
        """Every commit reachable from HEAD that changed how often ``token`` appears, newest first.

        Topological order, so a commit always comes before its parents.

        One ``git log -S`` over the whole history (not the window), limited
        to ``pathspecs``. ``follow`` tracks a single file back through its
        renames (``--follow``), for a name scoped to one file. Each commit
        comes with the hunks, without context, of the files whose count
        changed. ``-S`` takes the token as a plain string; ``token_delta``
        counts it as a whole word.

        Raises:
            ValueError: the token is empty or spans lines, or no pathspec was given.
            GitHistoryQueryError: git failed.
        """
        if not token or "\n" in token or not pathspecs:
            msg = f"token_commits needs a one-line token and a pathspec, got {token!r}"
            raise ValueError(msg)
        output = self._query(
            [
                "log",
                f"-S{token}",
                "-U0",
                "--no-merges",
                "--topo-order",
                "--find-renames",
                *(["--follow"] if follow else []),
                "--format=%x00%h %s",
                *_PATCH_FLAGS,
                "HEAD",
                "--",
                *pathspecs,
            ]
        )
        preamble, *records = output.split("\x00")
        if preamble.strip():
            msg = f"unexpected output before the first commit of git log -S: {preamble!r}"
            raise GitHistoryQueryError(msg)
        commits = []
        for record in records:
            header, _, patch = record.partition("\n")
            sha, _, subject = header.partition(" ")
            commits.append(TokenCommit(CommitRef(sha, subject), tuple(_parse_patch(patch))))
        return commits


class LineHistory(GitQueries):
    """Line-level git queries for one repository, run lazily and cached.

    Reached as ``GitHistory.line_history``, so it exists only inside a git
    root, and building it runs nothing. Line numbers refer to the file at
    HEAD, not the working tree. Queries bounded by the run's window read
    ``window``; the rest are ``GitQueries``', whose ``_query`` runs and
    caches every call, so a new kind of query is one more method on it.
    """

    def __init__(self, git_root: Path, window: str) -> None:
        super().__init__(git_root)
        self.window = window

    def commit_hunks(self, sha: str) -> list[Hunk]:
        """The Python-file hunks one commit made, with no context lines.

        Renames are detected, so an edited moved file is one hunk set under
        its new name with ``old_file`` naming the old one. A merge commit
        prints a combined diff, which the parser rejects.
        """
        output = self._query(
            ["show", "-U0", "--format=", "--find-renames", *_PATCH_FLAGS, sha, "--", "*.py"]
        )
        return _parse_patch(output)

    def head_source(self, path: str) -> str | None:
        """The text of ``path`` as committed at HEAD, or None when HEAD has no such file.

        Line numbers from ``line_ages`` and ``function_history`` refer to this
        text, so a check that blames lines finds them here rather than in the
        working tree, where uncommitted edits can shift them.
        """
        if not self._query(["ls-tree", "--name-only", "HEAD", "--", path]):
            return None
        return self.blob("HEAD", path)

    def head_paths(self) -> frozenset[str]:
        """Every file path HEAD holds, from one ``git ls-tree``.

        A check about to blame analyzed files asks it which of them are
        committed at all: an untracked file has no lines to blame.
        """
        output = self._query(["ls-tree", "-r", "-z", "--name-only", "HEAD"])
        return frozenset(entry for entry in output.split("\0") if entry)

    def head_files_matching(self, pattern: str) -> frozenset[str]:
        """The Python files at HEAD with a line matching ``pattern``.

        One ``git grep`` over the HEAD tree; ``pattern`` is a POSIX extended
        regular expression, matched case-insensitively. It narrows which files
        a check reads at HEAD, so it may match more than the check counts.
        """
        output = self._query(
            ["grep", "-z", "-l", "-I", "-i", "-E", "-e", pattern, "HEAD", "--", "*.py"],
            empty_exit=1,
        )
        return frozenset(entry.removeprefix("HEAD:") for entry in output.split("\0") if entry)

    def head_tree(self, path: str) -> ast.Module | None:
        """``head_source`` parsed, or None when HEAD has no such file.

        Raises:
            GitHistoryQueryError: git failed, or the file at HEAD does not parse.
        """
        source = self.head_source(path)
        if source is None:
            return None
        try:
            return ast.parse(source)
        except SyntaxError as err:
            msg = f"{path} at HEAD does not parse: {err}"
            raise GitHistoryQueryError(msg) from err

    def function_history(self, path: str, qualname: str) -> list[FunctionRevision] | None:
        """The window's commits that touched one definition at HEAD, newest first.

        ``qualname`` is the dotted name from module level (``Class.method``,
        ``outer.inner``). The range is the definition's lines in the file at
        HEAD, decorators included, so uncommitted edits cannot shift it.
        ``git log -L`` follows the range back through renames of the file,
        but not into another file: a function moved between files has a
        history that begins at the commit that added it to its current file
        (``line_ages`` is what sees through such a move).

        Returns:
            The revisions, or None when the file or the definition does not
            exist at HEAD: it has no committed history to read.

        Raises:
            ValueError: the window is malformed, or ``qualname`` names more
                than one definition at HEAD (a property and its setter).
            GitHistoryQueryError: git failed, or the file at HEAD does not parse.
        """
        tree = self.head_tree(path)
        if tree is None:
            return None
        matches = [node for name, node in qualified_definitions(tree) if name == qualname]
        if not matches:
            return None
        if len(matches) > 1:
            msg = f"{qualname} names {len(matches)} definitions in {path} at HEAD"
            raise ValueError(msg)
        node = matches[0]
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        return self.ranges_history([(path, start, end)])

    def ranges_history(self, ranges: Iterable[tuple[str, int, int]]) -> list[FunctionRevision]:
        """The window's commits that touched any of several HEAD line ranges, newest first.

        Each range is ``(path, first line, last line)`` in the file at HEAD.
        One ``git log`` tracks them all, so asking about many functions costs
        one walk of the window rather than one per function. Each revision's
        hunks are one per range the commit touched (git merges ranges that
        overlap or abut), under the file's name at that commit: the ranges
        follow renames, as ``function_history``'s does. Which range a hunk
        belongs to is the caller's to tell from its text.

        Raises:
            ValueError: the window is malformed, or no range was given.
            GitHistoryQueryError: git failed (a range past the end of its
                file at HEAD included).
        """
        line_ranges = [f"-L{start},{end}:{path}" for path, start, end in ranges]
        if not line_ranges:
            msg = "ranges_history needs at least one range"
            raise ValueError(msg)
        output = self._query(
            [
                "log",
                *line_ranges,
                "--format=%x00%H",
                "--no-merges",
                f"--since={_parse_window(self.window)}",
                *_PATCH_FLAGS,
                "HEAD",
            ]
        )
        return [
            FunctionRevision(sha, tuple(hunks)) for sha, hunks in _patch_records(output, "log -L")
        ]

    def changes_matching(self, pattern: str, since: datetime) -> list[tuple[str, list[Hunk]]]:
        """Each commit since ``since`` that added or removed a line matching ``pattern``.

        Newest first.

        One ``git log -G`` for every name a check asks about, so many
        candidates cost one walk. ``pattern`` is a POSIX extended regular
        expression (``-G``'s syntax), matched against the added and removed
        lines of Python files. Each commit comes with its sha and the hunks
        it made to the files that matched, with no context lines; which
        lines matched is the caller's to read from them. ``since`` is the
        check's own bar, not the run's window.

        Raises:
            GitHistoryQueryError: git failed (a malformed pattern included).
        """
        output = self._query(
            [
                "log",
                f"-G{pattern}",
                "-U0",
                "--format=%x00%H",
                "--no-merges",
                "--find-renames",
                f"--since={since.isoformat()}",
                *_PATCH_FLAGS,
                "HEAD",
                "--",
                "*.py",
            ]
        )
        return _patch_records(output, "log -G")

    def edit_hunks(self, shas: Iterable[str], paths: Iterable[str]) -> dict[str, list[Hunk]]:
        """The hunks several commits made to files that existed before them, by sha.

        One ``git show -U0`` for every commit, limited to ``paths`` (taken
        literally, not as globs) and to modified or renamed files: a file a
        commit created or deleted contributes no hunks. A commit whose edits
        all fall outside that maps to an empty list (git prints nothing for
        it, not even its header).

        Raises:
            ValueError: no sha or no path was given.
            GitHistoryQueryError: git failed, or answered for a commit not asked.
        """
        wanted = list(shas)
        pathspecs = [f":(literal){path}" for path in paths]
        if not wanted or not pathspecs:
            msg = "edit_hunks needs at least one sha and one path"
            raise ValueError(msg)
        output = self._query(
            [
                "show",
                "-U0",
                "--format=%x00%H",
                "--find-renames",
                "--diff-filter=MR",
                *_PATCH_FLAGS,
                *wanted,
                "--",
                *pathspecs,
            ]
        )
        records = dict(_patch_records(output, "show"))
        stray = sorted(set(records) - set(wanted))
        if stray:
            msg = f"git show answered for commits not asked: {stray}"
            raise GitHistoryQueryError(msg)
        return {sha: records.get(sha, []) for sha in wanted}

    def head_changed_paths(self) -> frozenset[str]:
        """The Python files whose working-tree text differs from HEAD (staged or not).

        A check that reads line numbers from the analyzed working tree uses
        this to know where they are also HEAD's, the lines git queries take.
        """
        output = self._query(["diff", "--name-only", "-z", "HEAD", "--", "*.py"])
        return frozenset(entry for entry in output.split("\0") if entry)

    def line_ages(self, path: str, lines: Iterable[int]) -> dict[int, datetime]:
        """When each of the given HEAD lines of ``path`` was last written.

        The author time of the commit ``line_commits`` names for the line.

        Raises:
            GitHistoryQueryError: as ``line_commits`` does.
        """
        return {line: blamed.written for line, blamed in self.line_commits(path, lines).items()}

    def line_commits(self, path: str, lines: Iterable[int]) -> dict[int, BlamedLine]:
        """The commit that last wrote each of the given HEAD lines of ``path``.

        The commit ``git blame`` holds responsible. Whitespace-only changes
        are ignored (a reformat does not make a line young), and lines moved
        within the file or from another file changed in the same commit keep
        their original commit (``-M -C``), so moving a function does not
        reset its age. Not limited to the window: a line is as old as it is.

        Raises:
            GitHistoryQueryError: git failed (a line past the end of the
                file at HEAD included), or answered for other lines.
        """
        wanted = sorted(set(lines))
        if not wanted:
            return {}
        ranges = [f"-L{start},{end}" for start, end in _line_ranges(wanted)]
        output = self._query(
            ["blame", "--line-porcelain", "-w", "-M", "-C", *ranges, "HEAD", "--", path]
        )
        blamed = _parse_line_porcelain(output)
        if sorted(blamed) != wanted:
            msg = f"git blame on {path} answered lines {sorted(blamed)}, asked {wanted}"
            raise GitHistoryQueryError(msg)
        return blamed

    def files_line_ages(
        self, wanted: Mapping[str, Iterable[int]]
    ) -> dict[str, dict[int, datetime]]:
        """``line_ages`` for the given lines of several files, the blames run side by side.

        ``git blame`` takes one file per call, so lines in many files cost one
        process each; this runs them concurrently rather than one after
        another. Each call is cached under its own arguments, as one
        ``line_ages`` call is.

        Raises:
            GitHistoryQueryError: as ``line_ages`` does, for any of the files.
        """
        return {
            path: {line: blamed.written for line, blamed in lines.items()}
            for path, lines in self.files_line_commits(wanted).items()
        }

    def files_line_commits(
        self, wanted: Mapping[str, Iterable[int]]
    ) -> dict[str, dict[int, BlamedLine]]:
        """``line_commits`` for the given lines of several files, the blames run side by side.

        Raises:
            GitHistoryQueryError: as ``line_commits`` does, for any of the files.
        """
        with ThreadPoolExecutor() as pool:
            futures = {path: pool.submit(self.line_commits, path, wanted[path]) for path in wanted}
        return {path: future.result() for path, future in futures.items()}
