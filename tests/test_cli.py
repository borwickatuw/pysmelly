"""Tests for CLI behavior."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from pysmelly import __version__
from pysmelly.cli import GUIDANCE_CONTENT, GUIDANCE_END_MARKER, SHORT_GUIDANCE_CONTENT, main


class TestVersion:
    def test_version_matches_package_metadata(self, capsys, monkeypatch, tmp_path):
        """--version reports the package version, independent of the working directory.

        Regression: the version was previously resolved via `git describe` in the
        current working directory, so running pysmelly against another repo reported
        that repo's commit instead of pysmelly's own version.
        """
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out == f"pysmelly, version {__version__}\n"

    def test_version_does_not_shell_out_to_git(self, capsys, monkeypatch):
        """--version must not invoke git; the version comes from package metadata."""

        def _fail(*_args, **_kwargs):
            raise AssertionError("--version must not call subprocess/git")

        monkeypatch.setattr(subprocess, "run", _fail)
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


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

    def test_context_shows_init_hint_when_no_pysmelly_md(self, tmp_path, capsys, monkeypatch):
        """Guidance preamble suggests pysmelly init when PYSMELLY.md is missing."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "--- Guidance ---" in output
        assert "pysmelly init" in output

    def test_no_context_suppresses_guidance(self, tmp_path, capsys):
        """--no-context suppresses the guidance preamble."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "--- Guidance ---" not in output

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
        """git-history subcommand exits clean on a repo with nothing to flag."""
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
            code = 0
        except SystemExit as e:
            code = e.code
        assert code == 0
        out = capsys.readouterr().out
        assert "All checks passed." in out
        assert "Git history window: 6m (git --since '6 months ago', as of " in out

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


class TestCheckSelectionIsRepeatable:
    """``--check`` unions its selections instead of replacing them.

    The regression this pins: a second ``--check`` silently discarded the
    first, so a combined invocation reported only the last check's findings
    and nothing said so. A review that asked for two checks at once got a
    quiet under-report.
    """

    SOURCE = 'UNUSED_CONSTANT = "nobody references me"\n\n\ndef unused_func():\n    pass\n'

    def _run(self, tmp_path, capsys, args):
        (tmp_path / "example.py").write_text(self.SOURCE)
        try:
            main(["--no-context", "--more-please", *args, str(tmp_path)])
        except SystemExit:
            pass
        return capsys.readouterr().out

    def test_one_check_runs_only_that_check(self, tmp_path, capsys):
        output = self._run(tmp_path, capsys, ["--check", "dead-code"])
        assert "=== dead-code" in output
        assert "=== dead-constants" not in output

    def test_two_checks_run_both(self, tmp_path, capsys):
        """Both selections survive: the second no longer replaces the first."""
        output = self._run(tmp_path, capsys, ["--check", "dead-code", "--check", "dead-constants"])
        assert "=== dead-code" in output
        assert "=== dead-constants" in output
        assert "Total: 2 finding(s)" in output

    def test_repeated_name_is_not_counted_twice(self, tmp_path, capsys):
        """The selection is a union, so naming a check twice runs it once."""
        output = self._run(tmp_path, capsys, ["--check", "dead-code", "--check", "dead-code"])
        assert "=== dead-code (1 finding(s)) ===" in output

    def test_unknown_check_name_still_fails_fast(self, tmp_path, capsys):
        """An unknown name in a repeated selection is still rejected."""
        (tmp_path / "example.py").write_text(self.SOURCE)
        with pytest.raises(SystemExit) as exc:
            main(["--check", "dead-code", "--check", "no-such-check", str(tmp_path)])
        assert exc.value.code == 2
        assert "no-such-check" in capsys.readouterr().err

    def test_git_history_check_is_repeatable_too(self, git_repo, capsys):
        """The history subcommand takes a union as well."""
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
                    "--check",
                    "knowledge-silo",
                    str(git_repo),
                ]
            )
            code = 0
        except SystemExit as e:
            code = e.code
        assert code == 0
        assert "All checks passed." in capsys.readouterr().out

    def test_git_history_unknown_check_name_still_fails_fast(self, git_repo, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["git-history", "--check", "abandoned-code", "--check", "no-such", str(git_repo)])
        assert exc.value.code == 2
        assert "no-such" in capsys.readouterr().err


class TestInit:
    @pytest.fixture(autouse=True)
    def _isolate_cwd(self, tmp_path, monkeypatch):
        """`init` writes CLAUDE.md relative to the cwd — keep that inside tmp_path.

        Without this, a test passing an absolute target appended a reference to
        pysmelly's own CLAUDE.md.
        """
        monkeypatch.chdir(tmp_path)

    def test_init_writes_full_guidance(self, tmp_path, capsys):
        """pysmelly init writes full guidance by default."""
        target = tmp_path / "PYSMELLY.md"
        try:
            main(["init", str(target)])
        except SystemExit:
            pass
        content = target.read_text()
        assert "pysmelly-guidance" in content
        assert "Code Smell Review Guide" in content
        assert "How to act on AST findings" in content
        output = capsys.readouterr().out
        assert f"Wrote {target}" in output

    def test_init_short_writes_short_guidance(self, tmp_path, capsys):
        """pysmelly init --short writes the short variant."""
        target = tmp_path / "PYSMELLY.md"
        try:
            main(["init", "--short", str(target)])
        except SystemExit:
            pass
        content = target.read_text()
        assert "pysmelly-guidance" in content
        assert "(Short)" in content
        assert "How to act on AST findings" not in content

    def test_init_short_default_path(self, tmp_path, capsys):
        """pysmelly init --short without path writes to PYSMELLY.md."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            try:
                main(["init", "--short"])
            except SystemExit:
                pass
            content = (tmp_path / "PYSMELLY.md").read_text()
            assert "(Short)" in content
        finally:
            os.chdir(prev)

    def test_init_creates_claude_md_reference(self, tmp_path, capsys):
        """pysmelly init creates CLAUDE.md with pysmelly reference."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            try:
                main(["init"])
            except SystemExit:
                pass
            claude_md = (tmp_path / "CLAUDE.md").read_text()
            assert "pysmelly" in claude_md
            assert "PYSMELLY.md" in claude_md
        finally:
            os.chdir(prev)

    def test_init_adds_reference_when_claude_md_merely_mentions_pysmelly(self, tmp_path, capsys):
        """A CLAUDE.md that talks about pysmelly still gets a guidance reference."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            (tmp_path / "CLAUDE.md").write_text(
                "# proj\n\n`make pysmelly` runs the pysmelly design-smell scan.\n"
            )
            (tmp_path / "docs").mkdir()
            try:
                main(["init", "--short", "docs/PYSMELLY.md"])
            except SystemExit:
                pass
            claude_md = (tmp_path / "CLAUDE.md").read_text()
            assert "Read [docs/PYSMELLY.md](docs/PYSMELLY.md) before running pysmelly" in claude_md
        finally:
            os.chdir(prev)

    def test_init_idempotent_claude_md(self, tmp_path, capsys):
        """Running init twice does not duplicate the CLAUDE.md reference."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            try:
                main(["init"])
            except SystemExit:
                pass
            try:
                main(["init"])
            except SystemExit:
                pass
            claude_md = (tmp_path / "CLAUDE.md").read_text()
            assert claude_md.count("## pysmelly") == 1
        finally:
            os.chdir(prev)


class TestGuidanceStaleness:
    def test_full_guidance_recognized_as_current(self, tmp_path, capsys):
        """Full PYSMELLY.md is not flagged as outdated."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            try:
                main(["init"])
            except SystemExit:
                pass
            (tmp_path / "app.py").write_text("x = 1\n")
            try:
                main(["--min-severity", "high", str(tmp_path)])
            except SystemExit:
                pass
            output = capsys.readouterr().out
            assert "outdated" not in output
        finally:
            os.chdir(prev)

    def test_short_guidance_recognized_as_current(self, tmp_path, capsys):
        """Short PYSMELLY.md is not flagged as outdated."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            try:
                main(["init", "--short"])
            except SystemExit:
                pass
            (tmp_path / "app.py").write_text("x = 1\n")
            try:
                main(["--min-severity", "high", str(tmp_path)])
            except SystemExit:
                pass
            output = capsys.readouterr().out
            assert "outdated" not in output
        finally:
            os.chdir(prev)

    def test_custom_path_recognized_via_claude_md(self, tmp_path, capsys):
        """Guidance at a custom path is found via CLAUDE.md reference."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            docs = tmp_path / "docs"
            docs.mkdir()
            try:
                main(["init", "docs/PYSMELLY.md"])
            except SystemExit:
                pass
            # PYSMELLY.md should NOT exist at root
            assert not (tmp_path / "PYSMELLY.md").exists()
            (tmp_path / "app.py").write_text("x = 1\n")
            try:
                main(["--min-severity", "high", str(tmp_path)])
            except SystemExit:
                pass
            output = capsys.readouterr().out
            assert "outdated" not in output
            assert "pysmelly init" not in output
        finally:
            os.chdir(prev)

    def test_tampered_guidance_flagged_as_outdated(self, tmp_path, capsys):
        """Modified PYSMELLY.md is flagged as outdated."""
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)
            (tmp_path / "PYSMELLY.md").write_text("<!-- pysmelly-guidance badhash -->\nold")
            (tmp_path / "app.py").write_text("x = 1\n")
            try:
                main(["--min-severity", "high", str(tmp_path)])
            except SystemExit:
                pass
            output = capsys.readouterr().out
            assert "outdated" in output
        finally:
            os.chdir(prev)


# A guidance file the way the fleet keeps it (PYSMELLY-REVIEW Practice 8a): the
# generated block, then the repo's own adjudication register. s3-bagit's was
# 312 lines when `pysmelly init --short` cut it to 35 (2026-09-18 review C9).
REGISTER = "".join(
    f"| `check-{i}` | `src/mod_{i}.py` `fn_{i}` | standing — trailing space kept \n"
    if i % 7 == 0
    else f"| `check-{i}` | `src/mod_{i}.py` `fn_{i}` | standing — reason {i} |\n"
    for i in range(300)
)
REGISTER_TAIL = (
    "\n\n______________________________________________________________________\n\n"
    "## Standing adjudication for this repo\n\n" + REGISTER + "\nno trailing newline at EOF"
)


def _guidance_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def _pre_marker_file(content: str) -> str:
    """A guidance file as `pysmelly init` wrote it before the end marker existed."""
    return f"<!-- pysmelly-guidance {_guidance_hash(content)} -->\n{content}"


class TestInitPreservesExtendedFile:
    """`init` replaces only its generated block and refuses what it cannot bound."""

    @pytest.fixture(autouse=True)
    def _isolate_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def _init(self, *args: str) -> int:
        with pytest.raises(SystemExit) as exc:
            main(["init", *args])
        return exc.value.code or 0

    def test_fresh_write_brackets_the_template_with_both_markers(self, tmp_path):
        assert self._init("--short", "docs/PYSMELLY.md") == 0
        text = (tmp_path / "docs/PYSMELLY.md").read_text(encoding="utf-8")
        assert text == (
            f"<!-- pysmelly-guidance {_guidance_hash(SHORT_GUIDANCE_CONTENT)} -->\n\n"
            f"{SHORT_GUIDANCE_CONTENT}\n{GUIDANCE_END_MARKER}\n"
        )

    def test_rerun_on_a_current_file_is_byte_identical(self, tmp_path):
        self._init()
        first = (tmp_path / "PYSMELLY.md").read_bytes()
        self._init()
        assert (tmp_path / "PYSMELLY.md").read_bytes() == first

    @pytest.mark.parametrize(
        ("first", "second", "expected"),
        [
            (["--short"], ["--short"], SHORT_GUIDANCE_CONTENT),
            ([], [], GUIDANCE_CONTENT),
            (["--short"], [], GUIDANCE_CONTENT),
            ([], ["--short"], SHORT_GUIDANCE_CONTENT),
        ],
        ids=["short-short", "full-full", "short-to-full", "full-to-short"],
    )
    def test_splice_keeps_a_300_line_register_byte_for_byte(
        self, tmp_path, first, second, expected
    ):
        target = tmp_path / "docs" / "PYSMELLY.md"
        self._init(*first, "docs/PYSMELLY.md")
        generated = target.read_text(encoding="utf-8")
        # An older template in the block, a prefix above it, the register below.
        stale = generated.replace(
            _guidance_hash(SHORT_GUIDANCE_CONTENT if first else GUIDANCE_CONTENT), "0ldhash0ld00"
        ).replace("Code Smell Review Guide", "Code Smell Review Guide (old wording)")
        target.write_bytes(("prefix kept\n\n" + stale + REGISTER_TAIL).encode())

        assert self._init(*second, "docs/PYSMELLY.md") == 0

        text = target.read_text(encoding="utf-8")
        block = (
            f"<!-- pysmelly-guidance {_guidance_hash(expected)} -->\n\n"
            f"{expected}\n{GUIDANCE_END_MARKER}"
        )
        assert text == "prefix kept\n\n" + block + "\n" + REGISTER_TAIL
        assert text.endswith(REGISTER + "\nno trailing newline at EOF")
        assert "old wording" not in text

    def test_spliced_file_with_register_is_current(self, tmp_path, capsys):
        self._init("--short", "docs/PYSMELLY.md")
        target = tmp_path / "docs" / "PYSMELLY.md"
        target.write_text(target.read_text(encoding="utf-8") + REGISTER_TAIL, encoding="utf-8")
        (tmp_path / "app.py").write_text("x = 1\n")
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["--min-severity", "high", str(tmp_path)])
        output = capsys.readouterr().out
        assert "outdated" not in output
        assert "pysmelly init" not in output

    @pytest.mark.parametrize(
        ("existing", "reason"),
        [
            (
                _pre_marker_file(SHORT_GUIDANCE_CONTENT).replace("Smell", "Smells", 1)
                + REGISTER_TAIL,
                "edited since pysmelly init generated it",
            ),
            ("# Our own pysmelly notes\n\n" + REGISTER, "no pysmelly-guidance start marker"),
            (
                f"<!-- pysmelly-guidance aaa -->\nx\n{GUIDANCE_END_MARKER}\n"
                f"<!-- pysmelly-guidance bbb -->\ny\n{GUIDANCE_END_MARKER}\n",
                "more than one",
            ),
            (f"{GUIDANCE_END_MARKER}\n<!-- pysmelly-guidance aaa -->\nx\n", "comes before"),
        ],
        ids=["pre-marker-edited", "hand-written", "two-pairs", "end-before-start"],
    )
    def test_refuses_a_file_without_one_intact_pair_and_writes_nothing(
        self, tmp_path, capsys, existing, reason
    ):
        target = tmp_path / "docs" / "PYSMELLY.md"
        target.parent.mkdir()
        target.write_bytes(existing.encode())
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# proj\n")

        assert self._init("--short", "docs/PYSMELLY.md") == 1

        err = capsys.readouterr().err
        assert "docs/PYSMELLY.md" in err
        assert "--force" in err
        assert "nothing was written" in err
        assert reason in err
        assert target.read_bytes() == existing.encode()
        assert claude_md.read_text() == "# proj\n"

    def test_refusal_creates_no_claude_md(self, tmp_path):
        (tmp_path / "PYSMELLY.md").write_text("hand-written\n")
        assert self._init() == 1
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_force_overwrites_the_whole_file(self, tmp_path, capsys):
        target = tmp_path / "PYSMELLY.md"
        target.write_text(_pre_marker_file(GUIDANCE_CONTENT) + REGISTER_TAIL, encoding="utf-8")
        assert self._init("--short", "--force") == 0
        text = target.read_text(encoding="utf-8")
        assert text == (
            f"<!-- pysmelly-guidance {_guidance_hash(SHORT_GUIDANCE_CONTENT)} -->\n\n"
            f"{SHORT_GUIDANCE_CONTENT}\n{GUIDANCE_END_MARKER}\n"
        )
        assert f"Wrote {target.name}" in capsys.readouterr().out

    def test_adding_the_end_marker_by_hand_lets_init_keep_the_register(self, tmp_path):
        """The migration the refusal asks for: mark the old block's end, re-run."""
        target = tmp_path / "PYSMELLY.md"
        old = _pre_marker_file(SHORT_GUIDANCE_CONTENT).replace("Smell", "Smells", 1)
        target.write_text(old + f"\n{GUIDANCE_END_MARKER}" + REGISTER_TAIL, encoding="utf-8")
        assert self._init("--short") == 0
        assert target.read_text(encoding="utf-8").endswith(GUIDANCE_END_MARKER + REGISTER_TAIL)

    def test_a_marker_quoted_in_prose_is_not_a_marker(self, tmp_path, capsys):
        """Modelled on s3-bagit's register, which quotes the start marker inline."""
        target = tmp_path / "PYSMELLY.md"
        prose = (
            "\nKeep the `<!-- pysmelly-guidance ... -->` marker line, and mind the\n"
            f"`{GUIDANCE_END_MARKER}` one too.\n"
        )
        old = _pre_marker_file(SHORT_GUIDANCE_CONTENT).replace("Smell", "Smells", 1)
        target.write_text(old + REGISTER_TAIL + prose, encoding="utf-8")

        assert self._init("--short") == 1
        assert "no <!-- /pysmelly-guidance --> end marker" in capsys.readouterr().err

        target.write_text(
            old + f"\n{GUIDANCE_END_MARKER}" + REGISTER_TAIL + prose, encoding="utf-8"
        )
        assert self._init("--short") == 0
        assert target.read_text(encoding="utf-8").endswith(
            GUIDANCE_END_MARKER + REGISTER_TAIL + prose
        )

    def test_outdated_hint_says_init_would_refuse_a_pre_marker_file(self, tmp_path, monkeypatch):
        from pysmelly.cli import _check_guidance_status

        (tmp_path / "PYSMELLY.md").write_text(
            "<!-- pysmelly-guidance 0ldhash0ld00 -->\nold\n" + REGISTER, encoding="utf-8"
        )
        status = _check_guidance_status() or ""
        assert "is outdated, but `pysmelly init` will refuse" in status
        assert "edited since pysmelly init generated it" in status
        assert GUIDANCE_END_MARKER in status
        assert "--force" in status

    def test_outdated_hint_with_both_markers_is_the_plain_one(self, tmp_path):
        from pysmelly.cli import _check_guidance_status

        (tmp_path / "PYSMELLY.md").write_text(
            f"<!-- pysmelly-guidance 0ldhash0ld00 -->\nold\n{GUIDANCE_END_MARKER}\n" + REGISTER,
            encoding="utf-8",
        )
        assert _check_guidance_status() == (
            "PYSMELLY.md is outdated — run `pysmelly init` to update it with the latest guidance."
        )


GUIDANCE_FIXTURES = Path(__file__).parent / "fixtures" / "guidance"
# s3-bagit's docs/PYSMELLY.md at fea7345: an unedited pre-end-marker --short
# block (mdformat put a blank line after its start marker), then its register.
S3_BAGIT = (GUIDANCE_FIXTURES / "s3-bagit-PYSMELLY.txt").read_text(encoding="utf-8")
S3_BAGIT_BLOCK_LAST_LINE = (
    "Configure via `.pysmelly.toml` or `[tool.pysmelly]` in `pyproject.toml`."
)
# A docs/PYSMELLY.md head from a real project: a start marker over a block
# rewritten by hand, so its hash proves nothing.
HAND_REWRITTEN_HEAD = (GUIDANCE_FIXTURES / "hand-rewritten-PYSMELLY-head.txt").read_text(
    encoding="utf-8"
)


def _without_blank_after_start_marker(text: str) -> str:
    """The same file as pre-21-6 `init` wrote it, before mdformat added the blank line."""
    marker_line_end = text.index("\n")
    assert text[marker_line_end : marker_line_end + 2] == "\n\n"
    return text[: marker_line_end + 1] + text[marker_line_end + 2 :]


class TestInitRecoversPreEndMarkerFile:
    """A file from before the end marker gets one where its start marker's hash proves."""

    @pytest.fixture(autouse=True)
    def _isolate_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def _init(self, *args: str) -> int:
        with pytest.raises(SystemExit) as exc:
            main(["init", *args])
        return exc.value.code or 0

    @staticmethod
    def _block(content: str) -> str:
        return (
            f"<!-- pysmelly-guidance {_guidance_hash(content)} -->\n\n"
            f"{content}\n{GUIDANCE_END_MARKER}"
        )

    @pytest.mark.parametrize("blank_line", [False, True], ids=["as-written", "mdformatted"])
    @pytest.mark.parametrize(
        ("old", "args", "expected"),
        [
            (SHORT_GUIDANCE_CONTENT, ["--short"], SHORT_GUIDANCE_CONTENT),
            (GUIDANCE_CONTENT, [], GUIDANCE_CONTENT),
            (SHORT_GUIDANCE_CONTENT, [], GUIDANCE_CONTENT),
            (GUIDANCE_CONTENT, ["--short"], SHORT_GUIDANCE_CONTENT),
        ],
        ids=["short-short", "full-full", "short-to-full", "full-to-short"],
    )
    def test_recovers_and_keeps_the_register(
        self, tmp_path, capsys, old, args, expected, blank_line
    ):
        stale = old.replace("Code Smell Review Guide", "Code Smell Review Guide (old wording)")
        start = f"<!-- pysmelly-guidance {_guidance_hash(stale)} -->\n"
        target = tmp_path / "PYSMELLY.md"
        target.write_bytes((start + ("\n" if blank_line else "") + stale + REGISTER_TAIL).encode())

        assert self._init(*args) == 0

        out = capsys.readouterr().out
        assert "verified its generated block against the start marker's hash" in out
        text = target.read_text(encoding="utf-8")
        assert text == self._block(expected) + "\n" + REGISTER_TAIL
        assert "old wording" not in text

        first = target.read_bytes()
        assert self._init(*args) == 0
        assert target.read_bytes() == first
        assert "end marker" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "fixture",
        [S3_BAGIT, _without_blank_after_start_marker(S3_BAGIT)],
        ids=["mdformatted", "as-written"],
    )
    def test_real_fleet_file_keeps_everything_after_the_block_byte_for_byte(
        self, tmp_path, fixture
    ):
        target = tmp_path / "docs" / "PYSMELLY.md"
        target.parent.mkdir()
        target.write_bytes(fixture.encode())
        after_old_block = fixture[
            fixture.index(S3_BAGIT_BLOCK_LAST_LINE) + len(S3_BAGIT_BLOCK_LAST_LINE) :
        ]
        assert len(after_old_block.splitlines()) > 550

        assert self._init("--short", "docs/PYSMELLY.md") == 0

        text = target.read_text(encoding="utf-8")
        assert text == self._block(SHORT_GUIDANCE_CONTENT) + after_old_block
        assert text.count(GUIDANCE_END_MARKER) == 1 + fixture.count(GUIDANCE_END_MARKER)
        first = target.read_bytes()
        assert self._init("--short", "docs/PYSMELLY.md") == 0
        assert target.read_bytes() == first

    @pytest.mark.parametrize(
        "existing",
        [
            S3_BAGIT.replace("Regenerate any time", "Regenerate whenever", 1),
            HAND_REWRITTEN_HEAD,
        ],
        ids=["edited-block", "hand-rewritten-block"],
    )
    def test_refuses_a_block_that_no_longer_matches_its_hash(self, tmp_path, capsys, existing):
        target = tmp_path / "docs" / "PYSMELLY.md"
        target.parent.mkdir()
        target.write_bytes(existing.encode())
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# proj\n")

        assert self._init("--short", "docs/PYSMELLY.md") == 1

        err = " ".join(capsys.readouterr().err.split())
        assert "the generated block was edited since pysmelly init generated it" in err
        assert "where it ends cannot be verified" in err
        assert "nothing was written" in err
        assert "--force" in err
        assert target.read_bytes() == existing.encode()
        assert claude_md.read_text() == "# proj\n"

    def test_more_than_one_proven_end_is_ambiguous(self, monkeypatch):
        """Two line ends closing a prefix with the marker's hash prove nothing."""
        from pysmelly.cli import GuidanceMarkerError, _generated_block_span

        class _EveryPrefixMatches:
            def update(self, data: bytes) -> None:
                pass

            def hexdigest(self) -> str:
                return "0" * 64

        monkeypatch.setattr(hashlib, "sha256", _EveryPrefixMatches)
        with pytest.raises(GuidanceMarkerError, match="more than one line"):
            _generated_block_span("<!-- pysmelly-guidance 000000000000 -->\na\nb\n")

    def test_outdated_hint_for_a_recoverable_file_is_the_plain_one(self, tmp_path):
        from pysmelly.cli import _check_guidance_status

        (tmp_path / "PYSMELLY.md").write_text(S3_BAGIT, encoding="utf-8")
        assert _check_guidance_status() == (
            "PYSMELLY.md is outdated — run `pysmelly init` to update it with the latest guidance."
        )

    def test_outdated_hint_for_a_hand_rewritten_file_says_init_would_refuse(self, tmp_path):
        from pysmelly.cli import _check_guidance_status

        (tmp_path / "PYSMELLY.md").write_text(HAND_REWRITTEN_HEAD, encoding="utf-8")
        status = _check_guidance_status() or ""
        assert "is outdated, but `pysmelly init` will refuse" in status
        assert "edited since pysmelly init generated it" in status
        assert "--force" in status


TEST_EXCLUDE_PATTERNS = ("'tests/'", "'test_*'", '"tests/"', '"test_*"', '"conftest.py"')

# The full template's two lines that taught excluding tests, as Phase 24-4
# rewrote them: (pre-24-4 line, current line).
PRE_24_4_LINES = (
    (
        "uvx pysmelly --exclude 'tests/' --exclude 'test_*'   # one pattern per flag",
        "uvx pysmelly --exclude 'vendor/' --exclude '*_pb2.py'   # one pattern per flag",
    ),
    ('exclude = ["tests/", "test_*", "conftest.py"]\n', 'exclude = ["vendor/", "*_pb2.py"]\n'),
)


def _pre_24_4_full_template() -> str:
    """The full template as fleet copies generated before 24-4 carry it."""
    old = GUIDANCE_CONTENT
    for before, current in PRE_24_4_LINES:
        old = old.replace(current, before)
    if "Do not exclude tests." in old:
        start = old.index("Do not exclude tests.")
        old = old[:start] + old[old.index("## How to act on AST findings", start) :]
    return old


class TestInitTemplateDropsTestExclude:
    """Phase 24-4: tests are parsed as evidence, so `init` no longer teaches excluding them."""

    @pytest.fixture(autouse=True)
    def _isolate_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def _init(self, *args: str) -> int:
        with pytest.raises(SystemExit) as exc:
            main(["init", *args])
        return exc.value.code or 0

    @pytest.mark.parametrize("args", [[], ["--short"]], ids=["full", "short"])
    def test_generated_guidance_has_no_test_exclude(self, tmp_path, args):
        assert self._init(*args) == 0
        text = (tmp_path / "PYSMELLY.md").read_text(encoding="utf-8")
        assert [pattern for pattern in TEST_EXCLUDE_PATTERNS if pattern in text] == []

    def test_full_guidance_shows_vendored_and_generated_excludes(self):
        assert "--exclude 'vendor/' --exclude '*_pb2.py'" in GUIDANCE_CONTENT
        assert 'exclude = ["vendor/", "*_pb2.py"]' in GUIDANCE_CONTENT
        assert "Do not exclude tests." in GUIDANCE_CONTENT
        assert "`testpaths` and `python_files`" in GUIDANCE_CONTENT

    def test_exclude_help_shows_vendored_and_generated_paths(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        help_text = " ".join(capsys.readouterr().out.split())
        assert "'test_*'" not in help_text
        assert "vendored or generated code" in help_text
        assert "'*_pb2.py' for names, 'vendor/' for directories" in help_text

    @pytest.mark.parametrize("end_marker", [True, False], ids=["end-marker", "pre-end-marker"])
    def test_pre_24_4_fleet_copy_is_outdated_and_init_updates_it(self, tmp_path, end_marker):
        """An unedited copy of the old full template is the update path, not a refusal."""
        from pysmelly.cli import _check_guidance_status

        old = _pre_24_4_full_template()
        assert 'exclude = ["tests/", "test_*", "conftest.py"]' in old
        assert old != GUIDANCE_CONTENT
        block = f"<!-- pysmelly-guidance {_guidance_hash(old)} -->\n\n{old}"
        if end_marker:
            block += f"\n{GUIDANCE_END_MARKER}"
        target = tmp_path / "PYSMELLY.md"
        target.write_bytes((block + REGISTER_TAIL).encode())

        assert _check_guidance_status() == (
            "PYSMELLY.md is outdated — run `pysmelly init` to update it with the latest guidance."
        )
        assert self._init() == 0

        text = target.read_text(encoding="utf-8")
        current = (
            f"<!-- pysmelly-guidance {_guidance_hash(GUIDANCE_CONTENT)} -->\n\n"
            f"{GUIDANCE_CONTENT}\n{GUIDANCE_END_MARKER}"
        )
        assert text == current + ("" if end_marker else "\n") + REGISTER_TAIL
        assert '"conftest.py"' not in text
        assert _check_guidance_status() is None


class TestGuidanceContentSync:
    """Ensure short and full guidance cover the same key concepts.

    ``# pysmelly: ignore[`` is the canonical full-line directive form: both
    variants must teach it, because ``pysmelly init`` writes them into consumer
    repos and the trailing form they used to show dies on a formatter rewrap.
    """

    KEY_PHRASES = [
        "HIGH",
        "MEDIUM",
        "LOW",
        "# pysmelly: ignore[",
        ".pysmelly.toml",
    ]

    @pytest.mark.parametrize("phrase", KEY_PHRASES)
    def test_key_phrase_in_full(self, phrase):
        assert phrase in GUIDANCE_CONTENT

    @pytest.mark.parametrize("phrase", KEY_PHRASES)
    def test_key_phrase_in_short(self, phrase):
        assert phrase in SHORT_GUIDANCE_CONTENT


class TestDiffRefFailsLoudly:
    """A diff that cannot be taken must not read as "nothing changed"."""

    def _git_repo(self, tmp_path):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e"}
        env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True, env=env)

    def test_unresolvable_ref_exits_nonzero(self, tmp_path, capsys):
        """Regression: get_changed_lines returned {} on a bad ref, so every
        finding was filtered out and the run reported "All checks passed"."""
        self._git_repo(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["--diff", "no-such-ref", str(tmp_path)])
        assert exc.value.code == 1
        assert "no-such-ref" in capsys.readouterr().err

    def test_get_changed_lines_raises_without_git(self, tmp_path, monkeypatch):
        from pysmelly import discovery
        from pysmelly.discovery import GitDiffError, get_changed_lines

        def _no_git(*_args, **_kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(discovery.subprocess, "run", _no_git)
        with pytest.raises(GitDiffError):
            get_changed_lines("HEAD", tmp_path)


class TestGuidanceUnreadable:
    def test_unreadable_guidance_is_not_reported_as_current(self, tmp_path, monkeypatch):
        """An unreadable PYSMELLY.md yields a suggestion, never silence."""
        from pysmelly.cli import _check_guidance_status

        monkeypatch.chdir(tmp_path)
        guidance = tmp_path / "PYSMELLY.md"
        guidance.write_text("stub\n")
        original = type(guidance).read_text

        def _boom(self, *args, **kwargs):
            if self.name == "PYSMELLY.md":
                raise OSError(13, "Permission denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(guidance), "read_text", _boom)
        status = _check_guidance_status()
        assert status is not None
        assert "PYSMELLY.md" in status


# The pysmelly sections of four fleet CLAUDE.md files, copied verbatim
# (2026-10-15). Each reworded `pysmelly init`'s sentence, and before the
# reference regex was widened each printed a false "Run `pysmelly init`" banner.
REPO_A_REFERENCE = """\
## pysmelly

Read [docs/PYSMELLY.md](docs/PYSMELLY.md) — findings register and review conventions — before running `make pysmelly`.

## Cross-Repository Ideas
"""

DEPLOYER_REFERENCE = """\
## pysmelly

Read [docs/PYSMELLY.md](docs/PYSMELLY.md) — the findings register and review
conventions — before running pysmelly code smell analysis on this project. The
phase-by-phase reasoning behind the standing verdicts is in
[docs/internal/PYSMELLY.md](docs/internal/PYSMELLY.md).

## Cross-Repository Ideas
"""

S3_ARCHIVE_REFERENCE = """\
`docs/` holds [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) (streaming
model and S3-to-S3 design), [`DECISIONS.md`](docs/DECISIONS.md)
(numbered, dated design decisions), [`OPERATIONS.md`](docs/OPERATIONS.md)
(failure modes, exit-code contract, runbook index),
[`RESUMABLE-EXTRACT.md`](docs/RESUMABLE-EXTRACT.md),
[`GOVERNANCE.md`](docs/GOVERNANCE.md) (access model, risk posture, PII
inventory, continuity),
[`HOWTO-SIMPLIFY.md`](docs/HOWTO-SIMPLIFY.md) (cleanup checklist),
[`PYSMELLY.md`](docs/PYSMELLY.md), [`PLAN-METHOD.md`](docs/PLAN-METHOD.md)
(what every `plan.toml` name means), `investigations/` (dated
point-in-time records) and the fileplan state directories.

## pysmelly

Read [docs/PYSMELLY.md](docs/PYSMELLY.md) — findings register and review
conventions — before running `make pysmelly`.
"""

REPO_B_REFERENCE = """\
## pysmelly

Read [docs/PYSMELLY.md](docs/PYSMELLY.md) — findings register and
review conventions — before running `make pysmelly`.
"""

FLEET_REFERENCES = {
    "repo-a": REPO_A_REFERENCE,
    "deployer": DEPLOYER_REFERENCE,
    "s3-archive": S3_ARCHIVE_REFERENCE,
    "repo-b": REPO_B_REFERENCE,
}


def _fleet_layout(root, claude_md_text):
    """A repo laid out like the fleet's: CLAUDE.md at the root, guidance in docs/."""
    (root / "CLAUDE.md").write_text(claude_md_text, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "app.py").write_text("x = 1\n")


class TestGuidanceReference:
    """The CLAUDE.md reference pysmelly reads to find the guidance file."""

    @pytest.mark.parametrize("repo", sorted(FLEET_REFERENCES))
    def test_fleet_reference_resolves_to_docs(self, repo, tmp_path, monkeypatch):
        from pysmelly.cli import _find_guidance_path

        monkeypatch.chdir(tmp_path)
        _fleet_layout(tmp_path, FLEET_REFERENCES[repo])
        assert _find_guidance_path() == Path("docs/PYSMELLY.md")

    @pytest.mark.parametrize("repo", sorted(FLEET_REFERENCES))
    def test_fleet_reference_with_current_guidance_prints_no_banner(
        self, repo, tmp_path, monkeypatch, capsys
    ):
        """End to end: a current docs/PYSMELLY.md behind a fleet reference is quiet."""
        monkeypatch.chdir(tmp_path)
        _fleet_layout(tmp_path, FLEET_REFERENCES[repo])
        content_hash = hashlib.sha256(GUIDANCE_CONTENT.encode()).hexdigest()[:12]
        (tmp_path / "docs" / "PYSMELLY.md").write_text(
            f"<!-- pysmelly-guidance {content_hash} -->\n{GUIDANCE_CONTENT}"
            "\n## Findings register\n\nhand-kept entries\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            main(["--min-severity", "high", "."])
        output = capsys.readouterr().out
        assert "pysmelly init" not in output
        assert "cannot read the reference" not in output

    def test_init_does_not_append_a_second_reference_to_fleet_wording(
        self, tmp_path, monkeypatch, capsys
    ):
        """The widened regex is the writer's idempotence key too."""
        monkeypatch.chdir(tmp_path)
        _fleet_layout(tmp_path, REPO_B_REFERENCE)
        with pytest.raises(SystemExit):
            main(["init", "--short", "docs/PYSMELLY.md"])
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == REPO_B_REFERENCE
        assert "CLAUDE.md already references docs/PYSMELLY.md" in capsys.readouterr().out

    def test_second_link_before_before_running_is_not_misread(self, tmp_path, monkeypatch):
        """`Read [a](a.md) and [b](b.md) before running pysmelly` never resolves to a.md.

        The `Read [..](PATH)` link must be the one the sentence is about; a
        sentence that names two is reported as unparseable instead of guessed.
        """
        from pysmelly.cli import UnparseableGuidanceReferenceError, _find_guidance_path

        monkeypatch.chdir(tmp_path)
        (tmp_path / "CLAUDE.md").write_text(
            "Read [the overview](README.md) and [guidance](docs/PYSMELLY.md)\n"
            "before running pysmelly.\n"
        )
        with pytest.raises(UnparseableGuidanceReferenceError):
            _find_guidance_path()

    @pytest.mark.parametrize(
        "text",
        [
            # the sentence ends before pysmelly is named
            "Read [g](docs/PYSMELLY.md) before running the tests. Then run pysmelly.\n",
            # `before running` is in the next paragraph
            "Read [g](docs/PYSMELLY.md) first.\n\nAlways lint before running pysmelly.\n",
            # a blank line made of whitespace still ends the paragraph
            "Read [g](docs/PYSMELLY.md)\n   \nbefore running pysmelly.\n",
        ],
    )
    def test_reference_does_not_span_sentence_or_paragraph(self, text, tmp_path, monkeypatch):
        from pysmelly.cli import GUIDANCE_REFERENCE_RE

        monkeypatch.chdir(tmp_path)
        assert GUIDANCE_REFERENCE_RE.search(text) is None

    def test_unparseable_reference_gets_its_own_message(self, tmp_path, monkeypatch, capsys):
        """A PYSMELLY.md link pysmelly cannot read is reported, not guessed past."""
        monkeypatch.chdir(tmp_path)
        _fleet_layout(
            tmp_path,
            "## pysmelly\n\nThe findings register is [docs/PYSMELLY.md](docs/PYSMELLY.md).\n",
        )
        with pytest.raises(SystemExit):
            main(["--min-severity", "high", "."])
        output = capsys.readouterr().out
        assert (
            "CLAUDE.md links docs/PYSMELLY.md but pysmelly cannot read the reference; "
            "use the sentence pysmelly init writes: Read [docs/PYSMELLY.md](docs/PYSMELLY.md) "
            "before running pysmelly code smell analysis on this project."
        ) in output
        assert "Run `pysmelly init`" not in output

    def test_unparseable_reference_raises_rather_than_falling_back(self, tmp_path, monkeypatch):
        from pysmelly.cli import UnparseableGuidanceReferenceError, _find_guidance_path

        monkeypatch.chdir(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("See [register](docs/PYSMELLY.md).\n")
        (tmp_path / "PYSMELLY.md").write_text("a root-level file must not be picked up\n")
        with pytest.raises(UnparseableGuidanceReferenceError) as exc:
            _find_guidance_path()
        assert exc.value.link == "docs/PYSMELLY.md"

    def test_claude_md_without_a_guidance_link_defaults_to_root(self, tmp_path, monkeypatch):
        """No PYSMELLY.md link at all: init's default location, and the init banner."""
        from pysmelly.cli import _check_guidance_status, _find_guidance_path

        monkeypatch.chdir(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# proj\n\n`make pysmelly` runs the scan.\n")
        assert _find_guidance_path() == Path("PYSMELLY.md")
        assert "Run `pysmelly init`" in (_check_guidance_status() or "")
