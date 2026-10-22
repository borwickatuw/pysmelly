"""Tests for the test role: pytest config decides files, definitions declare themselves."""

import ast
import subprocess
from pathlib import Path

import pytest

from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.pytest_config import (
    NO_PYTEST_CONFIG,
    PytestConfig,
    PytestConfigError,
    load_pytest_config,
)
from pysmelly.roles import TEST_RUNNER_MODULES

PLAIN = "def helper():\n    return 1\n"


def _context(files: dict[str, str], config: PytestConfig = NO_PYTEST_CONFIG) -> AnalysisContext:
    trees = {Path(name): ast.parse(code) for name, code in files.items()}
    return AnalysisContext(trees, verbose=False, pytest_config=config)


def _config(root: Path, files: dict[str, str]) -> PytestConfig:
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return load_pytest_config(root, root)


def _roles(root: Path, config_files: dict[str, str], sources: list[str]) -> dict[Path, str]:
    config = _config(root, config_files)
    return _context(dict.fromkeys(sources, PLAIN), config).roles.files


PYPROJECT_TESTS = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'


class TestConfigSources:
    def test_pyproject_ini_options(self, tmp_path):
        roles = _roles(tmp_path, {"pyproject.toml": PYPROJECT_TESTS}, ["tests/test_a.py"])
        assert roles == {
            Path("tests/test_a.py"): "python_files 'test_*.py' (pytest default) "
            "under testpaths 'tests'"
        }

    def test_pytest_ini(self, tmp_path):
        config = _config(tmp_path, {"pytest.ini": "[pytest]\ntestpaths = checks\n"})
        assert config.source == "pytest.ini [pytest]"
        assert config.testpaths == ("checks",)

    def test_hidden_pytest_ini(self, tmp_path):
        config = _config(tmp_path, {".pytest.ini": "[pytest]\ntestpaths = checks\n"})
        assert config.source == ".pytest.ini [pytest]"
        assert config.testpaths == ("checks",)

    def test_tox_ini(self, tmp_path):
        config = _config(tmp_path, {"tox.ini": "[pytest]\ntestpaths = checks other\n"})
        assert config.source == "tox.ini [pytest]"
        assert config.testpaths == ("checks", "other")

    def test_setup_cfg(self, tmp_path):
        config = _config(tmp_path, {"setup.cfg": "[tool:pytest]\ntestpaths = checks\n"})
        assert config.source == "setup.cfg [tool:pytest]"
        assert config.testpaths == ("checks",)

    def test_python_files_from_config(self, tmp_path):
        config = _config(tmp_path, {"pytest.ini": "[pytest]\npython_files = check_*.py tests.py\n"})
        roles = _context(
            {"app/check_x.py": PLAIN, "app/tests.py": PLAIN, "app/test_y.py": PLAIN}, config
        ).roles.files
        assert roles == {
            Path("app/check_x.py"): "python_files 'check_*.py' under the rootdir (no testpaths)",
            Path("app/tests.py"): "python_files 'tests.py' under the rootdir (no testpaths)",
        }

    def test_python_files_pattern_with_a_path(self, tmp_path):
        config = _config(tmp_path, {"pytest.ini": "[pytest]\npython_files = unit/*.py\n"})
        roles = _context({"pkg/unit/a.py": PLAIN, "pkg/b.py": PLAIN}, config).roles.files
        assert set(roles) == {Path("pkg/unit/a.py")}

    def test_testpaths_glob(self, tmp_path):
        config = _config(
            tmp_path,
            {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["pkgs/*/tests"]\n'},
        )
        roles = _context(
            {"pkgs/a/tests/test_x.py": PLAIN, "pkgs/a/src/test_y.py": PLAIN}, config
        ).roles.files
        assert set(roles) == {Path("pkgs/a/tests/test_x.py")}

    def test_testpaths_names_a_file(self, tmp_path):
        config = _config(
            tmp_path, {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["smoke.py"]\n'}
        )
        roles = _context({"smoke.py": PLAIN}, config).roles.files
        assert roles == {Path("smoke.py"): "named by testpaths 'smoke.py'"}

    def test_string_value_in_toml_is_split(self, tmp_path):
        config = _config(
            tmp_path, {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = "a b"\n'}
        )
        assert config.testpaths == ("a", "b")

    def test_paths_are_read_from_the_rootdir_not_the_base(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_TESTS, encoding="utf-8")
        config = load_pytest_config(tmp_path, tmp_path / "tests")
        roles = _context({"test_a.py": PLAIN}, config).roles.files
        assert set(roles) == {Path("test_a.py")}


class TestPrecedence:
    def test_pytest_ini_wins_even_when_empty(self, tmp_path):
        config = _config(tmp_path, {"pytest.ini": "", "pyproject.toml": PYPROJECT_TESTS})
        assert config.source == "pytest.ini [pytest]"
        assert config.testpaths == ()

    def test_pytest_ini_wins_over_hidden(self, tmp_path):
        config = _config(
            tmp_path,
            {"pytest.ini": "[pytest]\ntestpaths = a\n", ".pytest.ini": "[pytest]\ntestpaths = b\n"},
        )
        assert config.testpaths == ("a",)

    def test_pyproject_wins_over_tox_and_setup_cfg(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "pyproject.toml": PYPROJECT_TESTS,
                "tox.ini": "[pytest]\ntestpaths = tox\n",
                "setup.cfg": "[tool:pytest]\ntestpaths = cfg\n",
            },
        )
        assert config.source == "pyproject.toml [tool.pytest.ini_options]"
        assert config.testpaths == ("tests",)

    def test_pyproject_without_the_table_is_passed_over(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "pyproject.toml": "[tool.black]\nline-length = 99\n",
                "tox.ini": "[pytest]\ntestpaths = tox\n",
            },
        )
        assert config.source == "tox.ini [pytest]"

    def test_tox_wins_over_setup_cfg(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "tox.ini": "[pytest]\ntestpaths = tox\n",
                "setup.cfg": "[tool:pytest]\ntestpaths = c\n",
            },
        )
        assert config.testpaths == ("tox",)

    def test_tox_without_the_section_is_passed_over(self, tmp_path):
        config = _config(
            tmp_path,
            {"tox.ini": "[tox]\nenvlist = py312\n", "setup.cfg": "[tool:pytest]\ntestpaths = c\n"},
        )
        assert config.source == "setup.cfg [tool:pytest]"

    def test_setup_cfg_pytest_section_is_not_pytest_config(self, tmp_path):
        config = _config(tmp_path, {"setup.cfg": "[pytest]\ntestpaths = c\n"})
        assert config.source is None


class TestDefaults:
    def test_no_config_uses_pytest_defaults_anywhere_under_the_rootdir(self, tmp_path):
        roles = _roles(tmp_path, {}, ["a/test_x.py", "b/c/y_test.py", "b/c/tests_z.py"])
        assert roles == {
            Path("a/test_x.py"): "python_files 'test_*.py' (pytest default) "
            "under the rootdir (no testpaths)",
            Path("b/c/y_test.py"): "python_files '*_test.py' (pytest default) "
            "under the rootdir (no testpaths)",
        }

    def test_testpaths_limits_where_default_python_files_apply(self, tmp_path):
        roles = _roles(
            tmp_path, {"pyproject.toml": PYPROJECT_TESTS}, ["tests/test_a.py", "src/test_b.py"]
        )
        assert set(roles) == {Path("tests/test_a.py")}

    def test_helper_under_testpaths_is_not_test_role_by_location(self, tmp_path):
        """testpaths names where pytest searches; what matches no pattern needs import evidence."""
        roles = _roles(tmp_path, {"pyproject.toml": PYPROJECT_TESTS}, ["tests/helpers.py"])
        assert roles == {}


class TestConftest:
    def test_conftest_under_testpaths_is_test(self, tmp_path):
        roles = _roles(tmp_path, {"pyproject.toml": PYPROJECT_TESTS}, ["tests/sub/conftest.py"])
        assert roles == {Path("tests/sub/conftest.py"): "conftest.py under testpaths 'tests'"}

    def test_conftest_outside_testpaths_is_production(self, tmp_path):
        roles = _roles(tmp_path, {"pyproject.toml": PYPROJECT_TESTS}, ["src/pkg/conftest.py"])
        assert roles == {}

    def test_conftest_without_testpaths_is_test_under_the_rootdir(self, tmp_path):
        roles = _roles(tmp_path, {}, ["conftest.py"])
        assert roles == {Path("conftest.py"): "conftest.py under the rootdir (no testpaths)"}


class TestQuizCase:
    def test_production_module_named_test_star_is_production(self, tmp_path):
        """A filename alone is never evidence once testpaths does not reach the file."""
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {"src/quiz/test_knowledge.py": "import json\n\ndef test_answer(q):\n    return q\n"},
            config,
        )
        assert ctx.roles.files == {}
        assert ctx.roles.definitions == {}
        assert ctx.roles.of(Path("src/quiz/test_knowledge.py"), "test_answer") is None


class TestDefinitionLayer:
    def _declared(self, files: dict[str, str]) -> dict[Path, dict[str, str]]:
        config = PytestConfig(
            rootdir=Path(),
            base=Path(),
            source="pyproject.toml [tool.pytest.ini_options]",
            testpaths=("tests",),
            python_files=("test_*.py",),
            python_files_default=False,
        )
        return _context(files, config).roles.definitions

    @pytest.mark.parametrize(
        "source",
        [
            "import unittest\n\nclass Checks(unittest.TestCase):\n    def helper(self): ...\n",
            "import unittest as ut\n\nclass Checks(ut.TestCase):\n    def helper(self): ...\n",
            "from unittest import TestCase\n\nclass Checks(TestCase):\n    def helper(self): ...\n",
            "from unittest import TestCase as TC\n\nclass Checks(TC):\n    def helper(self): ...\n",
            "from django.test import TestCase\n\nclass Checks(TestCase):\n    def helper(self): ...\n",
        ],
    )
    def test_testcase_subclass_through_imports(self, source):
        declared = self._declared({"src/app/checks.py": source})[Path("src/app/checks.py")]
        assert declared["Checks"].startswith("`Checks` subclasses ")
        assert declared["Checks.helper"] == declared["Checks"]

    def test_testcase_subclass_in_the_same_module(self):
        source = "import unittest\n\nclass Base(unittest.TestCase): ...\n\nclass Leaf(Base): ...\n"
        declared = self._declared({"app/checks.py": source})[Path("app/checks.py")]
        assert (
            declared["Leaf"] == "`Leaf` subclasses unittest.TestCase via app.checks.Base (line 5)"
        )

    def test_testcase_subclass_across_modules(self):
        files = {
            "app/__init__.py": "",
            "app/base.py": "import unittest\n\nclass Base(unittest.TestCase): ...\n",
            "app/leaf.py": "from .base import Base as B\n\nclass Leaf(B): ...\n",
        }
        declared = self._declared(files)
        assert declared[Path("app/leaf.py")]["Leaf"] == (
            "`Leaf` subclasses unittest.TestCase via app.base.Base (line 3)"
        )

    def test_unrelated_testcase_name_is_not_a_test(self):
        source = "from myapp import TestCase\n\nclass Checks(TestCase): ...\n"
        assert self._declared({"app/checks.py": source}) == {}

    @pytest.mark.parametrize(
        ("source", "decorator"),
        [
            ("import pytest\n\n@pytest.fixture\ndef client(): ...\n", "@pytest.fixture"),
            (
                "import pytest\n\n@pytest.fixture(scope='module')\ndef client(): ...\n",
                "@pytest.fixture",
            ),
            ("import pytest as pt\n\n@pt.fixture\ndef client(): ...\n", "@pytest.fixture"),
            ("from pytest import fixture\n\n@fixture\ndef client(): ...\n", "@pytest.fixture"),
            ("import pytest\n\n@pytest.mark.slow\ndef client(): ...\n", "@pytest.mark.slow"),
            (
                "from pytest import mark\n\n@mark.parametrize('a', [1])\ndef client(a): ...\n",
                "@pytest.mark.parametrize",
            ),
        ],
    )
    def test_pytest_decorators(self, source, decorator):
        declared = self._declared({"app/plugin.py": source})
        assert declared == {Path("app/plugin.py"): {"client": f"{decorator} on `client` (line 4)"}}

    def test_mark_on_a_class_covers_its_methods(self):
        source = "import pytest\n\n@pytest.mark.slow\nclass Suite:\n    def check(self): ...\n"
        declared = self._declared({"app/suite.py": source})[Path("app/suite.py")]
        assert declared["Suite.check"] == "@pytest.mark.slow on `Suite` (line 4)"

    def test_test_function_in_a_module_importing_pytest(self):
        source = (
            "import pytest\n\ndef test_answer(): ...\n\nclass Suite:\n    def test_m(self): ...\n"
        )
        declared = self._declared({"app/answers.py": source})[Path("app/answers.py")]
        assert declared["test_answer"] == (
            "`test_answer` is a test_ function in a module importing pytest (line 3)"
        )
        assert "test_m" in declared["Suite.test_m"]

    def test_test_function_without_pytest_is_not_a_test(self):
        assert self._declared({"app/answers.py": "def test_answer(): ...\n"}) == {}

    def test_test_function_nested_in_a_function_is_not_a_test(self):
        source = "import pytest\n\ndef build():\n    def test_inner(): ...\n"
        assert self._declared({"app/answers.py": source}) == {}

    def test_declaration_gives_its_file_the_role(self, tmp_path):
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        source = "import unittest\n\nclass Checks(unittest.TestCase): ...\n\ndef helper(): ...\n"
        ctx = _context({"src/app/checks.py": source, "src/app/core.py": PLAIN}, config)
        assert ctx.roles.files == {
            Path("src/app/checks.py"): "`Checks` subclasses unittest.TestCase (line 3)"
        }
        assert ctx.roles.of(Path("src/app/checks.py"), "helper") == (
            "`Checks` subclasses unittest.TestCase (line 3)"
        )
        assert ctx.roles.of(Path("src/app/core.py"), "helper") is None

    def test_config_reason_comes_first(self, tmp_path):
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context({"tests/test_a.py": "import pytest\n\ndef test_a(): ...\n"}, config)
        assert ctx.roles.files[Path("tests/test_a.py")].startswith("python_files ")
        assert ctx.roles.of(Path("tests/test_a.py"), "test_a").startswith("`test_a` is")


class TestMalformedConfig:
    @pytest.mark.parametrize(
        ("name", "text"),
        [
            ("pytest.ini", "testpaths = tests\n"),
            ("tox.ini", "[pytest]\ntestpaths = a\ntestpaths = b\n"),
            ("setup.cfg", "[tool:pytest\n"),
            ("pyproject.toml", "[tool.pytest.ini_options\n"),
        ],
    )
    def test_unparseable_file_fails_fast(self, tmp_path, name, text):
        (tmp_path / name).write_text(text, encoding="utf-8")
        with pytest.raises(PytestConfigError, match=name):
            load_pytest_config(tmp_path, tmp_path)

    def test_wrong_type_fails_fast(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = 3\n", encoding="utf-8"
        )
        with pytest.raises(PytestConfigError, match="'testpaths' must be a list"):
            load_pytest_config(tmp_path, tmp_path)

    def test_unbalanced_quote_fails_fast(self, tmp_path):
        (tmp_path / "pytest.ini").write_text('[pytest]\ntestpaths = "a\n', encoding="utf-8")
        with pytest.raises(PytestConfigError, match="cannot be split"):
            load_pytest_config(tmp_path, tmp_path)

    def test_cli_exits_1_naming_the_file(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "tox.ini").write_text("[pytest]\nx = 1\nx = 2\n", encoding="utf-8")
        (tmp_path / "a.py").write_text(PLAIN, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([str(tmp_path), "--no-context"])
        assert exc.value.code == 1
        assert f"Error: {tmp_path / 'tox.ini'}" in capsys.readouterr().err


class TestEntryPointPlugins:
    """``pytest11`` entry points in pyproject.toml name modules pytest registers as plugins."""

    def test_module_references_are_read(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "pyproject.toml": (
                    "[project.entry-points.pytest11]\n"
                    'timing = "myplugin.timing"\n'
                    'reports = " myplugin.reports "\n'
                )
            },
        )
        assert config.entry_point_plugins == ("myplugin.timing", "myplugin.reports")

    def test_read_with_a_pytest_config_too(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "pyproject.toml": (
                    "[tool.pytest.ini_options]\n"
                    'testpaths = ["tests"]\n'
                    "[project.entry-points.pytest11]\n"
                    'timing = "myplugin.timing"\n'
                )
            },
        )
        assert config.entry_point_plugins == ("myplugin.timing",)

    def test_attribute_reference_names_no_module(self, tmp_path):
        """``pkg.mod:Plugin`` registers the object, not the module's functions."""
        config = _config(
            tmp_path,
            {"pyproject.toml": '[project.entry-points.pytest11]\nx = "pkg.mod:Plugin"\n'},
        )
        assert config.entry_point_plugins == ()

    def test_no_pyproject_or_no_entry_points(self, tmp_path):
        assert load_pytest_config(tmp_path, tmp_path).entry_point_plugins == ()
        config = _config(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n'})
        assert config.entry_point_plugins == ()

    def test_wrong_type_fails_fast(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project.entry-points]\npytest11 = 3\n", encoding="utf-8"
        )
        with pytest.raises(PytestConfigError, match="pytest11"):
            load_pytest_config(tmp_path, tmp_path)

    def test_unparseable_pyproject_fails_fast_beside_pytest_ini(self, tmp_path):
        """pytest.ini wins the config, but the entry points still come from pyproject.toml."""
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
        with pytest.raises(PytestConfigError, match="pyproject.toml"):
            load_pytest_config(tmp_path, tmp_path)


class TestVerboseListing:
    def _run(self, tmp_path, monkeypatch, capsys, *args: str) -> str:
        (tmp_path / "pyproject.toml").write_text(PYPROJECT_TESTS, encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text(PLAIN, encoding="utf-8")
        (tmp_path / "tests" / "conftest.py").write_text(PLAIN, encoding="utf-8")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "test_quiz.py").write_text(PLAIN, encoding="utf-8")
        (tmp_path / "app" / "checks.py").write_text(
            "import unittest\n\nclass Checks(unittest.TestCase): ...\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "dead-code", *args])
        return capsys.readouterr().out

    def test_verbose_lists_test_role_files_and_why(self, tmp_path, monkeypatch, capsys):
        out = self._run(tmp_path, monkeypatch, capsys, "-v")
        assert out.splitlines()[:5] == [
            "Parsed 4 Python files",
            "Test role: 3 file(s); pytest config: pyproject.toml [tool.pytest.ini_options]",
            "  app/checks.py: `Checks` subclasses unittest.TestCase (line 3)",
            "  tests/conftest.py: conftest.py under testpaths 'tests'",
            "  tests/test_a.py: python_files 'test_*.py' (pytest default) under testpaths 'tests'",
        ]

    def test_without_verbose_nothing_is_listed(self, tmp_path, monkeypatch, capsys):
        out = self._run(tmp_path, monkeypatch, capsys)
        assert "Test role" not in out
        assert out.splitlines()[:2] == ["Parsed 4 Python files", ""]

    def test_git_history_verbose_lists_them_too(self, git_repo, monkeypatch, capsys):
        (git_repo / "test_a.py").write_text(PLAIN, encoding="utf-8")
        subprocess.run(["git", "add", "test_a.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add test_a"], cwd=git_repo, check=True)
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["git-history", str(git_repo), "--no-context", "-v"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[2:4] == [
            "Test role: 1 file(s); pytest config: none (pytest defaults)",
            "  test_a.py: python_files 'test_*.py' (pytest default) under the rootdir (no testpaths)",
        ]


HELPER_WITH_PYTEST = "import pytest\n\n\ndef make():\n    return 1\n"


class TestSupportModulesUnderTestpaths:
    """Under a test path, imported only by tests is enough: testpaths says where tests live."""

    def test_location_alone_makes_nothing_test_role(self, tmp_path):
        """Nothing imports these, so nothing is evidence: being under testpaths is not."""
        roles = _roles(
            tmp_path,
            {"pyproject.toml": PYPROJECT_TESTS},
            ["tests/__init__.py", "tests/fixtures/build.py", "src/app.py"],
        )
        assert roles == {}

    def test_support_under_testpaths_takes_the_role_on_import_evidence(self, tmp_path):
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {
                "tests/__init__.py": "",
                "tests/test_a.py": "from tests.fixtures.build import make\n",
                "tests/fixtures/__init__.py": "",
                "tests/fixtures/build.py": HELPER_WITH_PYTEST,
            },
            config,
        )
        roles = ctx.roles.files
        assert roles[Path("tests/fixtures/build.py")] == (
            "imported only by tests (tests/test_a.py); imports pytest"
        )
        # The packages a test's import passes through are imported only by tests.
        assert roles[Path("tests/fixtures/__init__.py")] == (
            "imported only by tests (tests/fixtures/build.py, tests/test_a.py);"
            " under testpaths 'tests'"
        )
        assert roles[Path("tests/__init__.py")] == (
            "imported only by tests (tests/fixtures/build.py, tests/test_a.py);"
            " under testpaths 'tests'"
        )

    def test_plain_helper_imported_only_by_tests_is_test_support(self, tmp_path):
        """It imports no test runner: under a test path, its importers are the evidence."""
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {"tests/test_a.py": "from tests.helpers import helper\n", "tests/helpers.py": PLAIN},
            config,
        )
        assert ctx.roles.files[Path("tests/helpers.py")] == (
            "imported only by tests (tests/test_a.py); under testpaths 'tests'"
        )

    def test_same_plain_helper_outside_testpaths_stays_production(self, tmp_path):
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {"tests/test_a.py": "from pkg.helpers import helper\n", "pkg/helpers.py": PLAIN},
            config,
        )
        assert set(ctx.roles.files) == {Path("tests/test_a.py")}

    def test_helper_a_production_module_imports_stays_production(self, tmp_path):
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {
                "tests/test_a.py": "from tests.helpers import helper\n",
                "src/app.py": "from tests.helpers import helper\n",
                "tests/helpers.py": PLAIN,
            },
            config,
        )
        assert set(ctx.roles.files) == {Path("tests/test_a.py")}

    def test_plain_helpers_helper_follows_to_a_fixed_point(self, tmp_path):
        """inner.py is listed first, so a single pass would visit it too early."""
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {
                "tests/inner.py": PLAIN,
                "tests/test_a.py": "from tests.outer import helper\n",
                "tests/outer.py": "from tests.inner import helper\n",
                "tests/shared.py": PLAIN,
                "tests/test_b.py": "from tests.shared import helper\n",
                "src/app.py": "from tests.shared import helper\n",
            },
            config,
        )
        roles = ctx.roles.files
        assert roles[Path("tests/outer.py")] == (
            "imported only by tests (tests/test_a.py); under testpaths 'tests'"
        )
        assert roles[Path("tests/inner.py")] == (
            "imported only by tests (tests/outer.py); under testpaths 'tests'"
        )
        # A production importer anywhere in the chain keeps the module production.
        assert Path("tests/shared.py") not in roles

    def test_import_evidence_is_not_read_outside_testpaths(self, tmp_path):
        """With testpaths, pytest searches only there, so nothing outside is inferred."""
        config = _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        ctx = _context(
            {
                "tests/test_a.py": "from pkg.testing import make\n",
                "pkg/testing.py": HELPER_WITH_PYTEST,
            },
            config,
        )
        assert set(ctx.roles.files) == {Path("tests/test_a.py")}


class TestSupportModulesByImports:
    """With no testpaths: imported only by tests, plus evidence of being test support."""

    def test_module_named_by_pytest_plugins_is_test(self):
        """pytest loads it as a plugin of the tests; nothing imports it."""
        ctx = _context(
            {
                "tests/conftest.py": 'pytest_plugins = ["tests.plugins.timing"]\n',
                "tests/plugins/timing.py": "def pytest_runtest_setup(item):\n    pass\n",
            }
        )
        assert ctx.roles.files[Path("tests/plugins/timing.py")] == (
            "named by pytest_plugins in tests/conftest.py"
        )

    def test_plugin_named_by_a_plugin_follows(self):
        ctx = _context(
            {
                "tests/conftest.py": 'pytest_plugins = "tests.outer"\n',
                "tests/outer.py": 'pytest_plugins = ("tests.inner",)\n',
                "tests/inner.py": PLAIN,
            }
        )
        assert ctx.roles.files[Path("tests/inner.py")] == (
            "named by pytest_plugins in tests/outer.py"
        )

    def test_plugin_a_production_module_imports_stays_production(self):
        ctx = _context(
            {
                "tests/conftest.py": 'pytest_plugins = ["shipped.fixtures"]\n',
                "shipped/fixtures.py": PLAIN,
                "shipped/api.py": "from shipped import fixtures\n",
            }
        )
        assert Path("shipped/fixtures.py") not in ctx.roles.files

    def test_pytest_plugins_in_a_production_module_is_no_evidence(self):
        ctx = _context(
            {
                "app/main.py": 'pytest_plugins = ["app.extra"]\n',
                "app/extra.py": PLAIN,
            }
        )
        assert Path("app/extra.py") not in ctx.roles.files

    def test_sibling_helper_importing_pytest_is_test(self):
        ctx = _context(
            {
                "tests/test_a.py": "from helpers import make\n",
                "tests/helpers.py": HELPER_WITH_PYTEST,
            }
        )
        assert ctx.roles.files[Path("tests/helpers.py")] == (
            "imported only by tests (tests/test_a.py); imports pytest"
        )

    def test_helper_importing_unittest_mock_is_test(self):
        ctx = _context(
            {
                "tests/test_a.py": "import tests.fakes\n",
                "tests/fakes.py": "from unittest import mock\n",
            }
        )
        assert ctx.roles.files[Path("tests/fakes.py")].endswith("imports unittest")

    def test_helper_importing_a_test_module_is_test(self):
        ctx = _context(
            {
                "tests/test_a.py": "from tests.shared import X\n",
                "tests/shared.py": "from tests.test_b import Y\nX = Y\n",
                "tests/test_b.py": "Y = 1\n",
            }
        )
        assert ctx.roles.files[Path("tests/shared.py")] == (
            "imported only by tests (tests/test_a.py); imports test module tests/test_b.py"
        )

    def test_helpers_helper_follows_to_a_fixed_point(self):
        """inner.py is listed first, so a single pass would visit it too early."""
        ctx = _context(
            {
                "tests/inner.py": PLAIN,
                "tests/test_a.py": "from tests.outer import make\n",
                "tests/outer.py": "import pytest\nfrom tests.inner import helper\n",
            }
        )
        roles = ctx.roles.files
        assert roles[Path("tests/outer.py")].endswith("imports pytest")
        assert roles[Path("tests/inner.py")] == (
            "imported only by tests (tests/outer.py); every importer is test support"
        )

    def test_helper_imported_by_a_test_and_production_stays_production(self):
        ctx = _context(
            {
                "tests/test_a.py": "from shared.util import make\n",
                "app/main.py": "from shared.util import make\n",
                "shared/util.py": HELPER_WITH_PYTEST,
            }
        )
        assert Path("shared/util.py") not in ctx.roles.files

    def test_module_only_tests_import_but_without_evidence_stays_production(self):
        """A CLI module, or code beside its own test, is imported only by its tests."""
        ctx = _context(
            {
                "pkg/billing.py": PLAIN,
                "pkg/test_billing.py": "import pkg.billing\n",
                "src/myapp/cli.py": PLAIN,
                "tests/test_cli.py": "from myapp.cli import helper\n",
            }
        )
        assert set(ctx.roles.files) == {Path("pkg/test_billing.py"), Path("tests/test_cli.py")}

    def test_helper_of_production_import_chain_does_not_inherit(self):
        """Inheriting needs every importer to be test support, not merely a test."""
        ctx = _context(
            {
                "tests/test_a.py": "import tests.outer\nimport tests.inner\n",
                "tests/outer.py": "import pytest\nimport tests.inner\n",
                "tests/inner.py": PLAIN,
            }
        )
        assert Path("tests/inner.py") not in ctx.roles.files

    def test_module_nothing_imports_stays_production(self):
        ctx = _context({"tests/test_a.py": PLAIN, "tests/loose.py": HELPER_WITH_PYTEST})
        assert Path("tests/loose.py") not in ctx.roles.files

    def test_verbose_shows_the_import_reason(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("from helpers import make\n", "utf-8")
        (tmp_path / "tests" / "helpers.py").write_text(HELPER_WITH_PYTEST, "utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "dead-code", "-v"])
        assert (
            "  tests/helpers.py: imported only by tests (tests/test_a.py); imports pytest"
            in capsys.readouterr().out.splitlines()
        )


# Some projects point testpaths at the package, with tests inside it
# (Phase 34): location there says nothing about which modules are tests.
# cli.py is the entry point: nothing imports it, as a console script's module.
PACKAGE_PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["shop"]\n'
PACKAGE = {
    "shop/__init__.py": "",
    "shop/billing.py": (
        "def total(items):\n    return sum(items)\n\n\ndef unused_rebate():\n    return 0\n"
    ),
    "shop/cli.py": "from shop.billing import total\n\n\ndef main():\n    print(total([1]))\n",
    "shop/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef cart():\n    return [1]\n",
    "shop/test_billing.py": (
        "from shop.billing import total\n"
        "from shop.testing import make_cart\n"
        "from shop.fakes import fake_items\n\n\n"
        "def test_total():\n    assert total(make_cart() + fake_items()) == 3\n"
    ),
    "shop/testing.py": (
        "import pytest\n\nfrom shop.builders import one\n\n\ndef make_cart():\n    return [one()]\n"
    ),
    "shop/builders.py": "def one():\n    return 1\n",
    "shop/fakes.py": "def fake_items():\n    return [1, 1]\n",
}


class TestPackageAsTestpaths:
    """testpaths names the package: what pytest collects, and what only tests import, is test."""

    def _roles(self, tmp_path) -> dict[Path, str]:
        config = _config(tmp_path, {"pyproject.toml": PACKAGE_PYPROJECT})
        return _context(PACKAGE, config).roles.files

    def test_production_modules_stay_production(self, tmp_path):
        """billing.py is imported by tests and by cli.py, which is production."""
        roles = self._roles(tmp_path)
        for name in ("shop/__init__.py", "shop/billing.py", "shop/cli.py"):
            assert Path(name) not in roles, name

    def test_collected_tests_and_conftest_are_test_role(self, tmp_path):
        roles = self._roles(tmp_path)
        assert roles[Path("shop/test_billing.py")] == (
            "python_files 'test_*.py' (pytest default) under testpaths 'shop'"
        )
        assert roles[Path("shop/conftest.py")] == "conftest.py under testpaths 'shop'"

    def test_pytest_importing_helper_used_only_by_tests_is_test_role(self, tmp_path):
        roles = self._roles(tmp_path)
        assert roles[Path("shop/testing.py")] == (
            "imported only by tests (shop/test_billing.py); imports pytest"
        )
        assert roles[Path("shop/builders.py")] == (
            "imported only by tests (shop/testing.py); under testpaths 'shop'"
        )

    def test_module_used_only_by_tests_is_test_support(self, tmp_path):
        """The known cost: a production module only tests import would read the same."""
        assert self._roles(tmp_path)[Path("shop/fakes.py")] == (
            "imported only by tests (shop/test_billing.py); under testpaths 'shop'"
        )

    def test_deleted_module_under_the_package_is_production(self, tmp_path):
        ctx = _context(PACKAGE, _config(tmp_path, {"pyproject.toml": PACKAGE_PYPROJECT}))
        assert not ctx.roles.is_test("shop/gone.py")
        assert ctx.roles.is_test("shop/test_gone.py")

    def test_production_findings_are_reported(self, tmp_path, monkeypatch, capsys):
        """The regression: every module took the role, and the repo reported nothing."""
        (tmp_path / "pyproject.toml").write_text(PACKAGE_PYPROJECT, encoding="utf-8")
        (tmp_path / "shop").mkdir()
        for name, source in PACKAGE.items():
            (tmp_path / name).write_text(source, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "dead-code"])
        findings = [line.strip() for line in capsys.readouterr().out.splitlines()]
        assert any(line.startswith("shop/billing.py:5: unused_rebate()") for line in findings)


class TestUnanalyzedPaths:
    """Git history names files that are gone: layer 1 alone classifies them."""

    def test_deleted_paths_take_the_pytest_config_role(self, tmp_path):
        ctx = _context(
            {"src/app.py": PLAIN}, _config(tmp_path, {"pyproject.toml": PYPROJECT_TESTS})
        )
        assert ctx.roles.is_test("tests/test_gone.py")
        # Location alone is no evidence, and a deleted helper has no imports to read.
        assert not ctx.roles.is_test("tests/gone_helper.py")
        assert not ctx.roles.is_test("src/gone.py")
        assert not ctx.roles.is_test("src/test_gone.py")

    def test_without_testpaths_a_deleted_helper_is_production(self):
        """There is no import graph for a deleted file, so only python_files can say test."""
        ctx = _context({"app.py": PLAIN})
        assert ctx.roles.is_test("tests/test_gone.py")
        assert not ctx.roles.is_test("tests/gone_helper.py")

    def test_analyzed_files_use_the_full_role(self):
        ctx = _context(
            {
                "tests/test_a.py": "from helpers import make\n",
                "tests/helpers.py": HELPER_WITH_PYTEST,
            }
        )
        assert ctx.roles.is_test(Path("tests/helpers.py"))
        assert ctx.roles.is_test("tests/helpers.py")
        # The same kind of helper, deleted, has no imports to read.
        assert not ctx.roles.is_test("tests/other_helpers.py")

    def test_an_analyzed_file_is_not_reclassified_by_layer_1(self):
        """An analyzed production file stays production: its full role was already taken."""
        ctx = _context({"src/app.py": PLAIN, "tests/test_a.py": PLAIN})
        assert ctx.roles.is_test("tests/test_a.py")
        assert not ctx.roles.is_test("src/app.py")


# The shapes of antipatterns' src/exams/ fixtures (Phase 24-4), trimmed to what
# decides their role. The antipatterns checkout is for corpus runs; these
# tests stand on their own.
ANTIPATTERNS_PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["src/tests"]\n'
EXAMS = {
    "src/__init__.py": "",
    "src/exams/__init__.py": '"""Certification exams: here a "test" is the product."""\n',
    "src/exams/test_bank.py": (
        "QUESTIONS = (('Q1', '36 in'),)\n\n\n"
        "def answer_key():\n"
        "    return dict(QUESTIONS)\n\n\n"
        "def test_taker_percentile(score, all_scores):\n"
        "    below = sum(1 for other in all_scores if other < score)\n"
        "    return round(100 * below / len(all_scores))\n"
    ),
    "src/exams/grading.py": (
        "from .test_bank import answer_key, test_taker_percentile\n\n\n"
        "def grade_attempt(answers, cohort_scores):\n"
        "    key = answer_key()\n"
        "    correct = sum(1 for qid, given in answers.items() if key.get(qid) == given)\n"
        "    return correct, test_taker_percentile(correct, cohort_scores)\n"
    ),
    "src/exams/selfcheck.py": (
        "import unittest\n\n"
        "from .grading import grade_attempt\n"
        "from .test_bank import QUESTIONS\n\n\n"
        "class AnswerKeySelfCheck(unittest.TestCase):\n"
        "    def test_perfect_attempt_scores_full_marks(self):\n"
        "        answers = dict(QUESTIONS)\n"
        "        self.assertEqual(grade_attempt(answers, [0, 1])[0], len(QUESTIONS))\n"
    ),
    "src/tests/__init__.py": "",
    "src/tests/test_billing.py": "def test_cart_total():\n    assert 1 + 1 == 2\n",
}
TEST_BANK = Path("src/exams/test_bank.py")
SELFCHECK = Path("src/exams/selfcheck.py")


class TestAntipatternsExamsFixtures:
    """antipatterns' TestCase outside tests/ is a test; its test_* production module is not."""

    def _ctx(self, tmp_path, config_files: dict[str, str]) -> AnalysisContext:
        return _context(EXAMS, _config(tmp_path, config_files))

    def test_testcase_subclass_outside_tests_takes_the_role(self, tmp_path):
        ctx = self._ctx(tmp_path, {"pyproject.toml": ANTIPATTERNS_PYPROJECT})
        assert ctx.roles.files[SELFCHECK] == (
            "`AnswerKeySelfCheck` subclasses unittest.TestCase (line 7)"
        )
        assert ctx.roles.of(SELFCHECK, "AnswerKeySelfCheck.test_perfect_attempt_scores_full_marks")

    def test_test_named_production_module_stays_production(self, tmp_path):
        ctx = self._ctx(tmp_path, {"pyproject.toml": ANTIPATTERNS_PYPROJECT})
        assert TEST_BANK not in ctx.roles.files
        assert ctx.roles.of(TEST_BANK, "test_taker_percentile") is None
        # src/tests/__init__.py is empty and nothing imports it: no evidence, so production.
        assert set(ctx.roles.files) == {SELFCHECK, Path("src/tests/test_billing.py")}

    def test_without_testpaths_pytest_would_collect_the_test_named_module(self, tmp_path):
        """What antipatterns' testpaths is for: pytest's default python_files matches the name."""
        ctx = self._ctx(tmp_path, {})
        assert ctx.roles.files[TEST_BANK] == (
            "python_files 'test_*.py' (pytest default) under the rootdir (no testpaths)"
        )

    def test_dead_code_counts_the_testcase_as_a_test_caller(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "pyproject.toml").write_text(ANTIPATTERNS_PYPROJECT, encoding="utf-8")
        for name, source in EXAMS.items():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(source, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "dead-code"])
        findings = [line.strip() for line in capsys.readouterr().out.splitlines()]
        expected = (
            "src/exams/grading.py:4: grade_attempt(): no production callers; still referenced "
            "by src/exams/selfcheck.py:10 — test-only surface. Removing "
            "grade_attempt() also frees 3: QUESTIONS (src/exams/test_bank.py:1), answer_key() "
            "(src/exams/test_bank.py:4), test_taker_percentile() (src/exams/test_bank.py:8). "
            "Tests that use it, to delete or rewrite: src/exams/selfcheck.py (every test). Fix: "
        )
        assert any(line.startswith(expected) for line in findings), findings
        assert not [line for line in findings if line.startswith(str(TEST_BANK))]


README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


class TestReadmeDocumentsTheRole:
    """README's test-role section names every config source and key the role reads."""

    @pytest.mark.parametrize(
        ("name", "text", "section"),
        [
            ("pytest.ini", "[pytest]\n", "pytest"),
            (".pytest.ini", "[pytest]\n", "pytest"),
            ("pyproject.toml", "[tool.pytest.ini_options]\n", "tool.pytest.ini_options"),
            ("tox.ini", "[pytest]\n", "pytest"),
            ("setup.cfg", "[tool:pytest]\n", "tool:pytest"),
        ],
    )
    def test_each_config_source_is_documented(self, tmp_path, name, text, section):
        config = _config(tmp_path, {name: text})
        assert config.source == f"{name} [{section}]"
        assert f"`{name}`" in README
        assert f"`[{section}]`" in README

    @pytest.mark.parametrize("key", ["testpaths", "python_files", "pythonpath"])
    def test_each_pytest_key_is_documented(self, key):
        assert f"`{key}`" in README

    @pytest.mark.parametrize(
        "rule",
        [
            "### Tests and the test role",
            "**Definitions declare themselves.**",
            "**The filename alone is never evidence.**",
            "**Deleted paths.**",
            '**`exclude` means "never look".**',
        ],
    )
    def test_each_rule_has_its_paragraph(self, rule):
        assert rule in README

    @pytest.mark.parametrize("module", sorted(TEST_RUNNER_MODULES))
    def test_each_test_runner_module_is_documented(self, module):
        assert f"`{module}`" in README

    @pytest.mark.parametrize(
        "route",
        [
            "test-role modules import, and nothing else, is test support",
            "it imports a test-role module",
            "every module importing it is itself test support",
        ],
    )
    def test_each_support_evidence_route_is_documented(self, route):
        """The support routes in ``roles._TestSupport._evidence``, read with or without testpaths."""
        assert route in " ".join(README.split())
