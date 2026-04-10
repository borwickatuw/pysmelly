"""Tests for git history team checks."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from pysmelly.checks.history_team import (
    check_abandoned_code,
    check_divergent_change,
    check_knowledge_silo,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo

from .history_test_helpers import (
    _RECENT,
    feat_commits,
    make_ctx,
    make_large_file,
)


def _author_commits(filepath: str, author_counts: dict[str, int]) -> list[CommitInfo]:
    """Create commits with specific author distributions."""
    commits = []
    idx = 0
    for author, count in author_counts.items():
        for i in range(count):
            commits.append(
                CommitInfo(
                    hash=f"auth{idx:04d}",
                    date=_RECENT - timedelta(days=idx),
                    message=f"feat: work by {author} #{i}",
                    author=author,
                    files=[filepath],
                )
            )
            idx += 1
    return commits


# --- abandoned-code tests ---


class TestAbandonedCode:
    def test_untouched_file_among_active_peers(self):
        """File on disk with no commits in window, while peers are active -> finding."""
        files = {
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/old.py": make_large_file(30),
        }
        # old.py has no commits in the window (not in last_modified)
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified={})
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
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_directory_with_fewer_than_3_files(self):
        """Directory with < 3 files -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/old.py": "y = 2",
        }
        last_modified = {"pkg/a.py": _RECENT}
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified)
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
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/old.py": make_large_file(30),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert findings[0].line == 1

    def test_multiple_untouched_files(self):
        """Multiple untouched files in an active directory."""
        files = {
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/d.py": make_large_file(30),
            "pkg/old1.py": make_large_file(30),
            "pkg/old2.py": make_large_file(30),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
            "pkg/d.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        found_files = {f.file for f in findings}
        assert found_files == {"pkg/old1.py", "pkg/old2.py"}

    def test_separate_directories_independent(self):
        """Each directory is evaluated independently."""
        files = {
            "active/a.py": make_large_file(30),
            "active/b.py": make_large_file(30),
            "active/c.py": make_large_file(30),
            "active/old.py": make_large_file(30),
            "stale/a.py": make_large_file(30),
            "stale/b.py": make_large_file(30),
            "stale/c.py": make_large_file(30),
        }
        # active/ has 3 of 4 active; stale/ has 0 of 3 active
        last_modified = {
            "active/a.py": _RECENT,
            "active/b.py": _RECENT,
            "active/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
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
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_message_includes_window(self):
        """Finding message references the window period."""
        files = {
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/old.py": make_large_file(30),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert "6m" in findings[0].message

    def test_small_file_skipped(self):
        """File under 20 lines -> no finding."""
        files = {
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/tiny.py": "x = 1",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_large_file_flagged(self):
        """File over 20 lines -> finding produced."""
        files = {
            "pkg/a.py": make_large_file(30),
            "pkg/b.py": make_large_file(30),
            "pkg/c.py": make_large_file(30),
            "pkg/old.py": make_large_file(25),
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 1
        assert findings[0].file == "pkg/old.py"


# --- divergent-change tests ---


class TestDivergentChange:
    def test_many_scopes(self):
        """File with 4+ scopes -> finding."""
        files = {"models/user.py": make_large_file(100)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 1
        assert "4 different concerns" in findings[0].message

    def test_few_scopes_no_finding(self):
        """File with < 4 scopes -> no finding."""
        files = {"app.py": make_large_file(100)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"small.py": make_large_file(30)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_scopes_need_minimum_commits(self):
        """Scopes with only 1 commit don't count."""
        files = {"app.py": make_large_file(100)}
        commits = [
            CommitInfo(
                hash=f"{scope}0001",
                date=_RECENT,
                message=f"feat({scope}): one-off",
                files=["app.py"],
            )
            for scope in ["a", "b", "c", "d", "e"]
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_no_conventional_commits(self):
        """No scoped commits -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = feat_commits("app.py", 10)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is skipped."""
        files = {"pkg/__init__.py": make_large_file(100)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_divergent_change(ctx) == []

    def test_directory_fallback_scope(self):
        """Without conventional scopes, infer scope from co-changed directories."""
        files = {"models/user.py": make_large_file(100)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 1
        assert "4 different concerns" in findings[0].message

    def test_directory_fallback_too_few_dirs(self):
        """Directory fallback with < 4 dirs -> no finding."""
        files = {"models/user.py": make_large_file(100)}
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
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_structural_dirs_excluded(self):
        """Structural dirs (tests, docs, scripts, etc.) don't count as concerns."""
        files = {"models/user.py": make_large_file(100)}
        commits = []
        for i, dir_name in enumerate(["tests", "docs", "scripts", "examples", "benchmarks"]):
            for j in range(3):
                commits.append(
                    CommitInfo(
                        hash=f"struct_{dir_name}_{j:04d}",
                        date=_RECENT - timedelta(days=i * 3 + j),
                        message=f"Update {dir_name}",
                        files=["models/user.py", f"{dir_name}/helper.py"],
                    )
                )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0

    def test_own_dir_excluded(self):
        """The target file's own top-level dir is excluded from scope count."""
        files = {"fastapi/routing.py": make_large_file(100)}
        commits = []
        # Co-changes with fastapi + 3 other dirs = 4 total, but fastapi is excluded
        for i, dir_name in enumerate(["fastapi", "auth", "billing", "api"]):
            for j in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"own_{dir_name}_{j:04d}",
                        date=_RECENT - timedelta(days=i * 2 + j),
                        message=f"Update {dir_name}",
                        files=["fastapi/routing.py", f"{dir_name}/handler.py"],
                    )
                )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        # Only 3 external dirs (auth, billing, api) after excluding own dir -> no finding
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped by divergent-change."""
        files = {"test_user.py": make_large_file(100)}
        commits = []
        for i, dir_name in enumerate(["auth", "billing", "notifications", "reporting"]):
            for j in range(2):
                commits.append(
                    CommitInfo(
                        hash=f"test_skip_{dir_name}_{j:04d}",
                        date=_RECENT - timedelta(days=i * 2 + j),
                        message=f"Update {dir_name}",
                        files=["test_user.py", f"{dir_name}/handler.py"],
                    )
                )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_divergent_change(ctx)
        assert len(findings) == 0


# --- knowledge-silo tests ---


class TestKnowledgeSilo:
    def test_dominant_author(self):
        """One author with >= 80% of commits -> finding."""
        files = {"services/billing.py": "x = 1"}
        commits = _author_commits("services/billing.py", {"Alice": 8, "Bob": 2})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1
        assert "Alice" in findings[0].message
        assert "bus-factor" in findings[0].message

    def test_shared_ownership_no_finding(self):
        """Multiple authors with no dominant one -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _author_commits("app.py", {"Alice": 4, "Bob": 3, "Charlie": 3})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _author_commits("app.py", {"Alice": 4})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_billing.py": "x = 1"}
        commits = _author_commits("test_billing.py", {"Alice": 10})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_knowledge_silo(ctx) == []

    def test_dominance_exactly_80_percent(self):
        """Dominance at exactly 80% threshold -> finding."""
        files = {"app.py": "x = 1"}
        commits = _author_commits("app.py", {"Alice": 8, "Bob": 2})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1

    def test_dominance_below_threshold(self):
        """Dominance at 79% -> no finding."""
        files = {"app.py": "x = 1"}
        # 79/100 = 0.79, below 0.8
        commits = _author_commits("app.py", {"Alice": 79, "Bob": 21})
        ctx = make_ctx(files, commits=commits)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_skipped_for_solo_project(self):
        """1 author -> no findings (bus-factor is meaningless)."""
        files = {"services/billing.py": "x = 1"}
        commits = _author_commits("services/billing.py", {"Alice": 10})
        ctx = make_ctx(files, commits=commits, distinct_authors=1)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_skipped_for_two_authors(self):
        """2 authors -> no findings."""
        files = {"services/billing.py": "x = 1"}
        commits = _author_commits("services/billing.py", {"Alice": 8, "Bob": 2})
        ctx = make_ctx(files, commits=commits, distinct_authors=2)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 0

    def test_fires_with_three_plus_authors(self):
        """3 authors project-wide -> knowledge-silo can fire."""
        files = {"services/billing.py": "x = 1"}
        commits = _author_commits("services/billing.py", {"Alice": 8, "Bob": 2})
        ctx = make_ctx(files, commits=commits, distinct_authors=3)
        findings = check_knowledge_silo(ctx)
        assert len(findings) == 1
