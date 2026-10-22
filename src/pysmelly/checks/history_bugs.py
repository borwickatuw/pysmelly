"""Git history checks — bug patterns and fix signals."""

from __future__ import annotations

import functools

from pysmelly.checks.framework import is_django_settings_module
from pysmelly.checks.helpers import MAJORITY
from pysmelly.checks.history_helpers import (
    category_count,
    counted_commits,
    coupled_pairs,
    fix_commits,
    has_semantic_history,
    has_time_slices,
    notes_persisting_pair_review,
    notes_persisting_review,
    slices_since_review,
    source_files,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import FEATURE_CATEGORY, FIX_CATEGORY
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)


@check(
    "bug-magnet",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Files where a majority of commits are fixes — recurring problems",
    advice=Advice(
        fix=(
            "Read the file's fix commits for their recurring cause, restructure the code it"
            " lives in so that class of bug cannot recur, and add a test for it."
        ),
        dismiss=(
            "Dismiss only if the fixes correct data the file holds (a table of values,"
            " translations) rather than its logic."
        ),
    ),
)
@notes_persisting_review
def check_bug_magnet(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_semantic_history(history):
        return []

    findings: list[Finding] = []

    for _file_path, file_str in source_files(ctx):
        commits = counted_commits(history, file_str)
        if len(commits) < 5:
            continue

        fix_count = category_count(commits, FIX_CATEGORY)
        fix_ratio = fix_count / len(commits)
        if fix_ratio < MAJORITY:
            continue

        pct = int(fix_ratio * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="bug-magnet",
                message=(
                    f"{file_str} — {fix_count} of {len(commits)} commits "
                    f"({pct}%) are fixes — recurring problems suggest a "
                    f"structural issue worth redesigning"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "fix-propagation",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Files that co-change in fix commits — fixing one tends to break the other",
    advice=Advice(
        fix=(
            "Find what the two files both encode (a format, an assumption, duplicated logic)"
            " and move it into one place both use."
        ),
        dismiss="Dismiss only if one file is generated from the other.",
    ),
)
@notes_persisting_pair_review
def check_fix_propagation(ctx: AnalysisContext) -> list[Finding]:
    """Flag file pairs that keep being fixed in the same commit.

    The finding is sited on both files, as change-coupling's is, and a pair
    whose two files both carry a ``pysmelly: reviewed`` marker is judged on
    the fix commits after the later marker (``coupled_pairs``).
    """
    history = ctx.git_history
    if not has_semantic_history(history):
        return []

    # Count co-changes in fix commits only
    fixes = fix_commits(history)

    return [
        multi_site_finding(
            "fix-propagation",
            Severity.MEDIUM,
            [Site(counted.file_a, 1), Site(counted.file_b, 1)],
            functools.partial(
                _fix_propagation_message,
                counted.file_a,
                counted.file_b,
                counted.co_changes,
                counted.min_count,
            ),
        )
        for counted in coupled_pairs(ctx, history, fixes, min_co_changes=3, min_ratio=0.6)
    ]


def _fix_propagation_message(
    file_a: str, file_b: str, co_fixes: int, min_count: int, indices: tuple[int, ...]
) -> str | None:
    """fix-propagation's message for one pair; None once either file is
    ignored, since one file is not a pair."""
    if len(indices) < 2:
        return None
    return (
        f"{file_a} and {file_b} — {co_fixes} of {min_count} fix "
        f"commits touch both files — fixing one tends to break "
        f"the other"
    )


@check(
    "fix-follows-feature",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Features that reliably produce fix commits shortly after",
    advice=Advice(
        fix=(
            "Add tests for the behavior the follow-up fixes repaired, and simplify the code"
            " the features keep breaking."
        ),
        dismiss=(
            "Dismiss only if the fixes correct data or configuration the features added,"
            " not the file's logic."
        ),
    ),
)
@notes_persisting_review
def check_fix_follows_feature(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_semantic_history(history):
        return []
    if history.is_coarse_grained:
        return []

    slices = history.time_slices
    if len(slices) < 4:
        return []

    findings: list[Finding] = []

    for file_path, file_str in source_files(ctx):
        # Every feature that adds an app or a setting touches the settings
        # module, and so does the fix that follows it; the pairing says
        # nothing about the settings module's own design.
        if is_django_settings_module(ctx.all_trees[file_path]):
            continue
        # Find feature-then-fix pairs within 2 periods
        file_slices = slices_since_review(slices, history, file_str)
        pairs = 0
        for i, ts in enumerate(file_slices):
            feature_files = ts.files_by_category.get(FEATURE_CATEGORY, set())
            if file_str not in feature_files:
                continue
            # Look ahead up to 2 slices for fix activity
            for j in range(i + 1, min(i + 3, len(file_slices))):
                fix_files = file_slices[j].files_by_category.get(FIX_CATEGORY, set())
                if file_str in fix_files:
                    pairs += 1
                    break

        if pairs < 3:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="fix-follows-feature",
                message=(
                    f"{file_str} — {pairs} feature→fix sequences in last "
                    f"{history.window} — features are reliably followed by fixes"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "stabilization-failure",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Files that repeatedly burst with activity, go quiet, then burst again",
    advice=Advice(
        fix=(
            "Compare the bursts' commits for the part reworked each time, and settle that"
            " part's design (extract it, fix its interface) so it stops changing."
        ),
        dismiss=(
            "Dismiss only if the bursts follow an external schedule the file tracks"
            " (releases, yearly data updates)."
        ),
    ),
)
@notes_persisting_review
def check_stabilization_failure(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_time_slices(history, min_slices=6):
        return []
    slices = history.time_slices

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        commits = history.commits_since_review(file_str)
        if len(commits) < 8:
            continue

        # Mark which slices are active for this file
        file_slices = slices_since_review(slices, history, file_str)
        active = [file_str in ts.files_touched for ts in file_slices]

        # Count bursts: 2+ consecutive active slices separated by 3+ inactive
        bursts = 0
        i = 0
        while i < len(active):
            # Find start of a burst (2+ active)
            if active[i]:
                burst_len = 0
                while i < len(active) and active[i]:
                    burst_len += 1
                    i += 1
                if burst_len >= 2:
                    bursts += 1
                    # Now look for gap (3+ inactive)
                    gap_len = 0
                    while i < len(active) and not active[i]:
                        gap_len += 1
                        i += 1
                    if gap_len < 3:
                        # Not a real gap — merge with next burst
                        continue
                else:
                    i += 1
            else:
                i += 1

        if bursts < 3:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="stabilization-failure",
                message=(
                    f"{file_str} — {bursts} activity bursts in last "
                    f"{history.window} — code is repeatedly destabilized "
                    f"rather than converging"
                ),
                severity=Severity.LOW,
            )
        )

    return findings
