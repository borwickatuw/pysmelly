"""Tests for file discovery and the shared git invocation helper.

Character-set coverage: git quotes non-ASCII paths as octal escapes unless
`core.quotepath=off`, which made `git ls-files` name files that do not exist
and dropped them from analysis entirely.
"""

import subprocess

import pytest

from pysmelly import discovery
from pysmelly.cli import main
from pysmelly.discovery import get_python_files
from pysmelly.git_history import GitHistory

# Non-ASCII on purpose: filename, author and subject each exercise a different
# decoding path through `git`.
NON_ASCII_FILE = "café.py"
NON_ASCII_AUTHOR = "Tésté Üser"
NON_ASCII_MESSAGE = "feat: café module"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _commit_non_ascii_file(repo):
    (repo / NON_ASCII_FILE).write_text("def unused_func():\n    pass\n", encoding="utf-8")
    _git(repo, "config", "user.name", NON_ASCII_AUTHOR)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", NON_ASCII_MESSAGE)


class TestGetPythonFiles:
    def test_finds_non_ascii_filename_in_git_repo(self, git_repo):
        """A tracked file with a non-ASCII name is discovered, not silently skipped."""
        _commit_non_ascii_file(git_repo)

        files = get_python_files(git_repo)

        assert len(files) == 1
        assert files[0].exists()
        assert any(ord(char) > 127 for char in files[0].name)

    def test_finds_ascii_filename_in_git_repo(self, git_repo):
        (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "feat: add app")

        assert [f.name for f in get_python_files(git_repo)] == ["app.py"]

    def test_falls_back_to_rglob_outside_git(self, tmp_path):
        (tmp_path / NON_ASCII_FILE).write_text("x = 1\n", encoding="utf-8")

        assert len(get_python_files(tmp_path)) == 1


class TestGitHistoryDecoding:
    def test_non_ascii_path_author_and_subject_survive(self, git_repo):
        """Commit metadata round-trips as UTF-8 rather than octal escapes."""
        _commit_non_ascii_file(git_repo)

        history = GitHistory(git_repo, window="6m")

        assert len(history.commits_for_file) == 1
        tracked_path = next(iter(history.commits_for_file))
        assert any(ord(char) > 127 for char in tracked_path)
        commit = history.commits_for_file[tracked_path][0]
        assert commit.author == NON_ASCII_AUTHOR
        assert commit.message == NON_ASCII_MESSAGE


class TestOutsideGit:
    """Discovery walks the tree only when git cannot list it, and says so once."""

    @staticmethod
    def _cli(root, capsys) -> tuple[int, str]:
        (root / "app.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            main([str(root), "--no-context"])
        return exc.value.code, capsys.readouterr().err

    def test_a_tree_outside_git_is_walked_with_one_note(self, tmp_path, capsys):
        code, err = self._cli(tmp_path, capsys)
        note = (
            f"pysmelly: {tmp_path.resolve()} is not in a git repository; its files are found "
            "by walking the tree, so .gitignore does not apply and no finding carries a "
            "provenance line"
        )
        assert code == 0
        assert err.splitlines().count(note) == 1

    def test_without_git_installed_the_tree_is_walked_with_one_note(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(discovery.shutil, "which", lambda _: None)
        code, err = self._cli(tmp_path, capsys)
        assert code == 0
        assert [line for line in err.splitlines() if "walking the tree" in line] == [
            f"pysmelly: git is not installed; the files under {tmp_path.resolve()} are found "
            "by walking the tree, so .gitignore does not apply and no finding carries a "
            "provenance line"
        ]

    def test_a_repository_git_cannot_list_is_an_error(self, git_repo, capsys):
        """Walking it would read ignored files and drop provenance without a word."""
        (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        (git_repo / ".git" / "index").write_bytes(b"not an index")
        code, err = self._cli(git_repo, capsys)
        assert code == 1
        assert f"Error: git ls-files failed in {git_repo.resolve()}: " in err
        assert "index" in err.split("git ls-files failed", 1)[1]
        assert "walking the tree" not in err

    def test_a_repository_gets_no_note(self, git_repo, capsys):
        code, err = self._cli(git_repo, capsys)
        assert code == 0
        assert "walking the tree" not in err
