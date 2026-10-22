"""Tests for the static reading of builtin and standard-library classes.

pysmelly never imports code named by its input (DECISIONS.md, "pysmelly never
imports code named by its input").
"""

import subprocess
import sys
import textwrap

import pytest

from pysmelly.checks.stdlib_classes import MAX_HOPS, StdlibClasses
from pysmelly.cli import main


@pytest.fixture
def stdlib():
    return StdlibClasses()


class TestBuiltins:
    def test_a_builtin_class_gives_its_names(self, stdlib):
        names = stdlib.defined_names("dict")
        assert {"get", "keys", "__getitem__"} <= names

    def test_object_defines_nothing(self, stdlib):
        assert stdlib.defined_names("object") == frozenset()

    def test_a_builtin_that_is_not_a_class_is_unresolved(self, stdlib):
        assert stdlib.defined_names("len") is None
        assert stdlib.defined_names("NoSuchBuiltin") is None


class TestStandardLibrary:
    def test_enum(self, stdlib):
        names = stdlib.defined_names("enum.Enum")
        assert {"__init__", "__repr__", "_missing_", "name", "value"} <= names
        assert "describe" not in names

    def test_protocol_reads_generic_from_the_loaded_compiled_in_module(self, stdlib):
        """``Protocol`` inherits ``Generic``, which ``typing`` takes from ``_typing``."""
        names = stdlib.defined_names("typing.Protocol")
        assert {"__init_subclass__", "_is_protocol", "__class_getitem__"} <= names
        assert "list_entries" not in names

    def test_abc_and_collections_abc_through_a_star_import(self, stdlib):
        assert stdlib.defined_names("abc.ABC") == frozenset({"__slots__"})
        assert {"get", "keys", "__getitem__", "__len__"} <= stdlib.defined_names(
            "collections.abc.Mapping"
        )

    def test_a_relative_import_in_a_package(self, stdlib):
        """``unittest`` binds ``TestCase`` with ``from .case import ...``."""
        assert {"setUp", "assertEqual"} <= stdlib.defined_names("unittest.TestCase")

    def test_logging_handler_follows_a_base_in_the_same_file(self, stdlib):
        assert {"emit", "addFilter"} <= stdlib.defined_names("logging.Handler")

    def test_a_function_with_mro_entries_reads_them(self, stdlib):
        """``NamedTuple`` and ``TypedDict`` are functions; the class statement uses their entries."""
        assert stdlib.defined_names("typing.NamedTuple") == frozenset()
        assert stdlib.defined_names("typing.TypedDict") == frozenset()

    def test_a_subclass_of_a_compiled_in_class(self, stdlib):
        assert {"getvalue", "write"} <= stdlib.defined_names("io.StringIO")

    def test_third_party_and_missing_paths_are_unresolved(self, stdlib):
        assert stdlib.defined_names("django.db.models.Model") is None
        assert stdlib.defined_names("os.no_such_module.Thing") is None
        assert stdlib.defined_names("enum.NoSuchClass") is None

    def test_a_function_is_not_a_class(self, stdlib):
        assert stdlib.defined_names("os.path.join") is None


def _stdlib_root(tmp_path, files):
    """A fake standard-library root; file names must be real stdlib module names."""
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return StdlibClasses(root=tmp_path)


class TestStaticReading:
    def test_except_handlers_are_the_failure_path(self, tmp_path):
        stdlib = _stdlib_root(
            tmp_path,
            {
                "sched.py": """\
                try:
                    from _no_such_accelerator import Base
                except ImportError:
                    class Base:
                        def fallback(self): ...
                else:
                    class Base:
                        def chosen(self): ...
                """
            },
        )
        assert stdlib.defined_names("sched.Base") == frozenset({"chosen"})

    def test_a_later_binding_wins(self, tmp_path):
        stdlib = _stdlib_root(
            tmp_path,
            {
                "sched.py": """\
                class Base:
                    def first(self): ...
                class Base:
                    def second(self): ...
                """
            },
        )
        assert stdlib.defined_names("sched.Base") == frozenset({"second"})

    def test_an_alias_and_type_new(self, tmp_path):
        stdlib = _stdlib_root(
            tmp_path,
            {
                "sched.py": """\
                class Root:
                    def root(self): ...
                Made = type.__new__(Meta, "Made", (Root,), {"made": 1})
                Alias = Made
                """
            },
        )
        assert stdlib.defined_names("sched.Alias") == frozenset({"root", "made"})

    def test_an_import_cycle_is_unresolved(self, tmp_path):
        stdlib = _stdlib_root(
            tmp_path,
            {"sched.py": "from colorsys import *\n", "colorsys.py": "from sched import *\n"},
        )
        assert stdlib.defined_names("sched.Anything") is None

    def test_a_long_chain_is_given_up(self, tmp_path):
        body = "class C0:\n    pass\n" + "".join(
            f"class C{i}(C{i - 1}):\n    pass\n" for i in range(1, MAX_HOPS + 1)
        )
        stdlib = _stdlib_root(tmp_path, {"sched.py": body})
        assert stdlib.defined_names("sched.C1") == frozenset()
        assert stdlib.defined_names(f"sched.C{MAX_HOPS}") is None

    def test_an_unparseable_file_is_unresolved(self, tmp_path):
        stdlib = _stdlib_root(tmp_path, {"sched.py": "class (:\n"})
        assert stdlib.defined_names("sched.Base") is None

    def test_a_module_with_no_source_is_unresolved(self, tmp_path):
        stdlib = _stdlib_root(tmp_path, {})
        assert stdlib.defined_names("sched.scheduler") is None

    def test_a_non_stdlib_file_under_the_root_is_never_read(self, tmp_path):
        stdlib = _stdlib_root(tmp_path, {"not_stdlib_pkg.py": "class Base:\n    pass\n"})
        assert stdlib.defined_names("not_stdlib_pkg.Base") is None


# --- pysmelly never imports code named by its input -------------------------

SHADOW = """\
from pathlib import Path

Path(__file__).with_name("RAN_" + __name__).write_text("imported")
raise RuntimeError(__name__ + " was imported")
"""

ANALYZED = """\
import antigravity
import colorsys
import enum
import sched
import this


class Job(sched.scheduler):
    def run_all(self, blocking=None):
        pass


class Shade(colorsys.Palette):
    def mix(self, other=None):
        pass


class Colour(enum.Enum):
    def describe(self, verbose=None):
        pass


class Fly(antigravity.Wings):
    def flap(self, times=None):
        pass


class Zen(this.Koan):
    def recite(self, line=None):
        pass


def use():
    Job().run_all(True)
    Shade().mix(1)
    Colour.RED.describe(1)
    Fly().flap(2)
    Zen().recite(3)
"""


@pytest.fixture
def shadowing_project(tmp_path):
    """A project whose own modules shadow standard-library names, each with a side effect."""
    for name in ("sched", "colorsys", "enum"):
        (tmp_path / f"{name}.py").write_text(SHADOW, encoding="utf-8")
    (tmp_path / "app.py").write_text(ANALYZED, encoding="utf-8")
    return tmp_path


NAMED = {"antigravity", "this", "sched", "colorsys"}


def _ran(project):
    return sorted(path.name for path in project.glob("RAN_*"))


class TestNeverImportsInput:
    @pytest.mark.parametrize("cwd_on_path", [True, False])
    def test_the_cli_function(self, shadowing_project, monkeypatch, cwd_on_path):
        monkeypatch.chdir(shadowing_project)
        if cwd_on_path:
            monkeypatch.syspath_prepend(str(shadowing_project))
        before = set(sys.modules)
        with pytest.raises(SystemExit):
            main([".", "--no-context", "--all"])
        assert _ran(shadowing_project) == []
        assert NAMED & (set(sys.modules) - before) == set()

    def test_override_flags_with_the_project_on_sys_path(
        self, shadowing_project, monkeypatch, trees
    ):
        """The resolver reads the real stdlib source, not the project's shadow, and runs neither."""
        monkeypatch.chdir(shadowing_project)
        monkeypatch.syspath_prepend(str(shadowing_project))
        ctx = trees.code(ANALYZED)
        flags = {
            e.node.name: e.overrides_unresolved_base
            for entries in ctx.caller_values.by_file.values()
            for e in entries
        }
        assert flags == {
            "run_all": False,
            "mix": True,
            "describe": False,
            "flap": True,
            "recite": True,
            "use": False,
        }
        assert _ran(shadowing_project) == []

    def test_python_m_with_the_project_as_cwd(self, shadowing_project):
        """``-m`` puts the cwd first on ``sys.path``, where ``sched.py`` shadows the stdlib.

        ``enum.py`` is left out here: the interpreter's own ``re`` imports
        ``enum`` while ``pysmelly`` itself loads, before any analysis runs.
        """
        (shadowing_project / "enum.py").unlink()
        result = subprocess.run(
            [sys.executable, "-m", "pysmelly.cli", ".", "--no-context", "--all"],
            cwd=shadowing_project,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Traceback" not in result.stderr, result.stderr
        assert result.stdout.startswith("Parsed 3 Python files")
        assert _ran(shadowing_project) == []

    def test_named_modules_are_absent_from_sys_modules_afterwards(self, shadowing_project):
        (shadowing_project / "enum.py").unlink()
        probe = (
            "import sys\n"
            "from pysmelly.cli import main\n"
            "try:\n"
            "    main(['.', '--no-context', '--all'])\n"
            "except SystemExit:\n"
            "    pass\n"
            "print('LOADED', sorted(m for m in ('antigravity', 'this', 'sched', 'colorsys')"
            " if m in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=shadowing_project,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Traceback" not in result.stderr, result.stderr
        assert result.stdout.splitlines()[-1] == "LOADED []"
        assert _ran(shadowing_project) == []
