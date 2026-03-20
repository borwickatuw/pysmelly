"""Git history analysis — commit parsing and file-level indices."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    files: list[str] = field(default_factory=list)


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
                    "--format=%H%x00%aI%x00%s",
                    "--name-only",
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
                parts = line.split("\x00", 2)
                if len(parts) == 3:
                    commit_hash, date_str, message = parts
                    try:
                        date = datetime.fromisoformat(date_str)
                    except ValueError:
                        continue
                    current_commit = CommitInfo(hash=commit_hash, date=date, message=message)
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
