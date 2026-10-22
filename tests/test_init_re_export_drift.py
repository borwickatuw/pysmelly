"""init-re-export-drift: __init__.py re-exports that drifted from the code behind them."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from pysmelly.checks.imports import _unused_message, check_init_re_export_drift
from pysmelly.context import AnalysisContext
from pysmelly.project_entry_points import DeclaredEntryPoint, DeclaredKind
from pysmelly.pytest_config import NO_PYTEST_CONFIG


def _ctx(
    files: dict[str, str],
    base: Path,
    declared: tuple[DeclaredEntryPoint, ...] = (),
) -> AnalysisContext:
    """A context over ``files``, whose paths are relative to ``base`` on disk."""
    return AnalysisContext(
        {Path(name): ast.parse(code) for name, code in files.items()},
        verbose=False,
        pytest_config=dataclasses.replace(NO_PYTEST_CONFIG, rootdir=base, base=base),
        declared_entry_points=declared,
    )


def _messages(files: dict[str, str], base: Path, **kwargs) -> list[tuple[str, int, str]]:
    findings = check_init_re_export_drift(_ctx(files, base, **kwargs))
    return [(f.file, f.line, f.message) for f in findings]


# A package whose one re-export a consumer reaches through the package path.
_USED = {
    "pkg/__init__.py": "from .impl import helper\n",
    "pkg/impl.py": "def helper(): ...\n",
    "app.py": "from pkg import helper\n",
}


class TestStaleReExport:
    def test_name_the_submodule_no_longer_defines(self, tmp_path):
        files = {
            **_USED,
            "pkg/__init__.py": "from .impl import helper\nfrom .impl import renamed\n",
            "other.py": "from pkg import renamed\n",
        }
        [(file, line, message)] = _messages(files, tmp_path)
        assert (file, line) == ("pkg/__init__.py", 2)
        assert "re-exports `renamed` from `pkg.impl`" in message
        assert "`pkg.impl` no longer defines it" in message
        assert "raises ImportError" in message

    def test_module_the_project_no_longer_has(self, tmp_path):
        files = {**_USED, "pkg/__init__.py": "from .impl import helper\nfrom .gone import old\n"}
        [(_, line, message)] = [m for m in _messages(files, tmp_path) if "gone" in m[2]]
        assert line == 2
        assert "`pkg.gone` is no longer a module of the project" in message

    def test_module_left_out_of_the_analysis_is_not_missing(self, tmp_path):
        """An excluded or untracked module still exists on disk."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "generated.py").write_text("old = 1\n")
        files = {
            **_USED,
            "pkg/__init__.py": "from .impl import helper\nfrom .generated import old\n",
            "other.py": "from pkg import old\n",
        }
        assert _messages(files, tmp_path) == []

    def test_subpackage_left_out_of_the_analysis_is_not_missing(self, tmp_path):
        (tmp_path / "pkg" / "vendored" / "lib").mkdir(parents=True)
        files = {
            **_USED,
            "pkg/__init__.py": "from .impl import helper\nfrom .vendored.lib import old\n",
            "other.py": "from pkg import old\n",
        }
        assert _messages(files, tmp_path) == []

    def test_library_import_is_not_a_project_module(self, tmp_path):
        files = {**_USED, "pkg/__init__.py": "from .impl import helper\nfrom yaml import load\n"}
        assert _messages(files, tmp_path) == []

    def test_guarded_import_may_fail(self, tmp_path):
        """A try that catches the ImportError expects the import to fail."""
        files = {
            **_USED,
            "pkg/__init__.py": (
                "from .impl import helper\n"
                "try:\n    from ._version import __version__\n"
                "except ImportError:\n    __version__ = '0+unknown'\n"
            ),
        }
        assert _messages(files, tmp_path) == []

    def test_all_entry_nothing_binds(self, tmp_path):
        files = {
            **_USED,
            "pkg/__init__.py": "from .impl import helper\n__all__ = [\n    'helper',\n    'gone',\n]\n",
        }
        [(_, line, message)] = _messages(files, tmp_path)
        assert line == 4
        assert "`__all__` lists `gone`, which `pkg` no longer binds" in message
        assert "raises AttributeError" in message

    def test_all_entry_a_submodule_or_star_import_binds(self, tmp_path):
        files = {
            **_USED,
            "pkg/__init__.py": (
                "from .impl import helper\nfrom .more import *\n__all__ = ['helper', 'sub', 'extra']\n"
            ),
            "pkg/sub.py": "",
            "pkg/more.py": "extra = 1\n",
            "other.py": "from pkg import extra\n",
        }
        assert _messages(files, tmp_path) == []

    def test_all_entry_bound_by_a_fallback_is_bound(self, tmp_path):
        """30-1 caution 1: the handler's assignment binds the name too."""
        files = {
            **_USED,
            "pkg/__init__.py": (
                "from .impl import helper\n"
                "try:\n    from .fast import speed\n"
                "except ImportError:\n    speed = None\n"
                "__all__ = ['helper', 'speed']\n"
            ),
            "other.py": "from pkg import speed\n",
        }
        assert _messages(files, tmp_path) == []

    def test_only_init_files_are_read(self, tmp_path):
        files = {"pkg/__init__.py": "", "pkg/mod.py": "__all__ = ['gone']\n"}
        assert _messages(files, tmp_path) == []


class TestUnusedReExport:
    def test_used_through_the_package_path(self, tmp_path):
        assert _messages(_USED, tmp_path) == []

    def test_consumers_bypass_the_package(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .impl import a, b\nfrom .other import c\n",
            "pkg/impl.py": "def a(): ...\ndef b(): ...\n",
            "pkg/other.py": "def c(): ...\n",
            "app.py": "from pkg.impl import a, b\nfrom pkg.other import c\n",
        }
        findings = check_init_re_export_drift(_ctx(files, tmp_path))
        [finding] = findings
        assert (finding.file, finding.line) == ("pkg/__init__.py", 1)
        assert [site.line for site in finding.sites] == [1, 1, 2]
        assert finding.message == (
            "`pkg` re-exports `a`, `b` (from `.impl`); `c` (from `.other`), but no analyzed "
            "module imports them through `pkg`; no test files were analyzed, so a test that "
            "imports them would not be seen (include the tests in the analysis to rule that out)"
        )

    def test_no_excluded_tests_note_when_tests_are_analyzed(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .impl import a\n",
            "pkg/impl.py": "def a(): ...\n",
            "app.py": "from pkg.impl import a\n",
            "tests/test_app.py": "from pkg.impl import a\n\ndef test_a():\n    a()\n",
        }
        [(_file, _line, message)] = _messages(files, tmp_path)
        assert message == (
            "`pkg` re-exports `a` (from `.impl`), but no analyzed module imports it through `pkg`"
        )

    def test_message_rerenders_for_the_sites_left(self):
        from pysmelly.checks.imports import _ReExport

        unused = [_ReExport("a", 1, "pkg.impl"), _ReExport("c", 2, "shared.base")]
        message = _unused_message("pkg", unused, (1,), tests_seen=True)
        assert message == (
            "`pkg` re-exports `c` (from `shared.base`), but no analyzed module imports it "
            "through `pkg`"
        )

    def test_attribute_on_the_package_module(self, tmp_path):
        files = {**_USED, "app.py": "import pkg\npkg.helper()\n"}
        assert _messages(files, tmp_path) == []

    def test_attribute_on_a_package_imported_from_its_parent(self, tmp_path):
        files = {
            "top/__init__.py": "",
            "top/pkg/__init__.py": "from .impl import helper\n",
            "top/pkg/impl.py": "def helper(): ...\n",
            "app.py": "from top import pkg\npkg.helper()\n",
        }
        assert _messages(files, tmp_path) == []

    def test_star_import_of_the_package(self, tmp_path):
        files = {**_USED, "app.py": "from pkg import *\nhelper()\n"}
        assert _messages(files, tmp_path) == []

    def test_star_import_outside_all_does_not_reach_it(self, tmp_path):
        files = {
            **_USED,
            "pkg/__init__.py": "from .impl import helper\n__all__ = []\n",
            "app.py": "from pkg import *\n",
        }
        [(_, _, message)] = _messages(files, tmp_path)
        assert "re-exports `helper`" in message

    def test_dotted_string_names_it(self, tmp_path):
        files = {**_USED, "app.py": "TARGET = 'pkg.helper'\n"}
        assert _messages(files, tmp_path) == []

    def test_entry_point_names_it(self, tmp_path):
        declared = (
            DeclaredEntryPoint(
                DeclaredKind.CONSOLE_SCRIPT,
                "project.scripts",
                "tool",
                "pkg:helper",
                "pkg",
                "helper",
            ),
        )
        files = {k: v for k, v in _USED.items() if k != "app.py"}
        assert _messages(files, tmp_path, declared=declared) == []

    def test_shorter_sys_path_name_reaches_the_package(self, tmp_path):
        """A Django project imports ``site/works/views`` as ``works.views``."""
        files = {
            "site/works/__init__.py": "",
            "site/works/views/__init__.py": "from .hls import playlist\n",
            "site/works/views/hls.py": "def playlist(): ...\n",
            "site/urls.py": "from works.views import playlist\n",
        }
        assert _messages(files, tmp_path) == []

    def test_chained_package_re_export_counts_as_a_use(self, tmp_path):
        files = {
            "top/__init__.py": "from .pkg import helper\n",
            "top/pkg/__init__.py": "from .impl import helper\n",
            "top/pkg/impl.py": "def helper(): ...\n",
            "app.py": "from top import helper\n",
        }
        assert _messages(files, tmp_path) == []

    def test_name_the_init_reads_itself_is_not_a_re_export(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .impl import Handler\nHANDLERS = [Handler()]\n",
            "pkg/impl.py": "class Handler: ...\n",
        }
        assert _messages(files, tmp_path) == []

    def test_submodule_binding_is_not_a_name_re_export(self, tmp_path):
        files = {"pkg/__init__.py": "from . import signals\n", "pkg/signals.py": ""}
        assert _messages(files, tmp_path) == []

    def test_library_name_is_not_reported(self, tmp_path):
        files = {"pkg/__init__.py": "from json import loads\n"}
        assert _messages(files, tmp_path) == []

    def test_dunder_is_not_reported(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .about import __version__\n",
            "pkg/about.py": "__version__ = '1'\n",
        }
        assert _messages(files, tmp_path) == []

    def test_shadowed_binding_is_not_the_re_export(self, tmp_path):
        files = {
            "pkg/__init__.py": "from .impl import helper\ndef helper(): ...\n",
            "pkg/impl.py": "def helper(): ...\n",
        }
        assert _messages(files, tmp_path) == []

    def test_celery_app_is_imported_to_create_it(self, tmp_path):
        """Celery's Django integration: ``from .celery import app as celery_app``."""
        files = {
            "proj/__init__.py": "from .celery import app as celery_app\n__all__ = ('celery_app',)\n",
            "proj/celery.py": "from celery import Celery\napp = Celery('proj')\n",
        }
        assert _messages(files, tmp_path) == []

    def test_other_app_object_is_reported(self, tmp_path):
        files = {
            "proj/__init__.py": "from .server import app as web_app\n",
            "proj/server.py": "from flask import Flask\napp = Flask('proj')\n",
        }
        [(_, _, message)] = _messages(files, tmp_path)
        assert "re-exports `web_app` (from `.server`)" in message
