"""Parameters every production caller settles: dead-branch-by-callers and always-default-params.

Both read the caller-value index (``ctx.caller_values``, Phase 25-1): for each
parameter of an analyzed function, what each production call site passes (a
literal, None, the default, or something else), test-role call sites kept
apart.

**dead-branch-by-callers.** An ``if`` statement or a conditional expression
in the function's own body tests its parameters: truthiness (``if flag:``),
a comparison with a constant (``mode == "x"``, ``x is None``, ``n > 0``,
``mode in ("a", "b")``), ``isinstance(x, T)`` with builtin types, ``not``
and ``and``/``or`` over those. The test is evaluated once per production
call site, with that site's values; when every site gives the same answer
the other arm is dead, and the finding names its lines, every caller and
what it passes. When the true arm returns or raises and the test is always
true, the statements after the ``if`` are the dead arm. A value the analysis
cannot know (an expression, a passthrough splat, a default that is not a
constant) makes the outcome unknown, and an unknown outcome at any site is
no finding, unless the rest of the test settles it (``x or flag`` with
``flag`` always true). A parameter bound again before the test (assigned,
deleted, rebound in a loop that also holds the test, or ``nonlocal`` in a
nested function) is unknown at that test. Test-role callers never decide;
those that take the dead arm are cited as the tests keeping it.

**always-default-params.** A parameter with a default that no production
call site passes: every one uses the default, so the parameter is a
variation production never asks for, and the fix is to delete it and use
the default directly. It is the opposite end from unused-defaults (a
``None`` default no caller relies on: make the parameter required), and the
two cannot both hold for one parameter once there is a production caller.
A parameter the body never reads is vestigial-params' finding, so it is
left out here. A branch decided by an always-default parameter belongs to
this check's finding, which cites it, and dead-branch-by-callers does not
report it again.

**What is judged.** A function or method with at least one production
caller, outside a test-role file, that is not a dunder (``Cls(...)`` and the
operators reach those, and the index sees none of them), not decorated
beyond ``staticmethod``/``classmethod``/``override`` (a decorator can supply
or rewrite arguments), not a stub, not an abstract or framework-dispatched
signature, not referenced as a value, a decorator or a dotted string in
production code (a callback has callers the index cannot see), and not a
method that may override a base outside the analyzed code
(``CallerValues.overrides_unresolved_base``). Both messages end with the one
condition under which to dismiss them: the function is a genuine public
boundary that code outside the analyzed tree calls.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from pysmelly.checks.callers import (
    CallerValues,
    ParamValues,
    PassedKind,
    PassedValue,
    _has_interface_decorator,
    param_subjects,
)
from pysmelly.checks.framework import (
    FRAMEWORK_HOOK_METHODS,
    framework_dispatch_decorators,
    has_dictated_signature,
)
from pysmelly.checks.helpers import (
    TESTS_UNSEEN,
    _is_main_guard,
    clause_binding,
    is_bare_name,
    is_referenced_as_dotted_string,
    is_referenced_as_value_in_production,
    is_used_as_decorator,
)
from pysmelly.checks.swallowed_kwargs import _plain
from pysmelly.checks.test_surface import surface_of_sites
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

# Nodes that open a scope of their own: a name bound or tested inside one is
# not the enclosing function's parameter.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)

# Builtin types an ``isinstance`` test is evaluated against.
_BUILTIN_TYPES: dict[str, type] = {
    t.__name__: t for t in (bool, int, float, complex, str, bytes, list, tuple, dict, set)
}

_COMPARISONS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

# ``is`` compares identity, which is defined only for the singletons.
_SINGLETONS = (None, True, False, ...)


@dataclass(frozen=True)
class _Known:
    """A value the analysis knows at one call site."""

    value: Any


_Env = dict[str, _Known | None]


def _value(node: ast.expr, env: _Env) -> _Known | None:
    """The value of ``node`` given the parameters' values, or None when unknown."""
    if isinstance(node, ast.Constant):
        return _Known(node.value)
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_value(elt, env) for elt in node.elts]
        if any(item is None for item in items):
            return None
        return _known_collection(node, [item.value for item in items if item is not None])
    if isinstance(node, ast.Compare):
        return _compare(node, env)
    if isinstance(node, ast.Call):
        return _isinstance(node, env)
    if isinstance(node, ast.UnaryOp):
        return _unary(node, env)
    # An ``and``/``or`` evaluates to one of its operands, not to a bool, so
    # only its truth is known (``_truth``), never its value.
    return None


def _unary(node: ast.UnaryOp, env: _Env) -> _Known | None:
    """``not x`` as a bool; ``-x``/``+x`` of a known number (``n == -1``)."""
    if isinstance(node.op, ast.Not):
        truth = _truth(node.operand, env)
        return None if truth is None else _Known(not truth)
    operand = _value(node.operand, env)
    if operand is None or not isinstance(node.op, (ast.USub, ast.UAdd)):
        return None
    if isinstance(operand.value, bool) or not isinstance(operand.value, (int, float, complex)):
        return None
    return _Known(-operand.value if isinstance(node.op, ast.USub) else operand.value)


def _known_collection(node: ast.Tuple | ast.List | ast.Set, values: list[Any]) -> _Known | None:
    """A tuple, list or set display of known values; None when a set item is unhashable."""
    if isinstance(node, ast.Tuple):
        return _Known(tuple(values))
    if isinstance(node, ast.List):
        return _Known(values)
    try:
        return _Known(frozenset(values))
    except TypeError:
        return None


def _compare(node: ast.Compare, env: _Env) -> _Known | None:
    """A comparison chain whose every operand is known; ``is`` only against a singleton."""
    operands = [_value(item, env) for item in (node.left, *node.comparators)]
    if any(item is None for item in operands):
        return None
    values = [item.value for item in operands if item is not None]
    for op, left, right in zip(node.ops, values, values[1:], strict=False):
        if isinstance(op, (ast.Is, ast.IsNot)) and not any(
            any(side is singleton for singleton in _SINGLETONS) for side in (left, right)
        ):
            return None
        try:
            if not _COMPARISONS[type(op)](left, right):
                return _Known(value=False)
        except TypeError:
            return None
    return _Known(value=True)


def _isinstance(node: ast.Call, env: _Env) -> _Known | None:
    """``isinstance(x, T)`` with ``x`` known and ``T`` builtin types; else unknown."""
    if not is_bare_name(node.func, "isinstance"):
        return None
    if len(node.args) != 2 or node.keywords:
        return None
    subject = _value(node.args[0], env)
    spec = node.args[1]
    names = spec.elts if isinstance(spec, ast.Tuple) else [spec]
    types = [_BUILTIN_TYPES.get(name.id) for name in names if isinstance(name, ast.Name)]
    if subject is None or len(types) != len(names) or None in types:
        return None
    return _Known(isinstance(subject.value, tuple(t for t in types if t is not None)))


def _truth(node: ast.expr, env: _Env) -> bool | None:
    """Whether ``node`` is true given the parameters' values; None when unknown.

    ``and``/``or`` are three-valued: one false operand settles an ``and``,
    one true operand an ``or``, whatever the others are.
    """
    if isinstance(node, ast.BoolOp):
        settles = isinstance(node.op, ast.Or)
        results = [_truth(value, env) for value in node.values]
        if settles in results:
            return settles
        return None if None in results else not settles
    known = _value(node, env)
    if known is None:
        return None
    try:
        return bool(known.value)
    except TypeError:
        return None


# --- the function body: branches and rebindings --------------------------------


# --- the function body: branches and rebindings --------------------------------

# Every node that carries its own source position and can bind a name.
_Located = ast.stmt | ast.expr | ast.excepthandler | ast.pattern | ast.alias


@dataclass(frozen=True)
class _Rebind:
    """Where a parameter is bound again, and the ids of the loops around that."""

    line: int
    col: int
    loops: frozenset[int]


@dataclass(frozen=True)
class _Branch:
    """One test in the function's own body and the code each answer runs.

    ``true_arm`` and ``false_arm`` are ``(first, last)`` line ranges, None
    for an arm with no code of its own (an ``if`` without ``else``).
    ``loops`` holds the ids of the loops around the test. ``expression`` is
    the conditional expression whose test this is, None for an ``if``.
    """

    test: ast.expr
    line: int
    true_arm: tuple[int, int] | None
    false_arm: tuple[int, int] | None
    loops: frozenset[int]
    expression: ast.IfExp | None

    def sees(self, rebind: _Rebind) -> bool:
        """Whether the test may read the value ``rebind`` stores.

        It may when the rebinding comes first in the source, or when both
        sit in one loop, whose next pass reads what this pass stored.
        """
        end = (self.test.end_lineno or self.line, self.test.end_col_offset or 0)
        return (rebind.line, rebind.col) < end or bool(rebind.loops & self.loops)


def _span(nodes: list[ast.stmt] | list[ast.expr]) -> tuple[int, int] | None:
    if not nodes:
        return None
    last = nodes[-1].end_lineno
    return nodes[0].lineno, nodes[0].lineno if last is None else last


def _exits(body: list[ast.stmt]) -> bool:
    """Whether a block always leaves the function: it ends in ``return`` or ``raise``."""
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise))


def _if_branch(stmts: list[ast.stmt], index: int, loops: frozenset[int]) -> _Branch | None:
    """The branch ``stmts[index]`` makes, when it is an ``if``.

    Always true, an ``if`` with no ``else`` that returns or raises makes the
    statements after it the dead arm.
    """
    stmt = stmts[index]
    if not isinstance(stmt, ast.If):
        return None
    false_arm = _span(stmt.orelse)
    if not stmt.orelse and _exits(stmt.body):
        false_arm = _span(stmts[index + 1 :])
    return _Branch(stmt.test, stmt.lineno, _span(stmt.body), false_arm, loops, None)


def _expression_branch(node: ast.IfExp, loops: frozenset[int]) -> _Branch:
    return _Branch(node.test, node.lineno, _span([node.body]), _span([node.orelse]), loops, node)


def _bindings(node: ast.AST) -> Iterator[tuple[str, _Located]]:
    """The names a handler, an import alias or a ``match`` pattern binds itself."""
    if isinstance(node, (ast.ExceptHandler, ast.alias, ast.pattern)) and (
        name := clause_binding(node)
    ):
        yield name, node


def _evaluated_by_definition(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[ast.expr]:
    """The expressions a nested ``def`` or ``class`` statement evaluates in the enclosing scope."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(kw.value for kw in node.keywords)]
    defaults = [d for d in node.args.kw_defaults if d is not None]
    return [*node.decorator_list, *node.args.defaults, *defaults]


class _Body:
    """The branches and parameter rebindings of one function's own scope."""

    def __init__(self, node: _FunctionNode, params: set[str]) -> None:
        self.params = params
        self.branches: list[_Branch] = []
        self.rebinds: dict[str, list[_Rebind]] = {}
        self.rebound_anywhere: set[str] = set()
        self._block(node.body, frozenset())

    def _block(self, stmts: list[ast.stmt], loops: frozenset[int]) -> None:
        for index, stmt in enumerate(stmts):
            branch = _if_branch(stmts, index, loops)
            if branch is not None:
                self.branches.append(branch)
            self._node(stmt, loops)

    def _node(self, node: ast.AST, loops: frozenset[int]) -> None:
        """A statement, handler, ``match`` case, pattern, ``with`` item or alias."""
        if isinstance(node, ast.expr):
            self._expression(node, loops)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._bind(node.name, node, loops)
            for expr in _evaluated_by_definition(node):
                self._expression(expr, loops)
            self._nested(node)
            return
        for name, at in _bindings(node):
            self._bind(name, at, loops)
        inner = loops | {id(node)} if isinstance(node, _LOOPS) else loops
        for field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                self._block(value, inner)
                continue
            # A ``for`` loop's iterable is evaluated once, before the loop.
            at_loops = loops if field == "iter" else inner
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, ast.AST):
                    self._node(item, at_loops)

    def _expression(self, expr: ast.expr, loops: frozenset[int]) -> None:
        pending: list[ast.AST] = [expr]
        while pending:
            node = pending.pop()
            if isinstance(node, _SCOPES):
                self._nested(node)
                continue
            if isinstance(node, _COMPREHENSIONS):
                # Comprehension targets bind the comprehension's own scope; a
                # walrus inside one still binds the function's.
                for sub in ast.walk(node):
                    if isinstance(sub, ast.NamedExpr):
                        self._bind(sub.target.id, sub, loops)
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self._bind(node.id, node, loops)
            if isinstance(node, ast.IfExp):
                self.branches.append(_expression_branch(node, loops))
            pending.extend(ast.iter_child_nodes(node))

    def _nested(self, node: ast.AST) -> None:
        """A nested scope rebinds a parameter only through ``nonlocal``."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Nonlocal):
                self.rebound_anywhere.update(sub.names)

    def _bind(self, name: str, node: _Located, loops: frozenset[int]) -> None:
        if name in self.params:
            self.rebinds.setdefault(name, []).append(_Rebind(node.lineno, node.col_offset, loops))

    def unknown_at(self, branch: _Branch) -> set[str]:
        """The parameters that may hold another value than the caller's at ``branch``."""
        seen = {
            name
            for name, rebinds in self.rebinds.items()
            if any(branch.sees(rebind) for rebind in rebinds)
        }
        return self.rebound_anywhere | seen

    def rebinds_anywhere(self, name: str) -> bool:
        """Whether the parameter ``name`` is bound again anywhere in the function."""
        return name in self.rebinds or name in self.rebound_anywhere


def _reads(test: ast.expr) -> set[str]:
    """Every bare name ``test`` loads, nested scopes aside."""
    names: set[str] = set()
    pending: list[ast.AST] = [test]
    while pending:
        node = pending.pop()
        if isinstance(node, (*_SCOPES, *_COMPREHENSIONS)):
            continue
        if isinstance(node, ast.Name):
            names.add(node.id)
        pending.extend(ast.iter_child_nodes(node))
    return names


# --- which functions are judged, and what each site passes ---------------------


class _CallSites:
    """Where production calls sit, read once per file: entry-point guards and imports."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self._guards: dict[str, list[tuple[int, int]]] = {}
        self._imported: dict[str, set[str]] = {}

    def in_main_guard(self, call: dict) -> bool:
        """Whether the call sits in a module-level ``if __name__ == "__main__":``."""
        filepath = call["file"]
        if filepath not in self._guards:
            tree = self.ctx.all_trees[Path(filepath)]
            self._guards[filepath] = [
                (node.lineno, node.end_lineno or node.lineno)
                for node in tree.body
                if isinstance(node, ast.If) and _is_main_guard(node.test)
            ]
        return any(first <= call["line"] <= last for first, last in self._guards[filepath])

    def through_import(self, call: dict, owner: ast.ClassDef) -> bool:
        """Whether a method call's receiver is rooted in an imported name other than its class.

        ``re.search(...)`` reaches every project method named ``search`` in
        the index, which binds methods by name alone; a call through an
        imported module or class that is not the method's own class is
        probably someone else's method.
        """
        receiver = call["node"].func.value
        parts = []
        while isinstance(receiver, ast.Attribute):
            parts.append(receiver.attr)
            receiver = receiver.value
        if not isinstance(receiver, ast.Name) or owner.name in {*parts, receiver.id}:
            return False
        filepath = call["file"]
        if filepath not in self._imported:
            imports = self.ctx.module_imports.get(Path(filepath), [])
            self._imported[filepath] = {imp.bound for imp in imports}
        return receiver.id in self._imported[filepath]

    def are_the_callers(self, production: list[dict], owner: ast.ClassDef | None) -> bool:
        """Whether these production calls are all the function's callers.

        Not when every one is the process entry point's guard: the real
        caller is the interpreter or a console script, outside the analyzed
        code, so the function is a public boundary. Nor, for a method, when
        one goes through an imported name (``through_import``).
        """
        if all(self.in_main_guard(call) for call in production):
            return False
        return owner is None or not any(self.through_import(call, owner) for call in production)


def _judged(ctx: AnalysisContext, sites: _CallSites, entry: CallerValues) -> bool:
    """Whether every caller of ``entry`` is a call the index sees (module docstring)."""
    node = entry.node
    name = node.name
    production = entry.production_callers
    if entry.overrides_unresolved_base or not production or ctx.roles.is_test(entry.file):
        return False
    if name.startswith("__") and name.endswith("__"):
        return False
    if not _plain(node) or _has_interface_decorator(node) or name in FRAMEWORK_HOOK_METHODS:
        return False
    if framework_dispatch_decorators(node) or has_dictated_signature(node):
        return False
    if is_referenced_as_value_in_production(name, ctx) or is_used_as_decorator(name, ctx):
        return False
    if is_referenced_as_dotted_string(name, ctx):
        return False
    return sites.are_the_callers(production, entry.owner)


def _known(param: ParamValues, value: PassedValue) -> _Known | None:
    """What one site passes for ``param``, when it is a constant the analysis can read."""
    if value.kind is PassedKind.OMITTED:
        default = param.default
        return _Known(default.value) if isinstance(default, ast.Constant) else None
    if value.kind in {PassedKind.LITERAL, PassedKind.NONE} and value.literal is not None:
        try:
            return _Known(ast.literal_eval(value.literal))
        except (ValueError, SyntaxError):
            return None
    return None


def _known_production(param: ParamValues) -> list[tuple[PassedValue, Any]] | None:
    """Each production site with the constant it gives ``param``; None when any is unknown."""
    sites = []
    for value in param.production:
        known = _known(param, value)
        if known is None:
            return None
        sites.append((value, known.value))
    return sites


def _explicit_production(param: ParamValues) -> list[tuple[PassedValue, ast.expr]] | None:
    """Each production site with the argument it passes for ``param``.

    None when any site omits the parameter or may fill it through a
    passthrough splat: what reaches the body there is not an argument the
    analysis can read.
    """
    sites = []
    for value in param.production:
        if value.argument is None:
            return None
        sites.append((value, value.argument))
    return sites


def _label(param: ParamValues, value: PassedValue) -> str:
    """How a message shows what a site passes."""
    if value.kind is PassedKind.OMITTED:
        default = "" if param.default is None else f" {ast.unparse(param.default)}"
        return f"the default{default}"
    if value.passthrough:
        return "a passthrough splat"
    if value.literal is not None:
        return value.literal
    return "an expression"


def _sites(values: list[PassedValue]) -> str:
    """``a.py:3, :9, b.py:4`` — every call site, by file."""
    lines: dict[str, list[int]] = {}
    for value in values:
        lines.setdefault(value.file, []).append(value.line)
    return ", ".join(
        filepath + ", ".join(f":{n}" for n in sorted(numbers))
        for filepath, numbers in sorted(lines.items())
    )


def _cite(groups: dict[str, list[PassedValue]]) -> str:
    """``True at a.py:3, :9, b.py:4; False at c.py:1`` — every site, grouped by value."""
    return "; ".join(f"{label} at {_sites(values)}" for label, values in groups.items())


def _named_defaults(params: list[ParamValues]) -> str:
    """``'a' (default 1), 'b' (default None)`` — each parameter with its default."""
    return ", ".join(
        f"'{param.name}' (default {ast.unparse(param.default)})"
        for param in params
        if param.default is not None
    )


@dataclass(frozen=True)
class _Decided:
    """A branch every production caller settles one way."""

    branch: _Branch
    outcome: bool
    reads: tuple[str, ...]
    production: dict[str, list[PassedValue]]
    dissenting_tests: list[PassedValue]

    @property
    def dead(self) -> tuple[int, int] | None:
        return self.branch.false_arm if self.outcome else self.branch.true_arm


class _Function:
    """One judged function: its parameters, branches and what its callers decide."""

    def __init__(self, entry: CallerValues) -> None:
        self.entry = entry
        self.node = entry.node
        self.name = entry.node.name
        self.params = {param.name: param for param in entry.params}
        self.body = _Body(entry.node, set(self.params))

    @property
    def statements(self) -> list[ast.stmt]:
        """The function's own body."""
        return self.node.body

    @cached_property
    def read(self) -> frozenset[str]:
        """Every name the body mentions; a parameter outside it is vestigial-params' finding."""
        return frozenset(
            node.id
            for stmt in self.node.body
            for node in ast.walk(stmt)
            if isinstance(node, ast.Name)
        )

    def always_default(self) -> list[ParamValues]:
        """Defaulted parameters the body reads and no production site passes."""
        return [
            param
            for param in self.entry.params
            if param.default is not None
            and param.name in self.read
            and all(value.kind is PassedKind.OMITTED for value in param.production)
        ]

    def dead_branch_params(self) -> set[str]:
        """The parameters deciding a branch dead-branch-by-callers reports."""
        return {name for decided, owned in self.entries() if not owned for name in decided.reads}

    def decided(self) -> list[_Decided]:
        """Every branch all production sites settle one way, outer branches first.

        A branch inside an arm already found dead is left out: it never runs.
        """
        found: list[_Decided] = []
        for branch in sorted(self.body.branches, key=lambda b: (b.line, b.test.col_offset)):
            if any(d.dead and d.dead[0] <= branch.line <= d.dead[1] for d in found):
                continue
            decided = self._decide(branch)
            if decided is not None:
                found.append(decided)
        return found

    def _decide(self, branch: _Branch) -> _Decided | None:
        unknown = self.body.unknown_at(branch)
        reads = tuple(
            sorted(name for name in _reads(branch.test) if name in self.params.keys() - unknown)
        )
        if not reads:
            return None
        params = [self.params[name] for name in reads]

        def outcome(values: list[PassedValue]) -> bool | None:
            env: _Env = {
                param.name: _known(param, value)
                for param, value in zip(params, values, strict=True)
            }
            return _truth(branch.test, env)

        production = list(zip(*(param.production for param in params), strict=True))
        outcomes = {outcome(list(values)) for values in production}
        if len(outcomes) != 1 or None in outcomes:
            return None
        settled = outcomes.pop()
        groups: dict[str, list[PassedValue]] = {}
        for values in production:
            key = ", ".join(
                (f"{p.name}=" if len(params) > 1 else "") + _label(p, v)
                for p, v in zip(params, values, strict=True)
            )
            groups.setdefault(key, []).append(values[0])
        tests = list(zip(*(param.tests for param in params), strict=True))
        dissenting = [values[0] for values in tests if outcome(list(values)) is (not settled)]
        return _Decided(branch, bool(settled), reads, groups, dissenting)

    def entries(self) -> Iterator[tuple[_Decided, bool]]:
        """Each decided branch, and whether an always-default parameter decides it."""
        owned = {param.name for param in self.always_default()}
        for decided in self.decided():
            yield decided, bool(owned & set(decided.reads))


def _functions(ctx: AnalysisContext) -> Iterator[_Function]:
    sites = _CallSites(ctx)
    for entries in ctx.caller_values.by_file.values():
        for entry in entries:
            if _judged(ctx, sites, entry):
                yield _Function(entry)


def _lines(span: tuple[int, int]) -> str:
    first, last = span
    return f"line {first}" if first == last else f"lines {first}-{last}"


def _dead_text(decided: _Decided) -> str:
    """The dead arm, by its lines, or why there are none.

    Only an always-true ``if`` with no ``else`` (and a body that does not
    leave) has no dead lines: its false path runs nothing.
    """
    expression = decided.branch.expression
    if expression is not None:
        arm = "else" if decided.outcome else "if"
        dead, live = (
            (expression.orelse, expression.body)
            if decided.outcome
            else (expression.body, expression.orelse)
        )
        return (
            f"dead: the {arm} value `{ast.unparse(dead)}`, so the expression is always "
            f"`{ast.unparse(live)}`"
        )
    if decided.dead is not None:
        return f"dead: {_lines(decided.dead)}"
    return "the false path runs nothing, so the test itself is vestigial"


@check(
    "dead-branch-by-callers",
    severity=Severity.MEDIUM,
    description="A branch on a parameter that every production caller decides the same way",
    advice=Advice(
        fix=(
            "Delete the dead arm the message names and the test, keeping the live code"
            " unconditionally; a conditional expression becomes its live value."
        ),
        dismiss=(
            "Dismiss only if the function is a genuine public boundary that code outside the"
            " analyzed tree calls, and the test is defensive on its behalf."
        ),
    ),
)
def check_dead_branch_by_callers(ctx: AnalysisContext) -> list[Finding]:
    """Find branches whose arm no production caller can reach (module docstring)."""
    findings = []
    for function in _functions(ctx):
        name = function.name
        for decided, owned in function.entries():
            if owned:
                continue
            branch = decided.branch
            count = sum(len(values) for values in decided.production.values())
            message = (
                f"{name}(): `{ast.unparse(branch.test)}` (line {branch.line}) is always "
                f"{decided.outcome} for all {count} production caller(s) "
                f"({_cite(decided.production)}); {_dead_text(decided)}."
            )
            if decided.dissenting_tests:
                message += f" Only tests take the other arm: {_sites(decided.dissenting_tests)}."
            findings.append(
                Finding(
                    file=str(function.entry.file),
                    line=branch.line,
                    check="dead-branch-by-callers",
                    message=message,
                    severity=Severity.MEDIUM,
                    subjects=param_subjects(function.entry, decided.reads),
                )
            )
    return findings


@check(
    "always-default-params",
    # LOW: in a fleet sample of 8, half were test seams (away-run Q13).
    severity=Severity.LOW,
    description=(
        "A defaulted parameter no production caller ever passes (tests that pass it are named)"
    ),
    advice=Advice(
        fix=(
            "Delete the parameter and use its default directly in the body, with the branches"
            " the default decides and the tests that pass it."
        ),
        dismiss=(
            "Dismiss only if the function is a genuine public boundary that code outside the"
            " analyzed tree calls with the parameter."
        ),
    ),
)
def check_always_default_params(ctx: AnalysisContext) -> list[Finding]:
    """Find defaulted parameters every production caller leaves at the default."""
    findings = []
    for function in _functions(ctx):
        params = function.always_default()
        if not params:
            continue
        entry = function.entry
        name = function.name
        names = _named_defaults(params)
        omitting = list(params[0].production)
        it = "it" if len(params) == 1 else "them"
        message = (
            f"{name}() never receives {names} from any of its {len(omitting)} production "
            f"caller(s), which all omit {it} ({_sites(omitting)})"
        )
        passing = {
            (value.file, value.line): value
            for param in params
            for value in param.tests
            if value.kind is not PassedKind.OMITTED
        }
        if passing:
            surface = surface_of_sites(ctx, passing)
            message += (
                f"; only tests pass {it}, at {_sites(list(passing.values()))}"
                f" ({surface.describe()})"
            )
        branches = [
            f"line {d.branch.line} ({_dead_text(d)})"
            for d, by_default in function.entries()
            if by_default
        ]
        if branches:
            message += "; the branches the default decides: " + ", ".join(branches)
        if not ctx.roles.files:
            message += "; " + TESTS_UNSEEN.format(use=f"passes {it}")
        findings.append(
            Finding(
                file=str(entry.file),
                line=function.node.lineno,
                check="always-default-params",
                message=message,
                severity=Severity.LOW,
                subjects=param_subjects(entry, [param.name for param in params]),
            )
        )
    return findings
