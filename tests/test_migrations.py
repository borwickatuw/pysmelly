"""Tests for half-finished-migration, stalled-migration and self-deprecated."""

from __future__ import annotations

import ast
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.checks.migrations import (
    _name_link,
    check_half_finished_migration,
    check_self_deprecated,
    check_stalled_migration,
)
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, GIT_HISTORY_CATEGORY, Severity

PARSERS = """\
def parse(text):
    return text.split()


def parse_v2(text):
    return text.split(",")
"""

CALLS_BOTH = """\
from pkg.parsers import parse, parse_v2


def load(text):
    return parse(text)


def load_csv(text):
    return parse_v2(text)
"""

CALLS_NEW = """\
from pkg.parsers import parse_v2


def load_csv(text):
    return parse_v2(text)
"""

TEST_CALLS_OLD = """\
from pkg.parsers import parse


def test_parse():
    assert parse("a b") == ["a", "b"]
"""


def _files(**modules: str) -> dict[str, str]:
    files = {"pkg/__init__.py": "", "pkg/parsers.py": PARSERS}
    files.update({f"pkg/{name}.py": code for name, code in modules.items()})
    return files


def _ctx(files: dict[str, str]) -> AnalysisContext:
    trees = {Path(name): ast.parse(code) for name, code in files.items()}
    return AnalysisContext(trees, verbose=False)


class TestNameLink:
    @pytest.mark.parametrize(
        ("name", "stem", "generation"),
        [
            ("parse", "parse", 1),
            ("parse_v2", "parse", 2),
            ("_parse_v3", "_parse", 3),
            ("ClientV2", "Client", 2),
            ("fetch_legacy", "fetch", 0),
            ("legacy_fetch", "fetch", 0),
            ("LegacyClient", "Client", 0),
            ("old_render", "render", 0),
            ("render_new", "render", 2),
            ("NewClient", "Client", 2),
            ("parse2", "parse", 2),
        ],
    )
    def test_markers(self, name, stem, generation):
        link = _name_link(name)
        assert (link.stem, link.generation) == (stem, generation)

    @pytest.mark.parametrize("name", ["sha256", "int32", "base64", "x509"])
    def test_numbers_that_are_not_versions(self, name):
        assert _name_link(name).marker == ""

    def test_single_digit_links_only_to_a_bare_stem_in_its_module(self):
        """``utf8`` reads as generation 8 of ``utf``, which no module defines."""
        code = "def utf8(b):\n    return b\n\n\ndef show(b):\n    return utf8(b)\n"
        assert check_half_finished_migration(_ctx({"pkg/__init__.py": "", "pkg/t.py": code})) == []


class TestHalfFinishedMigration:
    def test_registered_low_ast(self):
        assert CHECK_SEVERITY["half-finished-migration"] is Severity.LOW
        assert CHECK_CATEGORIES["half-finished-migration"] == "ast"

    def test_both_members_called_in_production(self):
        findings = check_half_finished_migration(_ctx(_files(loader=CALLS_BOTH)))
        assert [(f.file, f.line) for f in findings] == [("pkg/parsers.py", 1)]
        message = findings[0].message
        assert "parse() and parse_v2() (line 5)" in message
        assert "'_v2' suffix" in message
        assert "parse() has 1 production caller(s), 0 test caller(s)" in message
        assert message.endswith("parse_v2() has 1 production caller(s)")

    def test_old_member_with_only_test_callers_does_not_fire(self):
        files = _files(loader=CALLS_NEW)
        files["tests/test_parsers.py"] = TEST_CALLS_OLD
        assert check_half_finished_migration(_ctx(files)) == []

    def test_old_member_with_no_callers_does_not_fire(self):
        assert check_half_finished_migration(_ctx(_files(loader=CALLS_NEW))) == []

    def test_new_delegating_to_old_is_not_a_production_caller(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/parsers.py": "def parse(t):\n    return t\n\n\n"
            "def parse_v2(t):\n    return parse(t)\n",
            "pkg/loader.py": CALLS_NEW,
        }
        assert check_half_finished_migration(_ctx(files)) == []

    def test_same_name_elsewhere_is_not_a_caller(self):
        """A call to another module's ``parse`` does not resolve to this one."""
        files = _files(
            loader="from other import parse\nfrom pkg.parsers import parse_v2\n\n\n"
            "def f(t):\n    return parse(t), parse_v2(t)\n"
        )
        assert check_half_finished_migration(_ctx(files)) == []

    def test_legacy_class_and_its_replacement(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/clients.py": "class LegacyClient:\n    pass\n\n\nclass Client:\n    pass\n",
            "pkg/app.py": "from pkg.clients import Client, LegacyClient\n\n\n"
            "def make(flag):\n    return LegacyClient() if flag else Client()\n",
        }
        findings = check_half_finished_migration(_ctx(files))
        assert [(f.file, f.line) for f in findings] == [("pkg/clients.py", 1)]
        assert "'Legacy' prefix" in findings[0].message

    def test_loose_markers_do_not_link_to_each_other(self):
        """``old_lines``/``new_lines`` are a diff's two sides, not a migration."""
        code = (
            "def old_lines(h):\n    return h\n\n\ndef new_lines(h):\n    return h\n\n\n"
            "def show(h):\n    return old_lines(h), new_lines(h)\n"
        )
        assert check_half_finished_migration(_ctx({"pkg/__init__.py": "", "pkg/d.py": code})) == []

    def test_loose_marker_links_only_within_its_module(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "def render(x):\n    return x\n",
            "pkg/b.py": "def render2(x):\n    return x\n",
            "pkg/app.py": "from pkg.a import render\nfrom pkg.b import render2\n\n\n"
            "def f(x):\n    return render(x), render2(x)\n",
        }
        assert check_half_finished_migration(_ctx(files)) == []

    def test_methods_in_one_class(self):
        code = (
            "class Store:\n"
            "    def save(self, x):\n        return x\n\n"
            "    def save_v2(self, x):\n        return x\n\n\n"
            "def use(store):\n    store.save(1)\n    store.save_v2(2)\n"
        )
        findings = check_half_finished_migration(_ctx({"pkg/__init__.py": "", "pkg/s.py": code}))
        assert [f.line for f in findings] == [2]
        assert "Store.save() and Store.save_v2()" in findings[0].message

    def test_method_sharing_its_name_with_another_definition_is_unresolved(self):
        code = (
            "class Store:\n"
            "    def save(self, x):\n        return x\n\n"
            "    def save_v2(self, x):\n        return x\n\n\n"
            "class Other:\n    def save(self, x):\n        return x\n\n\n"
            "def use(store):\n    store.save(1)\n    store.save_v2(2)\n"
        )
        assert check_half_finished_migration(_ctx({"pkg/__init__.py": "", "pkg/s.py": code})) == []

    def test_module_pair(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/client.py": "def fetch(u):\n    return u\n",
            "pkg/client_v2.py": "def fetch(u):\n    return u\n",
            "pkg/app.py": "from pkg import client, client_v2\n\n\n"
            "def f(u):\n    return client.fetch(u), client_v2.fetch(u)\n",
        }
        findings = check_half_finished_migration(_ctx(files))
        assert [(f.file, f.line) for f in findings] == [("pkg/client.py", 1)]
        assert "pkg/client.py → pkg/client_v2.py" in findings[0].message

    @pytest.mark.parametrize(
        ("old", "new"),
        [("pkg/legacy/views.py", "pkg/views.py"), ("pkg/v1/api.py", "pkg/v2/api.py")],
    )
    def test_marked_package_pair(self, old, new):
        old_mod, new_mod = (path[:-3].replace("/", ".") for path in (old, new))
        files = {
            "pkg/__init__.py": "",
            old: "def show(r):\n    return r\n",
            new: "def show(r):\n    return r\n",
            "pkg/app.py": f"import {old_mod}\nimport {new_mod}\n\n\n"
            f"def f(r):\n    return {old_mod}.show(r), {new_mod}.show(r)\n",
        }
        findings = check_half_finished_migration(_ctx(files))
        assert [(f.file, f.line) for f in findings] == [(old, 1)]
        assert "package" in findings[0].message

    def test_unmarked_modules_sharing_a_name_do_not_pair(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "def fetch(u):\n    return u\n",
            "pkg/b.py": "def fetch(u):\n    return u\n",
            "pkg/app.py": "from pkg import a, b\n\n\ndef f(u):\n    return a.fetch(u), b.fetch(u)\n",
        }
        assert check_half_finished_migration(_ctx(files)) == []


DEPRECATED = """\
import warnings


def old_api(x):
    warnings.warn("use new_api", DeprecationWarning, stacklevel=2)
    return x
"""


def _deprecated(code: str, caller: str = "from pkg.api import old_api\n\n\ndef f():\n"):
    files = {
        "pkg/__init__.py": "",
        "pkg/api.py": code,
        "pkg/app.py": caller + "    return old_api(1)\n",
    }
    return check_self_deprecated(_ctx(files))


class TestSelfDeprecated:
    def test_registered_medium_ast(self):
        assert CHECK_SEVERITY["self-deprecated"] is Severity.MEDIUM
        assert CHECK_CATEGORIES["self-deprecated"] == "ast"

    def test_deprecation_warning_with_a_production_caller(self):
        findings = _deprecated(DEPRECATED)
        assert [(f.file, f.line) for f in findings] == [("pkg/api.py", 4)]
        message = findings[0].message
        assert "old_api() is deprecated (warnings.warn(..., DeprecationWarning) at line 5)" in (
            message
        )
        assert "1 caller(s) to migrate — pkg/app.py:5" in message

    def test_category_keyword_and_aliased_import(self):
        code = (
            "from warnings import warn as w\n\n\n"
            "def old_api(x):\n    w('gone', category=PendingDeprecationWarning)\n    return x\n"
        )
        assert len(_deprecated(code)) == 1

    def test_non_deprecation_category_does_not_fire(self):
        assert _deprecated(DEPRECATED.replace("DeprecationWarning", "RuntimeWarning")) == []

    def test_deprecation_category_shadowed_by_an_import_does_not_fire(self):
        code = DEPRECATED.replace(
            "import warnings\n", "import warnings\nfrom mylib import DeprecationWarning\n"
        )
        assert _deprecated(code) == []

    def test_warn_that_is_not_warnings_warn_does_not_fire(self):
        code = DEPRECATED.replace("import warnings\n", "import logger as warnings\n")
        assert _deprecated(code) == []

    def test_conditional_warning_deprecates_an_argument_not_the_function(self):
        code = (
            "import warnings\n\n\ndef old_api(x, legacy=None):\n"
            "    if legacy is not None:\n"
            "        warnings.warn('legacy', DeprecationWarning)\n    return x\n"
        )
        assert _deprecated(code) == []

    def test_project_subclass_of_deprecation_warning(self):
        code = (
            "import warnings\n\n\nclass RemovedInV3(DeprecationWarning):\n    pass\n\n\n"
            "def old_api(x):\n    warnings.warn('gone', RemovedInV3)\n    return x\n"
        )
        assert len(_deprecated(code)) == 1

    def test_deprecated_decorator(self):
        code = (
            "from typing_extensions import deprecated\n\n\n"
            "@deprecated('use new_api')\ndef old_api(x):\n    return x\n"
        )
        findings = _deprecated(code)
        assert len(findings) == 1
        assert "@deprecated at line 4" in findings[0].message

    def test_other_decorator_named_deprecated_does_not_fire(self):
        code = "from mylib import deprecated\n\n\n@deprecated('x')\ndef old_api(x):\n    return x\n"
        assert _deprecated(code) == []

    def test_deprecated_init_deprecates_the_class(self):
        code = (
            "import warnings\n\n\nclass OldRunner:\n    def __init__(self):\n"
            "        warnings.warn('use Runner', DeprecationWarning)\n"
        )
        files = {
            "pkg/__init__.py": "",
            "pkg/api.py": code,
            "pkg/app.py": "from pkg.api import OldRunner\n\n\ndef f():\n    return OldRunner()\n",
        }
        findings = check_self_deprecated(_ctx(files))
        assert [(f.line, f.message.split(" ")[0]) for f in findings] == [(4, "OldRunner")]

    def test_only_test_callers_do_not_fire(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/api.py": DEPRECATED,
            "tests/test_api.py": "from pkg.api import old_api\n\n\n"
            "def test_old():\n    assert old_api(1) == 1\n",
        }
        assert check_self_deprecated(_ctx(files)) == []

    def test_caller_inside_another_deprecated_definition_does_not_count(self):
        code = DEPRECATED + (
            "\n\ndef older_api(x):\n"
            "    warnings.warn('use new_api', DeprecationWarning)\n    return old_api(x)\n"
        )
        files = {"pkg/__init__.py": "", "pkg/api.py": code}
        assert check_self_deprecated(_ctx(files)) == []


_OLD = 400
_YOUNG = 30


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _commit(repo: Path, files: dict[str, str], days_ago: int) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "--", name], cwd=repo, capture_output=True, check=True)
    stamp = _when(days_ago).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "commit", "-q", "-m", "work"], cwd=repo, env=env, capture_output=True, check=True
    )


def _git_ctx(repo: Path) -> AnalysisContext:
    trees = {
        path.relative_to(repo): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(repo.rglob("*.py"))
        if ".git" not in path.parts
    }
    return AnalysisContext(trees, verbose=False, git=GitSettings(root=repo))


THREE_CALLS = """\
from pkg.parsers import parse, parse_v2


def a(t):
    return parse(t)


def b(t):
    return parse(t)


def c(t):
    return parse_v2(t)
"""


class TestStalledMigration:
    def test_registered_medium_git_history(self):
        assert CHECK_SEVERITY["stalled-migration"] is Severity.MEDIUM
        assert CHECK_CATEGORIES["stalled-migration"] == GIT_HISTORY_CATEGORY

    def test_no_git_history_reports_nothing(self):
        assert check_stalled_migration(_ctx(_files(loader=CALLS_BOTH))) == []

    def test_old_pair_with_no_caller_removed_is_stalled(self, git_repo):
        _commit(git_repo, _files(loader=CALLS_BOTH), _OLD)
        findings = check_stalled_migration(_git_ctx(git_repo))
        assert [(f.file, f.line, f.severity) for f in findings] == [
            ("pkg/parsers.py", 1, Severity.MEDIUM)
        ]
        message = findings[0].message
        assert f"parse_v2() was written {_when(_OLD):%Y-%m-%d} ({_OLD} days ago)" in message
        assert "no commit in the last 90 days removed a production call of parse()" in message

    def test_young_replacement_is_not_stalled(self, git_repo):
        _commit(git_repo, _files(loader=CALLS_BOTH), _YOUNG)
        assert check_stalled_migration(_git_ctx(git_repo)) == []

    def test_caller_removed_recently_is_moving(self, git_repo):
        _commit(git_repo, _files(loader=THREE_CALLS), _OLD)
        _commit(
            git_repo,
            {"pkg/loader.py": THREE_CALLS.replace("return parse(t)", "return 0", 1)},
            _YOUNG,
        )
        assert check_stalled_migration(_git_ctx(git_repo)) == []

    def test_caller_removed_long_ago_is_still_stalled(self, git_repo):
        _commit(git_repo, _files(loader=THREE_CALLS), _OLD)
        _commit(
            git_repo, {"pkg/loader.py": THREE_CALLS.replace("return parse(t)", "return 0", 1)}, 200
        )
        assert len(check_stalled_migration(_git_ctx(git_repo))) == 1

    def test_test_caller_removed_recently_does_not_count(self, git_repo):
        files = _files(loader=CALLS_BOTH)
        files["tests/test_parsers.py"] = TEST_CALLS_OLD
        _commit(git_repo, files, _OLD)
        _commit(git_repo, {"tests/test_parsers.py": "def test_nothing():\n    pass\n"}, _YOUNG)
        assert len(check_stalled_migration(_git_ctx(git_repo))) == 1

    def test_reformatted_call_is_not_a_removal(self, git_repo):
        _commit(git_repo, _files(loader=CALLS_BOTH), _OLD)
        _commit(
            git_repo,
            {"pkg/loader.py": CALLS_BOTH.replace("return parse(text)", "return parse( text )")},
            _YOUNG,
        )
        assert len(check_stalled_migration(_git_ctx(git_repo))) == 1

    def test_uncommitted_replacement_is_not_stalled(self, git_repo):
        _commit(
            git_repo,
            {"pkg/__init__.py": "", "pkg/parsers.py": "def parse(t):\n    return t\n"},
            _OLD,
        )
        (git_repo / "pkg/parsers.py").write_text(PARSERS, encoding="utf-8")
        (git_repo / "pkg/loader.py").write_text(CALLS_BOTH, encoding="utf-8")
        assert check_stalled_migration(_git_ctx(git_repo)) == []
