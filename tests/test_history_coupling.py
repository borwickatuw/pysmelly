"""Tests for git history coupling checks."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from pysmelly.checks.history_coupling import (
    check_blast_radius,
    check_change_coupling,
    check_conscious_debt,
    check_emergency_hotspots,
    check_no_refactoring,
    check_test_erosion,
    check_yo_yo_code,
)
from pysmelly.checks.history_growth import check_churn_without_growth
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats

from .history_test_helpers import (
    _RECENT,
    bulk_commit,
    feat_commits,
    fix_commits,
    make_ctx,
    make_large_file,
)


def _make_commits_for_blast_radius(
    target_file: str, co_change_counts: list[int]
) -> list[CommitInfo]:
    """Create commits where target_file changes with N other files each time."""
    commits = []
    for i, n in enumerate(co_change_counts):
        files = [target_file] + [f"other_{i}_{j}.py" for j in range(n)]
        commits.append(
            CommitInfo(
                hash=f"abc{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit {i}",
                files=files,
            )
        )
    return commits


def _make_coupling_commits(
    file_a: str, file_b: str, together: int, a_alone: int = 0, b_alone: int = 0
) -> list[CommitInfo]:
    """Create commits for coupling analysis."""
    commits = []
    for i in range(together):
        commits.append(
            CommitInfo(
                hash=f"together{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"both {i}",
                files=[file_a, file_b],
            )
        )
    for i in range(a_alone):
        commits.append(
            CommitInfo(
                hash=f"alone_a{i:04d}",
                date=_RECENT - timedelta(days=together + i),
                message=f"just a {i}",
                files=[file_a],
            )
        )
    for i in range(b_alone):
        commits.append(
            CommitInfo(
                hash=f"alone_b{i:04d}",
                date=_RECENT - timedelta(days=together + a_alone + i),
                message=f"just b {i}",
                files=[file_b],
            )
        )
    return commits


def _emergency_commits(filepath: str, count: int) -> list[CommitInfo]:
    """Create emergency/hotfix commits touching a file."""
    return [
        CommitInfo(
            hash=f"emrg{i:04d}",
            date=_RECENT - timedelta(days=i),
            message=f"hotfix: urgent fix #{i}",
            files=[filepath],
        )
        for i in range(count)
    ]


# --- blast-radius tests ---


class TestBlastRadius:
    def test_high_blast_radius(self):
        """File with median >= 8 co-changes flagged."""
        files = {"services/payment.py": "x = 1"}
        # 7 commits, each touching 9+ other files — median exceeds threshold of 8
        commits = _make_commits_for_blast_radius("services/payment.py", [9, 10, 8, 11, 9, 10, 8])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1
        assert "payment.py" in findings[0].message
        assert findings[0].check == "blast-radius"

    def test_low_blast_radius_no_finding(self):
        """File with median < 8 co-changes not flagged."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [5, 6, 4, 5, 6, 5, 4])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_commits(self):
        """File with < 5 qualifying commits skipped."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [6, 7, 8, 6])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is skipped."""
        files = {"pkg/__init__.py": ""}
        commits = _make_commits_for_blast_radius("pkg/__init__.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_bulk_commits_excluded(self):
        """Commits touching >= 20 files are excluded."""
        files = {"services/payment.py": "x = 1"}
        # All commits are bulk (>= 20 other files)
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [20, 25, 30, 22, 21, 20, 25]
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"tests/test_billing.py": "x = 1"}
        commits = _make_commits_for_blast_radius("tests/test_billing.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_blast_radius(ctx) == []

    def test_threshold_scales_with_project(self):
        """median_commit_size=5 means threshold=12, so median of 10 doesn't fire but 13 does."""
        files = {"services/payment.py": "x = 1"}

        # median of 10 < threshold of 12 -> no finding
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [10, 10, 10, 10, 10, 10, 10]
        )
        ctx = make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

        # median of 13 >= threshold of 12 -> finding
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [13, 14, 12, 15, 13, 14, 12]
        )
        ctx = make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1


# --- change-coupling tests ---


class TestChangeCoupling:
    def test_high_coupling_no_import(self):
        """Highly coupled files with no import -> finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "api/views.py" in findings[0].message
        assert "billing/invoice.py" in findings[0].message
        assert "hidden coupling" in findings[0].message

    def test_coupled_files_with_import_no_finding(self):
        """Highly coupled files WITH import relationship -> no finding."""
        files = {
            "api/views.py": "from billing.invoice import create_invoice\nx = 1",
            "billing/invoice.py": "def create_invoice(): pass",
        }
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_relative_import_detected(self):
        """Relative import (from .utils import ...) counts as import relationship."""
        files = {
            "havoc/works/models/file.py": "from .utils import generate_permalink\nx = 1",
            "havoc/works/models/utils.py": "def generate_permalink(): pass",
        }
        commits = _make_coupling_commits(
            "havoc/works/models/file.py",
            "havoc/works/models/utils.py",
            together=8,
            a_alone=2,
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_relative_parent_import_detected(self):
        """Relative parent import (from .. import X) counts as import relationship."""
        files = {
            "pkg/sub/views.py": "from ..models import User\nx = 1",
            "pkg/models.py": "class User: pass",
        }
        commits = _make_coupling_commits("pkg/sub/views.py", "pkg/models.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_low_coupling_ratio(self):
        """Low coupling ratio (< 0.7) -> no finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        # 5 together, a has 10 alone, b has 5 alone
        # min(15, 10) = 10, ratio = 5/10 = 0.5 < 0.7
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=5, a_alone=10, b_alone=5
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_co_changes(self):
        """Fewer than 5 co-changes -> no finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits("api/views.py", "billing/invoice.py", together=4)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_test_source_pair_skipped(self):
        """Test file + source file pair is skipped."""
        files = {
            "app.py": "x = 1",
            "test_app.py": "y = 2",
        }
        commits = _make_coupling_commits("app.py", "test_app.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py files are excluded from coupling analysis."""
        files = {
            "pkg/__init__.py": "",
            "other/module.py": "x = 1",
        }
        commits = _make_coupling_commits(
            "pkg/__init__.py", "other/module.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_migration_files_skipped(self):
        """Migration files are excluded."""
        files = {
            "app/migrations/0001.py": "x = 1",
            "app/models.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "app/migrations/0001.py", "app/models.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_expected_coupling_suppressed(self):
        """Pairs matching expected-coupling config are suppressed."""
        files = {
            "app/settings.py": "x = 1",
            "app/urls.py": "y = 2",
        }
        commits = _make_coupling_commits("app/settings.py", "app/urls.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        # Without config, would find coupling
        findings = check_change_coupling(ctx)
        assert len(findings) == 1

        # With expected-coupling, suppressed
        ctx.expected_coupling = [["*/settings.py", "*/urls.py"]]
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_change_coupling(ctx) == []


# --- yo-yo-code tests ---


class TestYoYoCode:
    def test_high_gross_churn(self):
        """File with gross churn >= 3x its size -> finding."""
        files = {"utils/parser.py": make_large_file(200)}
        file_stats = {
            # 350 ins + 340 del = 690 gross, 690/200 = 3.45x
            "utils/parser.py": FileStats(total_insertions=350, total_deletions=340, commit_count=12)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 1
        assert "3.5x turnover" in findings[0].message
        assert findings[0].check == "yo-yo-code"

    def test_low_gross_churn_no_finding(self):
        """File with gross churn < 3x -> no finding."""
        files = {"app.py": make_large_file(200)}
        file_stats = {
            # 200 ins + 100 del = 300 gross, 300/200 = 1.5x
            "app.py": FileStats(total_insertions=200, total_deletions=100, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 100 lines -> no finding."""
        files = {"tiny.py": make_large_file(50)}
        file_stats = {
            "tiny.py": FileStats(total_insertions=200, total_deletions=200, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": make_large_file(200)}
        file_stats = {
            "app.py": FileStats(total_insertions=400, total_deletions=400, commit_count=4)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_parser.py": make_large_file(200)}
        file_stats = {
            "test_parser.py": FileStats(total_insertions=400, total_deletions=400, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_different_from_churn_without_growth(self):
        """A file can trigger yo-yo but not churn-without-growth (and vice versa).

        yo-yo: high gross churn (ins + del) relative to size
        churn-without-growth: many commits, low net change (ins - del)
        A file growing fast has high net but could also have high gross.
        """
        files = {"app.py": make_large_file(200)}
        file_stats = {
            # Net: 400-300 = 100 (50% of 200, above 10% threshold -> no churn-without-growth)
            # Gross: 400+300 = 700 (3.5x of 200 -> yo-yo triggers)
            "app.py": FileStats(total_insertions=400, total_deletions=300, commit_count=15)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        assert len(check_yo_yo_code(ctx)) == 1
        assert len(check_churn_without_growth(ctx)) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_yo_yo_code(ctx) == []


# --- conscious-debt tests ---


class TestConsciousDebt:
    def test_debt_marker_in_commit(self):
        """Commit with debt keyword -> finding."""
        files = {"models/cache.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="a1b2c3d4e5f6",
                date=_RECENT,
                message="Add temporary workaround for rate limiting",
                files=["models/cache.py"],
            )
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 1
        assert "workaround" in findings[0].message
        assert "still needed" in findings[0].message

    def test_multiple_debt_commits_grouped(self):
        """Multiple debt commits to same file are grouped."""
        files = {"app.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="aaaa1111",
                date=_RECENT,
                message="Add temporary hack for deploy",
                files=["app.py"],
            ),
            CommitInfo(
                hash="bbbb2222",
                date=_RECENT - timedelta(days=10),
                message="Quick fix workaround for auth",
                files=["app.py"],
            ),
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 1
        assert "2 debt commits" in findings[0].message

    def test_no_debt_keywords(self):
        """Normal commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = feat_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 0

    def test_file_not_in_analysis(self):
        """Debt commit touching file not in all_trees -> no finding."""
        files = {"app.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="aaaa1111",
                date=_RECENT,
                message="Add temporary hack",
                files=["other.py"],
            )
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_conscious_debt(ctx) == []


# --- emergency-hotspots tests ---


class TestEmergencyHotspots:
    def test_high_emergency_ratio(self):
        """File with >= 30% emergency commits and >= 3 -> finding."""
        files = {"services/payment.py": "x = 1", "other/safe.py": "y = 2"}
        # payment.py: 4 emergency + 5 feat = 9 commits, 44% emergency
        # safe.py: 10 feat commits (dilutes project-wide emergency rate)
        # project total: 4 emergency / 19 = 21% < 30% -> check runs
        commits = (
            _emergency_commits("services/payment.py", 4)
            + feat_commits("services/payment.py", 5)
            + feat_commits("other/safe.py", 10)
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 1
        assert "emergency" in findings[0].check
        assert "emergency/hotfix" in findings[0].message

    def test_low_emergency_ratio_no_finding(self):
        """File with < 30% emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2) + feat_commits("app.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_few_emergency_commits(self):
        """File with < 3 emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_project_wide_high_emergency_suppressed(self):
        """If project emergency rate > 30%, suppress all findings."""
        files = {"app.py": "x = 1"}
        # All commits are emergencies — project-wide rate is 100%
        commits = _emergency_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_emergency_hotspots(ctx) == []


# --- no-refactoring tests ---


class TestNoRefactoring:
    def test_no_refactoring_detected(self):
        """File with many fixes/features but zero refactoring -> finding."""
        files = {"models/user.py": make_large_file(100)}
        commits = fix_commits("models/user.py", 5) + feat_commits("models/user.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 1
        assert "zero refactoring" in findings[0].message

    def test_has_refactoring_no_finding(self):
        """File with some refactoring -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = (
            fix_commits("app.py", 5)
            + feat_commits("app.py", 3)
            + [
                CommitInfo(
                    hash="refac001",
                    date=_RECENT,
                    message="refactor: simplify logic",
                    files=["app.py"],
                )
            ]
        )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 8 commits -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = fix_commits("app.py", 4) + feat_commits("app.py", 3)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_fix_feature_skipped(self):
        """File with < 6 fix+feature commits -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = (
            fix_commits("app.py", 2)
            + feat_commits("app.py", 2)
            + [
                CommitInfo(
                    hash=f"misc{i:04d}",
                    date=_RECENT - timedelta(days=i),
                    message=f"chore: cleanup #{i}",
                    files=["app.py"],
                )
                for i in range(6)
            ]
        )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"small.py": make_large_file(30)}
        commits = fix_commits("small.py", 5) + feat_commits("small.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_user.py": make_large_file(100)}
        commits = fix_commits("test_user.py", 5) + feat_commits("test_user.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> semantic checks skip."""
        files = {"app.py": make_large_file(100)}
        commits = fix_commits("app.py", 5) + feat_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=0.3)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_no_refactoring(ctx) == []


# --- test-erosion tests ---


class TestTestErosion:
    def test_eroding_tests(self):
        """Source file with 3x+ more commits than test -> finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 9) + feat_commits("tests/test_billing.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "9 source commits" in findings[0].message
        assert "eroding" in findings[0].message

    def test_balanced_commits_no_finding(self):
        """Source and test with similar commit counts -> no finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 6) + feat_commits("tests/test_billing.py", 4)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_no_test_file_no_finding(self):
        """Source file with no corresponding test -> no finding."""
        files = {"pkg/billing.py": make_large_file(100)}
        commits = feat_commits("pkg/billing.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_few_source_commits_skipped(self):
        """Source file with < 5 commits -> no finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 4)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """Source file < 50 lines -> no finding."""
        files = {
            "pkg/small.py": make_large_file(30),
            "tests/test_small.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/small.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_test_file_not_flagged_as_source(self):
        """Test files are not analyzed as source files."""
        files = {
            "test_billing.py": make_large_file(100),
            "billing.py": make_large_file(100),
        }
        commits = feat_commits("test_billing.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        # test_billing.py itself should not be flagged
        assert all(f.file != "test_billing.py" for f in findings)

    def test_same_directory_test_file(self):
        """Test file in same directory as source is found."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "pkg/test_billing.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 9) + feat_commits("pkg/test_billing.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1

    def test_zero_test_commits(self):
        """Test file exists but has zero commits -> finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 6)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "6.0x ratio" in findings[0].message

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_test_erosion(ctx) == []


# --- bulk commit filter tests (test-erosion) ---


class TestBulkCommitFilterTestErosion:
    def test_test_erosion_skips_bulk_commits(self):
        """Bulk commits excluded from test-erosion calculation."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        # 6 normal source + 5 bulk source = 11 total source
        # 3 normal test commits
        # Without filter: 11/3 = 3.67x -> finding
        # With filter: 6/3 = 2.0x < 3.0 -> no finding
        normal_source = feat_commits("pkg/billing.py", 6)
        bulk_source = [bulk_commit("pkg/billing.py", i) for i in range(5)]
        test_commits = feat_commits("tests/test_billing.py", 3)
        ctx = make_ctx(files, commits=normal_source + bulk_source + test_commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0


# --- reviewed suppression tests (blast-radius) ---


class TestReviewedSuppressionBlastRadius:
    def test_blast_radius_suppressed_after_review(self):
        """Pre-review high-blast commits excluded after review."""
        files = {"services/payment.py": "x = 1"}
        review_date = _RECENT - timedelta(days=5)
        # 7 old high-blast commits + 5 new low-blast commits
        old_commits = [
            CommitInfo(
                hash=f"old{i:04d}",
                date=_RECENT - timedelta(days=20 + i),
                message=f"commit {i}",
                files=["services/payment.py"] + [f"other_{i}_{j}.py" for j in range(10)],
            )
            for i in range(7)
        ]
        new_commits = [
            CommitInfo(
                hash=f"new{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit new {i}",
                files=["services/payment.py", f"just_one_{i}.py"],
            )
            for i in range(5)
        ]
        ctx = make_ctx(files, commits=old_commits + new_commits)
        # Without review: median co-changes ~10 -> finding
        findings = check_blast_radius(ctx)
        assert len(findings) == 1

        # With review: only 5 new commits with 1 co-change each -> no finding
        ctx.git_history.reviewed_at = {"services/payment.py": review_date}
        findings = check_blast_radius(ctx)
        assert len(findings) == 0
