"""Shared helpers for git history checks."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from pysmelly.git_history import CommitInfo, FileStats, GitHistory, TimeSlice

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext

_MIN_MESSAGE_QUALITY = 0.5
_BULK_COMMIT_THRESHOLD = 30

# Files that are naturally stable and shouldn't be flagged
SKIP_NAMES = frozenset({"__init__.py", "conftest.py", "apps.py"})

# Files that match these patterns are config-like and naturally stable
SKIP_SUFFIXES = ("_config.py", "_settings.py", "settings.py", "config.py")

CATEGORY = "git-history"

# Minimum file sizes for various checks
MIN_LINES_SMALL = 20
MIN_LINES_MEDIUM = 50

# Skip commits touching this many .py files in co-change analysis
COCHANGE_SKIP_THRESHOLD = 20


def is_bulk_commit(commit: CommitInfo) -> bool:
    """Commits touching 30+ .py files are likely mechanical."""
    return sum(1 for f in commit.files if f.endswith(".py")) >= _BULK_COMMIT_THRESHOLD


def is_test_file(filepath: str) -> bool:
    name = Path(filepath).name
    return name.startswith("test_") or name.endswith("_test.py")


def get_line_count(file_path: str, all_trees: dict) -> int:
    """Current line count from last AST statement's end_lineno."""
    tree = all_trees.get(Path(file_path))
    if tree is None or not tree.body:
        return 0
    return tree.body[-1].end_lineno


def semantic_guard(history: GitHistory | None) -> GitHistory | None:
    """Return the history object if semantic checks should run, else None."""
    if history is None:
        return None
    if history.message_quality < _MIN_MESSAGE_QUALITY:
        return None
    return history


def slices_since_review(
    slices: list[TimeSlice], history: GitHistory, filepath: str
) -> list[TimeSlice]:
    """Filter time slices to only those after the file's review date."""
    review_date = history.reviewed_at.get(filepath)
    if review_date is None:
        return slices
    return [ts for ts in slices if ts.end > review_date]


def is_expected_coupling(file_a: str, file_b: str, patterns: list[list[str]]) -> bool:
    """Check if a file pair matches any expected-coupling pattern pair."""
    for pat_a, pat_b in patterns:
        if (fnmatch.fnmatch(file_a, pat_a) and fnmatch.fnmatch(file_b, pat_b)) or (
            fnmatch.fnmatch(file_a, pat_b) and fnmatch.fnmatch(file_b, pat_a)
        ):
            return True
    return False


def coupling_ratio(
    counts: dict[str, int], file_a: str, file_b: str, co_changes: int, threshold: float
) -> float | None:
    """Compute coupling ratio and return it if above threshold, else None.

    Returns co_changes / min(count_a, count_b) when both counts are non-zero
    and the ratio meets or exceeds *threshold*.
    """
    count_a = counts.get(file_a, 0)
    count_b = counts.get(file_b, 0)
    min_count = min(count_a, count_b)
    if min_count == 0:
        return None
    ratio = co_changes / min_count
    if ratio < threshold:
        return None
    return ratio


def history_time_slices(
    ctx: AnalysisContext, min_slices: int
) -> tuple[GitHistory, list[TimeSlice]] | None:
    """Return (history, slices) if enough time slices exist, else None."""
    history = ctx.git_history
    if history is None:
        return None
    slices = history.time_slices
    if len(slices) < min_slices:
        return None
    return history, slices


def churned_files(
    history: GitHistory,
    ctx: AnalysisContext,
    min_lines: int,
    min_commits: int,
) -> list[tuple[str, int, FileStats]]:
    """Collect non-test files with enough lines and commits for churn analysis.

    Returns (file_str, current_lines, stats) tuples.
    """
    results: list[tuple[str, int, FileStats]] = []
    for file_path in ctx.all_trees:
        file_str = str(file_path)
        if is_test_file(file_str):
            continue
        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < min_lines:
            continue
        stats = history.file_stats_since_review(file_str)
        if stats is None:
            continue
        if stats.commit_count < min_commits:
            continue
        results.append((file_str, current_lines, stats))
    return results
