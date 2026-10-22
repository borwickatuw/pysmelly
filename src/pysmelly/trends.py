"""Project trends: the git-history window's first half against its second.

Informational, not findings (operator call, 2026-10-20). The block carries no
severity, never enters the finding list, and so leaves the finding counts and
the exit status alone. It is project-level: it reads every commit in the
window, so `pysmelly: reviewed` markers and `--ignore-reviewed` have no
bearing on it.

The halves are the calendar window cut at its midpoint -- from the cutoff git
applies for ``--since`` to the moment of the run -- not the span between the
first and last commit, so a project whose history starts mid-window shows the
quiet first half as a zero baseline. ``GitHistory.time_slices`` is not reused:
its slices span the commits rather than the window, and their count can be
odd, so they do not split into two equal halves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from pysmelly.git_history import FIX_CATEGORY, CommitInfo, GitHistory, classify_commit

# A metric prints when it moves by at least this much relative to the first
# half (DECISIONS.md, "Project trends", Phase 31-5).
TREND_THRESHOLD = Fraction(1, 4)

_DAYS_PER_WEEK = 7
_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class HalfStats:
    """What one half of the window holds, counted over Python files only."""

    commits: int
    file_touches: int  # Python files per commit, summed
    fixes: int  # commits classify_commit labels FIX_CATEGORY
    created: int
    deleted: int


@dataclass(frozen=True)
class WindowHalves:
    """The window cut at its midpoint, and each half's counts."""

    split: datetime
    half_days: float
    first: HalfStats
    second: HalfStats


def _half_stats(commits: list[CommitInfo], lifecycle: dict[str, tuple[int, int]]) -> HalfStats:
    """Count one half's commits, file touches, fixes, creations and deletions."""
    events = [lifecycle.get(c.hash, (0, 0)) for c in commits]
    return HalfStats(
        commits=len(commits),
        file_touches=sum(len(c.files) for c in commits),
        fixes=sum(1 for c in commits if FIX_CATEGORY in classify_commit(c.message)),
        created=sum(created for created, _ in events),
        deleted=sum(deleted for _, deleted in events),
    )


def split_window(history: GitHistory, now: datetime) -> WindowHalves:
    """Count each half of the window that ends at ``now``.

    A commit dated before the midpoint is in the first half; the rest,
    including any dated after ``now`` by clock skew, are in the second.
    """
    start = history.window_start()
    split = start + (now - start) / 2
    lifecycle = history.lifecycle_by_commit
    first = [c for c in history.commits if c.date < split]
    second = [c for c in history.commits if c.date >= split]
    return WindowHalves(
        split=split,
        half_days=(split - start).total_seconds() / _SECONDS_PER_DAY,
        first=_half_stats(first, lifecycle),
        second=_half_stats(second, lifecycle),
    )


def _move(before: Fraction, after: Fraction) -> Fraction | None:
    """The relative move from ``before`` to ``after``, if it is one to report.

    The zero-baseline rule: from zero to zero is no move; from zero to
    anything else is reported, as "from zero", since no finite percentage
    describes it. Returns None for no reportable move, ``after`` itself when
    the baseline is zero, and the signed relative change otherwise.
    """
    if before == 0:
        return after if after != 0 else None
    change = (after - before) / before
    return change if abs(change) >= TREND_THRESHOLD else None


def _describe_move(before: Fraction, move: Fraction) -> str:
    """``+62%``, or ``from zero`` for a zero baseline."""
    if before == 0:
        return "from zero"
    return f"{float(move) * 100:+.0f}%"


def _ratio_line(
    label: str,
    first: tuple[int, int],
    second: tuple[int, int],
    render: str,
) -> str | None:
    """A per-commit metric's line, or None when it did not move.

    A half with no commits has no ratio at all (not a zero one), so the
    metric is not compared.
    """
    if first[1] == 0 or second[1] == 0:
        return None
    before, after = Fraction(*first), Fraction(*second)
    move = _move(before, after)
    if move is None:
        return None
    return (
        f"  {label}: {render.format(float(before))} -> {render.format(float(after))}"
        f" ({_describe_move(before, move)})"
    )


def format_trends(halves: WindowHalves) -> list[str]:
    """The ``Project trends`` block, or no lines when no metric moved."""
    first, second = halves.first, halves.second
    lines: list[str] = []

    created_move = _move(Fraction(first.created), Fraction(second.created))
    deleted_move = _move(Fraction(first.deleted), Fraction(second.deleted))
    if created_move is not None or deleted_move is not None:
        parts = [
            f"{name} {_describe_move(Fraction(before), move)}"
            for name, before, move in (
                ("created", first.created, created_move),
                ("deleted", first.deleted, deleted_move),
            )
            if move is not None
        ]
        lines.append(
            f"  files created / deleted: {first.created} / {first.deleted}"
            f" -> {second.created} / {second.deleted} ({', '.join(parts)})"
        )

    for line in (
        _ratio_line(
            "files per commit",
            (first.file_touches, first.commits),
            (second.file_touches, second.commits),
            "{:.1f}",
        ),
        _ratio_line(
            "fix-commit ratio",
            (first.fixes, first.commits),
            (second.fixes, second.commits),
            "{:.0%}",
        ),
    ):
        if line is not None:
            lines.append(line)

    # The halves are the same length, so commits per week moves exactly as
    # the commit count does.
    commits_move = _move(Fraction(first.commits), Fraction(second.commits))
    if commits_move is not None:
        weeks = halves.half_days / _DAYS_PER_WEEK
        lines.append(
            f"  commits per week: {float(first.commits / weeks):.1f}"
            f" -> {float(second.commits / weeks):.1f}"
            f" ({_describe_move(Fraction(first.commits), commits_move)})"
        )

    if not lines:
        return []
    return [
        f"Project trends (window halves split at {halves.split.date().isoformat()};"
        " informational, not findings):",
        *lines,
    ]


def project_trends(history: GitHistory) -> list[str]:
    """The block for the window ending now; no lines for an empty window.

    An empty window has nothing to compare, so it asks git nothing more.

    Raises:
        GitHistoryQueryError: a query behind the halves could not be run.
    """
    if not history.commits:
        return []
    return format_trends(split_window(history, datetime.now(UTC)))
