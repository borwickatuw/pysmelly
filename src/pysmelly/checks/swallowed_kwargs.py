"""swallowed-kwargs: keyword arguments a ``**kwargs`` sink silently drops.

A caller passes ``foo=`` to a function that has no ``foo`` parameter but takes
``**kwargs``, so the call is accepted. If nothing downstream ever looks at
``"foo"``, the option does nothing, and the caller believes it still works.
This is the usual remains of a parameter removed from a signature that kept
``**kwargs``, or of an option renamed on one side only.

**Callers.** Every call in the analyzed code that passes an explicit keyword
(``foo=...``; a ``**splat`` passes nothing this check can name) and resolves
to a project function:

- a module-level or nested function, through ``ctx.caller_values`` (the
  Phase 21-3 rule: by module and import, never by bare name);
- ``Cls(...)``, a module-level project class named through the module's
  imports or its own definitions, to the ``__init__`` it or a single-base
  project ancestor defines. A decorated class, a class with class keywords
  (a metaclass) and a class defining ``__new__`` are not followed;
- ``Cls.m(...)`` for a method the named class or a single-base ancestor
  defines;
- ``self.m(...)`` (or ``cls.m``) in a method, when exactly one analyzed class
  defines ``m`` and it is the enclosing class or one of its single-base
  project ancestors;
- ``super().m(...)`` in a method of a single-base class that no project class
  lists among several bases (where the MRO could put a mixin next), to the
  first single-base project ancestor that defines ``m``.

Anything else, including a method called on an object the analysis cannot
type, is unresolved and has no callee.

**The sink.** The keyword must bind no named parameter of the callee (a
positional-only one does not bind by name). A decorated callee is skipped,
since a decorator can consume or rewrite keywords (``staticmethod``,
``classmethod`` and ``override`` are transparent), and so is a stub body
(``pass``, ``...``, ``raise NotImplementedError``): an interface or a no-op
on purpose.

**Reading the collector.** Every load of the ``**kwargs`` name in the
callee, nested functions included, is one of:

- a read of a constant key: ``kwargs["foo"]``, ``kwargs.get("foo")``,
  ``.pop("foo")``, ``.setdefault("foo")``, ``"foo" in kwargs`` and
  ``del kwargs["foo"]`` (dropping it on purpose is handling it);
- a write (``kwargs["k"] = v``, ``kwargs.update(...)``), which reads nothing;
- a forward: ``**kwargs`` in a call;
- anything else, which may read every key: iterating it, ``.items()``,
  a key that is not a constant, a truth test or ``len``, passing it as a
  value, storing or returning it, ``{**kwargs}``, rebinding the name.

Any use of the last kind, or a ``locals()``/``vars()`` call, and the callee
is opaque: nothing is reported for it. A read of ``"foo"`` keeps ``foo=``.

**Following forwards.** Each forward is resolved as a call (above). A
forward that cannot be resolved (a library, a builtin, a method on an
object) may accept anything, so nothing is reported; neither is a forward
call that also passes ``foo=`` itself. A resolved target with a ``foo``
parameter accepts it. A target with ``**kwargs`` is judged the same way,
for at most ``MAX_FORWARD_HOPS`` forwards from the callee, and a cycle
abstains. A target with neither drops the call with ``TypeError`` when it
is reached, which the message says. ``foo=`` is reported only when every
forward the callee makes ends in a drop.

**Roles.** The finding sits at the production callers that pass the
keyword and counts them; a keyword only tests pass reports nothing, and the
message names the test files that pass it too.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from pysmelly.checks.callers import _Callers, _cite_test_sites, _is_stub_body, _reaches_module
from pysmelly.checks.helpers import (
    class_methods,
    is_bare_name,
    iter_module_classes,
    referenced_name,
)
from pysmelly.checks.records import _ModuleNames, _qualify
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

CHECK_NAME = "swallowed-kwargs"

# Forwards followed from the callee the caller names; a longer chain abstains.
MAX_FORWARD_HOPS = 3

# Decorators that leave the keywords a call binds as they are.
_TRANSPARENT_DECORATORS = frozenset({"staticmethod", "classmethod", "override"})
# Dict methods that read the constant key they are given.
_KEY_READ_METHODS = frozenset({"get", "pop", "setdefault"})
# Dict methods that add keys and read none.
_KEY_WRITE_METHODS = frozenset({"update"})
# Calls that hand a function's locals, the collector among them, to anything.
_INTROSPECTION = frozenset({"locals", "vars"})

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, eq=False)
class _Function:
    """A project function or method, where it is defined, and its class if a method.

    ``parents`` is the child-to-parent map of the defining module. The
    resolver hands out one instance per definition, so ``use`` is worked
    out once.
    """

    node: _FunctionNode
    path: Path
    owner: ast.ClassDef | None
    parents: dict[ast.AST, ast.AST] = field(repr=False)

    @property
    def label(self) -> str:
        prefix = f"{self.owner.name}." if self.owner is not None else ""
        return f"{prefix}{self.node.name}()"

    @property
    def where(self) -> str:
        return f"{self.path}:{self.node.lineno}"

    @property
    def collector(self) -> str | None:
        """The name of the ``**kwargs`` parameter; None when there is none."""
        return self.node.args.kwarg.arg if self.node.args.kwarg is not None else None

    @property
    def receives(self) -> bool:
        """Whether the first positional parameter is the receiver a call fills."""
        return self.owner is not None and not any(
            referenced_name(dec) == "staticmethod" for dec in self.node.decorator_list
        )

    def binds(self, keyword: str) -> bool:
        """Whether ``keyword=`` binds a named parameter (never a positional-only one)."""
        args = self.node.args
        named = list(args.args)
        if self.receives and not args.posonlyargs and named:
            named = named[1:]
        return any(arg.arg == keyword for arg in [*named, *args.kwonlyargs])

    @cached_property
    def use(self) -> _CollectorUse | None:
        """What the body does with its ``**kwargs``; None when it takes none."""
        collector = self.collector
        if collector is None:
            return None
        return _collector_use(self.node, collector, self.parents)

    def verdict(self, keyword: str) -> _Drop | tuple[ast.Call, ...] | None:
        """What this function alone does with ``keyword=`` in its ``**kwargs``.

        A ``_Drop`` when it ends here, the forwards to follow when it goes
        on, None when the keyword may be kept (bound, read, or opaque).
        """
        if self.binds(keyword):
            return None
        use = self.use
        if use is None:
            return _Drop((self,), rejects=True)
        if not _plain(self.node) or use.opaque or keyword in use.reads:
            return None
        if not use.forwards:
            return _Drop((self,), rejects=False)
        if any(kw.arg == keyword for forward in use.forwards for kw in forward.keywords):
            return None
        return use.forwards


@dataclass(frozen=True)
class _CollectorUse:
    """What a function does with its ``**kwargs``.

    ``reads`` holds the constant keys it reads. ``opaque`` says some use may
    read any key. ``forwards`` are the calls it splats the collector into.
    """

    reads: frozenset[str]
    opaque: bool
    forwards: tuple[ast.Call, ...]


def _constant_key(expr: ast.expr | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _collector_use(
    node: _FunctionNode, name: str, parents: dict[ast.AST, ast.AST]
) -> _CollectorUse:
    """Classify every load of ``name`` in ``node`` (see the module docstring)."""
    reads: set[str] = set()
    forwards: list[ast.Call] = []
    opaque = False
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in _INTROSPECTION
        ):
            opaque = True
        if not is_bare_name(child, name):
            continue
        if not isinstance(child.ctx, ast.Load):
            opaque = True
            continue
        parent = parents.get(child)
        if isinstance(parent, ast.keyword) and parent.arg is None:
            call = parents.get(parent)
            if isinstance(call, ast.Call):
                forwards.append(call)
                continue
        key, handled = _key_use(child, parent, parents)
        if key is not None:
            reads.add(key)
        elif not handled:
            opaque = True
    return _CollectorUse(frozenset(reads), opaque, tuple(forwards))


def _key_use(
    load: ast.Name, parent: ast.AST | None, parents: dict[ast.AST, ast.AST]
) -> tuple[str | None, bool]:
    """The constant key one load of the collector reads, and whether the use is understood.

    ``(key, True)`` for a read of ``key``; ``(None, True)`` for a write;
    ``(None, False)`` for anything that may read any key.
    """
    if isinstance(parent, ast.Subscript) and parent.value is load:
        key = _constant_key(parent.slice)
        if key is None:
            return None, False
        if isinstance(parent.ctx, ast.Store):
            return None, True
        return key, True
    if isinstance(parent, ast.Attribute) and parent.value is load:
        call = parents.get(parent)
        if not (isinstance(call, ast.Call) and call.func is parent):
            return None, False
        if parent.attr in _KEY_WRITE_METHODS:
            return None, True
        if parent.attr in _KEY_READ_METHODS and call.args:
            key = _constant_key(call.args[0])
            return key, key is not None
        return None, False
    if (
        isinstance(parent, ast.Compare)
        and len(parent.ops) == 1
        and isinstance(parent.ops[0], (ast.In, ast.NotIn))
        and parent.comparators[0] is load
    ):
        key = _constant_key(parent.left)
        return key, key is not None
    return None, False


class _Resolver:
    """Resolves a call to the one project function it reaches (module docstring)."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.paths = {str(path): path for path in ctx.all_trees}
        self._names = {path: _ModuleNames(path, tree, ctx) for path, tree in ctx.all_trees.items()}
        self._made: dict[int, _Function] = {}
        self._classes: dict[str, list[tuple[str, ast.ClassDef, Path]]] = defaultdict(list)
        self._class_paths: dict[int, Path] = {}
        method_counts: Counter[str] = Counter()
        mixed: set[str] = set()
        for path, tree in ctx.all_trees.items():
            for cls in iter_module_classes(tree):
                self._classes[cls.name].append((self._names[path].module, cls, path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self._class_paths[id(node)] = path
                    method_counts.update(method.name for method in class_methods(node))
                    if len(node.bases) > 1:
                        mixed.update(filter(None, map(referenced_name, node.bases)))
        self._method_counts = method_counts
        self._mixed = frozenset(mixed)
        self._functions: dict[int, list[_Function]] = defaultdict(list)
        for path, node, call in ctx.caller_values.function_calls():
            self._functions[id(call["node"])].append(self._function(node, path, None))

    def _function(self, node: _FunctionNode, path: Path, owner: ast.ClassDef | None) -> _Function:
        """The one ``_Function`` for a definition."""
        if id(node) not in self._made:
            self._made[id(node)] = _Function(node, path, owner, self._parents(path))
        return self._made[id(node)]

    def _parents(self, path: Path) -> dict[ast.AST, ast.AST]:
        return self.ctx.parent_map(self.ctx.all_trees[path])

    def target(self, path: Path, call: ast.Call) -> _Function | None:
        """The project function ``call`` reaches, or None when it is not known."""
        functions = self._functions.get(id(call), [])
        if functions:
            return functions[0] if len(functions) == 1 else None
        func = call.func
        if isinstance(func, ast.Attribute):
            receiver = func.value
            if (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id == "super"
            ):
                return self._super_target(path, call, func.attr)
            if isinstance(receiver, ast.Name):
                method = self._self_target(path, call, receiver.id, func.attr)
                if method is not None:
                    return method
            cls = self._class(path, receiver)
            return None if cls is None else self._find_method(cls, func.attr)
        cls = self._class(path, func)
        return None if cls is None else self._constructor(cls)

    # --- classes ---

    def _class(self, path: Path, expr: ast.expr) -> ast.ClassDef | None:
        """The module-level project class ``expr`` names in the module at ``path``."""
        qualified = _qualify(expr, self._names[path])
        if qualified is None:
            return None
        module, _, name = qualified.rpartition(".")
        found = [
            cls
            for defining, cls, _path in self._classes.get(name, [])
            if module and _reaches_module(module, defining)
        ]
        return found[0] if len(found) == 1 else None

    def _base(self, cls: ast.ClassDef) -> ast.ClassDef | None:
        """The one project base of a single-base class; None otherwise."""
        if len(cls.bases) != 1:
            return None
        return self._class(self._class_paths[id(cls)], cls.bases[0])

    def _lineage(self, cls: ast.ClassDef) -> Iterator[ast.ClassDef]:
        """``cls`` and its single-base project ancestors, nearest first."""
        seen: set[int] = set()
        current: ast.ClassDef | None = cls
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            yield current
            current = self._base(current)

    def _defined(self, cls: ast.ClassDef, name: str) -> _Function | None:
        for method in class_methods(cls):
            if method.name == name:
                return self._function(method, self._class_paths[id(cls)], cls)
        return None

    def _find_method(self, cls: ast.ClassDef, name: str) -> _Function | None:
        for ancestor in self._lineage(cls):
            method = self._defined(ancestor, name)
            if method is not None:
                return method
        return None

    def _constructor(self, cls: ast.ClassDef) -> _Function | None:
        for ancestor in self._lineage(cls):
            if ancestor.decorator_list or ancestor.keywords or self._defined(ancestor, "__new__"):
                return None
            init = self._defined(ancestor, "__init__")
            if init is not None:
                return init
        return None

    # --- calls inside methods ---

    def _enclosing_method(
        self, path: Path, call: ast.Call
    ) -> tuple[_FunctionNode, ast.ClassDef] | None:
        """The method whose own body makes ``call``, and its class."""
        parents = self._parents(path)
        current = parents.get(call)
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            current = parents.get(current)
        if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        cls = parents.get(current)
        return (current, cls) if isinstance(cls, ast.ClassDef) else None

    def _self_target(
        self, path: Path, call: ast.Call, receiver: str, name: str
    ) -> _Function | None:
        if self._method_counts[name] != 1:
            return None
        enclosing = self._enclosing_method(path, call)
        if enclosing is None:
            return None
        method, cls = enclosing
        positional = [*method.args.posonlyargs, *method.args.args]
        if not positional or positional[0].arg != receiver:
            return None
        if any(referenced_name(dec) == "staticmethod" for dec in method.decorator_list):
            return None
        return self._find_method(cls, name)

    def _super_target(self, path: Path, call: ast.Call, name: str) -> _Function | None:
        enclosing = self._enclosing_method(path, call)
        if enclosing is None:
            return None
        _method, cls = enclosing
        if cls.name in self._mixed:
            return None
        base = self._base(cls)
        return None if base is None else self._find_method(base, name)


@dataclass(frozen=True)
class _Drop:
    """Where one forwarding path ends: the functions it passes through, last one dropping.

    ``rejects`` says the last function has neither the parameter nor
    ``**kwargs``, so the call raises ``TypeError`` when it gets there.
    """

    chain: tuple[_Function, ...]
    rejects: bool


def _drops(resolver: _Resolver, func: _Function, keyword: str, hops: int = 0) -> list[_Drop] | None:
    """Every path on which ``func`` drops ``keyword``; None when any path may keep it."""
    verdict = func.verdict(keyword)
    if verdict is None or isinstance(verdict, _Drop):
        return None if verdict is None else [verdict]
    if hops == MAX_FORWARD_HOPS:
        return None
    drops: list[_Drop] = []
    for forward in verdict:
        target = resolver.target(func.path, forward)
        if target is None or target is func:
            return None
        further = _drops(resolver, target, keyword, hops + 1)
        if further is None:
            return None
        drops.extend(_Drop((func, *drop.chain), drop.rejects) for drop in further)
    return drops


def _plain(node: _FunctionNode) -> bool:
    """Neither decorated beyond the transparent decorators nor a stub."""
    transparent = all(
        referenced_name(dec) in _TRANSPARENT_DECORATORS for dec in node.decorator_list
    )
    return transparent and not _is_stub_body(node)


def _describe(drop: _Drop, keyword: str) -> str:
    """How one path drops the keyword, from the callee onward."""
    callee, *rest = drop.chain
    last = drop.chain[-1]
    if not rest:
        return (
            f"{callee.label} takes **{callee.collector} and never reads"
            f' "{keyword}" or forwards {callee.collector}'
        )
    route = " -> ".join(func.label for func in drop.chain)
    lead = (
        f"{callee.label} forwards **{callee.collector} along {route},"
        f" and {last.label} ({last.where})"
    )
    if drop.rejects:
        return (
            f"{lead} has no {keyword} parameter and no **kwargs, so the call raises"
            " TypeError once it gets there"
        )
    return f'{lead} takes **{last.collector} and never reads "{keyword}" or forwards it'


@dataclass(frozen=True)
class _Swallowed:
    """One finding's facts: the callee, the keyword, how it is dropped, and the tests."""

    callee: _Function
    keyword: str
    drops: tuple[_Drop, ...]
    sites: tuple[Site, ...]
    tests: tuple[dict, ...]

    def render(self, kept: tuple[int, ...]) -> str:
        sites = [self.sites[i] for i in kept]
        cited = ", ".join(f"{site.file}:{site.line}" for site in sites)
        how = "; ".join(_describe(drop, self.keyword) for drop in self.drops)
        message = (
            f"{self.keyword}= passed to {self.callee.label} ({self.callee.where}) is never used:"
            f" {how}. {len(sites)} production caller(s) pass it, at {cited}."
        )
        if self.tests:
            tested = _Callers([], list(self.tests), frozenset())
            message += f" Tests pass it too, in {_cite_test_sites(tested)}."
        return message + " Each caller believes the option takes effect."


@check(
    CHECK_NAME,
    severity=Severity.MEDIUM,
    description=(
        "Keyword arguments callers pass into a **kwargs that never reads them and never "
        "forwards them anywhere that does"
    ),
    advice=Advice(
        fix=(
            "Remove the keyword from every caller listed, or restore the parameter"
            " it was meant for."
        ),
        dismiss=(
            "Dismiss only if the callee is a compatibility shim that accepts and ignores"
            " unknown options on purpose."
        ),
    ),
)
def check_swallowed_kwargs(ctx: AnalysisContext) -> list[Finding]:
    """Find keywords a ``**kwargs`` callee drops (see the module docstring)."""
    resolver = _Resolver(ctx)
    passed: dict[tuple[int, str], tuple[_Function, list[dict]]] = {}
    for entries in ctx.call_index.values():
        for entry in entries:
            call: ast.Call = entry["node"]
            keywords = [kw.arg for kw in call.keywords if kw.arg is not None]
            if not keywords:
                continue
            callee = resolver.target(resolver.paths[entry["file"]], call)
            if callee is None or callee.collector is None:
                continue
            for keyword in keywords:
                if not callee.binds(keyword):
                    slot = passed.setdefault((id(callee.node), keyword), (callee, []))
                    slot[1].append(entry)

    findings: list[Finding] = []
    for (_node, keyword), (callee, calls) in passed.items():
        production = sorted(
            {Site(c["file"], c["line"]) for c in calls if not ctx.roles.is_test(c["file"])},
            key=lambda site: (site.file, site.line),
        )
        if not production:
            continue
        drops = _drops(resolver, callee, keyword)
        if drops is None:
            continue
        tests = tuple(c for c in calls if ctx.roles.is_test(c["file"]))
        swallowed = _Swallowed(callee, keyword, tuple(drops), tuple(production), tests)
        findings.append(
            multi_site_finding(CHECK_NAME, Severity.MEDIUM, production, swallowed.render)
        )
    return findings
