"""Tests for CLI behavior."""

import json

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

    def test_json_format(self, tmp_path, capsys):
        """--format=json produces valid JSON with expected keys."""
        (tmp_path / "example.py").write_text("x = 1\n")
        try:
            main(["--format", "json", "--min-severity", "high", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "total_files" in data
        assert "total_findings" in data
        assert "findings" in data

    def test_json_finding_fields(self, tmp_path, capsys):
        """Each JSON finding has all expected fields."""
        (tmp_path / "dead.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--format", "json", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["total_findings"] > 0
        finding = data["findings"][0]
        assert set(finding.keys()) == {"file", "line", "check", "message", "severity"}

    def test_min_severity_filters(self, tmp_path, capsys):
        """--min-severity high filters out medium and low findings."""
        # dead-code is HIGH severity
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
            main(["--format", "json", "--min-severity", "high", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        for f in data["findings"]:
            assert f["severity"] == "high"

    def test_check_flag_runs_single_check(self, tmp_path, capsys):
        """--check runs only the specified check."""
        (tmp_path / "example.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--format", "json", "--check", "dead-code", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        for f in data["findings"]:
            assert f["check"] == "dead-code"

    def test_skip_flag_excludes_check(self, tmp_path, capsys):
        """--skip excludes the specified check."""
        (tmp_path / "example.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--format", "json", "--skip", "dead-code", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        for f in data["findings"]:
            assert f["check"] != "dead-code"

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
            main(["--format", "json", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        for f in data["findings"]:
            assert not f["file"].startswith("/"), f"Expected relative path, got: {f['file']}"

    def test_exclude_pattern(self, tmp_path, capsys):
        """--exclude filters out matching files."""
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("def another_unused():\n    pass\n")
        try:
            main(["--format", "json", "--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["total_files"] == 1
        for f in data["findings"]:
            assert not f["file"].startswith("test_")

    def test_invalid_directory(self, capsys):
        """Non-existent directory prints error and exits 1."""
        try:
            main(["/nonexistent/path"])
        except SystemExit as e:
            assert e.code == 1
        output = capsys.readouterr().err
        assert "not a directory" in output
