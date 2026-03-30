"""Tests for CLI behavior."""

import subprocess

import pytest

from pysmelly.cli import main


class TestCLI:
    def test_clean_exit_code(self, tmp_path, capsys):
        """Exit 0 when no findings."""
        (tmp_path / "clean.py").write_text("x = 1\n")
        try:
            main(["--min-severity", "high", str(tmp_path)])
        except SystemExit as e:
            assert e.code == 0
        output = capsys.readouterr().out
        assert "All checks passed" in output

    def test_findings_exit_code(self, tmp_path):
        """Exit 1 when findings exist."""
        (tmp_path / "dead.py").write_text("def unused_func():\n    pass\n")
        try:
            main([str(tmp_path)])
        except SystemExit as e:
            assert e.code == 1

    def test_min_severity_filters(self, tmp_path, capsys):
        """--min-severity high filters out medium and low findings."""
        (tmp_path / "example.py").write_text("""\
def unused_func():
    pass

def used():
    pass

used()
used()
""")
        # With --min-severity high, should see dead-code but not internal-only
        try:
            main(["--no-context", "--min-severity", "high", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "dead-code" in output
        assert "internal-only" not in output

    def test_check_flag_runs_single_check(self, tmp_path, capsys):
        """--check runs only the specified check."""
        (tmp_path / "example.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", "--check", "dead-code", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "dead-code" in output

    def test_skip_flag_excludes_check(self, tmp_path, capsys):
        """--skip excludes the specified check."""
        (tmp_path / "example.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", "--skip", "dead-code", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "=== dead-code" not in output

    def test_list_checks(self, capsys):
        """--list-checks prints check names and exits cleanly."""
        try:
            main(["--list-checks"])
        except SystemExit as e:
            assert e.code == 0
        output = capsys.readouterr().out
        assert "dead-code" in output
        assert "high" in output

    def test_relative_paths_in_output(self, tmp_path, capsys):
        """Output uses paths relative to the target directory."""
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        # Should contain pkg/mod.py:1, not an absolute path
        assert "pkg/mod.py:" in output
        assert str(tmp_path) not in output

    def test_exclude_pattern(self, tmp_path, capsys):
        """--exclude filters out matching files."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("def another_unused():\n    pass\n")
        try:
            main(["--no-context", "--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 1 Python files" in output
        assert "test_app" not in output

    def test_exclude_directory_pattern(self, tmp_path, capsys):
        """--exclude with trailing / excludes entire directories."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("def unused_func():\n    pass\n")
        lam = tmp_path / "modules" / "auth" / "lambda"
        lam.mkdir(parents=True)
        (lam / "handler.py").write_text("def lambda_handler():\n    pass\n")
        try:
            main(["--no-context", "--exclude", "modules/*/lambda/", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 1 Python files" in output
        assert "handler" not in output

    def test_exclude_path_glob(self, tmp_path, capsys):
        """--exclude with / in pattern matches full relative path."""
        sub = tmp_path / "vendor" / "lib"
        sub.mkdir(parents=True)
        (sub / "mod.py").write_text("def vendored():\n    pass\n")
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", "--exclude", "vendor/lib/*.py", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 1 Python files" in output
        assert "vendored" not in output

    def test_multiple_targets(self, tmp_path, capsys):
        """Multiple target directories are analyzed together."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "mod_a.py").write_text("def unused_a():\n    pass\n")
        (dir_b / "mod_b.py").write_text("def unused_b():\n    pass\n")
        try:
            main(["--no-context", str(dir_a), str(dir_b)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 2 Python files" in output
        assert "mod_a" in output
        assert "mod_b" in output

    def test_inline_suppression(self, tmp_path, capsys):
        """# pysmelly: ignore suppresses findings."""
        (tmp_path / "suppressed.py").write_text(
            "def unused_func():  # pysmelly: ignore\n    pass\n"
        )
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "All checks passed" in output

    def test_inline_suppression_specific_check(self, tmp_path, capsys):
        """# pysmelly: ignore[check-name] suppresses only that check."""
        (tmp_path / "specific.py").write_text(
            "def unused_func():  # pysmelly: ignore[dead-code]\n    pass\n"
        )
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "=== dead-code" not in output

    def test_inline_suppression_line_above(self, tmp_path, capsys):
        """Suppression comment on the line above also works."""
        (tmp_path / "above.py").write_text(
            "# pysmelly: ignore[dead-code]\ndef unused_func():\n    pass\n"
        )
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "=== dead-code" not in output

    def test_invalid_directory(self, capsys):
        """Non-existent directory prints error and exits 1."""
        try:
            main(["/nonexistent/path"])
        except SystemExit as e:
            assert e.code == 1
        output = capsys.readouterr().err
        assert "not a directory" in output

    def test_exclude_test_no_per_finding_annotation(self, tmp_path, capsys):
        """--exclude test_* does not annotate individual findings (guidance banner suffices)."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("from app import unused_func\nunused_func()\n")
        try:
            main(["--no-context", "--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "dead-code" in output
        assert "[test files excluded]" not in output

    def test_context_on_by_default(self, tmp_path, capsys):
        """Guidance preamble is emitted by default."""
        (tmp_path / "app.py").write_text("x = 1\n")
        try:
            main(["--min-severity", "high", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "--- Guidance ---" in output
        assert "vestigial" in output

    def test_no_context_suppresses_guidance(self, tmp_path, capsys):
        """--no-context suppresses the guidance preamble."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "--- Guidance ---" not in output

    def test_context_with_test_exclude(self, tmp_path, capsys):
        """--exclude test_* includes test-specific guidance in preamble."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "speculative generality" in output

    def test_git_history_check_via_main_not_allowed(self, tmp_path, capsys):
        """abandoned-code is not a valid --check in main command."""
        (tmp_path / "app.py").write_text("x = 1\n")
        with pytest.raises(SystemExit):
            main(["--check", "abandoned-code", str(tmp_path)])

    def test_git_history_subcommand_without_git_repo_errors(self, tmp_path, capsys):
        """git-history subcommand without git repo -> error exit."""
        (tmp_path / "app.py").write_text("x = 1\n")
        with pytest.raises(SystemExit) as exc_info:
            main(["git-history", str(tmp_path)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "requires a git repository" in err

    def test_list_checks_shows_git_marker(self, capsys):
        """--list-checks shows [git] marker for git-history checks."""
        try:
            main(["--list-checks"])
        except SystemExit as e:
            assert e.code == 0
        output = capsys.readouterr().out
        assert "abandoned-code" in output
        assert "[git]" in output

    def test_git_history_subcommand_on_real_repo(self, git_repo, capsys):
        """git-history subcommand on a git repo runs without crash."""
        (git_repo / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "app.py"], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        try:
            main(
                [
                    "git-history",
                    "--no-context",
                    "--check",
                    "abandoned-code",
                    str(git_repo),
                ]
            )
        except SystemExit:
            pass
        # Should not crash — output is fine either way

    def test_git_history_checks_excluded_from_main(self, git_repo, capsys):
        """Main command excludes git-history checks."""
        (git_repo / "app.py").write_text("def unused_func():\n    pass\n")
        subprocess.run(["git", "add", "app.py"], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        try:
            main(["--no-context", str(git_repo)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "abandoned-code" not in output

    def test_reviewed_creates_commit(self, git_repo):
        """pysmelly git-history reviewed creates an empty commit with markers."""
        (git_repo / "old.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "old.py"], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        # Run from within the git repo
        import os

        prev = os.getcwd()
        try:
            os.chdir(git_repo)
            try:
                main(["git-history", "reviewed", "old.py"])
            except SystemExit as e:
                assert e.code == 0
        finally:
            os.chdir(prev)

        # Verify the commit was created
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "pysmelly: reviewed old.py" in result.stdout

    def test_reviewed_multiple_files(self, git_repo):
        """pysmelly git-history reviewed accepts multiple files."""
        (git_repo / "a.py").write_text("x = 1\n")
        (git_repo / "b.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        import os

        prev = os.getcwd()
        try:
            os.chdir(git_repo)
            try:
                main(["git-history", "reviewed", "a.py", "b.py"])
            except SystemExit as e:
                assert e.code == 0
        finally:
            os.chdir(prev)

        result = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "pysmelly: reviewed a.py" in result.stdout
        assert "pysmelly: reviewed b.py" in result.stdout

    def test_reviewed_no_args_errors(self, capsys):
        """pysmelly git-history reviewed with no files -> error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["git-history", "reviewed"])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "requires at least one file" in err

    def test_reviewed_nonexistent_file_errors(self, git_repo, capsys):
        """pysmelly git-history reviewed with nonexistent file -> error."""
        import os

        prev = os.getcwd()
        try:
            os.chdir(git_repo)
            with pytest.raises(SystemExit) as exc_info:
                main(["git-history", "reviewed", "nonexistent.py"])
            assert exc_info.value.code == 1
            err = capsys.readouterr().err
            assert "does not exist" in err
        finally:
            os.chdir(prev)
