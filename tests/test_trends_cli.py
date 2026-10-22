"""The git-history `Project trends` block, end to end through the CLI.

Each test builds a real repository with dated commits inside a 90-day window,
so the halves split 45 days ago: commits 50-80 days old are the first half,
commits 5-40 days old the second. Only `pysmelly.cli.main` is imported, so the
block's absence is what fails where the block does not exist.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.cli import main

_NOW = datetime.now(UTC).replace(microsecond=0)
_BLOCK_HEAD = "Project trends (window halves split at "


def _days_ago(days: int) -> datetime:
    return _NOW - timedelta(days=days)


def _commit(repo: Path, message: str, when: datetime, *, allow_empty: bool = False) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    args = ["git", "commit", "-q", "-m", message, *(["--allow-empty"] if allow_empty else [])]
    subprocess.run(args, cwd=repo, env=env, capture_output=True, check=True)


def _write(repo: Path, names: list[str], line: str) -> None:
    for name in names:
        with (repo / name).open("a", encoding="utf-8") as fh:
            fh.write(line)
    subprocess.run(["git", "add", *names], cwd=repo, capture_output=True, check=True)


def _delete(repo: Path, name: str) -> None:
    subprocess.run(["git", "rm", "-q", name], cwd=repo, capture_output=True, check=True)


@pytest.fixture
def run(capsys):
    """pysmelly git-history over a 90-day window, returning stdout only."""

    def _run(repo: Path, *flags: str) -> str:
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--window", "90d", *flags, str(repo)])
        return capsys.readouterr().out

    return _run


def _block(output: str) -> list[str]:
    """The trends block's metric lines, or [] when the block is absent."""
    lines = output.splitlines()
    heads = [i for i, line in enumerate(lines) if line.startswith(_BLOCK_HEAD)]
    if not heads:
        return []
    assert len(heads) == 1
    body = []
    for line in lines[heads[0] + 1 :]:
        if not line:
            break
        body.append(line)
    return body


def _seed_outside_window(repo: Path, names: list[str]) -> None:
    """Create the files 200 days ago, so the window itself creates nothing."""
    _write(repo, names, "x = 0\n")
    _commit(repo, "feat: initial", _days_ago(200))


class TestBlockShown:
    @pytest.fixture
    def repo(self, git_repo: Path) -> Path:
        """Every metric moves.

        First half: four commits, each creating one file; no fixes.
        Second half: two commits touching three files each, one a fix, the
        other deleting a file.
        """
        for i in range(4):
            _write(git_repo, [f"a{i}.py"], "x = 0\n")
            _commit(git_repo, f"feat: add a{i}", _days_ago(80 - i))
        _write(git_repo, ["a0.py", "a1.py", "a2.py"], "y = 1\n")
        _commit(git_repo, "fix: repair a0", _days_ago(20))
        _delete(git_repo, "a3.py")
        _write(git_repo, ["a0.py", "a1.py"], "z = 2\n")
        _commit(git_repo, "refactor: drop a3", _days_ago(10))
        return git_repo

    def test_all_four_metrics_print(self, repo, run):
        split = _days_ago(45).date().isoformat()
        output = run(repo)
        assert f"{_BLOCK_HEAD}{split}; informational, not findings):" in output
        assert _block(output) == [
            "  files created / deleted: 4 / 0 -> 0 / 1 (created -100%, deleted from zero)",
            "  files per commit: 1.0 -> 3.0 (+200%)",
            "  fix-commit ratio: 0% -> 50% (from zero)",
            "  commits per week: 0.6 -> 0.3 (-50%)",
        ]

    def test_block_sits_under_the_window_header(self, repo, run):
        lines = run(repo).splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("Git history window:"))
        assert lines[header + 1] == ""
        assert lines[header + 2].startswith(_BLOCK_HEAD)

    def test_summary_omits_the_block_and_keeps_the_counts(self, repo, run):
        full = run(repo, "--more-please")
        summary = run(repo, "--summary")
        assert _block(full)
        assert "Project trends" not in summary
        sections = dict(re.findall(r"^=== (\S+) \((\d+) finding\(s\)\) ===$", full, re.M))
        rows = dict(re.findall(r"^  (\S+)\s+\[\w+\s*\]\s+(\d+)$", summary, re.M))
        assert rows == sections
        total = re.findall(r"^Total: \d+ finding\(s\)$|^All checks passed\.$", full, re.M)
        assert total == re.findall(
            r"^Total: \d+ finding\(s\)$|^All checks passed\.$", summary, re.M
        )

    def test_reviewed_markers_do_not_bear_on_the_block(self, repo, run):
        _commit(
            repo,
            "chore: acknowledge\n\npysmelly: reviewed a0.py",
            _days_ago(15),
            allow_empty=True,
        )
        assert _block(run(repo)) == _block(run(repo, "--ignore-reviewed"))
        assert _block(run(repo))

    def test_single_check_run_still_prints_the_block(self, repo, run):
        assert _block(run(repo, "--check", "abandoned-code"))


class TestBlockPartiallyShown:
    def test_only_the_fix_ratio_moves(self, git_repo, run):
        """Same commit count, files per commit and no creations; fixes double."""
        files = ["a.py", "b.py"]
        _seed_outside_window(git_repo, files)
        for i in range(4):
            _write(git_repo, files, f"f{i} = {i}\n")
            message = "fix: repair case" if i == 0 else f"feat: add case {i}"
            _commit(git_repo, message, _days_ago(80 - i))
        for i in range(4):
            _write(git_repo, files, f"s{i} = {i}\n")
            message = "fix: repair case" if i < 2 else f"feat: add case {i}"
            _commit(git_repo, message, _days_ago(30 - i))
        assert _block(run(git_repo)) == ["  fix-commit ratio: 25% -> 50% (+100%)"]


class TestBlockOmitted:
    def test_no_metric_moves_by_a_quarter(self, git_repo, run):
        """Five commits then four (-20%), two files each, no fixes or creations."""
        files = ["a.py", "b.py"]
        _seed_outside_window(git_repo, files)
        for i in range(5):
            _write(git_repo, files, f"f{i} = {i}\n")
            _commit(git_repo, f"feat: add case {i}", _days_ago(80 - i))
        for i in range(4):
            _write(git_repo, files, f"s{i} = {i}\n")
            _commit(git_repo, f"feat: add case {i}", _days_ago(30 - i))
        output = run(git_repo)
        assert "Project trends" not in output
        assert _block(output) == []

    def test_empty_window_prints_no_block(self, git_repo, run):
        _seed_outside_window(git_repo, ["a.py"])
        assert "Project trends" not in run(git_repo)


class TestZeroBaselines:
    def test_history_starting_mid_window(self, git_repo, run):
        """No first-half commits: counts move from zero, ratios are undefined."""
        _write(git_repo, ["a.py", "b.py"], "x = 0\n")
        _commit(git_repo, "feat: start", _days_ago(30))
        _write(git_repo, ["a.py"], "y = 1\n")
        _commit(git_repo, "fix: repair start", _days_ago(20))
        assert _block(run(git_repo)) == [
            "  files created / deleted: 0 / 0 -> 2 / 0 (created from zero)",
            "  commits per week: 0.0 -> 0.3 (from zero)",
        ]

    def test_history_ending_mid_window(self, git_repo, run):
        """No second-half commits: every count falls to zero, -100%."""
        _write(git_repo, ["a.py"], "x = 0\n")
        _commit(git_repo, "feat: start", _days_ago(70))
        assert _block(run(git_repo)) == [
            "  files created / deleted: 1 / 0 -> 0 / 0 (created -100%)",
            "  commits per week: 0.2 -> 0.0 (-100%)",
        ]


def test_a_failed_trends_query_is_an_error(git_repo, capsys, monkeypatch):
    _write(git_repo, ["a.py"], "x = 0\n")
    _commit(git_repo, "feat: start", _days_ago(10))

    def fail(_self):
        from pysmelly.git_history import GitHistoryQueryError

        raise GitHistoryQueryError("cannot read the window start (test)")

    monkeypatch.setattr("pysmelly.git_history.GitHistory.window_start", fail)
    with pytest.raises(SystemExit) as exit_info:
        main(["git-history", "--no-context", "--window", "90d", str(git_repo)])
    assert exit_info.value.code == 1
    assert "Error: cannot read the window start (test)" in capsys.readouterr().err
