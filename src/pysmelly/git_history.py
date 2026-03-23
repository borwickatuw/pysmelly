"""Git history analysis — commit parsing and file-level indices."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median


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

# Conventional commit prefix → category
_PREFIX_CATEGORIES = {
    "fix": "fix",
    "feat": "feature",
    "refactor": "refactor",
}


def classify_commit(message: str) -> set[str]:
    """Classify a commit message into categories: fix, feature, refactor, debt.

    A commit can match multiple categories. Returns empty set for unclassified.
    """
    categories: set[str] = set()

    # Check conventional commit prefix first (strongest signal)
    colon_pos = message.find(":")
    if colon_pos != -1:
        prefix = message[:colon_pos].strip().lower()
        paren_pos = prefix.find("(")
        if paren_pos != -1:
            prefix = prefix[:paren_pos]
        if prefix in _PREFIX_CATEGORIES:
            categories.add(_PREFIX_CATEGORIES[prefix])

    # Keyword matching on the full message
    if _FIX_WORDS.search(message):
        categories.add("fix")
    if _FEATURE_WORDS.search(message):
        categories.add("feature")
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


def _is_quality_message(message: str) -> bool:
    """Check if a commit message meets basic quality criteria."""
    if len(message) <= 10:
        return False
    # Check for conventional commit prefix
    colon_pos = message.find(":")
    if colon_pos != -1:
        prefix = message[:colon_pos].strip().lower()
        # Handle scoped prefixes like "fix(auth):"
        paren_pos = prefix.find("(")
        if paren_pos != -1:
            prefix = prefix[:paren_pos]
        if prefix in _CONVENTIONAL_PREFIXES:
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


class GitHistory:
    """Parsed git history with file-level indices.

    Runs a single git log command and builds reverse indices for
    efficient lookup by file path.
    """

    def __init__(self, git_root: Path, window: str = "6m", commit_messages: str = "auto") -> None:
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
        self.authors_for_file: dict[str, dict[str, int]] = {}
        self._time_slices: list[TimeSlice] | None = None
        self._commits_per_slice: float | None = None
        self._parse()

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
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--format=%H%x00%aI%x00%an%x00%s",
                    "--name-only",
                    "--no-merges",
                    f"--since={since}",
                    "--",
                    "*.py",
                ],
                capture_output=True,
                text=True,
                cwd=self.git_root,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return

        if not result.stdout.strip():
            return

        # Parse git log output: header lines are \x00-delimited,
        # followed by blank-line-separated filename lines
        current_commit: CommitInfo | None = None
        for line in result.stdout.splitlines():
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
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--format=%H%x00%aI%x00%B%x00",
                    "--grep=pysmelly: reviewed",
                    f"--since={since}",
                ],
                capture_output=True,
                text=True,
                cwd=self.git_root,
                check=True,
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
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--format=%H",
                    "--numstat",
                    "--no-merges",
                    f"--since={since}",
                    "--",
                    "*.py",
                ],
                capture_output=True,
                text=True,
                cwd=self.git_root,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return

        if not result.stdout.strip():
            return

        # Track which files appeared in each commit for commit_count
        current_commit_files: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                # Blank line — flush current commit's files
                for filepath in current_commit_files:
                    self._file_stats.setdefault(filepath, FileStats()).commit_count += 1
                current_commit_files = set()
                continue
            # Hash line (40-char hex)
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                # Flush previous commit
                for filepath in current_commit_files:
                    self._file_stats.setdefault(filepath, FileStats()).commit_count += 1
                current_commit_files = set()
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

        # Flush last commit
        for filepath in current_commit_files:
            self._file_stats.setdefault(filepath, FileStats()).commit_count += 1

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
