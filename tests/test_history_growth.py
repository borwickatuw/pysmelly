"""Tests for git history growth checks."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from pysmelly.checks.history_growth import (
    check_churn_without_growth,
    check_growth_trajectory,
    check_hotspot_acceleration,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats, TimeSlice

from .history_test_helpers import (
    _NOW,
    commits_from_slices,
    make_ctx,
    make_large_file,
)


# --- growth-trajectory tests ---


class TestGrowthTrajectory:
    def test_rapid_growth(self):
        """File that grew >= 200 lines AND >= 2x -> finding."""
        files = {"models/user.py": make_large_file(380)}
        file_stats = {
            "models/user.py": FileStats(total_insertions=300, total_deletions=40, commit_count=15)
        }
        # start_lines = 380 - (300 - 40) = 120, growth = 260, ratio = 3.17x
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 1
        assert "grew from" in findings[0].message
        assert "models/user.py" in findings[0].file

    def test_small_file_skipped(self):
        """File < 100 lines -> no finding."""
        files = {"small.py": make_large_file(50)}
        file_stats = {
            "small.py": FileStats(total_insertions=40, total_deletions=5, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_moderate_growth_no_finding(self):
        """File that grew but < 200 lines -> no finding."""
        files = {"app.py": make_large_file(200)}
        file_stats = {
            "app.py": FileStats(total_insertions=120, total_deletions=20, commit_count=10)
        }
        # start_lines = 200 - (120-20) = 100, growth = 100 < 200 threshold
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_new_file_skipped(self):
        """File that didn't exist at start of window -> no finding."""
        files = {"new.py": make_large_file(300)}
        file_stats = {"new.py": FileStats(total_insertions=310, total_deletions=10, commit_count=5)}
        # start_lines = 300 - (310 - 10) = 0 -> skip
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_grew_less_than_2x(self):
        """File grew >= 200 lines but < 2x -> no finding."""
        files = {"big.py": make_large_file(500)}
        file_stats = {
            "big.py": FileStats(total_insertions=250, total_deletions=50, commit_count=15)
        }
        # start_lines = 500 - (250-50) = 300, growth = 200, ratio = 1.67x < 2.0
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_growth_trajectory(ctx) == []


# --- churn-without-growth tests ---


class TestChurnWithoutGrowth:
    def test_high_churn_low_growth(self):
        """Many commits but little net growth -> finding."""
        files = {"utils/parser.py": make_large_file(280)}
        file_stats = {
            "utils/parser.py": FileStats(total_insertions=200, total_deletions=188, commit_count=18)
        }
        # net_growth = 200 - 188 = 12, 10% of 280 = 28, 12 <= 28 -> flag
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 1
        assert "rewritten" in findings[0].message

    def test_growing_file_no_finding(self):
        """File with substantial net growth -> no finding."""
        files = {"app.py": make_large_file(300)}
        file_stats = {
            "app.py": FileStats(total_insertions=200, total_deletions=50, commit_count=15)
        }
        # net_growth = 150, 10% of 300 = 30, 150 > 30 -> no flag
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 10 commits -> no finding."""
        files = {"utils/parser.py": make_large_file(200)}
        file_stats = {
            "utils/parser.py": FileStats(total_insertions=100, total_deletions=95, commit_count=8)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"tiny.py": make_large_file(30)}
        file_stats = {
            "tiny.py": FileStats(total_insertions=50, total_deletions=48, commit_count=12)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_parser.py": make_large_file(200)}
        file_stats = {
            "test_parser.py": FileStats(total_insertions=200, total_deletions=195, commit_count=15)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_churn_without_growth(ctx) == []


# --- hotspot-acceleration tests ---


class TestHotspotAcceleration:
    def test_accelerating_hotspot(self):
        """Second half has 2x+ more activity -> finding."""
        # First half: 1 commit per slice; second half: 3 per slice
        slices = []
        base = _NOW - timedelta(days=8 * 14)
        for i in range(8):
            start = base + timedelta(days=i * 14)
            end = start + timedelta(days=14)
            ts = TimeSlice(start=start, end=end)
            n = 1 if i < 4 else 3
            for j in range(n):
                c = CommitInfo(
                    hash=f"c_{i}_{j}",
                    date=start + timedelta(days=j + 1),
                    message=f"feat: work {i}/{j}",
                    files=["services/api.py"],
                )
                ts.commits.append(c)
                ts.files_touched.add("services/api.py")
            slices.append(ts)
        commits = commits_from_slices(slices)
        files = {"services/api.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 1
        assert "accelerating" in findings[0].message

    def test_steady_rate_no_finding(self):
        """Constant commit rate -> no finding."""
        slices = []
        base = _NOW - timedelta(days=8 * 14)
        for i in range(8):
            start = base + timedelta(days=i * 14)
            end = start + timedelta(days=14)
            ts = TimeSlice(start=start, end=end)
            for j in range(2):
                c = CommitInfo(
                    hash=f"c_{i}_{j}",
                    date=start + timedelta(days=j + 1),
                    message=f"feat: work {i}/{j}",
                    files=["app.py"],
                )
                ts.commits.append(c)
                ts.files_touched.add("app.py")
            slices.append(ts)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        slices = []
        base = _NOW - timedelta(days=4 * 14)
        for i in range(4):
            start = base + timedelta(days=i * 14)
            end = start + timedelta(days=14)
            ts = TimeSlice(start=start, end=end)
            if i >= 2:
                c = CommitInfo(
                    hash=f"c_{i}",
                    date=start + timedelta(days=1),
                    message=f"feat: work {i}",
                    files=["app.py"],
                )
                ts.commits.append(c)
                ts.files_touched.add("app.py")
            slices.append(ts)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        slices = []
        base = _NOW - timedelta(days=8 * 14)
        for i in range(8):
            start = base + timedelta(days=i * 14)
            end = start + timedelta(days=14)
            ts = TimeSlice(start=start, end=end)
            n = 1 if i < 4 else 4
            for j in range(n):
                c = CommitInfo(
                    hash=f"c_{i}_{j}",
                    date=start + timedelta(days=j + 1),
                    message=f"feat: work {i}/{j}",
                    files=["test_api.py"],
                )
                ts.commits.append(c)
                ts.files_touched.add("test_api.py")
            slices.append(ts)
        commits = commits_from_slices(slices)
        files = {"test_api.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 0

    def test_too_few_slices(self):
        """Fewer than 4 slices -> no finding."""
        slices = []
        base = _NOW - timedelta(days=3 * 14)
        for i in range(3):
            start = base + timedelta(days=i * 14)
            end = start + timedelta(days=14)
            ts = TimeSlice(start=start, end=end)
            for j in range(3):
                c = CommitInfo(
                    hash=f"c_{i}_{j}",
                    date=start + timedelta(days=j + 1),
                    message=f"feat: work {i}/{j}",
                    files=["app.py"],
                )
                ts.commits.append(c)
                ts.files_touched.add("app.py")
            slices.append(ts)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_hotspot_acceleration(ctx) == []
