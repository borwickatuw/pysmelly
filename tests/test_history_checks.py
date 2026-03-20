"""Tests for git history checks (abandoned-code)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import PropertyMock, patch

from pysmelly.checks.history import check_abandoned_code
from pysmelly.context import AnalysisContext
from pysmelly.git_history import GitHistory


def _make_ctx(
    files: dict[str, str],
    last_modified: dict[str, datetime],
) -> AnalysisContext:
    """Build an AnalysisContext with a mocked git_history."""
    all_trees = {Path(name): ast.parse(code) for name, code in files.items()}
    ctx = AnalysisContext(all_trees, verbose=False)

    # Build a minimal GitHistory mock via patching
    history = object.__new__(GitHistory)
    history.commits_for_file = {}
    history.last_modified = last_modified
    history._message_quality = 0.5
    history.commit_messages = "auto"

    ctx._git_history = history
    ctx._git_history_computed = True
    return ctx


_NOW = datetime.now(timezone.utc)
_RECENT = _NOW - timedelta(days=30)  # 1 month ago
_STALE = _NOW - timedelta(days=400)  # ~13 months ago


class TestAbandonedCode:
    def test_stale_file_among_active_peers(self):
        """One stale file in an active directory -> finding."""
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
            "pkg/old.py": _STALE,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 1
        assert findings[0].file == "pkg/old.py"
        assert "months ago" in findings[0].message

    def test_all_files_stale_no_finding(self):
        """All files stale -> no active peers -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
        }
        last_modified = {
            "pkg/a.py": _STALE,
            "pkg/b.py": _STALE,
            "pkg/c.py": _STALE,
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
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/old.py": _STALE,
        }
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
        last_modified = {
            "pkg/__init__.py": _STALE,
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
            "tests/conftest.py": _STALE,
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
            "pkg/settings.py": _STALE,
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0

    def test_file_not_in_git_skipped(self):
        """File not in git history (new/untracked) -> no finding."""
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/new.py": "w = 4",
        }
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
            # pkg/new.py not in last_modified
        }
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
            "pkg/old.py": _STALE,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert findings[0].line == 1

    def test_multiple_stale_files(self):
        """Multiple stale files in an active directory."""
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
            "pkg/old1.py": _STALE,
            "pkg/old2.py": _STALE,
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
        last_modified = {
            "active/a.py": _RECENT,
            "active/b.py": _RECENT,
            "active/c.py": _RECENT,
            "active/old.py": _STALE,
            "stale/a.py": _STALE,
            "stale/b.py": _STALE,
            "stale/c.py": _STALE,
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        # Only the active directory should produce a finding
        assert len(findings) == 1
        assert findings[0].file == "active/old.py"

    def test_reviewed_file_not_flagged(self):
        """A stale file with a recent 'pysmelly: reviewed' marker is not flagged.

        The reviewed marker updates last_modified in GitHistory, so by the
        time the check runs, the file appears recently touched.
        """
        files = {
            "pkg/a.py": "x = 1",
            "pkg/b.py": "y = 2",
            "pkg/c.py": "z = 3",
            "pkg/old.py": "w = 4",
        }
        # old.py was reviewed recently — last_modified reflects the review commit
        last_modified = {
            "pkg/a.py": _RECENT,
            "pkg/b.py": _RECENT,
            "pkg/c.py": _RECENT,
            "pkg/old.py": _RECENT,  # updated by reviewed marker
        }
        ctx = _make_ctx(files, last_modified)
        findings = check_abandoned_code(ctx)
        assert len(findings) == 0
