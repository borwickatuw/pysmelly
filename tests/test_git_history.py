"""Tests for git history parsing infrastructure."""

from unittest.mock import patch

import pytest

from pysmelly.git_history import GitHistory, _is_quality_message, _parse_window


class TestParseWindow:
    def test_months(self):
        assert _parse_window("6m") == "6 months ago"

    def test_days(self):
        assert _parse_window("90d") == "90 days ago"

    def test_years(self):
        assert _parse_window("1y") == "1 years ago"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid git-window format"):
            _parse_window("6weeks")

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Invalid git-window format"):
            _parse_window("")

    def test_no_number(self):
        with pytest.raises(ValueError, match="Invalid git-window format"):
            _parse_window("m")


class TestMessageQuality:
    def test_conventional_commit_passes(self):
        assert _is_quality_message("fix: resolve null pointer in auth module")

    def test_scoped_conventional_commit(self):
        assert _is_quality_message("feat(auth): add OAuth2 support")

    def test_short_message_fails(self):
        assert not _is_quality_message("fix stuff")

    def test_wip_fails(self):
        assert not _is_quality_message("wip")

    def test_just_update_fails(self):
        assert not _is_quality_message("update")

    def test_long_descriptive_message_passes(self):
        assert _is_quality_message("Add retry logic to HTTP client for transient failures")

    def test_empty_string_fails(self):
        assert not _is_quality_message("")


class TestGitHistoryParsing:
    def test_parse_well_formed_log(self, git_repo):
        """GitHistory parses real git log output."""
        # Create a file and commit it
        py_file = git_repo / "app.py"
        py_file.write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial commit")

        history = GitHistory(git_repo, window="6m")
        assert "app.py" in history.last_modified
        assert len(history.commits_for_file.get("app.py", [])) == 1

    def test_empty_repo(self, git_repo):
        """Empty repo produces empty history."""
        history = GitHistory(git_repo, window="6m")
        assert history.commits_for_file == {}
        assert history.last_modified == {}

    def test_multiple_files(self, git_repo):
        """Multiple files across commits are indexed correctly."""
        (git_repo / "a.py").write_text("x = 1\n")
        (git_repo / "b.py").write_text("y = 2\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "feat: add both files")

        (git_repo / "a.py").write_text("x = 2\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "fix: update a")

        history = GitHistory(git_repo, window="6m")
        assert len(history.commits_for_file.get("a.py", [])) == 2
        assert len(history.commits_for_file.get("b.py", [])) == 1

    def test_not_a_git_repo(self, tmp_path):
        """Non-git directory produces empty history."""
        history = GitHistory(tmp_path, window="6m")
        assert history.commits_for_file == {}
        assert history.last_modified == {}

    def test_invalid_window_produces_empty_history(self, git_repo):
        """Invalid window format produces empty history (not a crash)."""
        (git_repo / "a.py").write_text("x = 1\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "initial")

        history = GitHistory(git_repo, window="invalid")
        assert history.commits_for_file == {}


class TestMessageQualityProperty:
    def test_structured_override(self, git_repo):
        history = GitHistory(git_repo, window="6m", commit_messages="structured")
        assert history.message_quality == 1.0

    def test_unstructured_override(self, git_repo):
        history = GitHistory(git_repo, window="6m", commit_messages="unstructured")
        assert history.message_quality == 0.0

    def test_auto_with_no_commits(self, git_repo):
        history = GitHistory(git_repo, window="6m", commit_messages="auto")
        assert history.message_quality == 0.0

    def test_auto_with_conventional_commits(self, git_repo):
        (git_repo / "a.py").write_text("x = 1\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "feat: add initial module")

        (git_repo / "a.py").write_text("x = 2\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "fix: correct value assignment")

        history = GitHistory(git_repo, window="6m", commit_messages="auto")
        assert history.message_quality == 1.0

    def test_auto_with_low_quality_commits(self, git_repo):
        (git_repo / "a.py").write_text("x = 1\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "wip")

        (git_repo / "a.py").write_text("x = 2\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "stuff")

        history = GitHistory(git_repo, window="6m", commit_messages="auto")
        assert history.message_quality == 0.0


def _git(cwd, *args):
    """Run a git command in the given directory."""
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
