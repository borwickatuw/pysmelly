"""Tests for git history bug-pattern checks."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from pysmelly.checks.history_bugs import (
    check_bug_magnet,
    check_fix_follows_feature,
    check_fix_propagation,
    check_stabilization_failure,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo

from .history_test_helpers import (
    _RECENT,
    bulk_commit,
    commits_from_slices,
    feat_commits,
    fix_commits,
    make_ctx,
    make_large_file,
    make_time_slices,
)


# --- bug-magnet tests ---


class TestBugMagnet:
    def test_majority_fixes(self):
        """File with >= 50% fix commits -> finding."""
        files = {"services/billing.py": "x = 1"}
        commits = fix_commits("services/billing.py", 8) + feat_commits("services/billing.py", 4)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 1
        assert "8 of 12" in findings[0].message
        assert findings[0].check == "bug-magnet"

    def test_minority_fixes_no_finding(self):
        """File with < 50% fix commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = fix_commits("app.py", 2) + feat_commits("app.py", 8)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = fix_commits("app.py", 4)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_billing.py": "x = 1"}
        commits = fix_commits("test_billing.py", 8)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> semantic checks skip."""
        files = {"app.py": "x = 1"}
        commits = fix_commits("app.py", 8)
        ctx = make_ctx(files, commits=commits, message_quality=0.3)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_bug_magnet(ctx) == []


# --- fix-propagation tests ---


class TestFixPropagation:
    def test_co_changing_in_fixes(self):
        """Files that co-change in fix commits -> finding."""
        files = {"api/views.py": "x = 1", "middleware/auth.py": "y = 2"}
        commits = [
            CommitInfo(
                hash=f"fix{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"fix: resolve issue #{i}",
                files=["api/views.py", "middleware/auth.py"],
            )
            for i in range(5)
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_fix_propagation(ctx)
        assert len(findings) == 1
        assert "fix commits touch both" in findings[0].message

    def test_low_co_fix_count(self):
        """Fewer than 3 co-fix commits -> no finding."""
        files = {"a.py": "x = 1", "b.py": "y = 2"}
        commits = [
            CommitInfo(
                hash=f"fix{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"fix: issue #{i}",
                files=["a.py", "b.py"],
            )
            for i in range(2)
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_fix_propagation(ctx)
        assert len(findings) == 0

    def test_non_fix_commits_ignored(self):
        """Feature commits don't count toward fix propagation."""
        files = {"a.py": "x = 1", "b.py": "y = 2"}
        commits = [
            CommitInfo(
                hash=f"feat{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"feat: add feature #{i}",
                files=["a.py", "b.py"],
            )
            for i in range(10)
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_fix_propagation(ctx)
        assert len(findings) == 0

    def test_test_source_pair_skipped(self):
        """Test↔source pairs are skipped."""
        files = {"app.py": "x = 1", "test_app.py": "y = 2"}
        commits = [
            CommitInfo(
                hash=f"fix{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"fix: issue #{i}",
                files=["app.py", "test_app.py"],
            )
            for i in range(5)
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_fix_propagation(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_fix_propagation(ctx) == []


# --- fix-follows-feature tests ---


class TestFixFollowsFeature:
    def test_feature_then_fix_pattern(self):
        """Feature slices followed by fix slices -> finding."""
        pattern = [
            "feature",
            "fix",  # pair 1
            "feature",
            "fix",  # pair 2
            "feature",
            "fix",  # pair 3
            "inactive",
            "inactive",
        ]
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=1.0,
            time_slices=slices,
            commits_per_slice=3.0,
        )
        findings = check_fix_follows_feature(ctx)
        assert len(findings) == 1
        assert "feature→fix" in findings[0].message

    def test_fewer_than_3_pairs_no_finding(self):
        """Only 2 feature->fix pairs -> no finding."""
        pattern = [
            "feature",
            "fix",  # pair 1
            "feature",
            "fix",  # pair 2
            "inactive",
            "inactive",
            "inactive",
            "inactive",
        ]
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=1.0,
            time_slices=slices,
            commits_per_slice=3.0,
        )
        findings = check_fix_follows_feature(ctx)
        assert len(findings) == 0

    def test_coarse_grained_skipped(self):
        """Coarse-grained history -> skip."""
        pattern = ["feature", "fix"] * 4
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=1.0,
            time_slices=slices,
            commits_per_slice=1.0,  # coarse
        )
        findings = check_fix_follows_feature(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> skip."""
        pattern = ["feature", "fix"] * 4
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.3,
            time_slices=slices,
            commits_per_slice=3.0,
        )
        findings = check_fix_follows_feature(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        pattern = ["feature", "fix"] * 4
        slices = make_time_slices("test_app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"test_app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=1.0,
            time_slices=slices,
            commits_per_slice=3.0,
        )
        findings = check_fix_follows_feature(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_fix_follows_feature(ctx) == []


# --- stabilization-failure tests ---


class TestStabilizationFailure:
    def test_repeated_bursts(self):
        """File with 3+ bursts separated by gaps -> finding."""
        # burst1(2 active), gap(3 inactive), burst2(2), gap(3), burst3(2), gap(3)
        pattern = (
            ["active", "active"]
            + ["inactive"] * 3
            + ["active", "active"]
            + ["inactive"] * 3
            + ["active", "active"]
            + ["inactive"] * 3
        )
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        # Need >= 8 total commits — each active slice has 1, so 6 total
        # Add more commits to the active slices
        extra = [
            CommitInfo(
                hash=f"extra{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"fix: extra fix #{i}",
                files=["app.py"],
            )
            for i in range(4)
        ]
        commits.extend(extra)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_stabilization_failure(ctx)
        assert len(findings) == 1
        assert "bursts" in findings[0].message

    def test_no_gaps_no_finding(self):
        """Continuous activity with no gaps -> no finding."""
        pattern = ["active"] * 12
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_stabilization_failure(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 8 commits -> no finding."""
        pattern = (
            ["active", "active"]
            + ["inactive"] * 3
            + ["active", "active"]
            + ["inactive"] * 3
            + ["active", "active"]
            + ["inactive"] * 3
        )
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        # Only 6 commits from slices, which is < 8
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_stabilization_failure(ctx)
        assert len(findings) == 0

    def test_too_few_slices(self):
        """Fewer than 6 slices -> no finding."""
        pattern = ["active", "active", "inactive", "active", "active"]
        slices = make_time_slices("app.py", pattern)
        commits = commits_from_slices(slices)
        extra = [
            CommitInfo(
                hash=f"extra{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"fix: #{i}",
                files=["app.py"],
            )
            for i in range(5)
        ]
        commits.extend(extra)
        files = {"app.py": make_large_file(100)}
        ctx = make_ctx(
            files,
            commits=commits,
            message_quality=0.5,
            time_slices=slices,
            commits_per_slice=2.0,
        )
        findings = check_stabilization_failure(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_stabilization_failure(ctx) == []


# --- bulk commit filter tests (bug-magnet) ---


class TestBulkCommitFilterBugMagnet:
    def test_bug_magnet_skips_bulk_commits(self):
        """Bulk commits (30+ .py files) excluded from bug-magnet calculation."""
        files = {"services/billing.py": "x = 1"}
        # 3 normal fix commits + 5 bulk fix commits = 8 total
        # Without filter: 8/8 = 100% fix ratio -> finding
        # With filter: only 3 normal commits < 5 min -> no finding
        normal_fixes = fix_commits("services/billing.py", 3)
        bulk_fixes = [bulk_commit("services/billing.py", i) for i in range(5)]
        ctx = make_ctx(files, commits=normal_fixes + bulk_fixes, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0


# --- reviewed suppression tests (bug-magnet) ---


class TestReviewedSuppressionBugMagnet:
    """Verify that 'pysmelly: reviewed' resets the analysis window for bug-magnet."""

    def test_bug_magnet_suppressed_after_review(self):
        """Pre-review fix commits are excluded, dropping below threshold."""
        files = {"services/billing.py": "x = 1"}
        # 8 fix commits before review, 2 feature commits after
        review_date = _RECENT - timedelta(days=5)
        old_fixes = [
            CommitInfo(
                hash=f"old_fix{i:04d}",
                date=_RECENT - timedelta(days=20 + i),
                message=f"fix: old bug #{i}",
                files=["services/billing.py"],
            )
            for i in range(8)
        ]
        new_feats = [
            CommitInfo(
                hash=f"new_feat{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"feat: new feature #{i}",
                files=["services/billing.py"],
            )
            for i in range(2)
        ]
        ctx = make_ctx(files, commits=old_fixes + new_feats, message_quality=1.0)
        # Without review: 8/10 = 80% fix ratio -> finding
        findings = check_bug_magnet(ctx)
        assert len(findings) == 1

        # With review: only 2 post-review commits (< 5 min) -> no finding
        ctx.git_history.reviewed_at = {"services/billing.py": review_date}
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_no_review_uses_full_history(self):
        """Without a review marker, full commit history is used."""
        files = {"services/billing.py": "x = 1"}
        commits = fix_commits("services/billing.py", 8) + feat_commits("services/billing.py", 4)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 1
