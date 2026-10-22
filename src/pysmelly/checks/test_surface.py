"""The test-only surface: the tests that use production code only tests use.

Phase 24 made tests evidence that never keeps production code alive: a
function, constant, exception class or parameter that only test-role code
references is reported as dead. What such a finding owes the reader is the
other half of the work item, the tests that use that code and have to be
deleted or rewritten with it. :class:`TestSurface` names them, per test
file, for dead-code, unreachable-from-entry-points, dead-constants,
dead-exceptions, always-default-params and write-only-attributes. Whether a
test goes or is rewritten stays the reader's call: a test that tests the
code goes with it, one that only uses it as a tool (a fixture faking a
dependency with it) is rewritten.

**Which tests.** Every reference is a site, a ``(file, line)``: the
graph's test-role references into a target unit (``UnitGraph.test_sites``)
or the sites a check cites (a call passing a parameter, a read of a
field). A site belongs to the top-level unit of its test file
(``ctx.unit_graph``: a top-level function, class or assignment), and, in a
top-level class, to the method that spans it. A ``@pytest.fixture`` among
the top-level functions takes with it the tests that request it by
parameter, in its own file and, for a ``conftest.py``, in the files under
its directory, and so on through fixtures requesting fixtures; in a class,
the methods requesting it.

**Inside a test class.** A method holding a site uses the targets, and so
does every method of the class that names it (``self.helper``,
``cls.helper``, or a parameter, for a fixture method), to a fixed point.
A site anywhere else in the class (its body, its decorators or bases) or
in a method no other method names (``setUp``, ``setUpTestData``, an
autouse fixture: the framework calls it for every test) is used by every
test of the class. A class all of whose tests use the targets is named
alone (``TestParse``); otherwise its tests are (``TestParse.test_a()``).

**Every test.** A test file is marked ``(every test)`` when it holds at
least one test pytest collects (a top-level ``test*`` function, or a
``test*`` method of a top-level class) and every one of them uses the
targets, so the file may go whole. A support module (a ``conftest.py``, a
helper) never is: something else may import it. Otherwise a file lists up
to :data:`MAX_TESTS_LISTED` of its tests and counts the rest.

**Files the graph cannot tie.** A check names the test files its own
evidence counted (dead-code's name-based call index, for one). A file whose
references the graph does not resolve to a target is still named, with no
tests listed under it, so no counted file is hidden.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pysmelly.checks.helpers import PYTEST_FIXTURE, ModuleResolver, filepath_to_module
from pysmelly.context import AnalysisContext
from pysmelly.pytest_config import is_collected_test_name, is_conftest

if TYPE_CHECKING:
    # reachability imports this module to describe its clusters' tests.
    from pysmelly.checks.reachability import Unit

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEF_NODES = (*_FUNCTION_NODES, ast.ClassDef)

# How many tests a file lists before counting the rest.
MAX_TESTS_LISTED = 5

# What a finding calls the tests of a surface: whether each goes or is
# rewritten is the reader's call, since a test may only use the target as a
# tool (a fixture faking a dependency) rather than test it.
TESTS_LABEL = "Tests that use {it}, to delete or rewrite"


@dataclass(frozen=True)
class TestFile:
    """One test file's share of a surface: the tests in it that use the targets, or all."""

    file: Path
    tests: tuple[str, ...]
    whole: bool

    def describe(self) -> str:
        """``tests/test_x.py (every test)``, ``tests/test_x.py: test_a(), TestB``, or the path."""
        if self.whole:
            return f"{self.file} (every test)"
        if not self.tests:
            return str(self.file)
        listed = ", ".join(self.tests[:MAX_TESTS_LISTED])
        if len(self.tests) > MAX_TESTS_LISTED:
            listed += f" (+{len(self.tests) - MAX_TESTS_LISTED} more)"
        return f"{self.file}: {listed}"


@dataclass(frozen=True)
class TestSurface:
    """The tests that use a set of production targets, by test file (sorted)."""

    tests_by_file: tuple[TestFile, ...]

    def __bool__(self) -> bool:
        return bool(self.tests_by_file)

    def describe(self) -> str:
        """Every test file and its tests, ``; ``-separated, a file all of whose tests use
        the targets marked so."""
        return "; ".join(test_file.describe() for test_file in self.tests_by_file)

    def listing(self) -> str:
        """Every test file and its tests, ``; ``-separated, for tests that change rather
        than go (they read a field): no file is marked as every test."""
        return "; ".join(
            replace(test_file, whole=False).describe() for test_file in self.tests_by_file
        )


@dataclass(frozen=True)
class _Part:
    """Where in a test file a site sits: its top-level unit and, in a top-level
    class, the method that spans it (``member``; None for the unit as a whole)."""

    unit: Unit
    member: str | None = None


def surface_of_units(
    ctx: AnalysisContext, targets: Iterable[Unit], files: Iterable[Path | str] = ()
) -> TestSurface:
    """The tests referencing any of the production units ``targets``.

    ``files`` adds test-role files the caller's own evidence counted, so each
    is named even when the graph ties none of its tests to a target.
    """
    test_sites = ctx.unit_graph.test_sites
    sites = {site for target in targets for site in test_sites.get(target, ())}
    return _surface(ctx, sites, {Path(path) for path in files})


def surface_of_sites(ctx: AnalysisContext, sites: Iterable[tuple[Path | str, int]]) -> TestSurface:
    """The tests holding the test-role ``(file, line)`` sites a check cites."""
    return _surface(ctx, {(Path(path), line) for path, line in sites}, set())


def _part_at(ctx: AnalysisContext, path: Path, line: int) -> _Part:
    """The part of ``path`` that spans ``line``; module-level code when no
    top-level statement does."""
    for stmt, units in ctx.unit_graph.statements[path]:
        if not _spans(stmt, line):
            continue
        if isinstance(stmt, ast.ClassDef):
            for member in _methods(stmt):
                if _spans(member, line):
                    return _Part(units[0], member.name)
        return _Part(units[0])
    return _Part((path, None))


def _spans(node: ast.stmt, line: int) -> bool:
    """Whether ``line`` falls inside ``node``, its decorators included."""
    decorators = node.decorator_list if isinstance(node, _DEF_NODES) else []
    first = min([node.lineno, *(decorator.lineno for decorator in decorators)])
    return first <= line <= (node.end_lineno or node.lineno)


def _methods(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [member for member in cls.body if isinstance(member, _FUNCTION_NODES)]


def _surface(ctx: AnalysisContext, sites: set[tuple[Path, int]], files: set[Path]) -> TestSurface:
    parts = {_part_at(ctx, path, line) for path, line in sites if ctx.roles.is_test(path)}
    parts |= _requesting_fixtures(ctx, parts)
    by_file: dict[Path, set[_Part]] = defaultdict(set)
    for part in parts:
        by_file[part.unit[0]].add(part)
    for path in files:
        if ctx.roles.is_test(path):
            by_file.setdefault(path, set())
    return TestSurface(
        tuple(_test_file(ctx, path, by_file[path]) for path in sorted(by_file, key=str))
    )


def _test_file(ctx: AnalysisContext, path: Path, parts: set[_Part]) -> TestFile:
    """The tests of ``path`` that ``parts`` use, by name, and whether every
    collected test of the file is among them."""
    names: list[str] = []
    collected: list[bool] = []  # per test pytest collects, whether it uses the targets
    for stmt, stmt_units in ctx.unit_graph.statements[path]:
        held = {part.member for part in parts if part.unit == stmt_units[0]}
        if isinstance(stmt, ast.ClassDef):
            tests = [m.name for m in _methods(stmt) if is_collected_test_name(m.name)]
            users = _class_users(stmt, held) if held else set()
            using = [test for test in tests if users is None or test in users]
            collected += [test in using for test in tests]
            if held and 0 < len(using) < len(tests):
                names += [f"{stmt.name}.{test}()" for test in using]
            elif held:
                names.append(stmt.name)
            continue
        if isinstance(stmt, _FUNCTION_NODES) and is_collected_test_name(stmt.name):
            collected.append(bool(held))
        if held:
            suffix = "()" if isinstance(stmt, _FUNCTION_NODES) else ""
            names += [f"{unit[1]}{suffix}" for unit in stmt_units if unit[1] is not None]
    if not names and parts:
        names = ["module-level code"]
    whole = bool(collected) and all(collected)
    return TestFile(path, tuple(dict.fromkeys(names)), whole)


def _class_users(cls: ast.ClassDef, held: set[str | None]) -> set[str] | None:
    """The methods of a test class that use the targets, given the members
    holding a site (None: the class body itself); None when every test does.

    A method naming a user (``self.helper``, a fixture parameter) is a user,
    to a fixed point. A user no other method names, that pytest does not
    collect, is a hook the framework runs for every test (``setUp``).
    """
    if None in held:
        return None
    named = {method.name: _names_in(method) for method in _methods(cls)}
    users = {member for member in held if member is not None}
    while more := {name for name, refs in named.items() if name not in users and refs & users}:
        users |= more
    named_by_others = {ref for name, refs in named.items() for ref in refs - {name}}
    if any(not is_collected_test_name(user) and user not in named_by_others for user in users):
        return None
    return users


def _names_in(method: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every attribute a method reads, and the parameters it takes (fixtures it requests)."""
    attributes = {
        node.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    return attributes | _parameters(method)


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = function.args
    return {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _requesting_fixtures(ctx: AnalysisContext, parts: set[_Part]) -> set[_Part]:
    """The test parts that request, by parameter, a top-level fixture among
    ``parts``, to a fixed point: a top-level function, or a method of a
    top-level class.

    A fixture defined in a ``conftest.py`` is visible under its directory;
    any other only in its own file.
    """
    found: set[_Part] = set()
    pending = _fixtures(ctx, [part.unit for part in parts if part.member is None])
    while pending:
        fixture_file, fixture_name = pending.pop()
        scope = fixture_file.parent if is_conftest(fixture_file) else None
        for path in ctx.all_trees:
            if not ctx.roles.is_test(path):
                continue
            if path != fixture_file and (scope is None or scope not in path.parents):
                continue
            for stmt, stmt_units in ctx.unit_graph.statements[path]:
                for member in _requesters(stmt, fixture_name):
                    part = _Part(stmt_units[0], member)
                    if part in parts or part in found:
                        continue
                    found.add(part)
                    if member is None:
                        pending += _fixtures(ctx, [part.unit])
    return found


def _requesters(stmt: ast.stmt, fixture: str) -> list[str | None]:
    """What in a top-level statement takes ``fixture`` as a parameter: the
    function itself (None), or a class's methods by name."""
    if isinstance(stmt, ast.ClassDef):
        return [method.name for method in _methods(stmt) if fixture in _parameters(method)]
    if isinstance(stmt, _FUNCTION_NODES) and fixture in _parameters(stmt):
        return [None]
    return []


def _fixtures(ctx: AnalysisContext, units: list[Unit]) -> list[tuple[Path, str]]:
    """The ``(file, name)`` of each unit that is a top-level ``@pytest.fixture`` function."""
    return [
        (path, name) for path, name in units if name is not None and _is_fixture(ctx, path, name)
    ]


def _is_fixture(ctx: AnalysisContext, path: Path, name: str) -> bool:
    """Whether the top-level function ``name`` of a test file is a ``@pytest.fixture``."""
    resolver = ModuleResolver(
        filepath_to_module(str(path), ctx.package_dirs), ctx.module_imports[path]
    )
    for stmt in ctx.all_trees[path].body:
        if isinstance(stmt, _FUNCTION_NODES) and stmt.name == name:
            targets = (d.func if isinstance(d, ast.Call) else d for d in stmt.decorator_list)
            return PYTEST_FIXTURE in resolver.resolve_all(targets)
    return False
