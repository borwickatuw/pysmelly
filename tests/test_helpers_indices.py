"""Tests for the indices that replace per-candidate tree walks:
``ctx.module_imports`` (call resolution), ``ctx.subscript_string_keys``
(dict-as-dataclass key reads) and ``ctx.raise_sites`` (dead-exceptions)."""

import ast
from pathlib import Path

from pysmelly.checks.callers import _CallMatcher, _CallTarget
from pysmelly.checks.helpers import (
    ModuleImport,
    RaiseSite,
    build_module_imports,
    build_raise_index,
    build_subscript_string_keys,
)
from pysmelly.context import AnalysisContext


def _imports(code: str) -> list[ModuleImport]:
    return build_module_imports({Path("mod.py"): ast.parse(code)}, frozenset())[Path("mod.py")]


def _keys(code: str) -> frozenset[str]:
    return build_subscript_string_keys({Path("mod.py"): ast.parse(code)})[Path("mod.py")]


class TestBuildModuleImports:
    def test_plain_import(self):
        assert _imports("import os") == [ModuleImport("os", "os", None, None)]

    def test_dotted_import_binds_and_names_the_root(self):
        assert _imports("import a.b.c") == [ModuleImport("a", "a", None, None)]

    def test_aliased_import_names_the_whole_module(self):
        assert _imports("import a.b as c") == [ModuleImport("c", "a.b", None, None)]

    def test_one_entry_per_alias(self):
        assert _imports("import os, sys as system") == [
            ModuleImport("os", "os", None, None),
            ModuleImport("system", "sys", None, None),
        ]

    def test_from_import(self):
        assert _imports("from s import n, m as k") == [
            ModuleImport("n", "s.n", "s", "n"),
            ModuleImport("k", "s.m", "s", "m"),
        ]

    def test_star_import(self):
        assert _imports("from s import *") == [ModuleImport("*", "s.*", "s", "*")]

    def test_imports_inside_functions_count(self):
        assert _imports("def f():\n    import inner\n") == [
            ModuleImport("inner", "inner", None, None)
        ]

    def test_walk_order_not_line_order(self):
        """A module-level import comes before one nested in an earlier
        function, as ``ast.walk`` (breadth-first) visits them."""
        code = "def f():\n    import late\n\nimport early\n"
        assert [imp.bound for imp in _imports(code)] == ["early", "late"]

    def test_no_imports(self):
        assert _imports("x = 1") == []

    def test_relative_import_resolves_against_the_files_package(self):
        trees = {
            Path("pkg/__init__.py"): ast.parse(""),
            Path("pkg/sub/__init__.py"): ast.parse(""),
            Path("pkg/sub/mod.py"): ast.parse("from ..util import f\nfrom . import g\n"),
        }
        ctx = AnalysisContext(trees, verbose=False)
        assert ctx.module_imports[Path("pkg/sub/mod.py")] == [
            ModuleImport("f", "pkg.util.f", "pkg.util", "f"),
            ModuleImport("g", "pkg.sub.g", "pkg.sub", "g"),
        ]
        assert ctx.module_imports[Path("pkg/__init__.py")] == []

    def test_context_builds_once(self):
        ctx = AnalysisContext({Path("mod.py"): ast.parse("import os")}, verbose=False)
        assert ctx.module_imports is ctx.module_imports


class TestCallMatcherReplaysImports:
    """``_CallMatcher`` replays a module's import list; the last binding of a
    name wins, as it did when the matcher walked the tree itself."""

    TARGET = _CallTarget(name="find", file=Path("pkg/util.py"), module="pkg.util")

    def _call(self, code: str) -> ast.Call:
        return ast.parse(code).body[0].value

    def test_later_binding_wins(self):
        imports = [
            ModuleImport("u", "other", None, None),
            ModuleImport("u", "pkg.util", "pkg", "util"),
        ]
        matcher = _CallMatcher(Path("app.py"), self.TARGET, imports)
        assert matcher(self._call("u.find()"))

    def test_earlier_binding_is_overridden(self):
        imports = [
            ModuleImport("u", "pkg.util", "pkg", "util"),
            ModuleImport("u", "other", None, None),
        ]
        matcher = _CallMatcher(Path("app.py"), self.TARGET, imports)
        assert not matcher(self._call("u.find()"))

    def test_aliased_from_import_binds_the_alias(self):
        imports = [ModuleImport("lookup", "pkg.util.find", "pkg.util", "find")]
        matcher = _CallMatcher(Path("app.py"), self.TARGET, imports)
        assert matcher.names == {"lookup"}
        assert matcher(self._call("lookup()"))
        assert not matcher(self._call("find()"))

    def test_star_import_binds_the_name(self):
        imports = [ModuleImport("*", "pkg.util.*", "pkg.util", "*")]
        matcher = _CallMatcher(Path("app.py"), self.TARGET, imports)
        assert matcher.names == {"find"}

    def test_defining_module_binds_the_name(self):
        matcher = _CallMatcher(Path("pkg/util.py"), self.TARGET, [])
        assert matcher.names == {"find"}


class TestBuildSubscriptStringKeys:
    def test_string_key_reads(self):
        assert _keys('x["a"]\nf()["b"]\ny = d["c"]["e"]\n') == {"a", "b", "c", "e"}

    def test_stores_and_deletes_do_not_count(self):
        assert _keys('x["a"] = 1\ndel x["b"]\nx["c"] += 1\n') == frozenset()

    def test_non_string_and_non_constant_keys_do_not_count(self):
        assert _keys('x[0]\nx[k]\nx["a":"b"]\nx[b"raw"]\n') == frozenset()

    def test_reads_inside_functions_count(self):
        assert _keys('def f(r):\n    return r["inner"]\n') == {"inner"}

    def test_per_file(self):
        trees = {Path("a.py"): ast.parse('x["a"]'), Path("b.py"): ast.parse("y = 1")}
        ctx = AnalysisContext(trees, verbose=False)
        assert ctx.subscript_string_keys == {
            Path("a.py"): frozenset({"a"}),
            Path("b.py"): frozenset(),
        }
        assert ctx.subscript_string_keys is ctx.subscript_string_keys


def _raises(code: str) -> dict[str, list[RaiseSite]]:
    return build_raise_index({Path("mod.py"): ast.parse(code)})


class TestBuildRaiseIndex:
    """The index behind ``ctx.raise_sites`` (plan item 23, sub-phase 23-4)."""

    def test_raise_class_and_raise_instance(self):
        assert _raises("raise A\nraise B('msg')\n") == {
            "A": [RaiseSite(Path("mod.py"), 1, 0)],
            "B": [RaiseSite(Path("mod.py"), 2, 0)],
        }

    def test_attribute_raises_are_keyed_by_the_attribute(self):
        assert _raises("raise errors.A\nraise errors.B()\n") == {
            "A": [RaiseSite(Path("mod.py"), 1, 0)],
            "B": [RaiseSite(Path("mod.py"), 2, 0)],
        }

    def test_bare_raise_and_unnamed_expressions_are_not_indexed(self):
        code = "try:\n    pass\nexcept E:\n    raise\nraise errs[0]\nraise make()()\n"
        assert _raises(code) == {}

    def test_cause_is_not_a_raise_of_the_cause(self):
        assert _raises("raise A from B\n") == {"A": [RaiseSite(Path("mod.py"), 1, 0)]}

    def test_nested_raises_carry_their_column(self):
        code = "def f():\n    if x:\n        raise A()\n"
        assert _raises(code) == {"A": [RaiseSite(Path("mod.py"), 3, 8)]}

    def test_every_site_is_kept_in_file_then_source_order(self):
        trees = {
            Path("a.py"): ast.parse("raise A\ndef f():\n    raise A()\n"),
            Path("b.py"): ast.parse("raise m.A\n"),
        }
        assert build_raise_index(trees) == {
            "A": [
                RaiseSite(Path("a.py"), 1, 0),
                RaiseSite(Path("a.py"), 3, 4),
                RaiseSite(Path("b.py"), 1, 0),
            ]
        }

    def test_variable_raise_is_keyed_by_what_its_assignment_builds(self):
        code = "def f():\n    err = A('x')\n    raise err\n"
        assert _raises(code) == {"A": [RaiseSite(Path("mod.py"), 3, 4)]}

    def test_variable_bound_to_a_class_or_an_attribute(self):
        code = (
            "def f():\n    cls = errors.A\n    raise cls\ndef g():\n    e: B = B()\n    raise e\n"
        )
        assert _raises(code) == {
            "A": [RaiseSite(Path("mod.py"), 3, 4)],
            "B": [RaiseSite(Path("mod.py"), 6, 4)],
        }

    def test_variable_assigned_twice_keys_both(self):
        code = "def f(x):\n    err = A()\n    if x:\n        err = B()\n    raise err\n"
        assert _raises(code) == {
            "A": [RaiseSite(Path("mod.py"), 5, 4)],
            "B": [RaiseSite(Path("mod.py"), 5, 4)],
        }

    def test_variable_resolves_through_the_nearest_assigning_scope(self):
        code = (
            "err = A()\n"
            "def f():\n    raise err\n"
            "def g():\n    err = B()\n    def h():\n        raise err\n"
        )
        assert _raises(code) == {
            "A": [RaiseSite(Path("mod.py"), 3, 4)],
            "B": [RaiseSite(Path("mod.py"), 7, 8)],
        }

    def test_handler_name_and_other_scopes_stay_unresolved(self):
        """``except E as err: raise err`` re-raises; ``exc`` bound in another function is not this one."""
        code = (
            "try:\n    pass\nexcept E as err:\n    raise err\n"
            "def f():\n    exc = A()\n"
            "def g():\n    raise exc\n"
        )
        assert _raises(code) == {
            "err": [RaiseSite(Path("mod.py"), 4, 4)],
            "exc": [RaiseSite(Path("mod.py"), 8, 4)],
        }

    def test_deep_nesting_does_not_recurse(self):
        code = "x = " + " + ".join(["a"] * 5000) + "\nraise A\n"
        assert _raises(code) == {"A": [RaiseSite(Path("mod.py"), 2, 0)]}

    def test_context_builds_once(self):
        ctx = AnalysisContext({Path("mod.py"): ast.parse("raise A")}, verbose=False)
        assert ctx.raise_sites == {"A": [RaiseSite(Path("mod.py"), 1, 0)]}
        assert ctx.raise_sites is ctx.raise_sites
