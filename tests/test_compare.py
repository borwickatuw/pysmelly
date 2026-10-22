"""Tests for `--compare REF`: the same checks at another ref, and the delta."""

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest

from pysmelly import cli, compare
from pysmelly.cli import main
from pysmelly.compare import Analysis, match_findings, ref_worktree
from pysmelly.discovery import GitRefError
from pysmelly.registry import Finding, Severity

APP = """\
def used():
    return 1


def unused_helper():
    return 2


print(used())
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _commit_app(repo: Path) -> None:
    (repo / "app.py").write_text(APP)
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "init")


def _worktrees(repo: Path) -> list[str]:
    return [
        line
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


@pytest.fixture
def repo(git_repo):
    _commit_app(git_repo)
    return git_repo


@pytest.fixture
def made_dirs(monkeypatch):
    """Record every temporary directory --compare creates."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(Path(path))
        return path

    monkeypatch.setattr(compare.tempfile, "mkdtemp", recording)
    return made


class TestCompareDeltas:
    def test_improved_names_the_resolved_finding(self, repo, capsys, made_dirs):
        (repo / "app.py").write_text(APP.replace("def unused_helper():\n    return 2\n", ""))
        code, out, _ = _run(
            ["--no-context", "--check", "dead-code", "--compare", "HEAD", str(repo)], capsys
        )
        assert code == 0
        assert "1 finding(s) then, 0 now (-1)" in out
        assert "dead-code                      1 -> 0  (-1)" in out
        assert "app.py: 0 finding(s), down from 1; fixed: dead-code x1" in out
        assert "=== resolved since HEAD (1) ===" in out
        assert "app.py: dead-code in unused_helper (was line 5):" in out
        assert "=== new since" not in out
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]
        assert made_dirs and not any(d.exists() for d in made_dirs)

    def test_worsened_names_the_new_finding_and_exits_1(self, repo, capsys, made_dirs):
        (repo / "app.py").write_text(APP + "\n\ndef another_unused():\n    return 3\n")
        code, out, _ = _run(
            ["--no-context", "--check", "dead-code", "--compare", "HEAD", str(repo)], capsys
        )
        assert code == 1
        assert "1 finding(s) then, 2 now (+1)" in out
        assert "app.py: 2 finding(s), up from 1" in out
        assert "=== new since HEAD (1) ===" in out
        assert ": dead-code in another_unused:" in out
        assert "=== resolved since" not in out
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]
        assert not any(d.exists() for d in made_dirs)

    def test_unchanged_when_only_lines_moved(self, repo, capsys):
        """Findings match on (check, file, definition): moving a finding's line is no change."""
        (repo / "app.py").write_text('"""Docstring."""\n\nimport os\n\nprint(os.sep)\n\n\n' + APP)
        code, out, _ = _run(
            ["--no-context", "--check", "dead-code", "--compare", "HEAD", str(repo)], capsys
        )
        assert code == 0
        assert "No change against HEAD (" in out
        assert "1 finding(s) at both." in out
        assert "=== per check ===" not in out

    def test_summary_keeps_counts_and_drops_the_lists(self, repo, capsys):
        (repo / "app.py").write_text(APP.replace("unused_helper", "renamed_unused"))
        code, out, _ = _run(
            ["--no-context", "--check", "dead-code", "--summary", "--compare", "HEAD", str(repo)],
            capsys,
        )
        assert code == 1
        assert "Resolved: 1, new: 1" in out
        assert "=== resolved" not in out
        assert "=== new" not in out

    def test_compares_a_subdirectory_target_at_ref(self, git_repo, capsys):
        pkg = git_repo / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text(APP)
        _git(git_repo, "add", "pkg/mod.py")
        _git(git_repo, "commit", "-qm", "init")
        (pkg / "mod.py").write_text(APP.replace("def unused_helper():\n    return 2\n", ""))
        code, out, _ = _run(
            ["--no-context", "--check", "dead-code", "--compare", "HEAD", str(pkg)], capsys
        )
        assert code == 0
        assert "mod.py: dead-code in unused_helper (was line 5):" in out


class TestCompareFailsFast:
    def test_bad_ref(self, repo, capsys, made_dirs):
        code, out, err = _run(["--compare", "no-such-ref", str(repo)], capsys)
        assert code == 1
        assert "cannot compare against 'no-such-ref'" in err
        assert out == ""
        assert made_dirs == []
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]

    def test_outside_a_git_repository(self, tmp_path, capsys):
        (tmp_path / "app.py").write_text(APP)
        code, _, err = _run(["--compare", "HEAD", str(tmp_path)], capsys)
        assert code == 1
        assert "--compare requires a git repository" in err

    def test_combined_with_diff(self, repo, capsys):
        code, _, err = _run(["--compare", "HEAD", "--diff", "HEAD", str(repo)], capsys)
        assert code == 1
        assert "--compare and --diff cannot be combined" in err

    def test_target_missing_at_ref_cleans_up(self, repo, capsys, made_dirs):
        (repo / "later").mkdir()
        (repo / "later" / "new.py").write_text("x = 1\n")
        code, _, err = _run(["--compare", "HEAD", str(repo / "later")], capsys)
        assert code == 1
        assert "later does not exist there" in err
        assert made_dirs and not any(d.exists() for d in made_dirs)
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]


class TestCompareCleanup:
    def test_worktree_removed_when_analysis_raises(self, repo, monkeypatch, made_dirs):
        def boom(*_args, **_kwargs):
            raise RuntimeError("check crashed")

        monkeypatch.setattr(cli, "_run_checks_and_filter", boom)
        with pytest.raises(RuntimeError, match="check crashed"):
            main(["--compare", "HEAD", str(repo)])
        assert made_dirs and not any(d.exists() for d in made_dirs)
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]

    def test_failed_checkout_removes_temp_dir(self, repo, made_dirs):
        with (
            pytest.raises(GitRefError, match="git worktree add failed"),
            ref_worktree(repo, "0" * 40),
        ):
            pass
        assert made_dirs and not any(d.exists() for d in made_dirs)
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]

    def test_failed_removal_is_an_error_not_silence(self, repo, made_dirs):
        sha = _git(repo, "rev-parse", "HEAD").strip()
        with (
            pytest.raises(GitRefError, match="git worktree remove failed"),
            ref_worktree(repo, sha) as tree,
        ):
            # Removing the registration out from under it makes git refuse.
            _git(repo, "worktree", "remove", "--force", str(tree))
        assert not any(d.exists() for d in made_dirs)

    def test_temp_dir_removed_when_worktree_removal_fails(self, repo, monkeypatch, made_dirs):
        real = compare.git_worktree

        def refuse_remove(args, cwd):
            if args[0] == "remove":
                raise GitRefError("git worktree remove failed: refused")
            return real(args, cwd)

        monkeypatch.setattr(compare, "git_worktree", refuse_remove)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        with pytest.raises(GitRefError, match="remove failed"), ref_worktree(repo, sha) as tree:
            assert tree.is_dir()
        assert made_dirs and not any(d.exists() for d in made_dirs)

    def test_interrupt_after_add_registers_still_removes_the_worktree(
        self, repo, monkeypatch, made_dirs
    ):
        real = compare.git_worktree

        def interrupted_add(args, cwd):
            out = real(args, cwd)
            if args[0] == "add":
                # Ctrl-C landing after git registered the worktree, before
                # the context manager learned that it had.
                raise KeyboardInterrupt
            return out

        monkeypatch.setattr(compare, "git_worktree", interrupted_add)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        with pytest.raises(KeyboardInterrupt), ref_worktree(repo, sha):
            pass
        assert made_dirs and not any(d.exists() for d in made_dirs)
        assert _worktrees(repo) == [f"worktree {repo.resolve()}"]


def _finding(line: int, message: str = "m", check: str = "dead-code") -> Finding:
    return Finding("a.py", line, check, message, Severity.HIGH)


class TestMatchFindings:
    SOURCE = """\
X = 1


class Box:
    @property
    def size(self):
        def inner():
            return 1

        return inner()
"""

    def _analysis(self, findings: list[Finding]) -> Analysis:
        return Analysis({Path("a.py"): ast.parse(self.SOURCE)}, findings)

    def test_enclosing_definition_is_innermost_and_includes_decorators(self):
        resolved, _ = match_findings(
            self._analysis([_finding(1), _finding(4), _finding(5), _finding(8)]),
            self._analysis([]),
        )
        assert [r.definition for r in resolved] == [
            "<module>",
            "Box",
            "Box.size",
            "Box.size.inner",
        ]

    def test_same_key_prefers_reporting_the_message_that_disappeared(self):
        resolved, new = match_findings(
            self._analysis([_finding(7, "kept"), _finding(8, "gone")]),
            self._analysis([_finding(8, "kept")]),
        )
        assert [r.finding.message for r in resolved] == ["gone"]
        assert new == []

    def test_changed_message_under_same_key_is_not_a_delta(self):
        resolved, new = match_findings(
            self._analysis([_finding(7, "3 callers")]),
            self._analysis([_finding(7, "4 callers")]),
        )
        assert resolved == [] and new == []

    def test_different_check_same_definition_is_a_delta(self):
        resolved, new = match_findings(
            self._analysis([_finding(7)]),
            self._analysis([_finding(7, check="unused-defaults")]),
        )
        assert [r.finding.check for r in resolved] == ["dead-code"]
        assert [n.finding.check for n in new] == ["unused-defaults"]
