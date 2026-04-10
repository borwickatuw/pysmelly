"""Shared test helpers for git history check tests."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats, GitHistory, TimeSlice

_NOW = datetime.now(timezone.utc)
_RECENT = _NOW - timedelta(days=30)  # 1 month ago
_STALE = _NOW - timedelta(days=400)  # ~13 months ago


def make_ctx(
    files: dict[str, str],
    last_modified: dict[str, datetime] | None = None,
    commits: list[CommitInfo] | None = None,
    file_stats: dict[str, FileStats] | None = None,
    message_quality: float = 0.5,
    authors_for_file: dict[str, dict[str, int]] | None = None,
    time_slices: list[TimeSlice] | None = None,
    commits_per_slice: float | None = None,
    distinct_authors: int = 5,
    median_commit_size: float = 1.0,
) -> AnalysisContext:
    """Build an AnalysisContext with a mocked git_history."""
    all_trees = {Path(name): ast.parse(code) for name, code in files.items()}
    ctx = AnalysisContext(all_trees, verbose=False)

    # Build a minimal GitHistory mock via patching
    history = object.__new__(GitHistory)
    history.commits_for_file = {}
    history.last_modified = last_modified or {}
    history._message_quality = message_quality
    history.commit_messages = "auto"
    history.window = "6m"
    history._commits = commits or []
    history._numstat_parsed = True
    history._file_stats = file_stats or {}
    history.authors_for_file = authors_for_file or {}
    history._time_slices = time_slices
    history._commits_per_slice = commits_per_slice
    history._distinct_authors = distinct_authors
    history._median_commit_size = median_commit_size
    history.reviewed_at = {}
    history._post_review_file_stats = {}

    # Build commits_for_file from commits
    if commits:
        for commit in commits:
            for filepath in commit.files:
                history.commits_for_file.setdefault(filepath, []).append(commit)

    # Build authors_for_file from commits if not provided
    if not authors_for_file and commits:
        history.authors_for_file = {}
        for commit in commits:
            if commit.author:
                for filepath in commit.files:
                    author_counts = history.authors_for_file.setdefault(filepath, {})
                    author_counts[commit.author] = author_counts.get(commit.author, 0) + 1

    ctx._git_history = history
    ctx._git_history_computed = True
    return ctx


def make_large_file(line_count: int) -> str:
    """Create a Python file with approximately N lines."""
    lines = [f"x_{i} = {i}" for i in range(line_count)]
    return "\n".join(lines) + "\n"


def fix_commits(filepath: str, count: int) -> list[CommitInfo]:
    """Create fix commits touching a file."""
    return [
        CommitInfo(
            hash=f"fix{i:04d}",
            date=_RECENT - timedelta(days=i),
            message=f"fix: resolve issue #{i}",
            files=[filepath],
        )
        for i in range(count)
    ]


def feat_commits(filepath: str, count: int) -> list[CommitInfo]:
    """Create feature commits touching a file."""
    return [
        CommitInfo(
            hash=f"feat{i:04d}",
            date=_RECENT - timedelta(days=i),
            message=f"feat: add feature #{i}",
            files=[filepath],
        )
        for i in range(count)
    ]


def make_time_slices(
    file_str: str,
    pattern: list[str],
    period_days: int = 14,
) -> list[TimeSlice]:
    """Create time slices with specified activity pattern.

    pattern is a list of strings like "feature", "fix", "both", "active", "inactive".
    """

    slices = []
    base = _NOW - timedelta(days=len(pattern) * period_days)
    for i, kind in enumerate(pattern):
        start = base + timedelta(days=i * period_days)
        end = start + timedelta(days=period_days)
        ts = TimeSlice(start=start, end=end)
        if kind == "inactive":
            pass
        elif kind == "feature":
            c = CommitInfo(
                hash=f"feat_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"feat: feature in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("feature", set()).add(file_str)
        elif kind == "fix":
            c = CommitInfo(
                hash=f"fix_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"fix: fix in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("fix", set()).add(file_str)
        elif kind == "both":
            c1 = CommitInfo(
                hash=f"feat_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"feat: feature in slice {i}",
                files=[file_str],
            )
            c2 = CommitInfo(
                hash=f"fix_s{i:04d}",
                date=start + timedelta(days=2),
                message=f"fix: fix in slice {i}",
                files=[file_str],
            )
            ts.commits.extend([c1, c2])
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("feature", set()).add(file_str)
            ts.files_by_category.setdefault("fix", set()).add(file_str)
        elif kind == "active":
            c = CommitInfo(
                hash=f"act_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"chore: work in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
        slices.append(ts)

    return slices


def commits_from_slices(slices: list[TimeSlice]) -> list[CommitInfo]:
    """Extract all commits from time slices."""
    commits = []
    for ts in slices:
        commits.extend(ts.commits)
    return commits


def bulk_commit(filepath: str, idx: int) -> CommitInfo:
    """Create a commit touching 30+ .py files (bulk)."""
    files = [filepath] + [f"bulk_{idx}_{j}.py" for j in range(30)]
    return CommitInfo(
        hash=f"bulk{idx:04d}",
        date=_RECENT - timedelta(days=idx),
        message=f"fix: bulk refactor #{idx}",
        files=files,
    )
