"""Helpers whose body is still spelled out elsewhere.

Feature B wrote a small helper; feature A's code still does the same thing
by hand. Two checks share one matcher:

- ``bypassed-helper`` (AST): a module-level helper with a 1-3 statement body
  whose shape other production code repeats, with the helper's parameters
  standing for whatever that code passes.
- ``bypassed-helper-history`` (git-history): the same findings, dated. The
  AST run carries no git history (DECISIONS.md, "Blame-age checks"), so the
  dated finding is its own check, as frozen-feature-flag is
  fossilized-toggles'. Blame gives the helper's creation commit and tells
  the copies written before it (never migrated) from those written after it
  (the helper was there and was not used).

**Normalization.** Code is turned into a shape: a nested tuple of node type
names and fields, with line numbers, ``ctx`` and other bookkeeping dropped.
Names are where the work is:

- a parameter of the helper is a *wildcard*: it matches any expression at a
  site, and every occurrence of one parameter must match the same
  expression there;
- a name the helper assigns is a *local*: it matches a local name of the
  site's scope, the same one at every occurrence;
- a name bound inside the expression (a lambda's parameters, a
  comprehension's targets) is numbered by binding order, so renaming it
  changes nothing;
- any other name, and a ``name.attr...`` chain rooted in one, resolves
  through the module's imports and module-level names to a dotted path
  (``Describer`` in compose.py and ``compose.Describer`` in figures.py are
  one name); a name the module neither defines nor imports (a builtin)
  stays bare.

**Which helpers.** Undecorated, synchronous, module-level functions whose
body (docstring aside) is 1-3 statements, with no ``yield``, ``await``,
``global``, ``nonlocal``, nested definition, or ``return`` before the last
statement. A one-statement ``return E`` body matches the expression ``E``
anywhere. Any other body matches a run of consecutive statements in one
block; its final ``return E`` matches a statement whose value is ``E``
(``x = E``, ``return E``, a bare ``E``), and a final ``return x`` of a local
is dropped, since inlined code just goes on using ``x``.

**Trivial bodies.** A shape's weight is its node count, leaving out
wildcards, locals, bound names, operators and the wrappers that mean nothing
on their own (``arg``, ``keyword``, ``Expr``...). A helper lighter than
``MIN_SHAPE_WEIGHT`` (``return x.attr``, ``return f(x)``, ``return x or
{}``) would match everywhere and is never a candidate. A helper whose
returned call's callee is one of its own parameters or locals is skipped
for the same reason.

**Drift.** A shape of ``DRIFT_MIN_WEIGHT`` or more also matches a site
that differs from it in one place, when that place is at most
``DRIFT_MAX_SHARE`` of the shape's weight; two differences under one node
count as one difference at that node. The finding prints both sides.
Copies that drifted apart are Phase 28's own story, and its named instance
is one: one describer adds ``**surface.params`` to the attrs its sibling
describer stamps.

**Reach.** A site is offered a helper defined in its own file, or in a
file its imports load (``ctx.import_graph``): code that cannot reach the
helper is not bypassing it.

**Cost.** Candidate sites are bucketed by a cheap key (the node type, and
for a call the dotted path its callee resolves to), and a helper is only
compared with its own bucket, never with every expression.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from pysmelly.checks.helpers import (
    body_without_docstring,
    filepath_to_module,
    import_bindings,
    name_chain,
    own_binding,
    source_trees,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import BlamedLine, LineHistory
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    MAX_DISPLAY_WIDTH,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)

# The longest body a helper may have. Past three statements a helper is a
# procedure, and its copies are near-duplicate-with-drift's to find.
MAX_HELPER_STATEMENTS = 3
# The lightest shape that is a candidate (see the module docstring).
MIN_SHAPE_WEIGHT = 6
# The lightest shape allowed to differ from a site in one place, and how much
# of the shape's weight that place may carry.
DRIFT_MIN_WEIGHT = 15
DRIFT_MAX_SHARE = 0.25

# A shape: a tuple whose first item is a node type name or one of the tags
# below, or a plain value (a field such as an attribute name).
Shape = object

_LIST = "*list"
_WILD = "*wild"
_LOCAL = "*local"
_VAR = "*var"
_REF = "*ref"
_BOUND = "*bound"
_CONST = "*const"
_OP = "*op"
# Shapes compared whole: two of them differ or they do not.
_ATOMS = frozenset({_REF, _CONST, _VAR, _BOUND, _OP})
# Shapes that carry no meaning of their own, so they add no weight.
_WEIGHTLESS = frozenset(
    {_LIST, _WILD, _LOCAL, _VAR, _BOUND, _OP, "arg", "arguments", "comprehension", "Expr"}
)
_IGNORED_FIELDS = frozenset({"ctx", "type_comment", "kind", "type_params"})
_OPERATORS = (ast.operator, ast.boolop, ast.cmpop, ast.unaryop)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_DISQUALIFYING = (
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.Global,
    ast.Nonlocal,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)
_VALUE_STATEMENTS = (ast.Return, ast.Assign, ast.AnnAssign, ast.Expr)
_KEYED_STATEMENTS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_BLOCK_FIELDS = ("body", "orelse", "finalbody")
_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_ScopeNode = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

# The AST check and its git-history twin report the same copies, so they share the
# edit and the dismissal.
_BYPASSED_ADVICE = Advice(
    fix=(
        "Replace each copy listed with a call to the helper; where a copy differs, make that"
        " place a parameter of the helper."
    ),
    dismiss=(
        "Dismiss only if a copy differs on purpose and must not change with the helper; say"
        " why beside it."
    ),
)


def _bound_names(body: list[ast.stmt]) -> set[str]:
    """Every name a body binds in its own scope; a nested definition binds only its name.

    Imports are left out: what an import binds resolves through
    ``ctx.module_imports``, which records imports at every depth.
    """
    names: set[str] = set()
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        # A lambda's parameters are reached as its ``ast.arg`` children; a
        # nested definition is not descended into, so its parameters are not.
        if name := own_binding(node):
            names.add(name)
        if not isinstance(node, _DEFINITIONS):
            pending.extend(ast.iter_child_nodes(node))
    return names


def _all_args(args: ast.arguments) -> list[ast.arg]:
    found = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return found + [arg for arg in (args.vararg, args.kwarg) if arg is not None]


class _Namer:
    """What a free name means in one module: an import's target, a module-level name, or itself."""

    def __init__(self, ctx: AnalysisContext, path: Path, tree: ast.Module) -> None:
        self.module = filepath_to_module(str(path), ctx.package_dirs)
        self.bindings = import_bindings(ctx.module_imports.get(path, []))
        self.module_names = _bound_names(tree.body)

    def ref(self, name: str) -> str:
        if name in self.bindings:
            return self.bindings[name]
        if name in self.module_names:
            return f"{self.module}.{name}"
        return name


@dataclass(frozen=True)
class _Scope:
    """Where code sits: its file, its module's names, and the names its own scope binds.

    ``function`` is the enclosing function, None at module or class level.
    ``wild`` holds a helper's parameters while its pattern is shaped and is
    empty for a site; ``pattern`` says which of the two is being shaped.
    """

    path: Path
    namer: _Namer
    locals: frozenset[str]
    function: _FunctionNode | None
    wild: frozenset[str] = frozenset()
    pattern: bool = False


class _Shaper:
    """Turns code in one scope into shapes, remembering the node behind each shape."""

    def __init__(self, scope: _Scope) -> None:
        self.scope = scope
        self.bound: list[str] = []
        self.origin: dict[int, ast.AST] = {}

    def shape(self, node: object) -> Shape:
        if isinstance(node, list):
            return (_LIST, *(self.shape(item) for item in node))
        if not isinstance(node, ast.AST):
            return node
        result = self._node(node)
        self.origin[id(result)] = node
        return result

    def _node(self, node: ast.AST) -> tuple:
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.arg):
            return ("arg", self._name(node.arg))
        if isinstance(node, ast.Constant):
            return (_CONST, type(node.value).__name__, node.value)
        if isinstance(node, _OPERATORS):
            return (_OP, type(node).__name__)
        if isinstance(node, ast.Attribute):
            chain = name_chain(node)
            if chain and self._is_free(chain[0]):
                return (_REF, ".".join([self.scope.namer.ref(chain[0]), *chain[1:]]))
        if isinstance(node, ast.Lambda):
            return self._binding(node, [arg.arg for arg in _all_args(node.args)])
        if isinstance(node, _COMPREHENSIONS):
            targets = [
                name.id
                for generator in node.generators
                for name in ast.walk(generator.target)
                if isinstance(name, ast.Name)
            ]
            return self._binding(node, targets)
        return self._generic(node)

    def _binding(self, node: ast.AST, names: list[str]) -> tuple:
        self.bound.extend(names)
        try:
            return self._generic(node)
        finally:
            del self.bound[len(self.bound) - len(names) :]

    def _generic(self, node: ast.AST) -> tuple:
        fields = (getattr(node, name, None) for name in node._fields if name not in _IGNORED_FIELDS)
        return (type(node).__name__, *(self.shape(value) for value in fields))

    def _is_free(self, name: str) -> bool:
        scope = self.scope
        return name not in self.bound and name not in scope.wild and name not in scope.locals

    def _name(self, name: str) -> tuple:
        if name in self.bound:
            return (_BOUND, len(self.bound) - 1 - self.bound[::-1].index(name))
        if name in self.scope.wild:
            return (_WILD, name)
        if name in self.scope.locals:
            return (_LOCAL if self.scope.pattern else _VAR, name)
        return (_REF, self.scope.namer.ref(name))


def _weight(shape: Shape) -> int:
    """How many meaningful nodes a shape holds (see the module docstring)."""
    if not isinstance(shape, tuple) or not shape:
        return 0
    own = 0 if shape[0] in _WEIGHTLESS else 1
    if shape[0] in _ATOMS:
        return own
    return own + sum(_weight(child) for child in shape[1:])


def _key(node: ast.AST, scope: _Scope) -> tuple:
    """A node's bucket: its type, plus a call's resolved callee or a statement's value's key."""
    if isinstance(node, ast.Call):
        chain = name_chain(node.func)
        if not chain or chain[0] in scope.locals or chain[0] in scope.wild:
            return ("Call", None)
        return ("Call", ".".join([scope.namer.ref(chain[0]), *chain[1:]]))
    if isinstance(node, _KEYED_STATEMENTS) and node.value is not None:
        return (type(node).__name__, _key(node.value, scope))
    return (type(node).__name__,)


@dataclass(frozen=True)
class _Helper:
    """A helper's pattern: one shape per statement, or ``E``'s alone for a ``return E`` body.

    ``is_expression`` says the pattern is that one expression, matched
    anywhere. Otherwise it is a run of statements, and ``tail`` says the
    last shape is the value of a ``return``, matched against the value of
    the site's statement.
    """

    file: Path
    node: ast.FunctionDef
    statements: int
    pattern: tuple[Shape, ...]
    is_expression: bool
    tail: bool
    weight: int
    key: tuple
    shaper: _Shaper

    def _offered(self, scope: _Scope, finder: _Finder) -> bool:
        """Not the helper's own body, and in a file that can reach the helper."""
        return scope.function is not self.node and self.file in finder.offered(scope)

    def matches(
        self, finder: _Finder
    ) -> Iterator[tuple[InlinedSite, list[ast.expr] | list[ast.stmt]]]:
        """Each site that spells out this helper, with the nodes it spans."""
        if self.is_expression:
            for node, scope in finder.index.expressions.get(self.key, []):
                if self._offered(scope, finder):
                    site, shaper = finder.shape(node, scope)
                    matched, difference = self._judge([(self.pattern[0], site, shaper)])
                    if matched:
                        yield _site(scope, [node], difference), [node]
            return
        count = len(self.pattern)
        for block, start, scope in finder.index.statements.get(self.key, []):
            window = block[start : start + count]
            if len(window) < count or not self._offered(scope, finder):
                continue
            targets: list[ast.expr | ast.stmt] = list(window)
            if self.tail:
                last = window[-1]
                if not isinstance(last, _VALUE_STATEMENTS) or last.value is None:
                    continue
                targets[-1] = last.value
            pairs = [
                (pattern, *finder.shape(target, scope))
                for pattern, target in zip(self.pattern, targets, strict=True)
            ]
            matched, difference = self._judge(pairs)
            if matched:
                yield _site(scope, list(window), difference), list(window)

    def _judge(self, pairs: list[tuple[Shape, Shape, _Shaper]]) -> tuple[bool, Difference | None]:
        """Whether the site matches, and where it drifts when it matches in all but one place."""
        matcher = _Matcher()
        found = [
            (left, right, shaper)
            for pattern, site, shaper in pairs
            for left, right in matcher.differences(pattern, site)
        ]
        if not found:
            return True, None
        if len(found) > 1 or self.weight < DRIFT_MIN_WEIGHT:
            return False, None
        left, right, shaper = found[0]
        if any(left is pattern for pattern, _site, _shaper in pairs):
            return False, None
        if _weight(left) > DRIFT_MAX_SHARE * self.weight:
            return False, None
        helper_node = self.shaper.origin.get(id(left))
        return True, Difference(_text(helper_node), _text(shaper.origin.get(id(right))))


def _helper(path: Path, namer: _Namer, node: ast.stmt) -> _Helper | None:
    """The pattern a module-level statement offers, or None when it is not a helper."""
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return None
    body = body_without_docstring(node.body)
    if not 1 <= len(body) <= MAX_HELPER_STATEMENTS:
        return None
    inner = [child for stmt in body for child in ast.walk(stmt)]
    if any(isinstance(child, _DISQUALIFYING) for child in inner):
        return None
    returns = [child for child in inner if isinstance(child, ast.Return)]
    if returns and (len(returns) > 1 or returns[0] is not body[-1]):
        return None
    params = frozenset(arg.arg for arg in _all_args(node.args))
    scope = _Scope(path, namer, frozenset(_bound_names(body)) - params, node, params, pattern=True)
    last = body[-1]
    if (
        len(body) > 1
        and isinstance(last, ast.Return)
        and isinstance(last.value, ast.Name)
        and last.value.id in scope.locals
    ):
        body = body[:-1]
        last = body[-1]
    tail_target: ast.AST = last
    if isinstance(last, ast.Return):
        if last.value is None:
            return None
        tail_target = last.value
    tail = isinstance(last, ast.Return)
    targets: list[ast.AST] = [*body[:-1], tail_target]
    is_expression = len(body) == 1 and tail
    key = _key(targets[0] if is_expression else body[0], scope)
    if key == ("Call", None):
        return None
    shaper = _Shaper(scope)
    pattern = tuple(shaper.shape(target) for target in targets)
    weight = sum(_weight(item) for item in pattern)
    if weight < MIN_SHAPE_WEIGHT:
        return None
    return _Helper(path, node, len(body), pattern, is_expression, tail, weight, key, shaper)


class _Matcher:
    """Compares a helper's pattern with a site's shape, binding wildcards and locals."""

    def __init__(self) -> None:
        self.binds: dict[tuple, Shape] = {}

    def differences(self, pattern: Shape, site: Shape) -> list[tuple[Shape, Shape]]:
        """Where ``site`` departs from ``pattern``; two departures under one node are that node."""
        if isinstance(pattern, tuple) and pattern and pattern[0] in {_WILD, _LOCAL}:
            return self._bind(pattern, site)
        if pattern == site:
            return []
        if not (
            isinstance(pattern, tuple)
            and isinstance(site, tuple)
            and len(pattern) == len(site)
            and pattern[0] == site[0]
            and pattern[0] not in _ATOMS
        ):
            return [(pattern, site)]
        found: list[tuple[Shape, Shape]] = []
        for left, right in zip(pattern[1:], site[1:], strict=True):
            if isinstance(left, tuple) and left and left[0] == _LIST:
                if not (isinstance(right, tuple) and len(left) == len(right)):
                    return [(pattern, site)]
                for item, other in zip(left[1:], right[1:], strict=True):
                    found += self.differences(item, other)
            elif isinstance(left, tuple):
                found += self.differences(left, right)
            elif left != right:
                return [(pattern, site)]
        return found if len(found) <= 1 else [(pattern, site)]

    def _bind(self, pattern: tuple, site: Shape) -> list[tuple[Shape, Shape]]:
        if pattern[0] == _LOCAL and not (isinstance(site, tuple) and site[0] == _VAR):
            return [(pattern, site)]
        prior = self.binds.setdefault(pattern, site)
        return [] if prior == site else [(pattern, site)]


def _text(node: ast.AST | None) -> str:
    if node is None:
        return "?"
    text = " ".join(ast.unparse(node).split())
    if len(text) > MAX_DISPLAY_WIDTH:
        return text[: MAX_DISPLAY_WIDTH - 1] + "…"
    return text


@dataclass(frozen=True)
class Difference:
    """Where a drifted copy departs from its helper: the helper's text and the copy's."""

    helper_text: str
    site_text: str


@dataclass(frozen=True)
class InlinedSite:
    """One place that spells out a helper's body, and the function it sits in."""

    file: Path
    line: int
    end_line: int
    difference: Difference | None
    function: _FunctionNode | None = field(compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class BypassedHelper:
    """A helper and the production sites that spell out its body."""

    file: Path
    node: ast.FunctionDef
    statements: int
    sites: tuple[InlinedSite, ...]


class _SiteIndex:
    """Every production expression and statement position, bucketed by ``_key``."""

    def __init__(self, ctx: AnalysisContext, namers: dict[Path, _Namer]) -> None:
        self.expressions: dict[tuple, list[tuple[ast.expr, _Scope]]] = defaultdict(list)
        self.statements: dict[tuple, list[tuple[list[ast.stmt], int, _Scope]]] = defaultdict(list)
        for path, tree in source_trees(ctx):
            namer = namers[path]
            self._index(tree, _Scope(path, namer, frozenset(), None))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = {arg.arg for arg in _all_args(node.args)}
                    local = frozenset(_bound_names(node.body) | params)
                    self._index(node, _Scope(path, namer, local, node))
                elif isinstance(node, ast.ClassDef):
                    self._index(node, _Scope(path, namer, frozenset(_bound_names(node.body)), None))

    def _index(self, owner: _ScopeNode, scope: _Scope) -> None:
        """Index one scope's own code; nested definitions are scopes of their own."""
        self._block(owner.body, scope)
        pending: list[ast.AST] = list(owner.body)
        while pending:
            node = pending.pop()
            if isinstance(node, _DEFINITIONS):
                continue
            if isinstance(node, ast.expr):
                self.expressions[_key(node, scope)].append((node, scope))
            for name in _BLOCK_FIELDS:
                block = getattr(node, name, None)
                if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                    self._block(block, scope)
            pending.extend(ast.iter_child_nodes(node))

    def _block(self, block: list[ast.stmt], scope: _Scope) -> None:
        for index, stmt in enumerate(block):
            self.statements[_key(stmt, scope)].append((block, index, scope))


class _Finder:
    """Matches every helper against the sites in its bucket."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.namers = {path: _Namer(ctx, path, tree) for path, tree in ctx.all_trees.items()}
        self.index = _SiteIndex(ctx, self.namers)
        self._shapes: dict[tuple[int, int], tuple[Shape, _Shaper]] = {}

    def helpers(self) -> Iterator[_Helper]:
        for path, tree in source_trees(self.ctx):
            for node in tree.body:
                helper = _helper(path, self.namers[path], node)
                if helper is not None:
                    yield helper

    def shape(self, node: ast.AST, scope: _Scope) -> tuple[Shape, _Shaper]:
        """A site's shape, made once however many helpers compare with it."""
        key = (id(node), id(scope))
        if key not in self._shapes:
            shaper = _Shaper(scope)
            self._shapes[key] = (shaper.shape(node), shaper)
        return self._shapes[key]

    def offered(self, scope: _Scope) -> frozenset[Path]:
        """The files a site in ``scope`` can reach: its own, and those its imports load."""
        return self.ctx.import_graph.get(scope.path, frozenset()) | {scope.path}


def _site(
    scope: _Scope, nodes: list[ast.expr] | list[ast.stmt], difference: Difference | None
) -> InlinedSite:
    first = nodes[0]
    last = nodes[-1]
    end = last.end_lineno if last.end_lineno is not None else last.lineno
    return InlinedSite(scope.path, first.lineno, end, difference, scope.function)


def _whole_body(function: _FunctionNode | None, nodes: list[ast.expr] | list[ast.stmt]) -> bool:
    """Whether ``nodes`` are all of ``function``'s body (a ``return E`` body by its ``E``)."""
    if function is None:
        return False
    body = body_without_docstring(function.body)
    if len(body) == 1 and isinstance(body[0], ast.Return) and nodes == [body[0].value]:
        return True
    last = body[-1]
    if isinstance(last, ast.Return) and isinstance(last.value, ast.Name) and body[:-1] == nodes:
        return True
    return body == nodes


def _preferred(ctx: AnalysisContext, one: _Helper, other: _Helper) -> _Helper:
    """Of two helpers that spell each other out, the one the other's module imports.

    When neither or both import the other, the one defined first.
    """
    one_used = one.file in ctx.import_graph.get(other.file, frozenset())
    other_used = other.file in ctx.import_graph.get(one.file, frozenset())
    if one_used != other_used:
        return one if one_used else other
    return min((one, other), key=lambda helper: (str(helper.file), helper.node.lineno))


def _find_bypassed_helpers(ctx: AnalysisContext) -> list[BypassedHelper]:
    """Every helper whose body production code still spells out, with those sites.

    A helper whose whole body is a copy of another reported helper (a twin)
    is not reported on its own: it is a site of the other's finding. When
    each is a copy of the other, ``_preferred`` picks which one is reported.
    """
    finder = _Finder(ctx)
    matched: dict[int, tuple[_Helper, list[tuple[InlinedSite, bool]]]] = {}
    for helper in finder.helpers():
        sites = [
            (site, _whole_body(site.function, nodes)) for site, nodes in helper.matches(finder)
        ]
        if sites:
            matched[id(helper.node)] = (helper, sites)
    twins: set[int] = set()
    for helper, sites in matched.values():
        for site, whole in sites:
            other = matched.get(id(site.function)) if whole else None
            if other is None:
                continue
            mirrored = any(
                whole_back and back.function is helper.node for back, whole_back in other[1]
            )
            if not mirrored or _preferred(ctx, helper, other[0]) is helper:
                twins.add(id(other[0].node))
    result = [
        BypassedHelper(helper.file, helper.node, helper.statements, tuple(s for s, _ in sites))
        for helper, sites in matched.values()
        if id(helper.node) not in twins
    ]
    return sorted(result, key=lambda found: (str(found.file), found.node.lineno))


def _site_text(site: InlinedSite) -> str:
    text = f"{site.file}:{site.line}"
    if site.function is not None:
        text += f" in `{site.function.name}`"
    if site.difference is not None:
        text += (
            f" (differs: `{site.difference.site_text}` where the helper has "
            f"`{site.difference.helper_text}`)"
        )
    return text


def _helper_clause(found: BypassedHelper, count: int) -> str:
    statements = "1-statement" if found.statements == 1 else f"{found.statements}-statement"
    where = "1 other site" if count == 1 else f"{count} other sites"
    return (
        f"`{found.node.name}` ({found.file}:{found.node.lineno}), a {statements} helper, "
        f"is spelled out by hand at {where}"
    )


def _bypassed_message(found: BypassedHelper, indices: tuple[int, ...]) -> str | None:
    """bypassed-helper's message over the sites at ``indices``; None when none is left."""
    if not indices:
        return None
    shown = [found.sites[index] for index in indices]
    listed = "; ".join(_site_text(site) for site in shown)
    return f"{_helper_clause(found, len(indices))}: {listed}"


def _anchored(check_name: str, found: BypassedHelper, render: partial[str | None]) -> Finding:
    """One finding per helper, anchored on its definition, each copy a site."""
    return multi_site_finding(
        check_name,
        Severity.MEDIUM,
        [Site(str(site.file), site.line) for site in found.sites],
        render,
        anchor=Site(str(found.file), found.node.lineno),
    )


@check(
    "bypassed-helper",
    severity=Severity.MEDIUM,
    description="A 1-3 statement helper whose body other code still spells out by hand",
    advice=_BYPASSED_ADVICE,
)
def check_bypassed_helper(ctx: AnalysisContext) -> list[Finding]:
    """Flag a small helper whose body production code still writes out by hand.

    One finding per helper, anchored on its definition; each copy is a site
    of the finding, so an ignore on one removes that copy.
    """
    return [
        _anchored("bypassed-helper", found, partial(_bypassed_message, found))
        for found in _find_bypassed_helpers(ctx)
    ]


@dataclass(frozen=True)
class _Dating:
    """The helper's creation commit, and each dated site's oldest line, by site index."""

    created: BlamedLine
    sites: dict[int, datetime]


def _date(lines: LineHistory, found: BypassedHelper, committed: frozenset[str]) -> _Dating | None:
    """Blame the helper and its sites at HEAD; None when the helper's file is not as committed."""
    helper_path = str(found.file)
    if helper_path not in committed:
        return None
    end = found.node.end_lineno if found.node.end_lineno is not None else found.node.lineno
    helper_lines = range(found.node.lineno, end + 1)
    wanted: dict[str, set[int]] = defaultdict(set)
    wanted[helper_path].update(helper_lines)
    for site in found.sites:
        if str(site.file) in committed:
            wanted[str(site.file)].update(range(site.line, site.end_line + 1))
    blamed = lines.files_line_commits(wanted)
    created = min((blamed[helper_path][line] for line in helper_lines), key=lambda b: b.written)
    sites = {
        index: min(
            blamed[str(site.file)][line].written for line in range(site.line, site.end_line + 1)
        )
        for index, site in enumerate(found.sites)
        if str(site.file) in committed
    }
    return _Dating(created, sites)


def _dated_message(found: BypassedHelper, dating: _Dating, indices: tuple[int, ...]) -> str | None:
    """bypassed-helper-history's message over the sites at ``indices``; None when none is left."""
    if not indices:
        return None
    created = dating.created
    groups: dict[str, list[int]] = {"before": [], "after": [], "undated": []}
    for index in indices:
        written = dating.sites.get(index)
        if written is None:
            groups["undated"].append(index)
        else:
            groups["before" if written < created.written else "after"].append(index)
    labels = {
        "before": "Written before it, never moved to it",
        "after": "Written after it",
        "undated": "Not committed as analyzed, so undated",
    }
    parts = [
        f"{labels[name]}: " + "; ".join(_site_text(found.sites[index]) for index in group)
        for name, group in groups.items()
        if group
    ]
    return (
        f"{_helper_clause(found, len(indices))}. `{found.node.name}` was created in "
        f"{created.sha[:8]} on {created.written:%Y-%m-%d} ({created.summary!r}). "
        f"{'. '.join(parts)}"
    )


@check(
    "bypassed-helper-history",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="bypassed-helper, dated: the helper's creation commit and which copies predate it",
    advice=_BYPASSED_ADVICE,
)
def check_bypassed_helper_history(ctx: AnalysisContext) -> list[Finding]:
    """bypassed-helper with blame: when the helper arrived, and which copies are older.

    The helper's creation commit is the oldest commit ``git blame`` holds
    responsible for any of its lines; a copy's date is its oldest line's. A
    file whose working tree differs from HEAD cannot be blamed at the
    analyzed line numbers, so a helper there is not reported and a copy
    there is listed undated.
    """
    history = ctx.git_history
    if history is None:
        return []
    found_all = _find_bypassed_helpers(ctx)
    if not found_all:
        return []
    lines = history.line_history
    committed = lines.head_paths() - lines.head_changed_paths()
    findings = []
    for found in found_all:
        dating = _date(lines, found, committed)
        if dating is not None:
            render = partial(_dated_message, found, dating)
            findings.append(_anchored("bypassed-helper-history", found, render))
    return findings
