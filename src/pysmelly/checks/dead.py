"""Dead code extension checks — exceptions, dispatch entries, test helpers."""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from pysmelly.checks.callers import _cite_file
from pysmelly.checks.helpers import (
    PYTEST_FIXTURE,
    ModuleResolver,
    RaiseSite,
    body_without_docstring,
    build_exception_index,
    build_plugin_graph,
    caught_exception_exprs,
    class_methods,
    filepath_to_module,
    is_bare_name,
    is_caught_anywhere,
    is_imported_elsewhere,
    is_in_dunder_all,
    is_isinstance_target,
    is_referenced_as_dotted_string,
    is_referenced_as_value,
    is_subclassed,
    iter_functions,
    iter_module_classes,
    iter_module_dicts,
    loaded_names,
    raises_not_implemented,
    referenced_name,
    referenced_names,
    source_trees,
)
from pysmelly.checks.test_surface import TESTS_LABEL, surface_of_units
from pysmelly.context import AnalysisContext
from pysmelly.pytest_config import is_collected_test_name, is_conftest
from pysmelly.registry import Advice, Finding, Severity, check

# A project class named after a builtin (``class TimeoutError(Exception)``)
# shares its name with every ``except TimeoutError`` written for the builtin,
# so a catch site cannot be tied to it by name.
_BUILTIN_NAMES = frozenset(dir(builtins))

_TYPE_TESTS = frozenset({"isinstance", "issubclass"})


class _CatchSite(NamedTuple):
    """One production ``except`` clause, or ``suppress(...)`` call, naming an exception."""

    file: str
    lineno: int


@dataclass(frozen=True)
class _ProductionExceptionUses:
    """How production code uses exception names, apart from raising them.

    ``caught`` maps a name to the production sites that catch it.
    ``escaped`` holds every name production code reads in a position from
    which it could still be raised by a route this analysis does not follow:
    a construction (``E(...)``), or a value passed, stored, returned or
    compared. The positions that cannot raise are left out: an ``except``
    clause, a ``suppress(...)`` argument, an ``isinstance``/``issubclass``
    type, a class base, an annotation, and the ``raise`` statement itself,
    which ``ctx.raise_sites`` answers for. ``subclasses`` maps a base name
    to the production classes that list it.
    """

    caught: dict[str, list[_CatchSite]]
    escaped: frozenset[str]
    subclasses: dict[str, set[str]]


def _caught_exprs(node: ast.AST) -> list[ast.expr]:
    """The exception expressions an ``except`` clause or a ``suppress(...)`` call catches."""
    if isinstance(node, ast.ExceptHandler):
        return caught_exception_exprs(node)
    if isinstance(node, ast.Call) and referenced_name(node.func) == "suppress":
        return list(node.args)
    return []


def _annotation_exprs(node: ast.AST) -> list[ast.expr]:
    """The annotations *node* carries: a function's parameter and return types, or a target's."""
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    arguments = node.args
    every_arg = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    every_arg.extend(arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
    exprs = [arg.annotation for arg in every_arg if arg.annotation is not None]
    if node.returns is not None:
        exprs.append(node.returns)
    return exprs


def _non_escaping_exprs(node: ast.AST) -> list[ast.expr]:
    """The expressions under *node* that name an exception without letting it escape."""
    if isinstance(node, ast.Raise):
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        return [] if exc is None else [exc]
    if isinstance(node, ast.ClassDef):
        return list(node.bases)
    if isinstance(node, ast.Call) and referenced_name(node.func) in _TYPE_TESTS:
        return node.args[1:2]
    return [*_caught_exprs(node), *_annotation_exprs(node)]


def _production_exception_uses(ctx: AnalysisContext) -> _ProductionExceptionUses:
    """One pass over each production file: catch sites, escaping names, subclasses."""
    caught: dict[str, list[_CatchSite]] = defaultdict(list)
    escaped: set[str] = set()
    subclasses: dict[str, set[str]] = defaultdict(set)
    for filepath, tree in source_trees(ctx):
        quiet: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ExceptHandler, ast.Call)):
                for name in referenced_names(_caught_exprs(node)):
                    caught[name].append(_CatchSite(str(filepath), node.lineno))
            if isinstance(node, ast.ClassDef):
                for base_name in referenced_names(node.bases):
                    subclasses[base_name].add(node.name)
            for expr in _non_escaping_exprs(node):
                quiet.update(id(sub) for sub in ast.walk(expr))
        escaped.update(loaded_names(tree, quiet))
    return _ProductionExceptionUses(dict(caught), frozenset(escaped), dict(subclasses))


def _with_subclasses(name: str, subclasses: dict[str, set[str]]) -> set[str]:
    """*name* and every production class that inherits from it, directly or not."""
    family = {name}
    pending = [name]
    while pending:
        for sub in subclasses.get(pending.pop(), set()):
            if sub not in family:
                family.add(sub)
                pending.append(sub)
    return family


def _cite_sites(sites: list[RaiseSite] | list[_CatchSite]) -> str:
    """``a.py:3, :9, b.py:4`` — the sites grouped by file, files sorted."""
    lines: dict[str, set[int]] = defaultdict(set)
    for site in sites:
        lines[str(site.file)].add(site.lineno)
    return ", ".join(_cite_file(filepath, sorted(lines[filepath])) for filepath in sorted(lines))


def _caught_never_raised_message(
    exc_name: str, family: set[str], catches: list[_CatchSite], test_raises: list[RaiseSite]
) -> str:
    """The caught-never-raised wording: catch sites, subclasses, test-only raises."""
    subclasses = sorted(family - {exc_name})
    subject = exc_name if not subclasses else f"{exc_name} or a subclass ({', '.join(subclasses)})"
    message = (
        f"{exc_name} (exception class) is caught ({_cite_sites(catches)}) but no production "
        f"code raises or constructs {subject}"
    )
    if test_raises:
        message += (
            f"; only tests raise it ({_cite_sites(test_raises)}), and a test-only raise "
            "does not make a production handler live"
        )
    return f"{message} — the handler is dead code"


def _caught_never_raised(ctx: AnalysisContext, exc_defs: dict[str, list[dict]]) -> list[Finding]:
    """Production exception classes that production code catches but never raises.

    A raise of the class or of any production subclass, in production code,
    makes it live, as does any reference that could still lead to a raise
    (``_ProductionExceptionUses.escaped``) or a dotted-path string naming
    it. Raises in test-role code do not: the message cites them instead.
    """
    uses = _production_exception_uses(ctx)
    findings = []
    for exc_name, defs in exc_defs.items():
        if len(defs) > 1 or exc_name in _BUILTIN_NAMES:
            continue
        def_file = defs[0]["file"]
        catches = uses.caught.get(exc_name)
        if not catches or ctx.roles.is_test(def_file):
            continue
        family = _with_subclasses(exc_name, uses.subclasses)
        if family & uses.escaped or any(is_referenced_as_dotted_string(n, ctx) for n in family):
            continue
        raises = [site for name in sorted(family) for site in ctx.raise_sites.get(name, [])]
        test_raises = [site for site in raises if ctx.roles.is_test(site.file)]
        if len(test_raises) < len(raises):
            continue
        findings.append(
            Finding(
                file=def_file,
                line=defs[0]["line"],
                check="dead-exceptions",
                message=_caught_never_raised_message(exc_name, family, catches, test_raises),
                severity=Severity.MEDIUM,
            )
        )
    return findings


@check(
    "dead-exceptions",
    severity=Severity.HIGH,
    description=(
        "Custom exception classes production code never raises, catches or names, "
        "or (MEDIUM) caught in production but never raised there"
    ),
    advice=Advice(
        fix=(
            "Delete the class and the tests named; for a class only caught, delete the"
            " except clauses cited and keep the code they guard."
        ),
        dismiss=(
            "Dismiss only for a plugin or library contract: a caller outside the analyzed"
            " code raises it."
        ),
    ),
)
def check_dead_exceptions(ctx: AnalysisContext) -> list[Finding]:
    """Find custom exception classes never raised, caught, imported, subclassed, or referenced.

    Test code does not keep a class alive: one only test-role files raise,
    catch, import, subclass or name is test-only surface, and the finding
    names the tests that go with it (``checks/test_surface.py``).

    A second kind, caught-never-raised (MEDIUM), reports a production class
    that production code catches while nothing in production raises it or a
    subclass of it (:func:`_caught_never_raised`).
    """
    findings = []
    exc_defs = build_exception_index(ctx.all_trees)
    production = dict(source_trees(ctx))

    for exc_name, defs in exc_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        if ctx.roles.is_test(def_file):
            continue
        def_tree = ctx.all_trees[Path(def_file)]

        unit = (Path(def_file), exc_name)
        if _is_exception_used(ctx, exc_name, def_file, production):
            continue
        # Any other production reference the graph resolves to the class: a
        # construction returned for a caller to raise, ``mod.Cls.__name__``.
        if ctx.unit_graph.referrers.get(unit):
            continue
        if is_referenced_as_dotted_string(exc_name, ctx):
            continue
        if is_in_dunder_all(exc_name, def_tree):
            continue

        test_files = [
            path
            for path, tree in ctx.all_trees.items()
            if path not in production and _is_exception_used(ctx, exc_name, def_file, {path: tree})
        ]
        message = f"{exc_name} (exception class) has no raise/except references"
        surface = surface_of_units(ctx, [unit], test_files)
        if surface:
            message = (
                f"{exc_name} (exception class) has no raise/except references in production "
                f"code — test-only surface. {TESTS_LABEL.format(it='it')}: {surface.describe()}"
            )
        findings.append(
            Finding(
                file=def_file,
                line=defs[0]["line"],
                check="dead-exceptions",
                message=message,
                severity=Severity.HIGH,
            )
        )

    return findings + _caught_never_raised(ctx, exc_defs)


def _is_exception_used(
    ctx: AnalysisContext, exc_name: str, def_file: str, trees: dict[Path, ast.Module]
) -> bool:
    """Whether code in ``trees`` raises, catches, imports, subclasses or names ``exc_name``."""
    if any(site.file in trees for site in ctx.raise_sites.get(exc_name, [])):
        return True
    importers = ctx.references.import_index.get(exc_name, set()) - {def_file}
    value_refs = ctx.references.value_references.get(exc_name, set())
    return (
        any(Path(path) in trees for path in importers | value_refs)
        or is_caught_anywhere(exc_name, trees)
        or is_subclassed(exc_name, trees)
        or is_isinstance_target(exc_name, trees)
    )


def _find_dispatch_dicts(
    all_trees: dict[Path, ast.Module],
) -> list[dict]:
    """Find top-level dispatch dicts (str keys, Name values, 3+ entries)."""
    min_entries = 3
    results = []
    for filepath, tree in all_trees.items():
        for node, d in iter_module_dicts(tree):
            if len(d.keys) < min_entries:
                continue
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in d.keys):
                continue
            if not all(isinstance(v, ast.Name) for v in d.values):
                continue
            # Skip when all values are UPPER_CASE — constants/config, not dispatch
            if all(v.id.isupper() for v in d.values):  # type: ignore[union-attr]
                continue

            var_name = None
            if isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id

            results.append(
                {
                    "file": str(filepath),
                    "filepath": filepath,
                    "line": node.lineno,
                    "var_name": var_name,
                    "dict_node": d,
                    "assign_node": node,
                }
            )
    return results


def _is_dict_passed_or_returned(var_name: str, tree: ast.Module, assign_node: ast.Assign) -> bool:
    """Check if the dict variable is passed to a function, returned, or assigned to another name."""
    for node in ast.walk(tree):
        if node is assign_node:
            continue
        # Passed as argument
        if isinstance(node, ast.Call):
            for arg in node.args:
                if is_bare_name(arg, var_name):
                    return True
            for kw in node.keywords:
                if is_bare_name(kw.value, var_name):
                    return True
        # Returned
        if isinstance(node, ast.Return) and node.value is not None:
            if is_bare_name(node.value, var_name):
                return True
        # Assigned to another name
        if isinstance(node, ast.Assign) and node is not assign_node:
            if is_bare_name(node.value, var_name):
                return True
    return False


def _count_string_occurrences(
    key_value: str,
    all_trees: dict[Path, ast.Module],
    exclude_file: Path,
    exclude_lines: set[int],
) -> int:
    """Count occurrences of a string constant across all files, excluding the dict definition."""
    count = 0
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            if node.value != key_value:
                continue
            # Exclude the dict definition key lines
            if filepath == exclude_file and node.lineno in exclude_lines:
                continue
            count += 1
    return count


@check(
    "dead-dispatch-entries",
    severity=Severity.MEDIUM,
    description="Dispatch dict entries whose key strings appear nowhere else",
    advice=Advice(
        fix="Delete the entry, and the function it maps to if nothing else uses it.",
        dismiss=(
            "Dismiss only if the key arrives from outside the code (user input, a config"
            " file, a URL, a message payload); name that source in the ignore."
        ),
    ),
)
def check_dead_dispatch_entries(ctx: AnalysisContext) -> list[Finding]:
    """Find entries in dispatch dicts whose key strings appear nowhere else in the codebase."""
    findings = []
    dispatch_dicts = _find_dispatch_dicts(ctx.all_trees)

    for dd in dispatch_dicts:
        var_name = dd["var_name"]
        filepath = dd["filepath"]
        tree = ctx.all_trees[filepath]

        # Suppress if the dict is passed to a function, returned, or reassigned
        if var_name and _is_dict_passed_or_returned(var_name, tree, dd["assign_node"]):
            continue

        d = dd["dict_node"]
        key_lines = {k.lineno for k in d.keys if getattr(k, "lineno", None) is not None}
        for key_node in d.keys:
            key_value = key_node.value  # type: ignore[union-attr]
            occurrences = _count_string_occurrences(key_value, ctx.all_trees, filepath, key_lines)
            if occurrences == 0:
                display_name = var_name or "dict"
                findings.append(
                    Finding(
                        file=dd["file"],
                        line=dd["line"],
                        check="dead-dispatch-entries",
                        message=(
                            f'{display_name}["{key_value}"] key never appears as a string elsewhere'
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


# pytest treats every function with this prefix in a plugin module (a
# conftest.py, a ``pytest_plugins`` module, a ``pytest11`` entry point) as a
# hook implementation, and refuses to start when the name is no known hook.
PYTEST_HOOK_PREFIX = "pytest_"
PYTEST_USEFIXTURES = "pytest.mark.usefixtures"
GETFIXTUREVALUE = "getfixturevalue"


class _TestHelper(NamedTuple):
    """A module-level function in a test-role file that pytest does not collect as a test.

    ``fixture`` is its ``@pytest.fixture`` decorator, or None for a plain helper.
    """

    name: str
    file: Path
    line: int
    fixture: ast.expr | None


def _resolver(ctx: AnalysisContext, filepath: Path) -> ModuleResolver:
    return ModuleResolver(
        filepath_to_module(str(filepath), ctx.package_dirs), ctx.module_imports[filepath]
    )


def _test_helpers(ctx: AnalysisContext) -> list[_TestHelper]:
    """Every test helper and fixture in test-role files.

    ``@pytest.fixture`` is resolved through the module's imports, so
    ``from pytest import fixture`` declares a fixture too. A function with
    any other decorator is left out: the decorator may register it with a
    framework that calls it.
    """
    helpers: list[_TestHelper] = []
    for filepath, tree in ctx.all_trees.items():
        if not ctx.roles.is_test(filepath):
            continue
        resolver = _resolver(ctx, filepath)
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if is_collected_test_name(node.name):
                continue
            fixture = next(
                (
                    deco
                    for deco in node.decorator_list
                    if resolver.resolve_all([deco.func if isinstance(deco, ast.Call) else deco])
                    == [PYTEST_FIXTURE]
                ),
                None,
            )
            if fixture is None and node.decorator_list:
                continue
            helpers.append(_TestHelper(node.name, filepath, node.lineno, fixture))
    return helpers


def _is_autouse(fixture: ast.expr) -> bool:
    """Whether a ``@pytest.fixture`` decorator declares ``autouse=True``."""
    return isinstance(fixture, ast.Call) and any(
        keyword.arg == "autouse"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in fixture.keywords
    )


def _pytest_plugin_files(ctx: AnalysisContext) -> set[Path]:
    """Every analyzed module pytest registers as a plugin, so whose hooks it calls.

    Those are each ``conftest.py``, each ``pytest11`` entry-point module in
    the project's ``pyproject.toml``, and each module a ``pytest_plugins``
    declaration names. pytest reads that declaration in conftest files, in
    test modules and in the plugins it loads, so the plugins a plugin
    declares are followed too.
    """
    by_module = ctx.module_graph.by_module
    plugin_graph = build_plugin_graph(ctx.all_trees, by_module)
    plugins = {path for path in ctx.all_trees if is_conftest(path)}
    for module in ctx.pytest_config.entry_point_plugins:
        plugins.update(by_module.get(module, ()))
    readers = plugins | set(ctx.roles.files)
    pending = list(readers)
    while pending:
        for path in plugin_graph.get(pending.pop(), ()):
            plugins.add(path)
            if path not in readers:
                readers.add(path)
                pending.append(path)
    return plugins


def _string_requests(tree: ast.Module, resolver: ModuleResolver) -> Iterator[str]:
    """Fixture names a module requests by string rather than by parameter.

    ``pytest.mark.usefixtures("a", "b")``, as a decorator or a
    ``pytestmark``, requests each name it lists, and
    ``request.getfixturevalue("a")`` requests its argument.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if resolver.resolve_all([node.func]) == [PYTEST_USEFIXTURES]:
            args = node.args
        elif isinstance(node.func, ast.Attribute) and node.func.attr == GETFIXTUREVALUE:
            args = node.args[:1]
        else:
            continue
        for arg in args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg.value


def _collect_fixture_param_names(ctx: AnalysisContext) -> set[str]:
    """Every fixture name test-role files request.

    A name is requested as a parameter of any function (a test, a fixture,
    a hook), or by string (``_string_requests``).
    """
    names: set[str] = set()
    for filepath, tree in ctx.all_trees.items():
        if not ctx.roles.is_test(filepath):
            continue
        for node in iter_functions(tree):
            for arg in node.args.args:
                if arg.arg not in {"self", "cls"}:
                    names.add(arg.arg)
            for arg in node.args.kwonlyargs:
                names.add(arg.arg)
        names.update(_string_requests(tree, _resolver(ctx, filepath)))
    return names


def _parses_collected_tests(ctx: AnalysisContext) -> bool:
    """Whether any analyzed test-role file holds a test pytest collects.

    The tests are the fixtures' consumers: a module-level ``test*``
    function, or a ``test*`` method of a module-level class, since a suite
    written as test classes requests its fixtures from methods. With none
    parsed (the tests were excluded), fixture usage cannot be judged.
    """
    for filepath, tree in ctx.all_trees.items():
        if not ctx.roles.is_test(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            members = node.body if isinstance(node, ast.ClassDef) else [node]
            if any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and is_collected_test_name(member.name)
                for member in members
            ):
                return True
    return False


@check(
    "orphaned-test-helpers",
    severity=Severity.MEDIUM,
    description="Test helper functions and unused fixtures with zero callers",
    advice=Advice(
        fix="Delete the helper or fixture.",
        dismiss=(
            "Dismiss only if a test outside the analyzed files requests it, such as a"
            " fixture a pytest plugin ships to other projects."
        ),
    ),
    about_tests=True,
)
def check_orphaned_test_helpers(ctx: AnalysisContext) -> list[Finding]:
    """Find helper functions and unused fixtures in test files with zero callers.

    What pytest calls without a request is never orphaned: a fixture
    declared ``autouse=True``, and a ``pytest_`` hook implementation in a
    module pytest registers as a plugin (``_pytest_plugin_files``).
    """
    findings = []
    fixture_params = _collect_fixture_param_names(ctx)
    plugin_files = _pytest_plugin_files(ctx)

    # If test files were excluded (e.g. via --exclude tests/), conftest.py may
    # still be present but the test files that *consume* its fixtures are missing.
    # In that case we can't determine fixture usage, so skip fixture findings.
    has_test_functions = _parses_collected_tests(ctx)

    for helper in _test_helpers(ctx):
        name = helper.name
        def_file = str(helper.file)
        if name.startswith(PYTEST_HOOK_PREFIX) and helper.file in plugin_files:
            continue

        if helper.fixture is not None:
            if not has_test_functions or _is_autouse(helper.fixture):
                continue
            # Fixtures are "called" by appearing as parameter names
            if name in fixture_params:
                continue
            findings.append(
                Finding(
                    file=def_file,
                    line=helper.line,
                    check="orphaned-test-helpers",
                    message=(f"{name} fixture in {Path(def_file).name} is never requested"),
                    severity=Severity.MEDIUM,
                )
            )
        else:
            # Non-fixture helpers: check calls, imports, value references
            calls = ctx.call_index.get(name, [])
            if calls:
                continue
            if is_imported_elsewhere(name, def_file, ctx):
                continue
            if is_referenced_as_value(name, ctx):
                continue
            findings.append(
                Finding(
                    file=def_file,
                    line=helper.line,
                    check="orphaned-test-helpers",
                    message=(f"{name}() in {Path(def_file).name} has no callers"),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- dead-abstraction helpers ---


def _is_abstract_class(node: ast.ClassDef) -> bool:
    """Check if a class is abstract (inherits from ABC or has @abstractmethod methods)."""
    for base in node.bases:
        if is_bare_name(base, "ABC"):
            return True
        if isinstance(base, ast.Attribute) and base.attr == "ABC":
            return True
    for kw in node.keywords:
        if kw.arg == "metaclass":
            if is_bare_name(kw.value, "ABCMeta"):
                return True
            if isinstance(kw.value, ast.Attribute) and kw.value.attr == "ABCMeta":
                return True
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in item.decorator_list:
                if is_bare_name(deco, "abstractmethod"):
                    return True
                if isinstance(deco, ast.Attribute) and deco.attr == "abstractmethod":
                    return True
    return False


def _count_abstract_methods(node: ast.ClassDef) -> int:
    """Count the number of @abstractmethod decorated methods in a class."""
    count = 0
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in item.decorator_list:
            if (
                is_bare_name(deco, "abstractmethod")
                or isinstance(deco, ast.Attribute)
                and deco.attr == "abstractmethod"
            ):
                count += 1
    return count


def _collect_subclasses(class_name: str, all_trees: dict[Path, ast.Module]) -> list[ast.ClassDef]:
    """All classes anywhere that list class_name as a base."""
    subclasses = []
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name == class_name:
                continue
            for base in node.bases:
                if is_bare_name(base, class_name) or (
                    isinstance(base, ast.Attribute) and base.attr == class_name
                ):
                    subclasses.append(node)
                    break
    return subclasses


@check(
    "dead-abstraction",
    severity=Severity.MEDIUM,
    description="Abstract base classes with no concrete implementations",
    advice=Advice(
        fix=(
            "Delete the ABC; with one implementation, fold it into that class and annotate"
            " callers with the concrete class."
        ),
        dismiss=(
            "Dismiss only if code outside the analyzed tree implements it, as a published"
            " plugin interface."
        ),
    ),
)
def check_dead_abstractions(ctx: AnalysisContext) -> list[Finding]:
    """Find ABCs that have no concrete subclasses anywhere in the codebase.

    ABCs are created 'for extensibility' that never materializes. The plugin
    system ships, nobody writes plugins, the ABC lives on as a monument to
    speculative generality.

    An ABC with exactly one concrete implementation is the same smell one
    notch earlier — an interface with a single implementer is indirection
    without polymorphism — reported at LOW since a second implementation
    may be genuinely planned (or live outside the repo).
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        for node in iter_module_classes(tree):
            if not _is_abstract_class(node):
                continue

            if is_subclassed(node.name, ctx.all_trees):
                if is_in_dunder_all(node.name, tree):
                    continue
                subclasses = _collect_subclasses(node.name, ctx.all_trees)
                concrete = [s for s in subclasses if not _is_abstract_class(s)]
                if len(subclasses) == 1 and len(concrete) == 1:
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="dead-abstraction",
                            message=(
                                f"{node.name} (ABC) has exactly one"
                                f" implementation ({concrete[0].name}) —"
                                f" a single-implementer interface is"
                                f" indirection without polymorphism"
                            ),
                            severity=Severity.LOW,
                        )
                    )
                continue
            if is_in_dunder_all(node.name, tree):
                continue
            if is_imported_elsewhere(node.name, str(filepath), ctx):
                continue
            if is_referenced_as_value(node.name, ctx):
                continue

            abstract_count = _count_abstract_methods(node)
            method_desc = f"with {abstract_count} abstract method(s)" if abstract_count else ""

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="dead-abstraction",
                    message=(f"{node.name} (ABC {method_desc}) has no concrete implementations"),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- broken-backends helpers ---


def _body_is_raise_not_implemented(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Check if a method body is just `raise NotImplementedError` (with optional docstring)."""
    body = body_without_docstring(method.body)
    if len(body) != 1:
        return False
    return raises_not_implemented(body[0])


@check(
    "broken-backends",
    severity=Severity.MEDIUM,
    description="Non-abstract classes where every method raises NotImplementedError",
    advice=Advice(
        fix=(
            "Make the class an ABC with abstractmethods, or delete it if nothing"
            " instantiates or subclasses it."
        ),
        dismiss=(
            "Dismiss only if the class is a deliberate placeholder for a backend that"
            " raises at runtime to report the feature unavailable, and its docstring says so."
        ),
    ),
)
def check_broken_backends(ctx: AnalysisContext) -> list[Finding]:
    """Find classes where all methods raise NotImplementedError but the class isn't abstract."""
    findings = []

    for filepath, tree in source_trees(ctx):
        for node in iter_module_classes(tree):
            if _is_abstract_class(node):
                continue

            methods = [m for m in class_methods(node) if m.name != "__init__"]

            if len(methods) < 2:
                continue

            if all(_body_is_raise_not_implemented(m) for m in methods):
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="broken-backends",
                        message=(
                            f"{node.name} has {len(methods)} methods all raising"
                            f" NotImplementedError but is not abstract"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings
