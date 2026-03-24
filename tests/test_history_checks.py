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
    check_emergency_hotspots,
    check_fix_follows_feature,
    check_fix_propagation,
    check_growth_trajectory,
    check_hotspot_acceleration,
    check_knowledge_silo,
    check_no_refactoring,
    check_stabilization_failure,
    check_test_erosion,
    check_yo_yo_code,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats, GitHistory, TimeSlice


def _make_ctx(
    files: dict[str, str],
    last_modified: dict[str, datetime] | None = None,
    commits: list[CommitInfo] | None = None,
    file_stats: dict[str, FileStats] | None = None,
    message_quality: float = 0.5,
    authors_for_file: dict[str, dict[str, int]] | None = None,
    time_slices: list[TimeSlice] | None = None,
    commits_per_slice: float | None = None,
    distinct_authors: int = 5,
    median_commit_size: float = 1.0,
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
    history.authors_for_file = authors_for_file or {}
    history._time_slices = time_slices
    history._commits_per_slice = commits_per_slice
    history._distinct_authors = distinct_authors
    history._median_commit_size = median_commit_size

    # Build commits_for_file from commits
    if commits:
        for commit in commits:
            for filepath in commit.files:
                history.commits_for_file.setdefault(filepath, []).append(commit)

    # Build authors_for_file from commits if not provided
    if not authors_for_file and commits:
        history.authors_for_file = {}
        for commit in commits:
            if commit.author:
                for filepath in commit.files:
                    author_counts = history.authors_for_file.setdefault(filepath, {})
                    author_counts[commit.author] = author_counts.get(commit.author, 0) + 1

    ctx._git_history = history
    ctx._git_history_computed = True
    return ctx


_NOW = datetime.now(timezone.utc)
_RECENT = _NOW - timedelta(days=30)  # 1 month ago
_STALE = _NOW - timedelta(days=400)  # ~13 months ago


def _make_large_file(line_count: int) -> str:
    """Create a Python file with approximately N lines."""
    lines = [f"x_{i} = {i}" for i in range(line_count)]
    return "\n".join(lines) + "\n"


# --- abandoned-code tests ---


class TestAbandonedCode:
    def test_untouched_file_among_active_peers(self):
        """File on disk with no commits in window, while peers are active -> finding."""
        files = {
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/old.py": _make_large_file(30),
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
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/old.py": _make_large_file(30),
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
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/old1.py": _make_large_file(30),
            "pkg/old2.py": _make_large_file(30),
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
            "active/a.py": _make_large_file(30),
            "active/b.py": _make_large_file(30),
            "active/c.py": _make_large_file(30),
            "active/old.py": _make_large_file(30),
            "stale/a.py": _make_large_file(30),
            "stale/b.py": _make_large_file(30),
            "stale/c.py": _make_large_file(30),
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
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/old.py": _make_large_file(30),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert "6m" in findings[0].message

    def test_small_file_skipped(self):
        """File under 20 lines -> no finding."""
        files = {
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/tiny.py": "x = 1",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_large_file_flagged(self):
        """File over 20 lines -> finding produced."""
        files = {
            "pkg/a.py": _make_large_file(30),
            "pkg/b.py": _make_large_file(30),
            "pkg/c.py": _make_large_file(30),
            "pkg/old.py": _make_large_file(25),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 1
        assert findings[0].file == "pkg/old.py"


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
        """File with median >= 8 co-changes flagged."""
        files = {"services/payment.py": "x = 1"}
        # 7 commits, each touching 9+ other files — median exceeds threshold of 8
        commits = _make_commits_for_blast_radius("services/payment.py", [9, 10, 8, 11, 9, 10, 8])
        ctx = _make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1
        assert "payment.py" in findings[0].message
        assert "blast-radius" == findings[0].check

    def test_low_blast_radius_no_finding(self):
        """File with median < 8 co-changes not flagged."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [5, 6, 4, 5, 6, 5, 4])
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

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"tests/test_billing.py": "x = 1"}
        commits = _make_commits_for_blast_radius("tests/test_billing.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = _make_ctx(files, commits=commits)
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
        ctx = _make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

        # median of 13 >= threshold of 12 -> finding
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [13, 14, 12, 15, 13, 14, 12]
        )
        ctx = _make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1


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

    def test_expected_coupling_suppressed(self):
        """Pairs matching expected-coupling config are suppressed."""
        files = {
            "app/settings.py": "x = 1",
            "app/urls.py": "y = 2",
        }
        commits = _make_coupling_commits("app/settings.py", "app/urls.py", together=8, a_alone=2)
        ctx = _make_ctx(files, commits=commits)
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


# --- growth-trajectory tests ---


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


# --- yo-yo-code tests ---


class TestYoYoCode:
    def test_high_gross_churn(self):
        """File with gross churn >= 3x its size -> finding."""
        files = {"utils/parser.py": _make_large_file(200)}
        file_stats = {
            # 350 ins + 340 del = 690 gross, 690/200 = 3.45x
            "utils/parser.py": FileStats(total_insertions=350, total_deletions=340, commit_count=12)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 1
        assert "3.5x turnover" in findings[0].message
        assert "yo-yo-code" == findings[0].check

    def test_low_gross_churn_no_finding(self):
        """File with gross churn < 3x -> no finding."""
        files = {"app.py": _make_large_file(200)}
        file_stats = {
            # 200 ins + 100 del = 300 gross, 300/200 = 1.5x
            "app.py": FileStats(total_insertions=200, total_deletions=100, commit_count=10)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 100 lines -> no finding."""
        files = {"tiny.py": _make_large_file(50)}
        file_stats = {
            "tiny.py": FileStats(total_insertions=200, total_deletions=200, commit_count=10)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": _make_large_file(200)}
        file_stats = {
            "app.py": FileStats(total_insertions=400, total_deletions=400, commit_count=4)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_parser.py": _make_large_file(200)}
        file_stats = {
            "test_parser.py": FileStats(total_insertions=400, total_deletions=400, commit_count=10)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_different_from_churn_without_growth(self):
        """A file can trigger yo-yo but not churn-without-growth (and vice versa).

        yo-yo: high gross churn (ins + del) relative to size
        churn-without-growth: many commits, low net change (ins - del)
        A file growing fast has high net but could also have high gross.
        """
        files = {"app.py": _make_large_file(200)}
        file_stats = {
            # Net: 400-300 = 100 (50% of 200, above 10% threshold -> no churn-without-growth)
            # Gross: 400+300 = 700 (3.5x of 200 -> yo-yo triggers)
            "app.py": FileStats(total_insertions=400, total_deletions=300, commit_count=15)
        }
        ctx = _make_ctx(files, file_stats=file_stats)
        assert len(check_yo_yo_code(ctx)) == 1
        assert len(check_churn_without_growth(ctx)) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_yo_yo_code(ctx) == []


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

    def test_directory_fallback_scope(self):
        """Without conventional scopes, infer scope from co-changed directories."""
        files = {"models/user.py": _make_large_file(100)}
        commits = []
        # Commits touching user.py alongside files in 4 different top-level dirs
        for i, dir_name in enumerate(["auth", "billing", "notifications", "reporting"]):
            for j in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"dir_{dir_name}_{j:04d}",
                        date=_RECENT - timedelta(days=i * 2 + j),
                        message=f"Update {dir_name} integration",
                        files=["models/user.py", f"{dir_name}/handler.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 1
        assert "4 different concerns" in findings[0].message

    def test_directory_fallback_too_few_dirs(self):
        """Directory fallback with < 4 dirs -> no finding."""
        files = {"models/user.py": _make_large_file(100)}
        commits = []
        for i, dir_name in enumerate(["auth", "billing"]):
            for j in range(3):
                commits.append(
                    CommitInfo(
                        hash=f"dir_{dir_name}_{j:04d}",
                        date=_RECENT - timedelta(days=i * 3 + j),
                        message=f"Update {dir_name} integration",
                        files=["models/user.py", f"{dir_name}/handler.py"],
                    )
                )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0


# --- knowledge-silo tests ---


class TestKnowledgeSilo:
    def test_dominant_author(self):
        """One author with >= 80% of commits -> finding."""
        files = {"services/billing.py": "x = 1"}
        authors = {"services/billing.py": {"Alice": 8, "Bob": 2}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1
        assert "Alice" in findings[0].message
        assert "bus-factor" in findings[0].message

    def test_shared_ownership_no_finding(self):
        """Multiple authors with no dominant one -> no finding."""
        files = {"app.py": "x = 1"}
        authors = {"app.py": {"Alice": 4, "Bob": 3, "Charlie": 3}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": "x = 1"}
        authors = {"app.py": {"Alice": 4}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_billing.py": "x = 1"}
        authors = {"test_billing.py": {"Alice": 10}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_knowledge_silo(ctx) == []

    def test_dominance_exactly_80_percent(self):
        """Dominance at exactly 80% threshold -> finding."""
        files = {"app.py": "x = 1"}
        authors = {"app.py": {"Alice": 8, "Bob": 2}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1

    def test_dominance_below_threshold(self):
        """Dominance at 79% -> no finding."""
        files = {"app.py": "x = 1"}
        # 79/100 = 0.79, below 0.8
        authors = {"app.py": {"Alice": 79, "Bob": 21}}
        ctx = _make_ctx(files, authors_for_file=authors)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_skipped_for_solo_project(self):
        """1 author -> no findings (bus-factor is meaningless)."""
        files = {"services/billing.py": "x = 1"}
        authors = {"services/billing.py": {"Alice": 10}}
        ctx = _make_ctx(files, authors_for_file=authors, distinct_authors=1)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_skipped_for_two_authors(self):
        """2 authors -> no findings."""
        files = {"services/billing.py": "x = 1"}
        authors = {"services/billing.py": {"Alice": 8, "Bob": 2}}
        ctx = _make_ctx(files, authors_for_file=authors, distinct_authors=2)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_fires_with_three_plus_authors(self):
        """3 authors project-wide -> knowledge-silo can fire."""
        files = {"services/billing.py": "x = 1"}
        authors = {"services/billing.py": {"Alice": 8, "Bob": 2}}
        ctx = _make_ctx(files, authors_for_file=authors, distinct_authors=3)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1


# --- emergency-hotspots tests ---


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


class TestEmergencyHotspots:
    def test_high_emergency_ratio(self):
        """File with >= 30% emergency commits and >= 3 -> finding."""
        files = {"services/payment.py": "x = 1", "other/safe.py": "y = 2"}
        # payment.py: 4 emergency + 5 feat = 9 commits, 44% emergency
        # safe.py: 10 feat commits (dilutes project-wide emergency rate)
        # project total: 4 emergency / 19 = 21% < 30% -> check runs
        commits = (
            _emergency_commits("services/payment.py", 4)
            + _feat_commits("services/payment.py", 5)
            + _feat_commits("other/safe.py", 10)
        )
        ctx = _make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 1
        assert "emergency" in findings[0].check
        assert "emergency/hotfix" in findings[0].message

    def test_low_emergency_ratio_no_finding(self):
        """File with < 30% emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2) + _feat_commits("app.py", 10)
        ctx = _make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_few_emergency_commits(self):
        """File with < 3 emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2)
        ctx = _make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_project_wide_high_emergency_suppressed(self):
        """If project emergency rate > 30%, suppress all findings."""
        files = {"app.py": "x = 1"}
        # All commits are emergencies — project-wide rate is 100%
        commits = _emergency_commits("app.py", 5)
        ctx = _make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_emergency_hotspots(ctx) == []


# --- no-refactoring tests ---


class TestNoRefactoring:
    def test_no_refactoring_detected(self):
        """File with many fixes/features but zero refactoring -> finding."""
        files = {"models/user.py": _make_large_file(100)}
        commits = _fix_commits("models/user.py", 5) + _feat_commits("models/user.py", 5)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 1
        assert "zero refactoring" in findings[0].message

    def test_has_refactoring_no_finding(self):
        """File with some refactoring -> no finding."""
        files = {"app.py": _make_large_file(100)}
        commits = (
            _fix_commits("app.py", 5)
            + _feat_commits("app.py", 3)
            + [
                CommitInfo(
                    hash="refac001",
                    date=_RECENT,
                    message="refactor: simplify logic",
                    files=["app.py"],
                )
            ]
        )
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 8 commits -> no finding."""
        files = {"app.py": _make_large_file(100)}
        commits = _fix_commits("app.py", 4) + _feat_commits("app.py", 3)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_fix_feature_skipped(self):
        """File with < 6 fix+feature commits -> no finding."""
        files = {"app.py": _make_large_file(100)}
        commits = (
            _fix_commits("app.py", 2)
            + _feat_commits("app.py", 2)
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
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"small.py": _make_large_file(30)}
        commits = _fix_commits("small.py", 5) + _feat_commits("small.py", 5)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_user.py": _make_large_file(100)}
        commits = _fix_commits("test_user.py", 5) + _feat_commits("test_user.py", 5)
        ctx = _make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> semantic checks skip."""
        files = {"app.py": _make_large_file(100)}
        commits = _fix_commits("app.py", 5) + _feat_commits("app.py", 5)
        ctx = _make_ctx(files, commits=commits, message_quality=0.3)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_no_refactoring(ctx) == []


# --- Time-slice based check helpers ---


def _make_time_slices(
    file_str: str,
    pattern: list[str],
    period_days: int = 14,
) -> list[TimeSlice]:
    """Create time slices with specified activity pattern.

    pattern is a list of strings like "feature", "fix", "both", "active", "inactive".
    """
    from pysmelly.git_history import classify_commit

    slices = []
    base = _NOW - timedelta(days=len(pattern) * period_days)
    for i, kind in enumerate(pattern):
        start = base + timedelta(days=i * period_days)
        end = start + timedelta(days=period_days)
        ts = TimeSlice(start=start, end=end)
        if kind == "inactive":
            pass
        elif kind == "feature":
            c = CommitInfo(
                hash=f"feat_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"feat: feature in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("feature", set()).add(file_str)
        elif kind == "fix":
            c = CommitInfo(
                hash=f"fix_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"fix: fix in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("fix", set()).add(file_str)
        elif kind == "both":
            c1 = CommitInfo(
                hash=f"feat_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"feat: feature in slice {i}",
                files=[file_str],
            )
            c2 = CommitInfo(
                hash=f"fix_s{i:04d}",
                date=start + timedelta(days=2),
                message=f"fix: fix in slice {i}",
                files=[file_str],
            )
            ts.commits.extend([c1, c2])
            ts.files_touched.add(file_str)
            ts.files_by_category.setdefault("feature", set()).add(file_str)
            ts.files_by_category.setdefault("fix", set()).add(file_str)
        elif kind == "active":
            c = CommitInfo(
                hash=f"act_s{i:04d}",
                date=start + timedelta(days=1),
                message=f"chore: work in slice {i}",
                files=[file_str],
            )
            ts.commits.append(c)
            ts.files_touched.add(file_str)
        slices.append(ts)

    return slices


def _commits_from_slices(slices: list[TimeSlice]) -> list[CommitInfo]:
    """Extract all commits from time slices."""
    commits = []
    for ts in slices:
        commits.extend(ts.commits)
    return commits


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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("test_app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"test_app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
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
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
        # Only 6 commits from slices, which is < 8
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        slices = _make_time_slices("app.py", pattern)
        commits = _commits_from_slices(slices)
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
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        commits = _commits_from_slices(slices)
        files = {"services/api.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        commits = _commits_from_slices(slices)
        files = {"test_api.py": _make_large_file(100)}
        ctx = _make_ctx(
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
        commits = _commits_from_slices(slices)
        files = {"app.py": _make_large_file(100)}
        ctx = _make_ctx(
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


# --- test-erosion tests ---


class TestTestErosion:
    def test_eroding_tests(self):
        """Source file with 3x+ more commits than test -> finding."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/billing.py", 9) + _feat_commits("tests/test_billing.py", 2)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "9 source commits" in findings[0].message
        assert "eroding" in findings[0].message

    def test_balanced_commits_no_finding(self):
        """Source and test with similar commit counts -> no finding."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/billing.py", 6) + _feat_commits("tests/test_billing.py", 4)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_no_test_file_no_finding(self):
        """Source file with no corresponding test -> no finding."""
        files = {"pkg/billing.py": _make_large_file(100)}
        commits = _feat_commits("pkg/billing.py", 10)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_few_source_commits_skipped(self):
        """Source file with < 5 commits -> no finding."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/billing.py", 4)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """Source file < 50 lines -> no finding."""
        files = {
            "pkg/small.py": _make_large_file(30),
            "tests/test_small.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/small.py", 10)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_test_file_not_flagged_as_source(self):
        """Test files are not analyzed as source files."""
        files = {
            "test_billing.py": _make_large_file(100),
            "billing.py": _make_large_file(100),
        }
        commits = _feat_commits("test_billing.py", 10)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        # test_billing.py itself should not be flagged
        assert all(f.file != "test_billing.py" for f in findings)

    def test_same_directory_test_file(self):
        """Test file in same directory as source is found."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "pkg/test_billing.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/billing.py", 9) + _feat_commits("pkg/test_billing.py", 2)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1

    def test_zero_test_commits(self):
        """Test file exists but has zero commits -> finding."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        commits = _feat_commits("pkg/billing.py", 6)
        ctx = _make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "6.0x ratio" in findings[0].message

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_test_erosion(ctx) == []


# --- bulk commit filter tests ---


def _bulk_commit(filepath: str, idx: int) -> CommitInfo:
    """Create a commit touching 30+ .py files (bulk)."""
    files = [filepath] + [f"bulk_{idx}_{j}.py" for j in range(30)]
    return CommitInfo(
        hash=f"bulk{idx:04d}",
        date=_RECENT - timedelta(days=idx),
        message=f"fix: bulk refactor #{idx}",
        files=files,
    )


class TestBulkCommitFilter:
    def test_bug_magnet_skips_bulk_commits(self):
        """Bulk commits (30+ .py files) excluded from bug-magnet calculation."""
        files = {"services/billing.py": "x = 1"}
        # 3 normal fix commits + 5 bulk fix commits = 8 total
        # Without filter: 8/8 = 100% fix ratio -> finding
        # With filter: only 3 normal commits < 5 min -> no finding
        normal_fixes = _fix_commits("services/billing.py", 3)
        bulk_fixes = [_bulk_commit("services/billing.py", i) for i in range(5)]
        ctx = _make_ctx(files, commits=normal_fixes + bulk_fixes, message_quality=1.0)
        findings = check_bug_magnet(ctx)
        assert len(findings) == 0

    def test_test_erosion_skips_bulk_commits(self):
        """Bulk commits excluded from test-erosion calculation."""
        files = {
            "pkg/billing.py": _make_large_file(100),
            "tests/test_billing.py": "def test_it(): pass",
        }
        # 6 normal source + 5 bulk source = 11 total source
        # 3 normal test commits
        # Without filter: 11/3 = 3.67x -> finding
        # With filter: 6/3 = 2.0x < 3.0 -> no finding
        normal_source = _feat_commits("pkg/billing.py", 6)
        bulk_source = [_bulk_commit("pkg/billing.py", i) for i in range(5)]
        test_commits = _feat_commits("tests/test_billing.py", 3)
        ctx = _make_ctx(files, commits=normal_source + bulk_source + test_commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0
