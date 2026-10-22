"""Tests for config file support."""

import pytest

from pysmelly.config import ConfigError, _validate_config, load_config

VALID_CHECKS = {
    "dead-code",
    "single-call-site",
    "scattered-constants",
    "internal-only",
    "abandoned-code",
}


class TestLoadConfig:
    def test_no_config_file(self, tmp_path):
        result = load_config(tmp_path, VALID_CHECKS)
        assert result == {}

    def test_warns_when_parent_has_config(self, tmp_path, capsys):
        """Warn if target dir has no config but a parent does."""
        (tmp_path / "pyproject.toml").write_text('[tool.pysmelly]\nskip = ["dead-code"]\n')
        src = tmp_path / "src"
        src.mkdir()
        result = load_config(src, VALID_CHECKS)
        assert result == {}
        err = capsys.readouterr().err
        assert "no config in" in err
        assert "has pysmelly config" in err

    def test_no_warning_when_no_parent_config(self, tmp_path, capsys):
        """No warning when neither target nor parent has config."""
        src = tmp_path / "src"
        src.mkdir()
        result = load_config(src, VALID_CHECKS)
        assert result == {}
        err = capsys.readouterr().err
        assert err == ""

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

    def test_git_history_is_unknown_key(self):
        """git-history bool config was removed (now a subcommand)."""
        with pytest.raises(ConfigError, match="unknown key"):
            _validate_config({"git-history": True}, "test", VALID_CHECKS)

    def test_git_window_validates(self):
        _validate_config({"git-window": "1y"}, "test", VALID_CHECKS)

    def test_git_window_invalid(self):
        with pytest.raises(ConfigError, match="invalid git-window"):
            _validate_config({"git-window": "6weeks"}, "test", VALID_CHECKS)

    def test_commit_messages_structured_validates(self):
        _validate_config({"commit-messages": "structured"}, "test", VALID_CHECKS)

    def test_commit_messages_invalid(self):
        with pytest.raises(ConfigError, match="invalid commit-messages 'maybe'"):
            _validate_config({"commit-messages": "maybe"}, "test", VALID_CHECKS)

    def test_expected_coupling_validates(self):
        _validate_config(
            {"expected-coupling": [["*/settings.py", "*/urls.py"]]},
            "test",
            VALID_CHECKS,
        )

    def test_expected_coupling_must_be_list(self):
        with pytest.raises(ConfigError, match="must be a list of pairs"):
            _validate_config({"expected-coupling": "bad"}, "test", VALID_CHECKS)

    def test_expected_coupling_items_must_be_pairs(self):
        with pytest.raises(ConfigError, match="must be 2-element lists"):
            _validate_config({"expected-coupling": [["only_one"]]}, "test", VALID_CHECKS)

    def test_expected_coupling_items_must_be_strings(self):
        with pytest.raises(ConfigError, match="must contain strings"):
            _validate_config({"expected-coupling": [[1, 2]]}, "test", VALID_CHECKS)


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

    def test_malformed_pyproject_exits(self, tmp_path, capsys):
        """A pyproject.toml that will not parse is an error, not "no config".

        Regression: _find_config_file swallowed TOMLDecodeError and returned
        None, so a broken pyproject.toml ran with default settings (no skip,
        no exclude) and reported a clean result.
        """
        from pysmelly.cli import main

        (tmp_path / "pyproject.toml").write_text("[tool.pysmelly\nskip = [\n")
        with pytest.raises(SystemExit) as exc_info:
            main([str(tmp_path)])
        assert exc_info.value.code == 1
        assert "pyproject.toml" in capsys.readouterr().err

    def test_malformed_parent_pyproject_does_not_abort(self, tmp_path, capsys):
        """An unparseable ancestor pyproject.toml only suppresses the advisory warning."""
        (tmp_path / "pyproject.toml").write_text("[tool.pysmelly\n")
        src = tmp_path / "src"
        src.mkdir()
        assert load_config(src, VALID_CHECKS) == {}
        assert capsys.readouterr().err == ""


BOTO3_ENTRY = """\
[[{prefix}catalog.patterns]]
name = "boto3-client-factory"
description = "AWS clients"
imports = ["boto3"]
condition = "also-imports:acme.aws"
suggest = "acme.aws.client() (the shared factory)"
"""


def _catalog_names(config: dict) -> list[str]:
    return [pattern.name for pattern in config["catalog"].patterns]


class TestUserCatalog:
    """The ``catalog`` key: stdlib-alternatives entries a project adds."""

    def test_pyproject_entry_is_parsed(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(BOTO3_ENTRY.format(prefix="tool.pysmelly."))
        config = load_config(tmp_path, VALID_CHECKS)
        (pattern,) = config["catalog"].patterns
        assert pattern.name == "boto3-client-factory"
        assert pattern.imports == ("boto3",)
        assert pattern.also_imports == "acme.aws"
        assert pattern.suggest == "acme.aws.client() (the shared factory)"

    def test_dotfile_entry_is_parsed(self, tmp_path):
        (tmp_path / ".pysmelly.toml").write_text(BOTO3_ENTRY.format(prefix=""))
        assert _catalog_names(load_config(tmp_path, VALID_CHECKS)) == ["boto3-client-factory"]

    def test_dotfile_catalog_wins_over_pyproject(self, tmp_path):
        """The two files never merge: .pysmelly.toml's catalog is the only one read."""
        (tmp_path / ".pysmelly.toml").write_text(BOTO3_ENTRY.format(prefix=""))
        (tmp_path / "pyproject.toml").write_text(
            "[[tool.pysmelly.catalog.patterns]]\n"
            'name = "requests-session"\n'
            'description = "HTTP"\n'
            'imports = ["requests"]\n'
            'suggest = "acme.http.session()"\n'
        )
        assert _catalog_names(load_config(tmp_path, VALID_CHECKS)) == ["boto3-client-factory"]

    def test_pyproject_catalog_ignored_when_dotfile_has_none(self, tmp_path):
        (tmp_path / ".pysmelly.toml").write_text('skip = ["dead-code"]\n')
        (tmp_path / "pyproject.toml").write_text(BOTO3_ENTRY.format(prefix="tool.pysmelly."))
        assert "catalog" not in load_config(tmp_path, VALID_CHECKS)

    def test_unknown_field_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            BOTO3_ENTRY.format(prefix="") + 'severity = "high"\n'
        )
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path, VALID_CHECKS)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "catalog patterns[0]: unknown field(s): severity" in err
        assert "Valid fields:" in err

    def test_missing_field_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            '[[catalog.patterns]]\nname = "x"\nimports = ["boto3"]\n'
        )
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "missing required field(s): description, suggest" in capsys.readouterr().err

    def test_wrong_type_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            '[[catalog.patterns]]\nname = "x"\ndescription = "d"\n'
            'imports = "boto3"\nsuggest = "s"\n'
        )
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "'imports' must be a non-empty list" in capsys.readouterr().err

    def test_condition_must_be_also_imports(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            BOTO3_ENTRY.format(prefix="").replace("also-imports:acme.aws", "requires:acme.aws")
        )
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "'condition' must be 'also-imports:<module>'" in capsys.readouterr().err

    def test_unknown_condition_fn_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            BOTO3_ENTRY.format(prefix="") + 'condition_fn = "is_big"\n'
        )
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "unknown condition_fn 'is_big'" in capsys.readouterr().err

    def test_catalog_must_be_a_table(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text('catalog = ["boto3"]\n')
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "catalog: must be a table, got list" in capsys.readouterr().err

    def test_unknown_catalog_key_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text("[catalog]\npatterns = []\nrules = []\n")
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "catalog: unknown key(s): rules" in capsys.readouterr().err

    def test_shadowing_a_shipped_name_exits(self, tmp_path, capsys):
        """A project entry adds a pattern; it never overrides a shipped one."""
        (tmp_path / "pyproject.toml").write_text(
            "[[tool.pysmelly.catalog.patterns]]\n"
            'name = "os-path-vs-pathlib"\n'
            'description = "path manipulation"\n'
            'imports = ["os.path"]\n'
            'suggest = "nothing, we like os.path"\n'
        )
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path, VALID_CHECKS)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "[tool.pysmelly] catalog" in err
        assert "'os-path-vs-pathlib' is already a shipped catalog entry" in err

    def test_duplicate_user_names_exit(self, tmp_path, capsys):
        entry = BOTO3_ENTRY.format(prefix="")
        (tmp_path / ".pysmelly.toml").write_text(entry + "\n" + entry)
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "duplicate pattern name 'boto3-client-factory'" in capsys.readouterr().err

    def test_pyproject_pair_is_parsed(self, tmp_path):
        """dual-stack-dependency pairs come through the same ``catalog`` table."""
        (tmp_path / "pyproject.toml").write_text(HTTP_PAIR.format(prefix="tool.pysmelly."))
        catalog = load_config(tmp_path, VALID_CHECKS)["catalog"]
        assert catalog.patterns == ()
        (pair,) = catalog.pairs
        assert pair.name == "acme-http"
        assert [side.label for side in pair.sides] == ["acme.legacy_http", "acme.http"]

    def test_patterns_and_pairs_together(self, tmp_path):
        (tmp_path / ".pysmelly.toml").write_text(
            BOTO3_ENTRY.format(prefix="") + HTTP_PAIR.format(prefix="")
        )
        catalog = load_config(tmp_path, VALID_CHECKS)["catalog"]
        assert ([p.name for p in catalog.patterns], [p.name for p in catalog.pairs]) == (
            ["boto3-client-factory"],
            ["acme-http"],
        )

    def test_shadowing_a_shipped_pair_exits(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            HTTP_PAIR.format(prefix="tool.pysmelly.").replace("acme-http", "os-path-pathlib")
        )
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path, VALID_CHECKS)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "[tool.pysmelly] catalog" in err
        assert "'os-path-pathlib' is already a shipped catalog pair" in err

    def test_invalid_pair_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            '[[catalog.pairs]]\nname = "x"\ndescription = "d"\n'
            'sides = [{ label = "a", imports = ["a"] }]\n'
        )
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "'sides' must be an array of exactly two tables" in capsys.readouterr().err

    def test_empty_catalog_table_exits(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text("[catalog]\n")
        with pytest.raises(SystemExit):
            load_config(tmp_path, VALID_CHECKS)
        assert "catalog: needs a 'patterns' or a 'pairs' array" in capsys.readouterr().err


HTTP_PAIR = """\
[[{prefix}catalog.pairs]]
name = "acme-http"
description = "internal HTTP"
sides = [
    {{ label = "acme.legacy_http", imports = ["acme.legacy_http"] }},
    {{ label = "acme.http", imports = ["acme.http"] }},
]
"""


class TestUserCatalogInStdlibAlternatives:
    """User entries run in stdlib-alternatives exactly as shipped ones do."""

    def _run(self, tmp_path, capsys) -> str:
        from pysmelly.cli import main

        with pytest.raises(SystemExit):
            main(["--no-context", "--check", "stdlib-alternatives", str(tmp_path)])
        return capsys.readouterr().out

    def _project(self, tmp_path, files: dict[str, str]) -> None:
        (tmp_path / "pyproject.toml").write_text(BOTO3_ENTRY.format(prefix="tool.pysmelly."))
        for name, code in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code)

    def test_entry_fires_when_condition_holds(self, tmp_path, capsys):
        self._project(
            tmp_path,
            {
                "acme/__init__.py": "",
                "acme/aws.py": "import boto3\n\ndef client(name):\n    return boto3.client(name)\n",
                "jobs/sync.py": "import boto3\nfrom acme import aws\n",
            },
        )
        out = self._run(tmp_path, capsys)
        assert (
            "boto3 used in 2 files (acme/aws.py:1, jobs/sync.py:1) — "
            "alternative: acme.aws.client() (the shared factory)"
        ) in out

    def test_entry_quiet_when_condition_fails(self, tmp_path, capsys):
        """also-imports: applies to project entries too."""
        self._project(tmp_path, {"jobs/sync.py": "import boto3\n"})
        out = self._run(tmp_path, capsys)
        assert "Parsed 1 Python files" in out
        assert "boto3" not in out

    def test_exclude_imports_applies(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            "[[catalog.patterns]]\n"
            'name = "legacy-db"\n'
            'description = "database access"\n'
            'imports = ["legacydb"]\n'
            'exclude_imports = ["legacydb.testing"]\n'
            'suggest = "acme.db"\n'
        )
        (tmp_path / "fixtures.py").write_text("import legacydb.testing\n")
        assert "acme.db" not in self._run(tmp_path, capsys)
        (tmp_path / "app.py").write_text("import legacydb\n")
        assert "legacydb imported (app.py:1) — alternative: acme.db" in self._run(tmp_path, capsys)

    def test_condition_fn_applies(self, tmp_path, capsys):
        (tmp_path / ".pysmelly.toml").write_text(
            "[[catalog.patterns]]\n"
            'name = "our-cli"\n'
            'description = "CLI parsing"\n'
            'imports = ["argparse"]\n'
            'condition_fn = "complex_argparse"\n'
            'suggest = "acme.cli"\n'
        )
        (tmp_path / "cli.py").write_text(
            "import argparse\np = argparse.ArgumentParser()\np.add_argument('x')\n"
        )
        assert "acme.cli" not in self._run(tmp_path, capsys)
        (tmp_path / "cli.py").write_text(
            "import argparse\np = argparse.ArgumentParser()\np.add_subparsers()\n"
        )
        assert "argparse imported (cli.py:1) — alternative: acme.cli" in self._run(tmp_path, capsys)
