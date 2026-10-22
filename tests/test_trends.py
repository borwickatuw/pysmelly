"""Project trends: the threshold, the zero-baseline rule, and the history inputs."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.git_history import GitHistory, GitHistoryQueryError
from pysmelly.trends import HalfStats, WindowHalves, format_trends

_SPLIT = datetime(2026, 7, 21, tzinfo=UTC)
_WEEK_HALF = 7.0  # a half one week long, so commits per week == commits


def _halves(first: HalfStats, second: HalfStats) -> WindowHalves:
    return WindowHalves(split=_SPLIT, half_days=_WEEK_HALF, first=first, second=second)


def _stats(commits=4, file_touches=8, fixes=1, created=4, deleted=4) -> HalfStats:
    return HalfStats(
        commits=commits,
        file_touches=file_touches,
        fixes=fixes,
        created=created,
        deleted=deleted,
    )


class TestThreshold:
    def test_identical_halves_print_nothing(self):
        assert format_trends(_halves(_stats(), _stats())) == []

    @pytest.mark.parametrize(
        ("before", "after", "shown"),
        [(4, 5, "+25%"), (4, 3, "-25%"), (5, 4, None), (5, 6, None), (8, 12, "+50%")],
    )
    def test_commit_count_shown_from_a_quarter(self, before, after, shown):
        """Fix and file counts scale with the commits, so only commits move."""
        first = _stats(commits=before, file_touches=2 * before, fixes=before, created=0, deleted=0)
        second = _stats(commits=after, file_touches=2 * after, fixes=after, created=0, deleted=0)
        lines = format_trends(_halves(first, second))
        if shown is None:
            assert lines == []
        else:
            assert lines[1:] == [f"  commits per week: {before:.1f} -> {after:.1f} ({shown})"]

    def test_block_heading_names_the_split_and_says_informational(self):
        lines = format_trends(_halves(_stats(), _stats(commits=8, file_touches=16, fixes=2)))
        assert lines[0] == (
            "Project trends (window halves split at 2026-07-21; informational, not findings):"
        )

    def test_created_moves_alone_names_only_created(self):
        lines = format_trends(_halves(_stats(), _stats(created=8)))
        assert lines[1:] == ["  files created / deleted: 4 / 4 -> 8 / 4 (created +100%)"]

    def test_deleted_moves_alone_names_only_deleted(self):
        lines = format_trends(_halves(_stats(), _stats(deleted=1)))
        assert lines[1:] == ["  files created / deleted: 4 / 4 -> 4 / 1 (deleted -75%)"]


class TestZeroBaselines:
    def test_zero_to_zero_is_no_move(self):
        first = _stats(fixes=0, created=0, deleted=0)
        assert format_trends(_halves(first, first)) == []

    def test_zero_to_some_is_from_zero(self):
        first = _stats(fixes=0, created=0, deleted=0)
        second = _stats(fixes=1, created=0, deleted=2)
        assert format_trends(_halves(first, second))[1:] == [
            "  files created / deleted: 0 / 0 -> 0 / 2 (deleted from zero)",
            "  fix-commit ratio: 0% -> 25% (from zero)",
        ]

    def test_a_half_without_commits_has_no_ratios(self):
        """Files per commit and fix ratio are undefined, not zero, with no commits."""
        empty = HalfStats(commits=0, file_touches=0, fixes=0, created=0, deleted=0)
        lines = format_trends(_halves(empty, _stats(created=0, deleted=0)))
        assert lines[1:] == ["  commits per week: 0.0 -> 4.0 (from zero)"]


def _commit(repo: Path, message: str, when: datetime) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, capture_output=True, check=True
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


class TestHistoryInputs:
    def test_lifecycle_counts_creations_and_deletions_not_renames(self, git_repo):
        now = datetime.now(UTC)
        (git_repo / "a.py").write_text("x = 0\n" * 20)
        (git_repo / "b.py").write_text("y = 0\n")
        (git_repo / "notes.txt").write_text("not python\n")
        _git(git_repo, "add", "a.py", "b.py", "notes.txt")
        _commit(git_repo, "feat: start", now - timedelta(days=30))
        _git(git_repo, "mv", "a.py", "moved.py")
        _commit(git_repo, "refactor: move a", now - timedelta(days=20))
        _git(git_repo, "rm", "-q", "b.py")
        _commit(git_repo, "refactor: drop b", now - timedelta(days=10))

        history = GitHistory(git_repo, window="90d")
        by_message = {c.message: history.lifecycle_by_commit.get(c.hash) for c in history.commits}
        assert by_message == {
            "feat: start": (2, 0),
            "refactor: move a": None,
            "refactor: drop b": (0, 1),
        }

    def test_window_start_is_the_cutoff_git_applies(self, git_repo):
        before = datetime.now(UTC).replace(microsecond=0)
        start = GitHistory(git_repo, window="90d").window_start()
        after = datetime.now(UTC)
        assert before - timedelta(days=90) <= start <= after - timedelta(days=90)

    def test_window_start_rejects_a_malformed_window(self, git_repo):
        with pytest.raises(ValueError, match="Invalid git-window format"):
            GitHistory(git_repo, window="soon").window_start()


class TestGitFailuresAreErrors:
    """A failed query is an error, never an empty half or a missing block."""

    @pytest.fixture
    def history(self, git_repo):
        (git_repo / "a.py").write_text("x = 0\n")
        _git(git_repo, "add", "a.py")
        _commit(git_repo, "feat: start", datetime.now(UTC) - timedelta(days=10))
        return GitHistory(git_repo, window="90d")

    def _failing_git(self, monkeypatch):
        def fail(args, _cwd):
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="boom")

        monkeypatch.setattr("pysmelly.git_history.run_git", fail)

    def test_window_start_failure(self, history, monkeypatch):
        self._failing_git(monkeypatch)
        with pytest.raises(GitHistoryQueryError, match="cannot read the window start"):
            history.window_start()

    def test_window_start_unexpected_answer(self, history, monkeypatch):
        answer = subprocess.CompletedProcess(["git"], 0, stdout="1776702827\n", stderr="")
        monkeypatch.setattr("pysmelly.git_history.run_git", lambda _args, _cwd: answer)
        with pytest.raises(GitHistoryQueryError, match="not --max-age=<epoch>"):
            history.window_start()

    def test_lifecycle_failure(self, history, monkeypatch):
        self._failing_git(monkeypatch)
        with pytest.raises(GitHistoryQueryError, match="cannot list created and deleted"):
            _ = history.lifecycle_by_commit
