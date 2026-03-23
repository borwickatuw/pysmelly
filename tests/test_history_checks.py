"""Tests for git history checks."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from pysmelly.checks.history import (
    check_abandoned_code,
    check_blast_radius,
    check_bug_magnet,
    check_change_coupling,
    check_churn_without_growth,
    check_conscious_debt,
    check_divergent_change,
    check_fix_propagation,
    check_growth_trajectory,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats, GitHistory


def _make_ctx(
    files: dict[str, str],
    last_modified: dict[str, datetime] | None = None,
    commits: list[CommitInfo] | None = None,
    file_stats: dict[str, FileStats] | None = None,
    message_quality: float = 0.5,
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

    # Build commits_for_file from commits
    if commits:
        for commit in commits:
            for filepath in commit.files:
                history.commits_for_file.setdefault(filepath, []).append(commit)

    ctx._git_history = history
    ctx._git_history_computed = True
    return ctx


_NOW = datetime.now(timezone.utc)
_RECENT = _NOW - timedelta(days=30)  # 1 month ago
_STALE = _NOW - timedelta(days=400)  # ~13 months ago


# --- abandoned-code tests ---


class TestAbandonedCode:
    def test_untouched_file_among_active_peers(self):
        """File on disk with no commits in window, while peers are active -> finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old.py": "w = 4",
        }
        # old.py has no commits in the window (not in last_modified)
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 1
        assert findings[0].file == "pkg/old.py"
        assert "no commits" in findings[0].message

    def test_all_files_untouched_no_finding(self):
        """No files have commits in window -> no active peers -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
        }
        # None have commits in the window
        ctx = _make_ctx(files, last_modified={})
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_all_files_active_no_finding(self):
        """All files have commits in window -> no untouched files -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_directory_with_fewer_than_3_files(self):
        """Directory with < 3 files -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/old.py": "y = 2",
        }
        last_modified = {"pkg/a.py": _RECENT}
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is naturally stable and should not be flagged."""
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
        }
        # __init__.py has no commits; peers are active
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_conftest_skipped(self):
        """conftest.py is naturally stable and should not be flagged."""
        files = {
            "tests/conftest.py": "",
            "tests/a.py": "x = 1",
            "tests/b.py": "y = 2",
            "tests/c.py": "z = 3",
        }
        last_modified = {
            "tests/a.py": _RECENT,
            "tests/b.py": _RECENT,
            "tests/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_config_file_skipped(self):
        """Config files (settings.py, etc.) are naturally stable."""
        files = {
            "pkg/settings.py": "x = 1",
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_minority_active_no_finding(self):
        """Less than half of peers active -> not meaningful -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/d.py": "w = 4",
        }
        # Only 1 of 4 files is active (< 50%)
        last_modified = {"pkg/a.py": _RECENT}
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_no_git_history_returns_empty(self):
        """No git_history on context -> empty list."""
        all_trees = {Path("pkg/a.py"): ast.parse("x = 1")}
        ctx = AnalysisContext(all_trees, verbose=False)
        findings = check_abandoned_code(ctx)
        assert findings == []

    def test_finding_uses_line_1(self):
        """File-level findings use line 1."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old.py": "w = 4",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert findings[0].line == 1

    def test_multiple_untouched_files(self):
        """Multiple untouched files in an active directory."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old1.py": "w = 4",
            "pkg/old2.py": "v = 5",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        found_files = {f.file for f in findings}
        assert found_files == {"pkg/old1.py", "pkg/old2.py"}

    def test_separate_directories_independent(self):
        """Each directory is evaluated independently."""
        files = {
            "active/a.py": "x = 1",
            "active/b.py": "y = 2",
            "active/c.py": "z = 3",
            "active/old.py": "w = 4",
            "stale/a.py": "x = 1",
            "stale/b.py": "y = 2",
            "stale/c.py": "z = 3",
        }
        # active/ has 3 of 4 active; stale/ has 0 of 3 active
        last_modified = {
            "active/a.py": _RECENT,
            "active/b.py": _RECENT,
            "active/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        # Only active/ should produce a finding (stale/ has no active peers)
        assert len(findings) == 1
        assert findings[0].file == "active/old.py"

    def test_reviewed_file_not_flagged(self):
        """A reviewed file appears in last_modified, so it's not flagged.

        The reviewed marker updates last_modified in GitHistory, so by the
        time the check runs, the file appears as having activity in the window.
        """
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old.py": "w = 4",
        }
        # old.py was reviewed — it now appears in last_modified
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
            "pkg/old.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_message_includes_window(self):
        """Finding message references the window period."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old.py": "w = 4",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert "6m" in findings[0].message


# --- blast-radius tests ---


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


class TestBlastRadius:
    def test_high_blast_radius(self):
        """File with median >= 5 co-changes flagged."""
        files = {"services/payment.py": "x = 1"}
        # 7 commits, each touching 6 other files
        commits = _make_commits_for_blast_radius("services/payment.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1
        assert "payment.py" in findings[0].message
        assert "blast-radius" == findings[0].check

    def test_low_blast_radius_no_finding(self):
        """File with median < 5 co-changes not flagged."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [2, 3, 1, 2, 3, 2, 1])
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_commits(self):
        """File with < 5 qualifying commits skipped."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [6, 7, 8, 6])
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is skipped."""
        files = {"pkg/__init__.py": ""}
        commits = _make_commits_for_blast_radius("pkg/__init__.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_bulk_commits_excluded(self):
        """Commits touching >= 20 files are excluded."""
        files = {"services/payment.py": "x = 1"}
        # All commits are bulk (>= 20 other files)
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [20, 25, 30, 22, 21, 20, 25]
        )
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_blast_radius(ctx) == []


# --- change-coupling tests ---


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
        ctx = _make_ctx(files, commits=commits)
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
        ctx = _make_ctx(files, commits=commits)
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
        ctx = _make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_co_changes(self):
        """Fewer than 5 co-changes -> no finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits("api/views.py", "billing/invoice.py", together=4)
        ctx = _make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_test_source_pair_skipped(self):
        """Test file + source file pair is skipped."""
        files = {
            "app.py": "x = 1",
            "test_app.py": "y = 2",
        }
        commits = _make_coupling_commits("app.py", "test_app.py", together=8, a_alone=2)
        ctx = _make_ctx(files, commits=commits)
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
        ctx = _make_ctx(files, commits=commits)
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
        ctx = _make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_change_coupling(ctx) == []


# --- growth-trajectory tests ---


def _make_large_file(line_count: int) -> str:
    """Create a Python file with approximately N lines."""
    lines = [f"x_{i} = {i}" for i in range(line_count)]
    return "\n".join(lines) + "\n"


class TestGrowthTrajectory:
    def test_rapid_growth(self):
        """File that grew >= 200 lines AND >= 2x -> finding."""
        files = {"models/user.py": _make_large_file(380)}
        file_stats = {
            "models/user.py": FileStats(total_insertions=300, total_deletions=40, commit_count=15)
        }
        # start_lines = 380 - (300 - 40) = 120, growth = 260, ratio = 3.17x
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 1
        assert "grew from" in findings[0].message
        assert "models/user.py" in findings[0].file

    def test_small_file_skipped(self):
        """File < 100 lines -> no finding."""
        files = {"small.py": _make_large_file(50)}
        file_stats = {
            "small.py": FileStats(total_insertions=40, total_deletions=5, commit_count=10)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_moderate_growth_no_finding(self):
        """File that grew but < 200 lines -> no finding."""
        files = {"app.py": _make_large_file(200)}
        file_stats = {
            "app.py": FileStats(total_insertions=120, total_deletions=20, commit_count=10)
        }
        # start_lines = 200 - (120-20) = 100, growth = 100 < 200 threshold
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_new_file_skipped(self):
        """File that didn't exist at start of window -> no finding."""
        files = {"new.py": _make_large_file(300)}
        file_stats = {"new.py": FileStats(total_insertions=310, total_deletions=10, commit_count=5)}
        # start_lines = 300 - (310 - 10) = 0 -> skip
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_growth_trajectory(ctx)
        assert len(findings) == 0

    def test_grew_less_than_2x(self):
        """File grew >= 200 lines but < 2x -> no finding."""
        files = {"big.py": _make_large_file(500)}
        file_stats = {
            "big.py": FileStats(total_insertions=250, total_deletions=50, commit_count=15)
        }
        # start_lines = 500 - (250-50) = 300, growth = 200, ratio = 1.67x < 2.0
        ctx = _make_ctx(files, file_stats=file_stats)
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
        files = {"utils/parser.py": _make_large_file(280)}
        file_stats = {
            "utils/parser.py": FileStats(total_insertions=200, total_deletions=188, commit_count=18)
        }
        # net_growth = 200 - 188 = 12, 10% of 280 = 28, 12 <= 28 -> flag
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 1
        assert "rewritten" in findings[0].message

    def test_growing_file_no_finding(self):
        """File with substantial net growth -> no finding."""
        files = {"app.py": _make_large_file(300)}
        file_stats = {
            "app.py": FileStats(total_insertions=200, total_deletions=50, commit_count=15)
        }
        # net_growth = 150, 10% of 300 = 30, 150 > 30 -> no flag
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 10 commits -> no finding."""
        files = {"utils/parser.py": _make_large_file(200)}
        file_stats = {
            "utils/parser.py": FileStats(total_insertions=100, total_deletions=95, commit_count=8)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"tiny.py": _make_large_file(30)}
        file_stats = {
            "tiny.py": FileStats(total_insertions=50, total_deletions=48, commit_count=12)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_parser.py": _make_large_file(200)}
        file_stats = {
            "test_parser.py": FileStats(total_insertions=200, total_deletions=195, commit_count=15)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_churn_without_growth(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_churn_without_growth(ctx) == []


# --- Semantic check helpers ---


def _fix_commits(filepath: str, count: int) -> list[CommitInfo]:
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


def _feat_commits(filepath: str, count: int) -> list[CommitInfo]:
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


# --- bug-magnet tests ---


class TestBugMagnet:
    def test_majority_fixes(self):
        """File with >= 50% fix commits -> finding."""
        files = {"services/billing.py": "x = 1"}
        commits = _fix_commits("services/billing.py", 8) + _feat_commits("services/billing.py", 4)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 1
        assert "8 of 12" in findings[0].message
        assert "bug-magnet" == findings[0].check

    def test_minority_fixes_no_finding(self):
        """File with < 50% fix commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _fix_commits("app.py", 2) + _feat_commits("app.py", 8)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _fix_commits("app.py", 4)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_billing.py": "x = 1"}
        commits = _fix_commits("test_billing.py", 8)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> semantic checks skip."""
        files = {"app.py": "x = 1"}
        commits = _fix_commits("app.py", 8)
        ctx = _make_ctx(files, commits=commits, message_quality=0.3)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_fix_propagation(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_fix_propagation(ctx) == []


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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 1
        assert "2 debt commits" in findings[0].message

    def test_no_debt_keywords(self):
        """Normal commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _feat_commits("app.py", 5)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_conscious_debt(ctx) == []


# --- divergent-change tests ---


class TestDivergentChange:
    def test_many_scopes(self):
        """File with 4+ scopes -> finding."""
        files = {"models/user.py": _make_large_file(100)}
        commits = []
        for scope in ["auth", "billing", "notifications", "reporting"]:
            for i in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"{scope}{i:04d}",
                        date=_RECENT - timedelta(days=i),
                        message=f"feat({scope}): update user model",
                        files=["models/user.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 1
        assert "4 different concerns" in findings[0].message

    def test_few_scopes_no_finding(self):
        """File with < 4 scopes -> no finding."""
        files = {"app.py": _make_large_file(100)}
        commits = []
        for scope in ["auth", "billing"]:
            for i in range(3):
                commits.append(
                    CommitInfo(
                        hash=f"{scope}{i:04d}",
                        date=_RECENT - timedelta(days=i),
                        message=f"feat({scope}): update",
                        files=["app.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"small.py": _make_large_file(30)}
        commits = []
        for scope in ["a", "b", "c", "d"]:
            for i in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"{scope}{i:04d}",
                        date=_RECENT - timedelta(days=i),
                        message=f"feat({scope}): update",
                        files=["small.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_scopes_need_minimum_commits(self):
        """Scopes with only 1 commit don't count."""
        files = {"app.py": _make_large_file(100)}
        commits = [
            CommitInfo(
                hash=f"{scope}0001",
                date=_RECENT,
                message=f"feat({scope}): one-off",
                files=["app.py"],
            )
            for scope in ["a", "b", "c", "d", "e"]
        ]
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_no_conventional_commits(self):
        """No scoped commits -> no finding."""
        files = {"app.py": _make_large_file(100)}
        commits = _feat_commits("app.py", 10)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is skipped."""
        files = {"pkg/__init__.py": _make_large_file(100)}
        commits = []
        for scope in ["a", "b", "c", "d"]:
            for i in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"{scope}{i:04d}",
                        date=_RECENT - timedelta(days=i),
                        message=f"feat({scope}): update",
                        files=["pkg/__init__.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_divergent_change(ctx) == []
