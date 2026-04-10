"""Git history checks — growth and churn signals."""

from __future__ import annotations

from statistics import median

from pysmelly.checks.history_helpers import (
    CATEGORY,
    MIN_LINES_MEDIUM,
    churned_files,
    get_line_count,
    history_time_slices,
    is_bulk_commit,
    is_test_file,
    slices_since_review,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


@check(
    "growth-trajectory",
    severity=Severity.LOW,
    category=CATEGORY,
    description="Files growing rapidly within the time window",
)
def check_growth_trajectory(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)
        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < 100:
            continue

        stats = history.file_stats_since_review(file_str)
        if stats is None:
            continue

        net_change = stats.total_insertions - stats.total_deletions
        start_lines = current_lines - net_change

        # Skip new files (start <= 0)
        if start_lines <= 0:
            continue

        growth = current_lines - start_lines
        if growth < 200:
            continue

        ratio = current_lines / start_lines
        if ratio < 2.0:
            continue

        pct = int((ratio - 1) * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="growth-trajectory",
                message=(
                    f"{file_str} grew from ~{start_lines} to {current_lines} lines "
                    f"(+{pct}%) in last {history.window} — accelerating accumulation "
                    f"of responsibilities"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "churn-without-growth",
    severity=Severity.LOW,
    category=CATEGORY,
    description="Many commits but stable/shrinking line count — wrong abstractions",
)
def check_churn_without_growth(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_str, current_lines, stats in churned_files(
        history, ctx, min_lines=MIN_LINES_MEDIUM, min_commits=10
    ):
        net_growth = stats.total_insertions - stats.total_deletions
        # Flag if net growth <= 10% of current line count
        if net_growth > current_lines * 0.1:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="churn-without-growth",
                message=(
                    f"{file_str} — {stats.commit_count} commits but only "
                    f"+{net_growth} net lines ({current_lines} lines total) "
                    f"in last {history.window} — code is being rewritten, "
                    f"not extended"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "hotspot-acceleration",
    severity=Severity.MEDIUM,
    category=CATEGORY,
    description="Files whose change frequency is increasing over time",
)
def check_hotspot_acceleration(ctx: AnalysisContext) -> list[Finding]:
    result = history_time_slices(ctx, min_slices=4)
    if result is None:
        return []
    history, slices = result

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if is_test_file(file_str):
            continue

        commits = history.commits_since_review(file_str)
        if len(commits) < 5:
            continue

        file_slices = slices_since_review(slices, history, file_str)
        if len(file_slices) < 4:
            continue
        mid = len(file_slices) // 2
        first_half = file_slices[:mid]
        second_half = file_slices[mid:]

        # Count commits per slice for this file (skip bulk commits)
        first_counts = []
        for ts in first_half:
            count = sum(1 for c in ts.commits if file_str in c.files and not is_bulk_commit(c))
            first_counts.append(count)

        second_counts = []
        for ts in second_half:
            count = sum(1 for c in ts.commits if file_str in c.files and not is_bulk_commit(c))
            second_counts.append(count)

        first_median = median(first_counts) if first_counts else 0
        second_median = median(second_counts) if second_counts else 0

        # Need meaningful activity in second half
        second_total = sum(second_counts)
        if second_total < 3:
            continue

        # Second half must be >= 2x first half
        if first_median == 0:
            # If first half was silent but second half is active, that's acceleration
            if second_median < 2:
                continue
        elif second_median < first_median * 2:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="hotspot-acceleration",
                message=(
                    f"{file_str} — change frequency accelerating: "
                    f"median {first_median:.0f}→{second_median:.0f} "
                    f"commits per period in last {history.window} — "
                    f"emerging hotspot"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
