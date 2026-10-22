"""Shared helpers for git history checks."""

from __future__ import annotations

import fnmatch
import functools
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import INIT_FILE, MAJORITY
from pysmelly.git_history import (
    FIX_CATEGORY,
    CommitInfo,
    FileStats,
    GitHistory,
    GitHistoryQueryError,
    TimeSlice,
    classify_commit,
)
from pysmelly.registry import CheckFn, Finding, SiteRender

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext

_BULK_COMMIT_THRESHOLD = 30

# Files that are naturally stable and shouldn't be flagged
SKIP_NAMES = frozenset({INIT_FILE, "apps.py"})

# Files that match these patterns are config-like and naturally stable
SKIP_SUFFIXES = ("_config.py", "_settings.py", "settings.py", "config.py")

# Minimum file sizes for various checks
MIN_LINES_SMALL = 20
MIN_LINES_MEDIUM = 50

# Skip commits touching this many .py files in co-change analysis
COCHANGE_SKIP_THRESHOLD = 20

# Commit subjects and code quoted in a message are cut to this many characters.
_PREVIEW_WIDTH = 60


def is_bulk_commit(commit: CommitInfo) -> bool:
    """Commits touching 30+ .py files are likely mechanical."""
    return len(commit.python_files) >= _BULK_COMMIT_THRESHOLD


def counted_commits(history: GitHistory, path: str) -> list[CommitInfo]:
    """The commits to *path* since its last review, less bulk (mechanical) commits."""
    return [c for c in history.commits_since_review(path) if not is_bulk_commit(c)]


def category_count(commits: Iterable[CommitInfo], category: str) -> int:
    """How many of *commits* the message classifier puts in *category*."""
    return sum(1 for c in commits if category in classify_commit(c.message))


def get_line_count(file_path: str, all_trees: dict) -> int:
    """Current line count from last AST statement's end_lineno."""
    tree = all_trees.get(Path(file_path))
    if tree is None or not tree.body:
        return 0
    return tree.body[-1].end_lineno


def has_semantic_history(history: GitHistory | None) -> TypeGuard[GitHistory]:
    """Whether history exists with commit messages good enough for semantic checks."""
    return history is not None and history.message_quality >= MAJORITY


def slices_since_review(
    slices: list[TimeSlice], history: GitHistory, filepath: str
) -> list[TimeSlice]:
    """Filter time slices to only those after the file's review date."""
    review_date = history.reviewed_at.get(filepath)
    if review_date is None:
        return slices
    return [ts for ts in slices if ts.end > review_date]


def notes_persisting_review(check_fn: CheckFn) -> CheckFn:
    """Mark each finding on a reviewed file with the review date.

    Apply it to a check that reads only the commits made after a file's
    ``pysmelly: reviewed`` marker. Any finding such a check reports on a
    reviewed file fired on post-review commits, so the pattern outlived the
    review; the message says ``reviewed YYYY-MM-DD, pattern persists`` so the
    acknowledgment is visibly registered rather than looking ignored. A file
    with no marker, or a run with ``--ignore-reviewed`` (which empties
    ``reviewed_at``), gets no note.
    """

    @functools.wraps(check_fn)
    def wrapper(ctx: AnalysisContext) -> list[Finding]:
        findings = check_fn(ctx)
        history = ctx.git_history
        if history is None:
            return findings
        return [_with_review_note(finding, history) for finding in findings]

    return wrapper


def _with_review_note(finding: Finding, history: GitHistory) -> Finding:
    review_date = history.reviewed_at.get(finding.file)
    if review_date is None:
        return finding
    return replace(
        finding,
        message=f"{finding.message} — reviewed {review_date:%Y-%m-%d}, pattern persists",
    )


def notes_persisting_pair_review(check_fn: CheckFn) -> CheckFn:
    """Mark each pair finding whose files are all reviewed with the latest review date.

    Apply it to a check that judges pairs of files through
    ``coupled_pairs``: a pair is narrowed to the commits after its
    later marker only when both files carry one, so the note goes on a
    finding only when every file it is sited on is reviewed (for a
    package-level finding, every pair in it was narrowed). The note reads as
    ``notes_persisting_review``'s does, dated by the latest marker, and the
    finding's ``render`` keeps it when suppression re-renders the message.
    """

    @functools.wraps(check_fn)
    def wrapper(ctx: AnalysisContext) -> list[Finding]:
        findings = check_fn(ctx)
        history = ctx.git_history
        if history is None:
            return findings
        return [_with_pair_review_note(finding, history) for finding in findings]

    return wrapper


def _with_pair_review_note(finding: Finding, history: GitHistory) -> Finding:
    files = {site.file for site in finding.places}
    dates = [date for path in files if (date := history.reviewed_at.get(path)) is not None]
    if len(dates) < len(files):
        return finding
    note = f"reviewed {max(dates):%Y-%m-%d}, pattern persists"
    render = finding.render
    if render is not None:
        render = functools.partial(_render_with_note, render, note)
    return replace(finding, message=f"{finding.message} — {note}", render=render)


def _render_with_note(render: SiteRender, note: str, indices: tuple[int, ...]) -> str | None:
    message = render(indices)
    return None if message is None else f"{message} — {note}"


def is_expected_coupling(file_a: str, file_b: str, patterns: list[list[str]]) -> bool:
    """Check if a file pair matches any expected-coupling pattern pair."""
    for pat_a, pat_b in patterns:
        if (fnmatch.fnmatch(file_a, pat_a) and fnmatch.fnmatch(file_b, pat_b)) or (
            fnmatch.fnmatch(file_a, pat_b) and fnmatch.fnmatch(file_b, pat_a)
        ):
            return True
    return False


def coupling_ratio(counts: dict[str, int], file_a: str, file_b: str, co_changes: int) -> float:
    """Ratio of co-changes to the less-changed file's commit count.

    Returns co_changes / min(count_a, count_b), or 0.0 when either file has
    no counted commits (no coupling is measurable). Callers compare against
    their own threshold.
    """
    min_count = min(counts.get(file_a, 0), counts.get(file_b, 0))
    if min_count == 0:
        return 0.0
    return co_changes / min_count


def has_time_slices(history: GitHistory | None, min_slices: int) -> TypeGuard[GitHistory]:
    """Whether history exists with at least *min_slices* time slices."""
    return history is not None and len(history.time_slices) >= min_slices


def is_naturally_stable(file_path: Path) -> bool:
    """Whether a file is one churn says nothing about.

    Package ``__init__``/``apps`` modules and settings-shaped modules change
    on a schedule set by the project layout, not by the design of the code
    inside them. (A ``conftest.py`` is test support when it has the test
    role, and the history checks leave test-role files out already.)
    """
    return file_path.name in SKIP_NAMES or file_path.name.endswith(SKIP_SUFFIXES)


def source_files(ctx: AnalysisContext) -> Iterator[tuple[Path, str]]:
    """Yield every analyzed file without the test role as a ``(path, str(path))`` pair.

    Every per-file history check walks the same set: the analyzed trees minus
    the test-role files, whose churn describes the code under test rather
    than the code itself.
    """
    for file_path in ctx.all_trees:
        if ctx.roles.is_test(file_path):
            continue
        yield file_path, str(file_path)


def _cochange_counts(
    commits: Iterable[CommitInfo], analyzed_files: set[str]
) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Count how often files, and pairs of files, change in the same commit.

    Only analyzed ``.py`` paths count, excluding package ``__init__`` files and
    Django migrations, neither of which carries a design signal. Commits
    touching ``COCHANGE_SKIP_THRESHOLD`` or more such files are skipped whole:
    a sweeping mechanical edit couples every file it touches to every other.

    Returns:
        ``(file_counts, pair_counts)`` — qualifying commits per file, and per
        ``(file_a, file_b)`` pair with the two names in sorted order. Both are
        empty when no commit qualifies.
    """
    file_counts: dict[str, int] = {}
    pair_counts: dict[tuple[str, str], int] = {}

    for commit in commits:
        py_files = sorted(
            f
            for f in commit.python_files
            if f in analyzed_files and Path(f).name != INIT_FILE and not is_migration_file(f)
        )
        if len(py_files) >= COCHANGE_SKIP_THRESHOLD:
            continue

        for f in py_files:
            file_counts[f] = file_counts.get(f, 0) + 1

        for i, file_a in enumerate(py_files):
            for file_b in py_files[i + 1 :]:
                pair = (file_a, file_b)
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    return file_counts, pair_counts


@dataclass(frozen=True)
class PairCochanges:
    """One pair's co-change count, with the per-file counts its ratio reads.

    ``file_counts`` covers the same commits as ``co_changes``: the whole
    window, or only the commits after the pair's review cutoff.
    """

    file_a: str
    file_b: str
    co_changes: int
    file_counts: dict[str, int]

    @property
    def ratio(self) -> float:
        return coupling_ratio(self.file_counts, self.file_a, self.file_b, self.co_changes)

    @property
    def min_count(self) -> int:
        return min(self.file_counts.get(self.file_a, 0), self.file_counts.get(self.file_b, 0))


def pair_review_cutoff(history: GitHistory, file_a: str, file_b: str) -> datetime | None:
    """The later of two files' ``pysmelly: reviewed`` markers, or None unless both carry one.

    A pair is acknowledged only when both of its files are: reviewing one
    file says nothing about the partner it keeps changing with.
    """
    review_a = history.reviewed_at.get(file_a)
    review_b = history.reviewed_at.get(file_b)
    if review_a is None or review_b is None:
        return None
    return max(review_a, review_b)


def _cochanges_since_review(
    history: GitHistory, commits: Iterable[CommitInfo], analyzed_files: set[str]
) -> Iterator[PairCochanges]:
    """``_cochange_counts`` per pair, honouring ``pysmelly: reviewed`` for pairs.

    A pair whose two files both carry a marker is counted over the commits
    after the later marker (``pair_review_cutoff``), co-changes and per-file
    counts alike; any other pair over every commit given. A reviewed pair
    with no co-change after its cutoff is not yielded.
    """
    commits = list(commits)
    file_counts, pair_counts = _cochange_counts(commits, analyzed_files)
    after_cutoff: dict[datetime, tuple[dict[str, int], dict[tuple[str, str], int]]] = {}
    for (file_a, file_b), co_changes in pair_counts.items():
        cutoff = pair_review_cutoff(history, file_a, file_b)
        if cutoff is None:
            yield PairCochanges(file_a, file_b, co_changes, file_counts)
            continue
        if cutoff not in after_cutoff:
            after_cutoff[cutoff] = _cochange_counts(
                (c for c in commits if c.postdates(cutoff)), analyzed_files
            )
        counts, pairs = after_cutoff[cutoff]
        reviewed_co_changes = pairs.get((file_a, file_b), 0)
        if reviewed_co_changes:
            yield PairCochanges(file_a, file_b, reviewed_co_changes, counts)


def coupled_pairs(
    ctx: AnalysisContext,
    history: GitHistory,
    commits: Iterable[CommitInfo],
    *,
    min_co_changes: int,
    min_ratio: float,
) -> Iterator[PairCochanges]:
    """The pairs of analyzed files that *commits* change together often enough.

    A pair qualifies with at least *min_co_changes* co-changes and a coupling
    ratio of at least *min_ratio*, counted under the pair review rule
    (``_cochanges_since_review``). A test file paired with a source file, and
    a pair the project declares as expected coupling, never qualify.
    """
    analyzed_files = {str(p) for p in ctx.all_trees}
    for counted in _cochanges_since_review(history, commits, analyzed_files):
        file_a, file_b = counted.file_a, counted.file_b
        if counted.co_changes < min_co_changes or counted.ratio < min_ratio:
            continue
        if ctx.roles.is_test(file_a) != ctx.roles.is_test(file_b):
            continue
        if is_expected_coupling(file_a, file_b, ctx.expected_coupling):
            continue
        yield counted


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
    for _file_path, file_str in source_files(ctx):
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


def preview(text: str) -> str:
    """``text`` cut to ``_PREVIEW_WIDTH`` characters, an ellipsis marking the cut."""
    if len(text) > _PREVIEW_WIDTH:
        return text[: _PREVIEW_WIDTH - 1] + "…"
    return text


def commit_subject(commit: CommitInfo) -> str:
    """A commit's message as a finding quotes it (``preview``)."""
    return preview(commit.message)


def head_path_of(history: GitHistory, paths: Iterable[str]) -> dict[str, str]:
    """Every name the HEAD ``paths`` had in the window, mapped to the HEAD name.

    ``GitHistory.renames`` lists ``(old, new)`` newest first, so a chain of
    renames resolves in one pass.
    """
    mapped = {path: path for path in paths}
    for old, new in history.renames:
        if new in mapped:
            mapped.setdefault(old, mapped[new])
    return mapped


def post_review_commit_counts(
    ctx: AnalysisContext, history: GitHistory
) -> Iterator[tuple[str, Path, int]]:
    """Each analyzed production file but a migration, as ``(path, file_path, commits)``.

    ``commits`` counts the window's commits after the file's review marker,
    including those made under a name it had before a rename in the window.
    """
    analyzed = {
        path: file_path for file_path, path in source_files(ctx) if not is_migration_file(path)
    }
    names: dict[str, set[str]] = {}
    for name, head_path in head_path_of(history, analyzed).items():
        names.setdefault(head_path, set()).add(name)
    for path, file_path in analyzed.items():
        touching = {
            commit.hash
            for name in names[path]
            for commit in history.commits_for_file.get(name, [])
            if after_review(history, path, commit)
        }
        yield path, file_path, len(touching)


def commits_by_hash(history: GitHistory, paths: Iterable[str]) -> dict[str, CommitInfo]:
    """The window's commits that touched any of ``paths``, by hash."""
    return {
        commit.hash: commit for path in paths for commit in history.commits_for_file.get(path, [])
    }


def fix_commits(history: GitHistory) -> Iterator[CommitInfo]:
    """The window's commits the message classifier calls fixes, newest first."""
    return (c for c in history.commits if FIX_CATEGORY in classify_commit(c.message))


def window_commit(commits: dict[str, CommitInfo], sha: str) -> CommitInfo:
    """The window commit git named by ``sha`` in a line-level query.

    Raises:
        GitHistoryQueryError: the window's ``git log`` does not list it, so
            the two queries disagree about the window.
    """
    commit = commits.get(sha)
    if commit is None:
        msg = f"git reported {sha}, which the window's git log does not list"
        raise GitHistoryQueryError(msg)
    return commit


def after_review(history: GitHistory, path: str, commit: CommitInfo) -> bool:
    """Whether ``commit`` postdates ``path``'s review marker (or it has none)."""
    return commit.postdates(history.reviewed_at.get(path))
