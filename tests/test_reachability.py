"""Tests for unreachable-from-entry-points (``checks/reachability.py``)."""

import ast
import textwrap
from pathlib import Path

import pytest

from pysmelly.checks.reachability import NO_ROOTS_NOTE, check_unreachable_from_entry_points
from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.discovery import SHEBANG
from pysmelly.project_entry_points import load_project_entry_points
from pysmelly.registry import Finding, Site

CHECK = "unreachable-from-entry-points"

# A script whose main() is the only root; each case adds what main reaches.
MAIN_GUARD = '\n\nif __name__ == "__main__":\n    main()\n'


def _findings(files: dict[str, str]) -> list[Finding]:
    sources = {Path(name): textwrap.dedent(code) for name, code in files.items()}
    trees = {path: ast.parse(source) for path, source in sources.items()}
    shebangs = frozenset(path for path, source in sources.items() if source.startswith(SHEBANG))
    ctx = AnalysisContext(trees, verbose=False)
    ctx.shebang_files = shebangs
    return check_unreachable_from_entry_points(ctx)


def _script(body: str) -> str:
    """``app.py``'s source: ``body`` (dedented) then the ``__main__`` guard calling main()."""
    return textwrap.dedent(body) + MAIN_GUARD


def _members(finding: Finding) -> list[tuple[str, int]]:
    return [(site.file, site.line) for site in finding.sites]


class TestClusters:
    def test_two_functions_calling_only_each_other_are_one_finding(self):
        app = _script(
            """\
            def main():
                pass


            def ping(n):
                return pong(n - 1)


            def pong(n):
                return ping(n - 1) if n else 0
            """
        )
        [finding] = _findings({"app.py": app})
        assert finding.check == CHECK
        assert _members(finding) == [("app.py", 5), ("app.py", 9)]
        assert finding.message == (
            "2 definition(s) no entry point reaches: app.py: ping():5, pong():9"
        )

    def test_a_chain_from_the_root_is_reachable(self):
        files = {
            "app.py": _script(
                """\
                from pkg import steps


                def main():
                    steps.first()
                """
            ),
            "pkg/__init__.py": "",
            "pkg/steps.py": """\
            LIMIT = 3


            def first():
                return _second()


            def _second():
                return Third().run(LIMIT)


            class Third:
                def run(self, limit):
                    return limit
            """,
        }
        assert _findings(files) == []

    def test_constants_and_classes_join_the_cluster_that_uses_them(self):
        app = _script(
            """\
            def main():
                pass


            ROUTES = ("a", "b")


            class Route:
                pass


            def _build():
                return [Route() for _ in ROUTES]
            """
        )
        [finding] = _findings({"app.py": app})
        assert finding.message == (
            "3 definition(s) no entry point reaches: app.py: ROUTES:5, Route:8, _build():12"
        )

    def test_a_cluster_of_constants_alone_is_dead_constants_territory(self):
        assert _findings({"app.py": _script("def main():\n    pass\n\nUNUSED = 1\n")}) == []

    def test_a_lone_public_function_is_left_to_dead_code(self):
        app = _script("def main():\n    pass\n\n\ndef orphan():\n    pass\n")
        assert _findings({"app.py": app}) == []

    def test_a_lone_private_function_is_reported(self):
        app = _script("def main():\n    pass\n\n\ndef _orphan():\n    pass\n")
        [finding] = _findings({"app.py": app})
        assert _members(finding) == [("app.py", 5)]

    def test_listing_is_capped_and_counts_the_rest(self):
        helpers = "".join(f"\n\ndef _h{i}():\n    _h{i + 1}()\n" for i in range(35))
        app = _script("def main():\n    pass\n" + helpers + "\n\ndef _h35():\n    _h0()\n")
        [finding] = _findings({"app.py": app})
        assert len(finding.sites) == 36
        assert "(+6 more)" in finding.message


class TestNoRoots:
    def test_no_roots_reports_nothing_and_says_so_on_stderr(self, capsys):
        files = {"lib.py": "def a():\n    b()\n\n\ndef b():\n    a()\n"}
        assert _findings(files) == []
        assert capsys.readouterr().err == NO_ROOTS_NOTE + "\n"

    def test_a_declared_root_nothing_analyzed_defines_counts_as_none(self, tmp_path, capsys):
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\ntool = "elsewhere.cli:main"\n', encoding="utf-8"
        )
        declared = load_project_entry_points(tmp_path)
        trees = {Path("lib.py"): ast.parse("def _a():\n    _b()\n\n\ndef _b():\n    _a()\n")}
        ctx = AnalysisContext(trees, verbose=False, declared_entry_points=declared)
        assert check_unreachable_from_entry_points(ctx) == []
        assert NO_ROOTS_NOTE in capsys.readouterr().err

    def test_a_library_package_exporting_names_abstains(self, capsys):
        files = {
            "pkg/__init__.py": "from pkg.core import parse\n\n__all__ = ['parse']\n",
            "pkg/core.py": "def parse():\n    return _scan()\n\n\ndef _scan():\n    return 1\n",
        }
        assert _findings(files) == []
        assert NO_ROOTS_NOTE in capsys.readouterr().err

    def test_cli_prints_the_note_on_stderr_only(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "lib.py").write_text("def _a():\n    _b()\n\n\ndef _b():\n    _a()\n")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([str(tmp_path), "--no-context", "--check", CHECK])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert NO_ROOTS_NOTE in captured.err
        assert NO_ROOTS_NOTE not in captured.out


# Each non-call reference to ``_target`` that reaches it from main(). The
# unreferenced control is TestNonCallReferences.test_control_is_reported.
NON_CALL_REFERENCES = {
    "passed as a value": """\
        def main():
            run(_target)


        def run(callback):
            return callback()
        """,
    "named in a dispatch dict": """\
        HANDLERS = {"t": _target}


        def main():
            return HANDLERS["t"]()
        """,
    "used as a decorator": """\
        @_target
        def _wrapped():
            pass


        def main():
            _wrapped()
        """,
    "getattr with a literal name": """\
        import sys


        def main():
            return getattr(sys.modules[__name__], "_target")()
        """,
    "a dotted string naming it": """\
        def main():
            return load("app._target")
        """,
    "a base class": """\
        class Child(_target):
            pass


        def main():
            return Child()
        """,
    "an attribute chain through an import": """\
        import app


        def main():
            return app._target
        """,
    "globals() in its module": """\
        def main():
            return globals()["_target"]()
        """,
    "a registering decorator": """\
        REGISTRY = []


        def register(fn):
            REGISTRY.append(fn)
            return fn


        @register
        def _target():
            pass


        def main():
            return REGISTRY
        """,
}


class TestNonCallReferences:
    TARGET = "\n\ndef _target():\n    pass\n"

    def _app(self, body: str) -> str:
        body = textwrap.dedent(body)
        target = "" if "def _target" in body else self.TARGET
        return body + target + MAIN_GUARD

    @pytest.mark.parametrize("body", NON_CALL_REFERENCES.values(), ids=NON_CALL_REFERENCES.keys())
    def test_reference_reaches_the_target(self, body):
        assert _findings({"app.py": self._app(body)}) == []

    def test_control_is_reported(self):
        [finding] = _findings({"app.py": self._app("def main():\n    pass\n")})
        assert "_target():" in finding.message

    def test_names_in_dunder_all_are_reached_with_their_module(self):
        files = {
            "app.py": _script("import pkg\n\n\ndef main():\n    pass\n"),
            "pkg/__init__.py": "from pkg.core import _target\n\n__all__ = ['_target']\n",
            "pkg/core.py": "def _target():\n    pass\n",
        }
        assert _findings(files) == []

    def test_a_transparent_decorator_does_not_register(self):
        body = "import functools\n\n\n@functools.cache\ndef _target():\n    pass\n\n\ndef main():\n    pass\n"
        [finding] = _findings({"app.py": body + MAIN_GUARD})
        assert "_target():" in finding.message

    def test_a_module_named_by_a_string_is_loaded_whole(self):
        files = {
            "app.py": _script(
                "import os\n\n\ndef main():\n    os.environ['SETTINGS'] = 'pkg.conf'\n"
            ),
            "pkg/__init__.py": "",
            "pkg/conf.py": "TIMEOUT = 3\n\n\ndef _computed():\n    return TIMEOUT\n",
            "pkg/other.py": "import pkg.conf\n",
        }
        assert _findings(files) == []

    def test_a_module_loaded_from_a_path_beside_the_caller(self):
        files = {
            "tools/run.py": _script(
                "def main():\n    return load_sibling(__file__, '_helpers').go()\n"
            ),
            "tools/_helpers.py": "def go():\n    return _inner()\n\n\ndef _inner():\n    pass\n",
            "tools/other.py": "import _helpers\n",
        }
        assert _findings(files) == []


class TestTests:
    LIB = """\
    def used():
        pass


    def _only_tested():
        return _helper()


    def _helper():
        pass
    """

    def _files(self, test_code: str) -> dict[str, str]:
        return {
            "app.py": _script("from lib import used\n\n\ndef main():\n    used()\n"),
            "lib.py": self.LIB,
            "tests/test_lib.py": test_code,
        }

    def test_a_test_caller_does_not_reach(self):
        test = (
            "from lib import _only_tested\n\n\ndef test_it():\n    assert _only_tested() is None\n"
        )
        [finding] = _findings(self._files(test))
        assert _members(finding) == [("lib.py", 5), ("lib.py", 9)]
        assert finding.message.endswith(
            "_helper():9. Tests that use them, to delete or rewrite: tests/test_lib.py (every test)"
        )

    def test_one_test_joins_the_unrelated_definitions_it_holds_alive(self):
        lib = "def used():\n    pass\n\n\ndef _a():\n    pass\n\n\ndef _b():\n    pass\n"
        test = "import lib\n\nROUTES = {'a': lib._a, 'b': lib._b}\n"
        files = {
            "app.py": _script("from lib import used\n\n\ndef main():\n    used()\n"),
            "lib.py": lib,
            "tests/test_lib.py": test,
        }
        [finding] = _findings(files)
        assert _members(finding) == [("lib.py", 5), ("lib.py", 9)]

    def test_a_module_only_tests_import_is_reported(self):
        files = {
            "app.py": _script("def main():\n    pass\n"),
            "spare.py": "def _a():\n    return _b()\n\n\ndef _b():\n    pass\n",
            "tests/test_spare.py": "from spare import _a\n\n\ndef test_a():\n    _a()\n",
        }
        [finding] = _findings(files)
        assert "spare.py (module unreached): _a():1, _b():5" in finding.message


TEMPLATE_TAGS = """\
from django import template

from app.templatetags._text import collapse

register = template.Library()


@register.filter
def shout(value):
    return _upper(collapse(value))


def _upper(value):
    return value.upper()


def dead_helper_a():
    return dead_helper_b()


def dead_helper_b():
    return dead_helper_a()
"""

# A helper module beside the tag module, imported by it: Django never loads
# it as a library, since it has no ``register``.
TEMPLATE_TEXT = """\
def collapse(value):
    return " ".join(value.split())


def unused_a():
    return unused_b()


def unused_b():
    return unused_a()
"""


class TestTemplateTags:
    FILES = {
        "app/__init__.py": "",
        "app/templatetags/__init__.py": "",
        "app/templatetags/shout.py": TEMPLATE_TAGS,
        "app/templatetags/_text.py": TEMPLATE_TEXT,
    }

    def test_registered_tags_reach_their_helpers_and_a_dead_pair_is_reported(self):
        findings = _findings(self.FILES)
        assert [_members(f) for f in findings] == [
            [("app/templatetags/_text.py", 5), ("app/templatetags/_text.py", 9)],
            [("app/templatetags/shout.py", 17), ("app/templatetags/shout.py", 21)],
        ]

    def test_a_templatetags_module_without_a_library_is_not_rooted_whole(self):
        app = _script(
            "from app.templatetags.util import collapse\n\n\ndef main():\n    collapse('x')\n"
        )
        files = {
            "app/__init__.py": "",
            "app/templatetags/__init__.py": "",
            "app/templatetags/util.py": TEMPLATE_TEXT,
            "run.py": app,
        }
        [finding] = _findings(files)
        assert _members(finding) == [
            ("app/templatetags/util.py", 5),
            ("app/templatetags/util.py", 9),
        ]

    def test_a_helper_no_registered_tag_calls_is_reported(self):
        code = TEMPLATE_TAGS.replace("_upper(collapse(value))", "collapse(value)")
        findings = _findings({**self.FILES, "app/templatetags/shout.py": code})
        assert [_members(f) for f in findings][1:] == [
            [("app/templatetags/shout.py", 13)],
            [("app/templatetags/shout.py", 17), ("app/templatetags/shout.py", 21)],
        ]


class TestOrphanModules:
    def test_a_module_nothing_analyzed_refers_to_is_left_alone(self):
        files = {
            "app.py": _script("def main():\n    pass\n"),
            "loose.py": "def _a():\n    return _b()\n\n\ndef _b():\n    pass\n",
        }
        assert _findings(files) == []


# A script whose guard calls nothing analyzed: its top-level code reaches
# ``_helper`` through ``TABLE``; nothing reaches the ``_dead`` pair.
SCRIPT_BODY = """\
import sys


def _helper():
    return 1


def _dead():
    return _dead_too()


def _dead_too():
    return _dead()


TABLE = {"a": _helper}
"""


class TestScriptModules:
    """A module with a ``__main__`` guard or a ``#!`` line is a root (Phase 35-1).
    The root is its top-level code, so what that code reaches is live and the
    rest of the module is judged like any other code."""

    DEAD_PAIR = [("tool.py", 8), ("tool.py", 12)]

    def test_a_guarded_module_roots_what_its_top_level_code_reaches(self):
        code = SCRIPT_BODY + '\n\nif __name__ == "__main__":\n    sys.exit(TABLE["a"]())\n'
        [finding] = _findings({"tool.py": code})
        assert _members(finding) == self.DEAD_PAIR

    def test_the_reversed_guard_counts(self):
        code = SCRIPT_BODY + '\n\nif "__main__" == __name__:\n    sys.exit(TABLE["a"]())\n'
        [finding] = _findings({"tool.py": code})
        assert _members(finding) == self.DEAD_PAIR

    def test_a_shebang_module_roots_what_its_top_level_code_reaches(self):
        code = "#!/usr/bin/env python3\n" + SCRIPT_BODY + '\n\nsys.exit(TABLE["a"]())\n'
        [finding] = _findings({"tool.py": code})
        assert _members(finding) == [("tool.py", 9), ("tool.py", 13)]

    # Another script, so the repo has a root and the check does not abstain.
    APP = {"app.py": _script("def main():\n    pass\n")}

    def test_a_module_with_neither_is_still_an_orphan(self):
        code = SCRIPT_BODY + '\n\nsys.exit(TABLE["a"]())\n'
        assert _findings({**self.APP, "tool.py": code}) == []

    def test_a_guard_nested_in_a_function_is_not_a_root(self):
        code = SCRIPT_BODY + '\n\ndef run():\n    if __name__ == "__main__":\n        _dead()\n'
        assert _findings({**self.APP, "tool.py": code}) == []


class TestSites:
    def _finding(self) -> Finding:
        app = _script(
            """\
            def main():
                pass


            LIMIT = 2


            def _a():
                return _b(LIMIT)


            def _b(n):
                return _a() if n else 0
            """
        )
        [finding] = _findings({"app.py": app})
        return finding

    def test_one_site_per_member(self):
        assert self._finding().sites == (Site("app.py", 5), Site("app.py", 8), Site("app.py", 12))

    def test_render_drops_an_ignored_member(self):
        finding = self._finding()
        assert finding.render is not None
        assert finding.render((0, 2)) == (
            "2 definition(s) no entry point reaches: app.py: LIMIT:5, _b():12"
        )

    def test_render_refuses_constants_alone(self):
        finding = self._finding()
        assert finding.render is not None
        assert finding.render((0,)) is None

    def test_per_site_ignore_through_the_cli(self, tmp_path, monkeypatch, capsys):
        app = _script(
            """\
            def main():
                pass


            def _a():
                return _b()


            def _b():  # pysmelly: ignore[unreachable-from-entry-points]
                return _a()
            """
        )
        (tmp_path / "app.py").write_text(app)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", CHECK])
        out = capsys.readouterr().out
        assert "no entry point reaches: app.py: _a():5 (1 other site ignored). Fix: " in out
