"""unreachable-from-entry-points: production code no entry point reaches.

dead-code asks whether anything calls a function. Code that outlived its
design often fails a weaker test: its members call each other, or are
called by tests, and nothing the program runs reaches any of them. This
check walks forward from the entry-point index (``ctx.entry_points``) and
reports what the walk leaves out, one finding per connected cluster.

**The graph.** A node is a *unit*: a top-level function, class, or simple
``NAME = value`` assignment in a production file, or the rest of a
module's top-level code (its imports, expression statements, ``if``/
``try`` blocks and ``__main__`` guard). A class's methods and a function's
nested functions belong to the enclosing top-level unit, so reaching a
class reaches every method on it: dispatch through ``obj.method()`` cannot
be followed statically, and judging methods one by one would report every
framework hook. Test-role files hold units too, but their edges
(``test_edges``) are never followed, so a test calling a function never
reaches it.

**Edges.** A unit reaches:

- every function its calls resolve to under the Phase 21-3 rule
  (``ctx.caller_values``, production callers only);
- every analyzed definition a name it loads resolves to through its
  module's imports (``entry_points.DefinitionResolver``, and failing that a
  module whose dotted name ends with the imported one, the 21-3 rule's
  tolerance of a directory on ``sys.path``). That covers a function passed
  as a value, a name in a dispatch dict, a base class, a decorator, a type
  annotation and an attribute chain such as ``mod.func``. A dispatch dict
  bound at module level is an assignment unit, so the functions it names
  are reached when the dict is;
- the modules its import statements load, packages on the way included;
- the definition a string constant spells as a dotted path
  (``"pkg.mod.func"`` or ``"pkg.mod:func"``), or the whole module one names
  (Django settings, ``ROOT_URLCONF``, Celery's ``include``);
- a module a call loads from a file path: a string argument that is a
  module's file stem, placed by ``__file__`` beside the caller or by
  another string argument naming its directory
  (``load_sibling(__file__, "_paths")``);
- with ``getattr(x, "name")``, every top-level definition called ``name``,
  since the object is rarely resolvable;
- its own module's top-level code, which must run before any of its
  definitions can be used.

A module's top-level code also reaches the modules ``ctx.import_graph``
says it loads (imports inside functions count there), its parent
packages' ``__init__.py``, the names its ``__all__`` lists, every
top-level definition when it calls ``globals()``, and every top-level
definition carrying a decorator that may register it (any decorator
outside :data:`TRANSPARENT_DECORATORS`, such as ``@app.route`` or a
project's ``@register``), since the registry calls it. A module loaded by
name, as a whole-module root, by a dotted string or from a path, reaches
every top-level definition it holds: whatever loads a module by name reads
its attributes by name too.

**Roots.** Every resolved root of ``ctx.entry_points`` in production code.
A script module (a ``__main__`` guard, a ``__main__.py`` or a ``#!`` line
1; Phase 35-1) roots its top-level code only, so its other definitions are
judged like any others; a module loaded by name roots every definition.
A repo with none abstains, with a note on stderr, rather than reporting
everything. That includes a library package with no scripts: what other
projects import from it is a root this tool cannot see, so reporting from
its ``__init__.py`` exports alone would flag internals the exports reach
only through a caller in another repo (DECISIONS.md, Phase 26-2).

**Orphan modules.** A production module that no unit of another file
refers to (nothing analyzed imports it, names it or calls into it) and that
the walk does not reach is left out. Having no guard or shebang, it is a
script run some way the index cannot see, a module only another repo
imports, or dead; reachability cannot tell which, and dead-code still
judges its public functions.

**Clusters.** The unreached units are grouped into connected components
over the edges between them, read in either direction. A test-role unit
that references several unreached units joins them too, since that test
holds them alive together; the finding names the test files. An unreached
module's top-level code joins the component of its definitions, so a
module only unreached code or tests import comes out as one cluster. A
component is reported when it holds a function or class, unless it is a
single public free function, which dead-code already reports.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from itertools import pairwise
from pathlib import Path

from pysmelly.checks.entry_points import DefinitionResolver
from pysmelly.checks.helpers import (
    INIT_FILE,
    assigned_pairs,
    attr_root_name,
    decorator_target,
    dotted_strings,
    filepath_to_package,
    import_bindings,
    is_bare_name,
    resolve_relative_import,
)
from pysmelly.checks.test_surface import TESTS_LABEL, TestSurface, surface_of_units
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

CHECK = "unreachable-from-entry-points"

NO_ROOTS_NOTE = (
    f"pysmelly: {CHECK}: no entry point found in the analyzed code "
    "(no [project.scripts] or entry-points, __main__ block, CLI command or framework root), "
    "so nothing is reported as unreachable"
)

# Decorators that wrap or describe a definition without registering it
# anywhere: a definition carrying only these is reached only through
# references. Any other decorator may register what it decorates (a route,
# a hook, a check), so the definition is reached with its module.
TRANSPARENT_DECORATORS = frozenset(
    {
        "functools.cache",
        "functools.lru_cache",
        "functools.wraps",
        "functools.cached_property",
        "functools.total_ordering",
        "functools.singledispatch",
        "contextlib.contextmanager",
        "contextlib.asynccontextmanager",
        "dataclasses.dataclass",
        "typing.overload",
        "typing.final",
        "typing.runtime_checkable",
        "typing_extensions.overload",
        "typing_extensions.final",
        "typing_extensions.runtime_checkable",
        "abc.abstractmethod",
        "enum.unique",
    }
)
# Builtin decorators, recognised by bare name when nothing rebinds it.
_TRANSPARENT_BUILTINS = frozenset({"property", "staticmethod", "classmethod"})

# How many members a finding lists before counting the rest: enough that the
# largest cluster the Phase 26-2 fleet measure found (24) prints whole.
MAX_LISTED_MEMBERS = 30

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

# A unit: a top-level definition's name in its file, or None for the
# module's remaining top-level code. Public: the cascade and Phase 29's
# finding chains name definitions this way.
Unit = tuple[Path, str | None]


class MemberKind(StrEnum):
    """What kind of top-level definition a reportable unit is."""

    FUNCTION = "function"
    CLASS = "class"
    ASSIGNMENT = "assignment"


@dataclass(frozen=True)
class Member:
    """A reportable unit: a top-level function, class or assignment."""

    file: Path
    name: str
    line: int
    kind: MemberKind

    def title(self) -> str:
        """The name, with ``()`` for a function."""
        suffix = "()" if self.kind is MemberKind.FUNCTION else ""
        return f"{self.name}{suffix}"

    def label(self) -> str:
        return f"{self.title()}:{self.line}"


def _member_kind(stmt: ast.stmt) -> MemberKind:
    return MemberKind.CLASS if isinstance(stmt, ast.ClassDef) else MemberKind.FUNCTION


def _assigned_names(stmt: ast.stmt) -> list[str]:
    """The names a simple top-level assignment binds; dunders (``__all__``) excluded."""
    targets = [target for target, _ in assigned_pairs(stmt)]
    if not all(isinstance(target, ast.Name) for target in targets):
        return []
    return [t.id for t in targets if isinstance(t, ast.Name) and not t.id.startswith("__")]


def _dunder_all_names(stmt: ast.stmt) -> list[str]:
    """The strings a top-level ``__all__ = [...]`` or ``__all__ += [...]`` lists."""
    # ``__all__ += [...]`` extends the list, so an augmented assignment counts too.
    pairs = [(stmt.target, stmt.value)] if isinstance(stmt, ast.AugAssign) else assigned_pairs(stmt)
    values = [value for target, value in pairs if is_bare_name(target, "__all__")]
    if not values:
        return []
    value = values[0]
    if not isinstance(value, (ast.List, ast.Tuple)):
        return []
    return [
        elt.value
        for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def _imported_modules(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """Every module an import statement may load: each package on the way, and for
    a from-import each imported name, which may be a submodule."""
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:
        source = resolve_relative_import(node.module, node.level, package)
        names = [source, *(f"{source}.{alias.name}" for alias in node.names)]
    return [
        ".".join(parts[:depth])
        for parts in (name.split(".") for name in names)
        for depth in range(1, len(parts) + 1)
    ]


def _getattr_literals(node: ast.AST) -> list[str]:
    """The attribute name a ``getattr(obj, "name", ...)`` call spells out."""
    if not (isinstance(node, ast.Call) and is_bare_name(node.func, "getattr")):
        return []
    args = node.args
    if len(args) < 2 or not isinstance(args[1], ast.Constant):
        return []
    return [args[1].value] if isinstance(args[1].value, str) else []


def _path_load_arguments(call: ast.Call) -> tuple[set[str], bool]:
    """A call's string arguments, and whether ``__file__`` appears in any argument."""
    args = [*call.args, *(kw.value for kw in call.keywords)]
    strings = {
        arg.value for arg in args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    beside = any(is_bare_name(node, "__file__") for arg in args for node in ast.walk(arg))
    return strings, beside


class UnitGraph:
    """The production units, the edges between them, and the roots.

    Built once per run as ``ctx.unit_graph``. unreachable-from-entry-points
    walks it forward from :attr:`roots`; the dead-code cascade
    (``checks/cascade.py``) reads :attr:`referrers` to find what a removal
    leaves with no production referrer.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.resolver = DefinitionResolver(ctx)
        self.production = {
            path: tree for path, tree in ctx.all_trees.items() if path not in ctx.roles.files
        }
        self.members: dict[Unit, Member] = {}
        self.by_name: dict[str, set[Unit]] = defaultdict(set)
        self.edges: dict[Unit, set[Unit]] = defaultdict(set)
        # What each test-role unit references in production. Never followed
        # for reachability; it joins the clusters one test holds alive.
        self.test_edges: dict[Unit, set[Unit]] = defaultdict(set)
        # Per production unit, the ``(file, line)`` of every test-role
        # reference into it: finer than ``test_edges``, so the test-only
        # surface can tell which of a test class's methods hold one.
        self.test_sites: dict[Unit, set[tuple[Path, int]]] = defaultdict(set)
        # Per analyzed file, test-role ones included, each top-level statement
        # with the units it belongs to (``(path, None)`` for module-level code).
        self.statements: dict[Path, list[tuple[ast.stmt, list[Unit]]]] = {}
        self._unit_of_call: dict[int, Unit] = {}
        self._unit_of_function: dict[int, Unit] = {}
        self._targets_cache: dict[str, frozenset[Unit]] = {}
        # Each module by every trailing run of its dotted name, for the
        # Phase 21-3 rule's tolerance of a directory on sys.path
        # (``from works.mime import f`` reaching ``myapp/works/mime``).
        self._by_suffix: dict[str, list[Path]] = defaultdict(list)
        for module, paths in sorted(self.resolver.by_module.items()):
            parts = module.split(".")
            for start in range(1, len(parts)):
                self._by_suffix[".".join(parts[start:])].extend(paths)
        for path, tree in ctx.all_trees.items():
            self._collect_units(path, tree)
        self._members_by_file: dict[Path, list[Unit]] = defaultdict(list)
        for unit in self.members:
            self._members_by_file[unit[0]].append(unit)
        self._by_stem: dict[str, list[Path]] = defaultdict(list)
        for path in self.production:
            if path.name != INIT_FILE:
                self._by_stem[path.stem].append(path)
        for path in ctx.all_trees:
            self._reference_edges(path)
        self._module_edges()
        self._call_edges()

    def _collect_units(self, path: Path, tree: ast.Module) -> None:
        """Each top-level statement of ``path`` with the units it belongs to;
        a production file's definitions are registered as members."""
        production = path in self.production
        owners: list[tuple[ast.stmt, list[Unit]]] = []
        for stmt in tree.body:
            if isinstance(stmt, _DEF_NODES):
                names = [stmt.name]
                kind = _member_kind(stmt)
            else:
                names = _assigned_names(stmt)
                kind = MemberKind.ASSIGNMENT
            units: list[Unit] = [(path, name) for name in names]
            if production:
                for unit, name in zip(units, names, strict=True):
                    self.members.setdefault(unit, Member(path, name, stmt.lineno, kind))
                    self.by_name[name].add(unit)
            owners.append((stmt, units or [(path, None)]))
        self.statements[path] = owners

    def unit_for(self, path: Path, qualname: str | None) -> Unit:
        """The unit holding the definition ``qualname`` in ``path``."""
        if qualname is None:
            return (path, None)
        head = (path, qualname.partition(".")[0])
        return head if head in self.members else (path, None)

    def _module_files(self, module: str) -> list[Path]:
        """The files a module name names: exactly, else as the tail of a longer name."""
        return self.resolver.module_files(module) or self._by_suffix.get(module, [])

    def _module_targets(self, module: str) -> set[Unit]:
        return {(path, None) for path in self._module_files(module) if path in self.production}

    def _definition_targets(self, dotted: str) -> set[Unit]:
        """The unit of the definition ``dotted`` names, following re-exports; failing
        that, the definitions it names when its module part is a tail of a longer name."""
        found = self.resolver.definition(dotted)
        if found is not None:
            return {self.unit_for(found.file, found.qualname)}
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            module, qualname = ".".join(parts[:split]), ".".join(parts[split:])
            units = {
                self.unit_for(path, qualname)
                for path in self._by_suffix.get(module, ())
                if qualname in self.resolver.defs[path]
            }
            if units:
                return units
        return set()

    def _targets(self, dotted: str) -> frozenset[Unit]:
        """The production units an absolute dotted path names: a definition, a module, or both."""
        cached = self._targets_cache.get(dotted)
        if cached is not None:
            return cached
        units = self._definition_targets(dotted) | self._module_targets(dotted)
        result = frozenset(unit for unit in units if unit[0] in self.production)
        self._targets_cache[dotted] = result
        return result

    def _reference_edges(self, path: Path) -> None:
        """What each unit of ``path`` references; a test-role file's go to ``test_edges``."""
        production = path in self.production
        edges = self.edges if production else self.test_edges
        bindings = import_bindings(self.ctx.module_imports[path])
        own = {member.name for member in self.members.values() if member.file == path}
        package = filepath_to_package(str(path), self.ctx.package_dirs)
        module_unit: Unit = (path, None)
        for stmt, units in self.statements[path]:
            targets: set[Unit] = set()
            for node in ast.walk(stmt):
                found = set(self._node_targets(path, node, bindings, own))
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for module in _imported_modules(node, package):
                        found |= self._module_targets(module)
                targets |= found
                if not production and isinstance(node, (ast.expr, ast.stmt)):
                    for target in found:
                        self.test_sites[target].add((path, node.lineno))
                if isinstance(node, ast.Call):
                    self._unit_of_call[id(node)] = units[0]
                    if production and is_bare_name(node.func, "globals"):
                        edges[module_unit].update(self._members_by_file[path])
                elif isinstance(node, _FUNCTION_NODES):
                    self._unit_of_function[id(node)] = units[0]
            if production:
                for name in _dunder_all_names(stmt):
                    targets.update(self._name_targets(path, ast.Name(id=name, ctx=ast.Load())))
                if isinstance(stmt, _DEF_NODES) and not self._transparent(
                    path, stmt, bindings, own
                ):
                    edges[module_unit].update(units)
            for unit in units:
                edges[unit].update(targets)

    def _name_targets(self, path: Path, expr: ast.expr) -> frozenset[Unit]:
        dotted = self.resolver.name(path, expr)
        return frozenset() if dotted is None else self._targets(dotted)

    def _node_targets(
        self, path: Path, node: ast.AST, bindings: dict[str, str], own: set[str]
    ) -> Iterable[Unit]:
        """The units one AST node references: a loaded name or attribute chain,
        a dotted string, the literal name of a ``getattr``, or a module a call
        loads from a path."""
        if isinstance(node, (ast.Name, ast.Attribute)):
            if not isinstance(node.ctx, ast.Load):
                return []
            head = attr_root_name(node)
            if head in bindings or (head in own and isinstance(node, ast.Attribute)):
                return self._name_targets(path, node)
            return [(path, head)] if head in own else []
        found: set[Unit] = set()
        for dotted in dotted_strings(node):
            found |= self._loaded_by_name(self._targets(dotted))
        for name in _getattr_literals(node):
            found |= self.by_name.get(name, set())
        if isinstance(node, ast.Call):
            found |= self._loaded_by_path(path, node)
        return found

    def _loaded_by_path(self, path: Path, call: ast.Call) -> set[Unit]:
        """The modules a call may load from a file path: a string argument that is a
        module's file stem, where another argument places it, by ``__file__`` beside
        the calling file or by a string naming its directory
        (``load_sibling(__file__, "_paths")``, ``load_script("nightly-job", "sentinel")``)."""
        strings, beside = _path_load_arguments(call)
        modules = {
            module
            for text in strings
            for module in self._by_stem.get(text, ())
            if (beside and module.parent == path.parent) or module.parent.name in strings
        }
        return self._loaded_by_name((module, None) for module in modules)

    def _transparent(
        self,
        path: Path,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        bindings: dict[str, str],
        own: set[str],
    ) -> bool:
        """Whether every decorator on ``stmt`` only wraps it, registering it nowhere."""
        for decorator in stmt.decorator_list:
            target = decorator_target(decorator)
            if (
                isinstance(target, ast.Name)
                and target.id in _TRANSPARENT_BUILTINS
                and target.id not in bindings
                and target.id not in own
            ):
                continue
            if self.resolver.name(path, target) not in TRANSPARENT_DECORATORS:
                return False
        return True

    def _module_edges(self) -> None:
        for unit in self.members:
            self.edges[unit].add((unit[0], None))
        for path in self.production:
            module_unit: Unit = (path, None)
            for imported in self.ctx.import_graph.get(path, ()):
                if imported in self.production:
                    self.edges[module_unit].add((imported, None))
            for parent in path.parents:
                package = parent / INIT_FILE
                if package != path and package in self.production:
                    self.edges[module_unit].add((package, None))

    def _call_edges(self) -> None:
        """Calls the Phase 21-3 rule resolves to a function outside a class body;
        a test-role caller's go to ``test_edges``."""
        for path, definition, call in self.ctx.caller_values.function_calls():
            callee = self._unit_of_function.get(id(definition))
            caller = self._unit_of_call.get(id(call["node"]))
            if path not in self.production or callee is None or caller is None:
                continue
            if self.ctx.roles.is_test(call["file"]):
                self.test_edges[caller].add(callee)
                self.test_sites[callee].add((caller[0], call["node"].lineno))
            else:
                self.edges[caller].add(callee)

    def _loaded_by_name(self, units: Iterable[Unit]) -> set[Unit]:
        """``units``, with every top-level definition of each module among them:
        a host that loads a module by name reads its attributes by name too
        (Django's settings, a URLconf's ``urlpatterns``, a plugin's hooks)."""
        loaded = set(units)
        for path, name in list(loaded):
            if name is None:
                loaded.update(self._members_by_file.get(path, ()))
        return loaded

    @cached_property
    def roots(self) -> frozenset[Unit]:
        """The units holding a resolved entry-point root in production code; a
        whole-module root brings every top-level definition of its module, while
        a module run as a script roots only its top-level code."""
        index = self.ctx.entry_points
        loaded: list[Unit] = []
        scripts: list[Unit] = []
        for root in index.roots:
            if root.file is None or root.file not in self.production:
                continue
            unit = self.unit_for(root.file, root.qualname)
            (scripts if root.script_module else loaded).append(unit)
        return frozenset(self._loaded_by_name(loaded)) | frozenset(scripts)

    @cached_property
    def referrers(self) -> dict[Unit, frozenset[Unit]]:
        """Per unit, the production units with an edge into it, itself left out.
        Test-role units are never referrers: their edges are ``test_edges``."""
        found: dict[Unit, set[Unit]] = defaultdict(set)
        for source, targets in self.edges.items():
            for target in targets:
                if target != source:
                    found[target].add(source)
        return {unit: frozenset(sources) for unit, sources in found.items()}

    def all_units(self) -> set[Unit]:
        return set(self.members) | {(path, None) for path in self.production}

    def orphan_files(self) -> set[Path]:
        """The production files no unit of another file, production or test, refers
        to: nothing analyzed imports them, names them or calls into them."""
        referred = {
            target[0]
            for edges in (self.edges, self.test_edges)
            for source, targets in edges.items()
            for target in targets
            if target[0] != source[0]
        }
        return set(self.production) - referred


def _reach(graph: UnitGraph, roots: frozenset[Unit]) -> set[Unit]:
    reached = set(roots)
    pending = list(roots)
    while pending:
        unit = pending.pop()
        for target in graph.edges.get(unit, ()):
            if target not in reached:
                reached.add(target)
                pending.append(target)
    return reached


def _clusters(graph: UnitGraph, unreached: set[Unit]) -> list[set[Unit]]:
    """The connected components of the unreached units, edges read both ways.

    A test-role unit referencing several unreached units joins them: the
    test holds them alive together, so removing them is one piece of work.
    """
    neighbours: dict[Unit, set[Unit]] = defaultdict(set)
    for source in unreached:
        for target in graph.edges.get(source, ()):
            if target in unreached and target != source:
                neighbours[source].add(target)
                neighbours[target].add(source)
    for targets in graph.test_edges.values():
        held = sorted((unit for unit in targets if unit in unreached), key=_unit_order)
        for first, second in pairwise(held):
            neighbours[first].add(second)
            neighbours[second].add(first)
    seen: set[Unit] = set()
    components = []
    for start in sorted(unreached, key=_unit_order):
        if start in seen:
            continue
        component = {start}
        pending = [start]
        seen.add(start)
        while pending:
            for other in neighbours[pending.pop()]:
                if other not in seen:
                    seen.add(other)
                    component.add(other)
                    pending.append(other)
        components.append(component)
    return components


def _unit_order(unit: Unit) -> tuple[str, str]:
    return (str(unit[0]), unit[1] or "")


def _is_dead_code_subject(ctx: AnalysisContext, member: Member) -> bool:
    """Whether dead-code judges ``member``: a public, undecorated free function."""
    return member.kind is MemberKind.FUNCTION and any(
        entry["file"] == str(member.file) for entry in ctx.function_index.get(member.name, [])
    )


def _render(members: list[Member], unreached_modules: frozenset[Path], tests: TestSurface):
    def render(indices: tuple[int, ...]) -> str | None:
        chosen = [members[i] for i in indices]
        if not any(m.kind is not MemberKind.ASSIGNMENT for m in chosen):
            return None
        listed = chosen[:MAX_LISTED_MEMBERS]
        by_file: dict[Path, list[Member]] = {}
        for member in listed:
            by_file.setdefault(member.file, []).append(member)
        listing = "; ".join(
            f"{path}{' (module unreached)' if path in unreached_modules else ''}: "
            + ", ".join(m.label() for m in group)
            for path, group in by_file.items()
        )
        if len(chosen) > len(listed):
            listing += f" (+{len(chosen) - len(listed)} more)"
        message = f"{len(chosen)} definition(s) no entry point reaches: {listing}"
        if tests:
            message += f". {TESTS_LABEL.format(it='them')}: {tests.describe()}"
        return message

    return render


@check(
    CHECK,
    severity=Severity.MEDIUM,
    description=(
        "Clusters of production code no entry point reaches (scripts, __main__, CLI, "
        "framework roots); test callers do not count"
    ),
    advice=Advice(
        fix="Delete the cluster's definitions together, with the tests named.",
        dismiss=(
            "Dismiss only if a root pysmelly cannot see (a plugin entry point, a framework"
            " hook, a file the config excludes) runs the cluster; name it in the ignore."
        ),
    ),
)
def check_unreachable_from_entry_points(ctx: AnalysisContext) -> list[Finding]:
    """One finding per connected cluster of units the walk from the roots misses."""
    graph = ctx.unit_graph
    roots = graph.roots
    if not roots:
        sys.stderr.write(f"{NO_ROOTS_NOTE}\n")
        return []
    reached = _reach(graph, roots)
    orphans = {path for path in graph.orphan_files() if (path, None) not in reached}
    unreached = {unit for unit in graph.all_units() - reached if unit[0] not in orphans}
    unreached_modules = frozenset(path for path, name in unreached if name is None)
    findings = []
    for component in _clusters(graph, unreached):
        members = sorted(
            (graph.members[unit] for unit in component if unit in graph.members),
            key=lambda m: (str(m.file), m.line, m.name),
        )
        if not any(m.kind is not MemberKind.ASSIGNMENT for m in members):
            continue
        if len(members) == 1 and _is_dead_code_subject(ctx, members[0]):
            continue
        sites = [Site(str(m.file), m.line) for m in members]
        render = _render(members, unreached_modules, surface_of_units(ctx, component))
        findings.append(multi_site_finding(CHECK, Severity.MEDIUM, sites, render))
    return findings
