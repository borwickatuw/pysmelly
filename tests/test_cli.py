"""Tests for CLI behavior."""

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
        main(["--list-checks"])
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

    def test_exclude_test_annotates_caller_findings(self, tmp_path, capsys):
        """--exclude test_* annotates caller-aware findings with [test files excluded]."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("from app import unused_func\nunused_func()\n")
        try:
            main(["--no-context", "--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "dead-code" in output
        assert "[test files excluded]" in output

    def test_exclude_non_test_no_annotation(self, tmp_path, capsys):
        """--exclude with non-test pattern does not annotate findings."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", "--exclude", "vendor_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
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
