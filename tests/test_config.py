"""Tests for config file support."""

import pytest

from pysmelly.config import ConfigError, _validate_config, load_config

VALID_CHECKS = {"dead-code", "single-call-site", "scattered-constants", "internal-only"}


class TestLoadConfig:
    def test_no_config_file(self, tmp_path):
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {}

    def test_dotfile_toml(self, tmp_path):
        (tmp_path / ".pysmelly.toml").write_text(
            'exclude = ["tests/", "test_*"]\nskip = ["single-call-site"]\n'
        )
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {"exclude": ["tests/", "test_*"], "skip": ["single-call-site"]}

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pysmelly]\nexclude = ["tests/"]\nmin-severity = "medium"\n'
        )
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {"exclude": ["tests/"], "min-severity": "medium"}

    def test_dotfile_takes_precedence(self, tmp_path):
        """When both exist, .pysmelly.toml wins."""
        (tmp_path / ".pysmelly.toml").write_text('skip = ["dead-code"]\n')
        (tmp_path / "pyproject.toml").write_text('[tool.pysmelly]\nskip = ["internal-only"]\n')
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {"skip": ["dead-code"]}

    def test_pyproject_without_tool_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n")
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {}

    def test_pyproject_without_pysmelly_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {}

    def test_empty_dotfile(self, tmp_path):
        (tmp_path / ".pysmelly.toml").write_text("")
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {}


class TestValidation:
    def test_unknown_key(self):
        with pytest.raises(ConfigError, match="unknown key.*bogus"):
            _validate_config({"bogus": True}, "test", VALID_CHECKS)

    def test_exclude_must_be_list(self):
        with pytest.raises(ConfigError, match="'exclude' must be a list"):
            _validate_config({"exclude": "tests/"}, "test", VALID_CHECKS)

    def test_skip_must_be_list(self):
        with pytest.raises(ConfigError, match="'skip' must be a list"):
            _validate_config({"skip": "dead-code"}, "test", VALID_CHECKS)

    def test_list_items_must_be_strings(self):
        with pytest.raises(ConfigError, match="items must be strings"):
            _validate_config({"exclude": [123]}, "test", VALID_CHECKS)

    def test_min_severity_must_be_string(self):
        with pytest.raises(ConfigError, match="'min-severity' must be a string"):
            _validate_config({"min-severity": 3}, "test", VALID_CHECKS)

    def test_invalid_severity_value(self):
        with pytest.raises(ConfigError, match="invalid min-severity 'critical'"):
            _validate_config({"min-severity": "critical"}, "test", VALID_CHECKS)

    def test_invalid_check_name(self):
        with pytest.raises(ConfigError, match="unknown check 'bogus-check'"):
            _validate_config({"check": "bogus-check"}, "test", VALID_CHECKS)

    def test_invalid_skip_check_name(self):
        with pytest.raises(ConfigError, match="unknown check.*bogus"):
            _validate_config({"skip": ["bogus"]}, "test", VALID_CHECKS)

    def test_valid_config_passes(self):
        _validate_config(
            {
                "exclude": ["tests/"],
                "skip": ["dead-code"],
                "min-severity": "medium",
                "check": "scattered-constants",
            },
            "test",
            VALID_CHECKS,
        )

    def test_check_must_be_string(self):
        with pytest.raises(ConfigError, match="'check' must be a string"):
            _validate_config({"check": ["dead-code"]}, "test", VALID_CHECKS)


class TestCLIConfigIntegration:
    def test_config_exclude_applied(self, tmp_path, capsys):
        """Config file excludes are picked up by CLI."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('exclude = ["test_*"]\n')
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("def another_unused():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 1 Python files" in output

    def test_config_skip_applied(self, tmp_path, capsys):
        """Config file skip is picked up by CLI."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('skip = ["dead-code"]\n')
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "=== dead-code" not in output

    def test_cli_exclude_extends_config(self, tmp_path, capsys):
        """CLI --exclude adds to config exclude, not replaces."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('exclude = ["vendor/"]\n')
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text("def vendored():\n    pass\n")
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        (tmp_path / "test_app.py").write_text("def test_unused():\n    pass\n")
        try:
            main(["--no-context", "--exclude", "test_*", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "Parsed 1 Python files" in output

    def test_config_min_severity(self, tmp_path, capsys):
        """Config min-severity is applied."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('min-severity = "high"\n')
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        assert "dead-code" in output
        # internal-only is MEDIUM, should be filtered by high severity
        assert "internal-only" not in output

    def test_cli_min_severity_overrides_config(self, tmp_path, capsys):
        """CLI --min-severity overrides config."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('min-severity = "high"\n')
        (tmp_path / "app.py").write_text("def unused_func():\n    pass\n")
        try:
            main(["--no-context", "--min-severity", "low", str(tmp_path)])
        except SystemExit:
            pass
        output = capsys.readouterr().out
        # With low severity, we should see more findings
        assert "dead-code" in output

    def test_invalid_config_exits(self, tmp_path, capsys):
        """Invalid config file causes exit with error."""
        from pysmelly.cli import main

        (tmp_path / ".pysmelly.toml").write_text('bogus_key = "value"\n')
        with pytest.raises(SystemExit) as exc_info:
            main([str(tmp_path)])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "unknown key" in err
