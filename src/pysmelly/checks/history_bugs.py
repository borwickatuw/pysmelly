"""Git history checks — bug patterns and fix signals."""

from __future__ import annotations

from pathlib import Path

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.history_helpers import (
    CATEGORY,
    COCHANGE_SKIP_THRESHOLD,
    coupling_ratio,
    history_time_slices,
    is_bulk_commit,
    is_expected_coupling,
    is_test_file,
    semantic_guard,
    slices_since_review,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import classify_commit
from pysmelly.registry import Finding, Severity, check


@check(
    "bug-magnet",
    severity=Severity.MEDIUM,
    category=CATEGORY,
    description="Files where a majority of commits are fixes — recurring problems",
)
def check_bug_magnet(ctx: AnalysisContext) -> list[Finding]:
    history = semantic_guard(ctx.git_history)
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if is_test_file(file_str):
            continue

        commits = [c for c in history.commits_since_review(file_str) if not is_bulk_commit(c)]
        if len(commits) < 5:
            continue

        fix_count = sum(1 for c in commits if "fix" in classify_commit(c.message))
        fix_ratio = fix_count / len(commits)
        if fix_ratio < 0.5:
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
    category=CATEGORY,
    description="Files that co-change in fix commits — fixing one tends to break the other",
)
def check_fix_propagation(ctx: AnalysisContext) -> list[Finding]:
    history = semantic_guard(ctx.git_history)
    if history is None:
        return []

    analyzed_files = {str(p) for p in ctx.all_trees}

    # Count co-changes in fix commits only
    pair_counts: dict[tuple[str, str], int] = {}
    file_fix_counts: dict[str, int] = {}

    for commit in history._commits:
        if "fix" not in classify_commit(commit.message):
            continue

        py_files = sorted(
            f
            for f in commit.files
            if f.endswith(".py")
            and f in analyzed_files
            and Path(f).name != "__init__.py"
            and not is_migration_file(f)
        )

        if len(py_files) >= COCHANGE_SKIP_THRESHOLD:
            continue

        for f in py_files:
            file_fix_counts[f] = file_fix_counts.get(f, 0) + 1

        for i in range(len(py_files)):
            for j in range(i + 1, len(py_files)):
                pair = (py_files[i], py_files[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    findings: list[Finding] = []

    for (file_a, file_b), co_fixes in pair_counts.items():
        if co_fixes < 3:
            continue

        # Skip test↔source pairs
        if is_test_file(file_a) != is_test_file(file_b):
            continue

        ratio = coupling_ratio(file_fix_counts, file_a, file_b, co_fixes, threshold=0.6)
        if ratio is None:
            continue

        if is_expected_coupling(file_a, file_b, ctx.expected_coupling):
            continue

        min_count = min(
            file_fix_counts.get(file_a, 0),
            file_fix_counts.get(file_b, 0),
        )
        findings.append(
            Finding(
                file=file_a,
                line=1,
                check="fix-propagation",
                message=(
                    f"{file_a} and {file_b} — {co_fixes} of {min_count} fix "
                    f"commits touch both files — fixing one tends to break "
                    f"the other"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "fix-follows-feature",
    severity=Severity.MEDIUM,
    category=CATEGORY,
    description="Features that reliably produce fix commits shortly after",
)
def check_fix_follows_feature(ctx: AnalysisContext) -> list[Finding]:
    history = semantic_guard(ctx.git_history)
    if history is None:
        return []
    if history.is_coarse_grained:
        return []

    slices = history.time_slices
    if len(slices) < 4:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if is_test_file(file_str):
            continue

        # Find feature-then-fix pairs within 2 periods
        file_slices = slices_since_review(slices, history, file_str)
        pairs = 0
        for i, ts in enumerate(file_slices):
            feature_files = ts.files_by_category.get("feature", set())
            if file_str not in feature_files:
                continue
            # Look ahead up to 2 slices for fix activity
            for j in range(i + 1, min(i + 3, len(file_slices))):
                fix_files = file_slices[j].files_by_category.get("fix", set())
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
                    f"{history.window} — features are reliably followed "
                    f"by fixes, consider more thorough testing or design review"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "stabilization-failure",
    severity=Severity.LOW,
    category=CATEGORY,
    description="Files that repeatedly burst with activity, go quiet, then burst again",
)
def check_stabilization_failure(ctx: AnalysisContext) -> list[Finding]:
    result = history_time_slices(ctx, min_slices=6)
    if result is None:
        return []
    history, slices = result

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
