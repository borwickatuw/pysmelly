"""Tests for git history parsing infrastructure."""

from unittest.mock import patch

import pytest

from pysmelly.git_history import (
    GitHistory,
    TimeSlice,
    _is_quality_message,
    _parse_window,
    _window_to_days,
    classify_commit,
)


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


class TestReviewedMarkers:
    def test_reviewed_updates_last_modified(self, git_repo):
        """'pysmelly: reviewed' in a commit message updates last_modified."""
        (git_repo / "old.py").write_text("x = 1\n")
        _git(git_repo, "add", "old.py")
        _git(git_repo, "commit", "-m", "initial")

        # Empty commit with reviewed marker
        _git(
            git_repo,
            "commit",
            "--allow-empty",
            "-m",
            "Review stale files\n\npysmelly: reviewed old.py",
        )

        history = GitHistory(git_repo, window="6m")
        # old.py should have last_modified from the review commit, not just the initial
        assert "old.py" in history.last_modified
        assert "old.py" in history.reviewed_at

    def test_reviewed_in_regular_commit(self, git_repo):
        """Review marker works in a commit that also touches files."""
        (git_repo / "old.py").write_text("x = 1\n")
        (git_repo / "new.py").write_text("y = 2\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "initial")

        (git_repo / "new.py").write_text("y = 3\n")
        _git(git_repo, "add", "new.py")
        _git(
            git_repo,
            "commit",
            "-m",
            "Update new.py\n\npysmelly: reviewed old.py",
        )

        history = GitHistory(git_repo, window="6m")
        assert "old.py" in history.reviewed_at

    def test_multiple_reviewed_in_one_commit(self, git_repo):
        """Multiple reviewed markers in a single commit."""
        (git_repo / "a.py").write_text("x = 1\n")
        (git_repo / "b.py").write_text("y = 2\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "initial")

        _git(
            git_repo,
            "commit",
            "--allow-empty",
            "-m",
            "Review stale files\n\npysmelly: reviewed a.py\npysmelly: reviewed b.py",
        )

        history = GitHistory(git_repo, window="6m")
        assert "a.py" in history.reviewed_at
        assert "b.py" in history.reviewed_at

    def test_no_reviewed_markers(self, git_repo):
        """Normal commits produce empty reviewed_at."""
        (git_repo / "a.py").write_text("x = 1\n")
        _git(git_repo, "add", "a.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        assert history.reviewed_at == {}


class TestFileStats:
    def test_numstat_basic(self, git_repo):
        """file_stats reports insertions and deletions."""
        (git_repo / "app.py").write_text("line1\nline2\nline3\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        (git_repo / "app.py").write_text("line1\nline2\nline3\nline4\nline5\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: add lines")

        history = GitHistory(git_repo, window="6m")
        stats = history.file_stats
        assert "app.py" in stats
        assert stats["app.py"].total_insertions > 0
        assert stats["app.py"].commit_count == 2

    def test_numstat_lazy(self, git_repo):
        """file_stats is not parsed until accessed."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "initial")

        history = GitHistory(git_repo, window="6m")
        assert history._numstat_parsed is False
        _ = history.file_stats
        assert history._numstat_parsed is True

    def test_numstat_empty_repo(self, git_repo):
        """Empty repo produces empty file_stats."""
        history = GitHistory(git_repo, window="6m")
        assert history.file_stats == {}

    def test_numstat_deletions(self, git_repo):
        """file_stats tracks deletions."""
        (git_repo / "app.py").write_text("a\nb\nc\nd\ne\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "initial")

        (git_repo / "app.py").write_text("a\nb\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "remove lines")

        history = GitHistory(git_repo, window="6m")
        stats = history.file_stats["app.py"]
        assert stats.total_deletions > 0


class TestClassifyCommit:
    def test_fix_prefix(self):
        assert "fix" in classify_commit("fix: resolve null pointer in auth")

    def test_fix_scoped(self):
        assert "fix" in classify_commit("fix(auth): resolve null pointer")

    def test_feat_prefix(self):
        assert "feature" in classify_commit("feat: add OAuth2 support")

    def test_refactor_prefix(self):
        assert "refactor" in classify_commit("refactor: simplify auth module")

    def test_fix_keyword(self):
        assert "fix" in classify_commit("Resolve bug in payment processing")

    def test_feature_keyword(self):
        assert "feature" in classify_commit("Add retry logic to HTTP client")

    def test_refactor_keyword(self):
        assert "refactor" in classify_commit("Simplify the validation pipeline")

    def test_debt_keyword_workaround(self):
        assert "debt" in classify_commit("Add temporary workaround for rate limiting")

    def test_debt_keyword_hack(self):
        assert "debt" in classify_commit("Quick hack to unblock deploy")

    def test_debt_keyword_todo(self):
        assert "debt" in classify_commit("TODO: clean up after migration")

    def test_unclassified(self):
        assert classify_commit("Update README") == set()

    def test_multiple_categories(self):
        cats = classify_commit("fix: add workaround for auth bug")
        assert "fix" in cats
        assert "debt" in cats

    def test_empty_message(self):
        assert classify_commit("") == set()

    def test_emergency_hotfix(self):
        assert "emergency" in classify_commit("hotfix: patch auth crash")

    def test_emergency_revert(self):
        assert "emergency" in classify_commit("Revert 'Add new billing flow'")

    def test_emergency_rollback(self):
        assert "emergency" in classify_commit("rollback deploy to v2.3")

    def test_emergency_cherry_pick(self):
        assert "emergency" in classify_commit("cherry-pick fix from main")

    def test_emergency_urgent(self):
        assert "emergency" in classify_commit("urgent: fix production outage")

    def test_classify_emoji_bug(self):
        assert "fix" in classify_commit("\U0001f41b Fix something")

    def test_classify_emoji_feature(self):
        assert "feature" in classify_commit("\u2728 Add something")

    def test_classify_emoji_refactor(self):
        assert "refactor" in classify_commit("\u267b\ufe0f Simplify logic")

    def test_classify_emoji_emergency(self):
        assert "emergency" in classify_commit("\U0001f525 Remove broken code")

    def test_classify_emoji_combined(self):
        """Emoji + keyword both detected."""
        cats = classify_commit("\U0001f41b Fix a bug")
        assert "fix" in cats

    def test_quality_message_emoji(self):
        """Emoji-prefixed message counts as quality."""
        assert _is_quality_message("\u2728 Add OAuth2 support")


class TestWindowToDays:
    def test_days(self):
        assert _window_to_days("90d") == 90

    def test_months(self):
        assert _window_to_days("6m") == 180

    def test_years(self):
        assert _window_to_days("1y") == 365

    def test_invalid_fallback(self):
        assert _window_to_days("invalid") == 180


class TestAuthorParsing:
    def test_author_in_commit_info(self, git_repo):
        """Author is parsed from git log output."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        assert len(history._commits) == 1
        assert history._commits[0].author == "Test"

    def test_authors_for_file_populated(self, git_repo):
        """authors_for_file index is built from commits."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        (git_repo / "app.py").write_text("x = 2\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "fix: update")

        history = GitHistory(git_repo, window="6m")
        assert "app.py" in history.authors_for_file
        assert history.authors_for_file["app.py"]["Test"] == 2


class TestTimeSlices:
    def test_time_slices_created(self, git_repo):
        """Time slices are created from commits."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        assert len(history.time_slices) >= 1
        assert history.time_slices[0].commits  # at least one commit in a slice

    def test_empty_repo_no_slices(self, git_repo):
        """Empty repo produces no time slices."""
        history = GitHistory(git_repo, window="6m")
        assert history.time_slices == []

    def test_commits_per_slice(self, git_repo):
        """commits_per_slice is computed."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        assert history.commits_per_slice >= 1.0

    def test_is_coarse_grained_with_few_commits(self, git_repo):
        """Single commit produces coarse-grained assessment."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        # With 1 commit in 1 slice, commits_per_slice = 1.0 < 2.0
        assert history.is_coarse_grained

    def test_files_touched_populated(self, git_repo):
        """Time slice files_touched includes committed files."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: initial")

        history = GitHistory(git_repo, window="6m")
        all_touched = set()
        for ts in history.time_slices:
            all_touched |= ts.files_touched
        assert "app.py" in all_touched

    def test_files_by_category_populated(self, git_repo):
        """Time slice files_by_category includes categorized files."""
        (git_repo / "app.py").write_text("x = 1\n")
        _git(git_repo, "add", "app.py")
        _git(git_repo, "commit", "-m", "feat: add module")

        history = GitHistory(git_repo, window="6m")
        found_feature = False
        for ts in history.time_slices:
            if "feature" in ts.files_by_category and "app.py" in ts.files_by_category["feature"]:
                found_feature = True
        assert found_feature


def _git(cwd, *args):
    """Run a git command in the given directory."""
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
