"""The module graph: each module's imports, bound names and re-export origins."""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.callers import _CallMatcher, _CallTarget
from pysmelly.checks.module_graph import (
    ImportedPath,
    ModuleGraph,
    NameOrigin,
    OriginKind,
    build_module_graph,
)
from pysmelly.context import AnalysisContext


def _graph(files: dict[str, str]) -> ModuleGraph:
    trees = {Path(name): ast.parse(code) for name, code in files.items()}
    return AnalysisContext(trees, verbose=False).module_graph


def _origin(files: dict[str, str], module: str, name: str) -> tuple[OriginKind, str, str]:
    found = _graph(files).origin(module, name)
    return found.kind, found.module, found.name


class TestNodes:
    def test_module_and_package_follow_the_source_root(self):
        graph = _graph({"src/pkg/__init__.py": "", "src/pkg/mod.py": ""})
        node = graph.nodes[Path("src/pkg/mod.py")]
        assert (node.module, node.package) == ("pkg.mod", "pkg")
        init = graph.nodes[Path("src/pkg/__init__.py")]
        assert (init.module, init.package) == ("pkg", "pkg")

    def test_imports_name_each_dotted_path(self):
        graph = _graph(
            {
                "pkg/__init__.py": "",
                "pkg/mod.py": "import os.path\nfrom . import sib\nfrom .x import *\n",
            }
        )
        assert graph.nodes[Path("pkg/mod.py")].imports == (
            ImportedPath("os.path", absolute=True, from_source=False),
            ImportedPath("pkg", absolute=False, from_source=True),
            ImportedPath("pkg.sib", absolute=False, from_source=False),
            ImportedPath("pkg.x", absolute=False, from_source=True),
        )

    def test_imports_inside_functions_count(self):
        graph = _graph({"mod.py": "def f():\n    import late\n"})
        assert graph.nodes[Path("mod.py")].imports == (
            ImportedPath("late", absolute=True, from_source=False),
        )

    def test_bindings_are_the_module_imports_entry(self):
        trees = {Path("mod.py"): ast.parse("import os\nfrom json import loads as l\n")}
        ctx = AnalysisContext(trees, verbose=False)
        assert ctx.module_graph.nodes[Path("mod.py")].bindings is ctx.module_imports[Path("mod.py")]

    def test_bound_names_cover_module_scope_only(self):
        code = (
            "import os.path\n"
            "from json import loads as parse\n"
            "def f():\n    inner = 1\n    import hidden\n"
            "class C:\n    attr = 1\n"
            "a, (b, *c) = 1, (2, 3)\n"
            "d: int = 1\n"
            "e: int\n"
            "for i in []:\n    in_loop = 1\n"
            "with open('x') as fh:\n    pass\n"
            "try:\n    from fast import speed\n"
            "except ImportError as err:\n    speed = None\n"
            "else:\n    ok = 1\n"
            "finally:\n    done = 1\n"
            "if (w := 2):\n    branch = 1\n"
            "match d:\n    case {'k': v, **rest}:\n        matched = 1\n"
            "type Alias = int\n"
        )
        names = _graph({"mod.py": code}).nodes[Path("mod.py")].bound_names
        assert names == {
            "os",
            "parse",
            "f",
            "C",
            "a",
            "b",
            "c",
            "d",
            "i",
            "in_loop",
            "fh",
            "speed",
            "err",
            "ok",
            "done",
            "w",
            "branch",
            "v",
            "rest",
            "matched",
            "Alias",
        }

    def test_loads_is_the_import_graph(self):
        trees = {
            Path("pkg/__init__.py"): ast.parse(""),
            Path("pkg/a.py"): ast.parse("from pkg import b"),
            Path("pkg/b.py"): ast.parse(""),
        }
        ctx = AnalysisContext(trees, verbose=False)
        assert ctx.import_graph is ctx.module_graph.loads
        assert ctx.import_graph[Path("pkg/a.py")] == {Path("pkg/__init__.py"), Path("pkg/b.py")}

    def test_by_module_names_every_file(self):
        graph = _graph({"pkg/__init__.py": "", "pkg/a.py": ""})
        assert graph.by_module == {"pkg": [Path("pkg/__init__.py")], "pkg.a": [Path("pkg/a.py")]}

    def test_context_builds_once(self):
        ctx = AnalysisContext({Path("mod.py"): ast.parse("")}, verbose=False)
        assert ctx.module_graph is ctx.module_graph

    def test_module_of_names_an_unanalyzed_path(self):
        graph = _graph({"pkg/__init__.py": ""})
        assert graph.module_of("pkg/deleted.py") == "pkg.deleted"


class TestOrigin:
    PACKAGE = {
        "pkg/__init__.py": "from .impl import Thing, helper as assist\nfrom .sub import Deep\n",
        "pkg/impl.py": "class Thing: ...\ndef helper(): ...\n",
        "pkg/sub/__init__.py": "from .deeper import Deep\n",
        "pkg/sub/deeper.py": "Deep = 1\n",
    }

    def test_defined_in_the_module_itself(self):
        assert _origin(self.PACKAGE, "pkg.impl", "Thing") == (
            OriginKind.DEFINED,
            "pkg.impl",
            "Thing",
        )

    def test_re_export_resolves_to_the_defining_module(self):
        assert _origin(self.PACKAGE, "pkg", "Thing") == (OriginKind.DEFINED, "pkg.impl", "Thing")

    def test_aliased_re_export_follows_the_original_name(self):
        assert _origin(self.PACKAGE, "pkg", "assist") == (
            OriginKind.DEFINED,
            "pkg.impl",
            "helper",
        )

    def test_chained_re_export_is_followed_to_the_end(self):
        assert _origin(self.PACKAGE, "pkg", "Deep") == (
            OriginKind.DEFINED,
            "pkg.sub.deeper",
            "Deep",
        )

    def test_reexports_map_each_from_import_binding(self):
        graph = _graph(self.PACKAGE)
        found = graph.reexports(Path("pkg/__init__.py"))
        assert {name: origin.module for name, origin in found.items()} == {
            "Thing": "pkg.impl",
            "assist": "pkg.impl",
            "Deep": "pkg.sub.deeper",
        }
        assert found["Thing"].path == Path("pkg/impl.py")

    def test_re_export_of_a_name_no_longer_defined_is_missing(self):
        files = {"pkg/__init__.py": "from .impl import gone\n", "pkg/impl.py": "kept = 1\n"}
        assert _origin(files, "pkg", "gone") == (OriginKind.MISSING, "pkg.impl", "gone")

    def test_unbound_name_on_an_analyzed_module_is_missing(self):
        assert _origin({"mod.py": "x = 1"}, "mod", "y") == (OriginKind.MISSING, "mod", "y")

    def test_import_from_a_library_is_external(self):
        files = {"pkg/__init__.py": "from requests import get\n"}
        assert _origin(files, "pkg", "get") == (OriginKind.EXTERNAL, "requests", "get")

    def test_module_outside_the_analysis_is_external(self):
        assert _origin({"mod.py": ""}, "elsewhere", "x") == (OriginKind.EXTERNAL, "elsewhere", "x")

    def test_relative_submodule_import_in_init_is_the_submodule(self):
        files = {"pkg/__init__.py": "from . import sub\n", "pkg/sub.py": ""}
        assert _origin(files, "pkg", "sub") == (OriginKind.MODULE, "pkg.sub", "")

    def test_unbound_submodule_is_the_submodule(self):
        files = {"pkg/__init__.py": "", "pkg/sub.py": ""}
        assert _origin(files, "pkg", "sub") == (OriginKind.MODULE, "pkg.sub", "")

    def test_plain_import_binding_is_the_module_it_names(self):
        files = {"pkg/__init__.py": "", "pkg/sub.py": "", "app.py": "import pkg.sub as s\n"}
        assert _origin(files, "app", "s") == (OriginKind.MODULE, "pkg.sub", "")
        assert _origin({"app.py": "import os"}, "app", "os") == (OriginKind.EXTERNAL, "os", "")

    def test_star_import_binds_public_names(self):
        files = {"pkg/__init__.py": "from .impl import *\n", "pkg/impl.py": "x = 1\n_y = 2\n"}
        assert _origin(files, "pkg", "x") == (OriginKind.DEFINED, "pkg.impl", "x")
        assert _origin(files, "pkg", "_y")[0] is OriginKind.MISSING

    def test_star_import_binds_only_what_all_lists(self):
        files = {
            "pkg/__init__.py": "from .impl import *\n",
            "pkg/impl.py": "__all__ = ['x', '_y']\n__all__ += ['z']\nx = _y = z = w = 1\n",
        }
        assert _origin(files, "pkg", "_y") == (OriginKind.DEFINED, "pkg.impl", "_y")
        assert _origin(files, "pkg", "z") == (OriginKind.DEFINED, "pkg.impl", "z")
        assert _origin(files, "pkg", "w")[0] is OriginKind.MISSING

    def test_star_import_through_a_dynamic_all_is_unknown(self):
        files = {
            "pkg/__init__.py": "from .impl import *\n",
            "pkg/impl.py": "__all__ = NAMES\nx = 1\n",
        }
        assert _origin(files, "pkg", "x") == (OriginKind.UNKNOWN, "pkg.impl", "x")

    def test_star_import_from_a_library_is_unknown(self):
        files = {"pkg/__init__.py": "from os.path import *\n"}
        assert _origin(files, "pkg", "join") == (OriginKind.UNKNOWN, "os.path", "join")

    def test_module_getattr_makes_an_unbound_name_unknown(self):
        files = {"mod.py": "def __getattr__(name): ...\n"}
        assert _origin(files, "mod", "lazy") == (OriginKind.UNKNOWN, "mod", "lazy")

    def test_later_binding_wins(self):
        files = {
            "pkg/__init__.py": "from .impl import f\ndef f(): ...\n",
            "pkg/impl.py": "def f(): ...\n",
        }
        assert _origin(files, "pkg", "f") == (OriginKind.DEFINED, "pkg", "f")

    def test_re_export_cycle_with_no_definition_is_missing(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .b import f\n",
            "pkg/b.py": "from .a import f\n",
        }
        assert _origin(files, "pkg.a", "f")[0] is OriginKind.MISSING

    def test_origin_carries_the_defining_file(self):
        found = _graph(self.PACKAGE).origin("pkg", "Deep")
        assert found == NameOrigin(
            OriginKind.DEFINED, "pkg.sub.deeper", "Deep", Path("pkg/sub/deeper.py")
        )


class TestEdges:
    def test_reexports_are_computed_once_per_file(self):
        graph = _graph({"pkg/__init__.py": "from .a import x\n", "pkg/a.py": "x = 1\n"})
        assert graph.reexports(Path("pkg/__init__.py")) is graph.reexports(Path("pkg/__init__.py"))

    def test_star_import_cycle_binds_nothing(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/a.py": "from .b import *\n",
            "pkg/b.py": "from .a import *\n",
        }
        assert _origin(files, "pkg.a", "x")[0] is OriginKind.MISSING

    def test_star_export_that_nothing_defines_binds_nothing(self):
        files = {"pkg/__init__.py": "from .a import *\n", "pkg/a.py": "__all__ = ['ghost']\n"}
        assert _origin(files, "pkg", "ghost")[0] is OriginKind.MISSING

    def test_annotated_all_is_read_and_a_bare_declaration_ignored(self):
        files = {
            "pkg/__init__.py": "from .a import *\n",
            "pkg/a.py": "__all__: list[str]\n__all__: list[str] = ['x']\nx = y = 1\n",
        }
        assert _origin(files, "pkg", "x")[0] is OriginKind.DEFINED
        assert _origin(files, "pkg", "y")[0] is OriginKind.MISSING

    def test_all_with_a_non_string_element_is_dynamic(self):
        files = {"pkg/__init__.py": "from .a import *\n", "pkg/a.py": "__all__ = [NAME]\n"}
        assert _origin(files, "pkg", "x")[0] is OriginKind.UNKNOWN

    def test_walrus_inside_a_lambda_binds_nothing_at_module_scope(self):
        graph = _graph({"mod.py": "f = lambda: (inner := 1)\n"})
        assert graph.nodes[Path("mod.py")].bound_names == {"f"}

    def test_import_roots_name_modules_by_their_path_from_the_root(self):
        trees = {Path("tests/helpers.py"): ast.parse(""), Path("app.py"): ast.parse("")}
        graph = build_module_graph(
            trees, frozenset(), (Path("tests"),), {path: [] for path in trees}
        )
        assert graph.by_module["helpers"] == [Path("tests/helpers.py")]
        assert graph.by_module["tests.helpers"] == [Path("tests/helpers.py")]
        assert "app" in graph.by_module


class TestGuardedBindings:
    """30-2: a ``try``/``except ImportError`` fallback does not hide the import it guards."""

    FALLBACK = {
        "pkg/__init__.py": (
            "try:\n    from .opt import maybe\nexcept ImportError:\n    maybe = None\n"
        ),
        "pkg/opt.py": "def maybe(): ...\n",
    }

    def test_fallback_assignment_does_not_hide_the_re_export(self):
        assert _origin(self.FALLBACK, "pkg", "maybe") == (OriginKind.DEFINED, "pkg.opt", "maybe")

    def test_fallback_answers_when_the_guarded_import_binds_nothing(self):
        files = {**self.FALLBACK, "pkg/opt.py": ""}
        assert _origin(files, "pkg", "maybe") == (OriginKind.DEFINED, "pkg", "maybe")

    def test_unguarded_import_of_a_missing_name_stays_missing(self):
        files = {"pkg/__init__.py": "maybe = None\nfrom .opt import maybe\n", "pkg/opt.py": ""}
        assert _origin(files, "pkg", "maybe") == (OriginKind.MISSING, "pkg.opt", "maybe")

    def test_later_unguarded_binding_still_wins(self):
        files = {
            **self.FALLBACK,
            "pkg/__init__.py": self.FALLBACK["pkg/__init__.py"] + "maybe = 1\n",
        }
        assert _origin(files, "pkg", "maybe") == (OriginKind.DEFINED, "pkg", "maybe")

    def test_bindings_carry_their_line_and_guard(self):
        code = (
            "import os\n"
            "try:\n    from .a import x\nexcept (ValueError, ImportError):\n    x = None\n"
            "try:\n    from .b import y\nexcept KeyError:\n    pass\n"
            "try:\n    import z\nexcept:\n    pass\n"
        )
        scope = _graph({"pkg/__init__.py": code}).nodes[Path("pkg/__init__.py")].scope
        assert [(b.name, b.line, b.optional, b.fallback) for b in scope] == [
            ("os", 1, False, False),
            ("x", 3, True, False),
            ("x", 5, False, True),
            ("y", 7, False, False),
            ("z", 11, True, False),
        ]

    def test_all_records_each_names_line(self):
        code = "__all__ = [\n    'a',\n    'b',\n]\n__all__ += ['c']\n"
        node = _graph({"mod.py": code}).nodes[Path("mod.py")]
        assert node.exports == {"a": 2, "b": 3, "c": 5}


class TestDefiningFiles:
    """change-coupling's re-export credit: the files a module's from-imports really bind."""

    PACKAGE = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "from .impl import thing\nfrom .more import *\n",
        "pkg/sub/impl.py": "def thing(): ...\n",
        "pkg/sub/more.py": "def extra(): ...\n",
    }

    def _defining(self, code: str) -> frozenset[Path]:
        graph = _graph({**self.PACKAGE, "app.py": code})
        return graph.defining_files(Path("app.py"))

    def test_re_export_credits_the_defining_file(self):
        assert self._defining("from pkg.sub import thing\n") == {Path("pkg/sub/impl.py")}

    def test_import_inside_a_function_counts(self):
        code = "def f():\n    from pkg.sub import thing\n"
        assert self._defining(code) == {Path("pkg/sub/impl.py")}

    def test_submodule_import_credits_the_submodule(self):
        assert self._defining("from pkg.sub import impl\n") == {Path("pkg/sub/impl.py")}

    def test_star_import_is_not_followed(self):
        assert self._defining("from pkg.sub import *\n") == frozenset()

    def test_name_through_a_package_star_import_is_followed(self):
        assert self._defining("from pkg.sub import extra\n") == {Path("pkg/sub/more.py")}

    def test_library_and_missing_names_credit_nothing(self):
        assert self._defining("from json import loads\nfrom pkg.sub import gone\n") == frozenset()


class TestClassBases:
    """30-3: the classes a module's classes inherit from, for change-coupling's shared-parent credit."""

    PACKAGE = {
        "handlers/__init__.py": "from .base import Handler\n",
        "handlers/base.py": "class Handler: ...\nclass Other: ...\n",
    }

    def _bases(self, code: str) -> set[tuple[str, str]]:
        graph = _graph({**self.PACKAGE, "handlers/audio.py": code})
        return {(base.module, base.name) for base in graph.base_classes(Path("handlers/audio.py"))}

    def test_class_bases_spell_each_base_of_a_module_level_class(self):
        code = (
            "import abc\nfrom .base import Handler\n"
            "class A(Handler, abc.ABC): ...\nclass B(Generic[T]): ...\nclass C(make_base()): ...\n"
            "def f():\n    class Inner(Handler): ...\n"
        )
        graph = _graph({**self.PACKAGE, "handlers/audio.py": code})
        assert graph.nodes[Path("handlers/audio.py")].class_bases == (
            "Handler",
            "abc.ABC",
            "Generic",
        )

    def test_base_imported_from_its_module(self):
        assert self._bases("from .base import Handler\nclass A(Handler): ...\n") == {
            ("handlers.base", "Handler")
        }

    def test_base_through_a_re_export(self):
        assert self._bases("from handlers import Handler\nclass A(Handler): ...\n") == {
            ("handlers.base", "Handler")
        }

    def test_base_as_an_attribute_of_its_module(self):
        assert self._bases("from . import base\nclass A(base.Handler): ...\n") == {
            ("handlers.base", "Handler")
        }

    def test_base_through_a_dotted_plain_import(self):
        assert self._bases("import handlers.base\nclass A(handlers.base.Handler): ...\n") == {
            ("handlers.base", "Handler")
        }

    def test_generic_base_is_its_class(self):
        assert self._bases("from .base import Handler\nclass A(Handler[int]): ...\n") == {
            ("handlers.base", "Handler")
        }

    def test_library_and_unresolved_bases_are_left_out(self):
        code = (
            "import abc\nfrom .base import Handler\n"
            "class A(abc.ABC): ...\nclass B(Handler.Nested): ...\nclass C(Gone): ...\n"
        )
        assert self._bases(code) == set()

    def test_nested_class_bases_are_left_out(self):
        assert (
            self._bases("from .base import Handler\ndef f():\n    class A(Handler): ...\n") == set()
        )


class TestResolve:
    PACKAGE = {
        "pkg/__init__.py": "",
        "pkg/impl.py": "class Thing: ...\n",
        "app.py": "import pkg.impl\nfrom pkg import impl\nimport json\n",
    }

    def _resolve(self, dotted: str) -> tuple[OriginKind, str, str]:
        found = _graph(self.PACKAGE).resolve("app", dotted)
        return found.kind, found.module, found.name

    def test_each_attribute_is_looked_up_on_the_module_reached(self):
        assert self._resolve("impl.Thing") == (OriginKind.DEFINED, "pkg.impl", "Thing")
        assert self._resolve("pkg.impl.Thing") == (OriginKind.DEFINED, "pkg.impl", "Thing")

    def test_a_module_chain_ends_at_the_module(self):
        assert self._resolve("pkg.impl") == (OriginKind.MODULE, "pkg.impl", "")

    def test_a_chain_that_leaves_the_analysis_stays_external(self):
        assert self._resolve("json.decoder.JSONDecoder")[0] is OriginKind.EXTERNAL

    def test_an_attribute_of_a_class_is_not_followed(self):
        assert self._resolve("impl.Thing.attr") == (OriginKind.UNKNOWN, "pkg.impl", "attr")


class TestEnclosingPackage:
    def test_deepest_analyzed_package_above(self):
        graph = _graph({"pkg/__init__.py": "", "pkg/sub/__init__.py": ""})
        assert graph.enclosing_package("pkg.sub.gone.deeper") == (
            "pkg.sub",
            Path("pkg/sub/__init__.py"),
        )

    def test_none_outside_the_project(self):
        graph = _graph({"pkg/__init__.py": "", "mod.py": ""})
        assert graph.enclosing_package("yaml.loader") is None
        assert graph.enclosing_package("pkg") is None

    def test_a_plain_module_is_not_a_package(self):
        graph = _graph({"mod.py": ""})
        assert graph.enclosing_package("mod.inner") is None


class TestCallResolutionReadsTheGraph:
    """return-none's 21-3 caller resolution takes modules and bindings from the graph."""

    def test_target_module_comes_from_the_graph(self):
        graph = _graph({"src/pkg/__init__.py": "", "src/pkg/util.py": "def find(): ..."})
        target = _CallTarget.defined_in(graph, "find", Path("src/pkg/util.py"))
        assert target == _CallTarget("find", Path("src/pkg/util.py"), "pkg.util")

    def test_matcher_replays_the_nodes_bindings(self):
        graph = _graph(
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "def find(): ...",
                "app.py": "from pkg.util import find as lookup\n",
            }
        )
        target = _CallTarget.defined_in(graph, "find", Path("pkg/util.py"))
        matcher = _CallMatcher.in_module(graph, Path("app.py"), target)
        assert matcher.names == {"lookup"}
