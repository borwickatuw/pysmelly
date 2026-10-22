"""literal-dispatch-param and fixture-literal-in-production: code shaped by its callers' values.

Both look for the is_even shape: a function that handles exactly the inputs
someone happened to try, case by case, instead of a rule that covers the
input. literal-dispatch-param finds it from the callers' side, on the
caller-value index (``ctx.caller_values``, Phase 25-1); fixture-literal-in-
production finds it from the tests' side, by the literals a test feeds in
and production checks for.

**literal-dispatch-param.** A function's body dispatches on one parameter's
literal values: an ``if``/``elif`` ladder of at least two arms, each testing
the parameter against constants (``p == "a"``, ``p in (1, 2)``, ``p is
None``, joined by ``or``), or a ``match p:`` whose cases are literal
patterns. Its fallthrough, the ``else``/``case _:`` arm or (when every arm
leaves the function) the statements after it, raises or returns None. The
values the ladder handles are exactly the values its callers pass: every
production site passes a constant (or omits a constant default), and the
production and test-role constants together equal the handled set. Then the
ladder is a table of its callers' answers and the fallthrough guards
nothing. When the handled values are numbers and every arm returns a number
or bool, the table stands in for a computation, the strongest form. A test
passing an expression the analysis cannot read is no evidence either way
and is left out; a production one makes the parameter unknown, and nothing
is reported.

The functions judged are lone-dissenter's (``caller_split._subjects``:
every caller visible to the index, and a method only by a unique name and
calls tied to its class). A parameter rebound anywhere in the body is not
judged, and one deciding a dead-branch-by-callers branch belongs to that
finding.

**fixture-literal-in-production.** A distinctive literal (a string or bytes
of ``MIN_DISTINCTIVE_LENGTH`` or more characters, or a number that is not
one of the everyday ones in ``_EVERYDAY_NUMBERS``: 0, 1, -1, 10, 100, 1000,
powers of two and HTTP status codes) that a test-role call passes to a
function as an argument, or that the ``assert`` holding the call compares
its result with, and that the function's own body decides on: an operand of
``==``/``!=``/``in``/``not in``, a ``match`` value pattern, or the key of a
dict display the module looks values up in by a computed key. The value
must have no production source: a literal production code uses in any
other way (an argument, an assignment, a return value, a record's key, a
named constant such as ``ARCHIVED = "archived"`` or an ``Enum`` member)
anywhere in the analyzed tree is production's own vocabulary, and is
skipped; that is the domain-constant false positive. The test call is tied
to the function the way lone-dissenter ties calls (``_subjects``). Two
rules, each measured on the fleet (DECISIONS.md, Phase 25-5), keep this to
the is_even shape: without the production-source rule a record's keys and
the statuses production itself emits are reported, and without the tie to
the function the external vocabularies tests stub (AWS error codes, file
format names) are.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pysmelly.checks.caller_decided import (
    _cite,
    _exits,
    _known,
    _known_production,
    _sites,
)
from pysmelly.checks.caller_split import _Subject, _subjects
from pysmelly.checks.callers import (
    PassedValue,
    _is_none_literal,
    literal_subjects,
    param_subjects,
)
from pysmelly.checks.repetition import DICT_ACCESS_METHODS
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

# A ladder is a dispatch only with two arms or more; one arm deciding every
# caller the same way is dead-branch-by-callers' finding.
MIN_LADDER_ARMS = 2

# Strings and bytes shorter than this are too common to tie a test to code.
MIN_DISTINCTIVE_LENGTH = 5

# The largest power of two treated as an everyday number (2**64).
_MAX_POWER_OF_TWO_EXPONENT = 64

_EVERYDAY_NUMBERS: frozenset[int] = frozenset(
    {0, 1, -1, 10, 100, 1000}
    | {2**exponent for exponent in range(_MAX_POWER_OF_TWO_EXPONENT + 1)}
    | {int(status) for status in HTTPStatus}
)


# --- literal-dispatch-param ------------------------------------------------------


@dataclass(frozen=True)
class _Arm:
    """One arm of a ladder: the values its test handles and its body."""

    values: frozenset[Any]
    line: int
    body: list[ast.stmt]


@dataclass(frozen=True)
class _Ladder:
    """A dispatch on one parameter's literal values, and what runs when none matches."""

    param: str
    line: int
    arms: list[_Arm]
    fallthrough: list[ast.stmt]

    @property
    def handled(self) -> frozenset[Any]:
        return frozenset().union(*(arm.values for arm in self.arms))


def _constant(node: ast.expr) -> tuple[bool, Any]:
    """``(True, value)`` for a constant or a negated number; ``(False, None)`` for anything else."""
    if isinstance(node, ast.Constant):
        return True, node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        return True, -node.operand.value
    return False, None


def _constants(nodes: list[ast.expr]) -> frozenset[Any] | None:
    """The values of a list of constant expressions; None when any is not one."""
    values = []
    for node in nodes:
        known, value = _constant(node)
        if not known:
            return None
        values.append(value)
    return frozenset(values)


def _tested(test: ast.expr) -> tuple[str, frozenset[Any]] | None:
    """The parameter a ladder test compares and the constants it accepts.

    ``p == c`` (either way round), ``p is c``, ``p in (c, ...)`` and an
    ``or`` of those on one name.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        parts = [_tested(value) for value in test.values]
        names = {part[0] for part in parts if part is not None}
        if None in parts or len(names) != 1:
            return None
        return names.pop(), frozenset().union(*(part[1] for part in parts if part is not None))
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    op, left, right = test.ops[0], test.left, test.comparators[0]
    if isinstance(op, (ast.Eq, ast.Is)):
        if isinstance(right, ast.Name):
            left, right = right, left
        values = _constants([right])
    elif isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
        values = _constants(right.elts)
    else:
        return None
    if not isinstance(left, ast.Name) or not values:
        return None
    return left.id, values


def _if_ladder(stmt: ast.If) -> tuple[str, list[_Arm], list[ast.stmt]] | None:
    """An ``if``/``elif`` chain on one parameter: its name, arms and ``else`` block."""
    arms: list[_Arm] = []
    names: set[str] = set()
    current: ast.If = stmt
    while True:
        tested = _tested(current.test)
        if tested is None:
            return None
        names.add(tested[0])
        arms.append(_Arm(tested[1], current.lineno, current.body))
        orelse = current.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            current = orelse[0]
            continue
        if len(names) != 1:
            return None
        return names.pop(), arms, orelse


def _pattern_values(pattern: ast.pattern) -> frozenset[Any] | None:
    """The constants a literal ``case`` pattern matches; None for any other pattern."""
    if isinstance(pattern, ast.MatchValue):
        return _constants([pattern.value])
    if isinstance(pattern, ast.MatchSingleton):
        return frozenset({pattern.value})
    if isinstance(pattern, ast.MatchOr):
        parts = [_pattern_values(item) for item in pattern.patterns]
        if any(part is None for part in parts):
            return None
        return frozenset().union(*(part for part in parts if part is not None))
    return None


def _is_wildcard(case: ast.match_case) -> bool:
    pattern = case.pattern
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None


def _match_ladder(stmt: ast.Match) -> tuple[str, list[_Arm], list[ast.stmt]] | None:
    """A ``match`` on one parameter with literal cases: its name, arms and wildcard block."""
    if not isinstance(stmt.subject, ast.Name):
        return None
    cases = list(stmt.cases)
    fallthrough: list[ast.stmt] = []
    if cases and _is_wildcard(cases[-1]) and cases[-1].guard is None:
        fallthrough = cases.pop().body
    arms = []
    for case in cases:
        values = _pattern_values(case.pattern)
        if values is None or case.guard is not None:
            return None
        arms.append(_Arm(values, case.pattern.lineno, case.body))
    return stmt.subject.id, arms, fallthrough


def _fails(block: list[ast.stmt]) -> bool:
    """Whether a fallthrough only raises or returns None, after any bare calls (a log line)."""
    if not block:
        return True
    *before, last = block
    if not all(isinstance(stmt, ast.Expr) for stmt in before):
        return False
    if isinstance(last, ast.Raise):
        return True
    return isinstance(last, ast.Return) and (last.value is None or _is_none_literal(last.value))


def literal_ladders(body: list[ast.stmt]) -> Iterator[_Ladder]:
    """Every literal dispatch in the function's own scope.

    A ladder without an ``else`` falls through to the statements after it
    only when it sits in the function's top-level block and every arm leaves
    the function; anywhere else what follows it is not known to be its
    fallthrough.
    """
    for top, stmt, after in _statements(body):
        if isinstance(stmt, ast.If):
            found = _if_ladder(stmt)
        elif isinstance(stmt, ast.Match):
            found = _match_ladder(stmt)
        else:
            continue
        if found is None:
            continue
        param, arms, fallthrough = found
        if len(arms) < MIN_LADDER_ARMS:
            continue
        if not fallthrough:
            if not top or not all(_exits(arm.body) for arm in arms):
                continue
            fallthrough = after
        if _fails(fallthrough):
            yield _Ladder(param, stmt.lineno, arms, fallthrough)


def _statements(body: list[ast.stmt]) -> Iterator[tuple[bool, ast.stmt, list[ast.stmt]]]:
    """Each statement of the function's own scope, whether it is top-level, and what follows it.

    An ``elif`` is part of its ladder, not a statement of its own, and
    nested ``def``/``class`` bodies are other scopes.
    """
    pending: list[tuple[bool, list[ast.stmt]]] = [(True, body)]
    while pending:
        top, block = pending.pop()
        for index, stmt in enumerate(block):
            yield top, stmt, block[index + 1 :]
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            pending.extend((False, inner) for inner in _blocks(stmt))


def _blocks(stmt: ast.stmt) -> Iterator[list[ast.stmt]]:
    """The statement blocks directly inside ``stmt``, an ``elif`` chain's arms flattened."""
    if isinstance(stmt, ast.If):
        current = stmt
        while True:
            yield current.body
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            if current.orelse:
                yield current.orelse
            return
    if isinstance(stmt, ast.Match):
        for case in stmt.cases:
            yield case.body
        return
    for field in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            yield block
    for handler in getattr(stmt, "handlers", []):
        yield handler.body


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _tabulates_numbers(ladder: _Ladder) -> bool:
    """Numbers in, a number or bool constant out of every arm: a computation written as a table."""
    if not all(_is_number(value) for value in ladder.handled):
        return False
    for arm in ladder.arms:
        if len(arm.body) != 1 or not isinstance(arm.body[0], ast.Return):
            return False
        result = arm.body[0].value
        if not isinstance(result, ast.Constant) or not isinstance(result.value, (int, float)):
            return False
    return True


@dataclass(frozen=True)
class _Dispatch:
    """A ladder whose handled values are exactly what its callers pass."""

    ladder: _Ladder
    production: list[tuple[PassedValue, Any]]
    tests: list[PassedValue]


def _dispatches(subject: _Subject) -> Iterator[_Dispatch]:
    """The ladders whose handled values are exactly the constants production and tests pass.

    A test passing a value the analysis cannot read is left out of the set.
    """
    function = subject.function
    owned = function.dead_branch_params()
    for ladder in literal_ladders(function.statements):
        param = function.params.get(ladder.param)
        if param is None or ladder.param in owned or function.body.rebinds_anywhere(ladder.param):
            continue
        production = _known_production(param)
        if production is None:
            continue
        tests = []
        passed = [value for _site, value in production]
        for site in subject.test_values(param):
            known = _known(param, site)
            if known is not None:
                tests.append(site)
                passed.append(known.value)
        if frozenset(passed) == ladder.handled:
            yield _Dispatch(ladder, production, tests)


def _arms_text(ladder: _Ladder) -> str:
    """``'csv' (line 4), 'json' (line 6)`` — each handled value with its arm's line."""
    return ", ".join(
        f"{', '.join(sorted(repr(value) for value in arm.values))} (line {arm.line})"
        for arm in ladder.arms
    )


def _fallthrough_text(ladder: _Ladder) -> str:
    if not ladder.fallthrough:
        return "the fallthrough, which falls off the end and returns None"
    first = ladder.fallthrough[0].lineno
    last = ladder.fallthrough[-1].end_lineno or first
    where = f"line {first}" if first == last else f"lines {first}-{last}"
    action = "raises" if isinstance(ladder.fallthrough[-1], ast.Raise) else "returns None"
    return f"the fallthrough at {where}, which {action}"


def _dispatch_message(name: str, dispatch: _Dispatch) -> str:
    ladder = dispatch.ladder
    groups: dict[str, list[PassedValue]] = {}
    for site, value in dispatch.production:
        groups.setdefault(repr(value), []).append(site)
    message = (
        f"{name}(): the dispatch on '{ladder.param}' at line {ladder.line} handles exactly the "
        f"values its callers pass, {_arms_text(ladder)}: production passes {_cite(groups)}"
    )
    if dispatch.tests:
        message += f", and tests pass them at {_sites(dispatch.tests)}"
    message += f". No caller reaches {_fallthrough_text(ladder)}, so the ladder is a table of "
    message += "its callers' answers rather than a rule."
    if _tabulates_numbers(ladder):
        message += (
            " Numbers go in and numbers come out, so the table stands in for a computation "
            "(is_even written as cases)."
        )
    return message


@check(
    "literal-dispatch-param",
    severity=Severity.MEDIUM,
    description="A literal dispatch whose cases are exactly the values its callers pass",
    advice=Advice(
        fix=(
            "Compute the result from the input when numbers map to numbers; otherwise name"
            " the closed vocabulary (an Enum, or a mapping both sides share) so no value can"
            " fall through, or write the rule that covers every input."
        ),
        dismiss=(
            "Dismiss only if the function is a genuine public boundary that code outside the"
            " analyzed tree calls with other values."
        ),
    ),
)
def check_literal_dispatch_param(ctx: AnalysisContext) -> list[Finding]:
    """Find ladders on a parameter that handle just what the callers pass (module docstring)."""
    findings = []
    for subject in _subjects(ctx):
        entry = subject.entry
        for dispatch in _dispatches(subject):
            findings.append(
                Finding(
                    file=str(entry.file),
                    line=dispatch.ladder.line,
                    check="literal-dispatch-param",
                    message=_dispatch_message(entry.name, dispatch),
                    severity=Severity.MEDIUM,
                    subjects=param_subjects(entry, [dispatch.ladder.param]),
                )
            )
    return findings


# --- fixture-literal-in-production ------------------------------------------------


@dataclass(frozen=True)
class _Site:
    file: str
    line: int
    how: str


def _distinctive(value: Any) -> bool:
    if isinstance(value, (str, bytes)):
        return len(value) >= MIN_DISTINCTIVE_LENGTH
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return True
    return int(value) not in _EVERYDAY_NUMBERS


def _key(node: ast.expr) -> tuple[str, Any] | None:
    """A distinctive literal's identity (strings, bytes and numbers apart); else None."""
    known, value = _constant(node)
    if not known or not _distinctive(value):
        return None
    kind = "number" if _is_number(value) else type(value).__name__
    return kind, value


def _operands(node: ast.Compare) -> Iterator[ast.expr]:
    """A comparison's literal-bearing operands, a display's items included."""
    for operand in (node.left, *node.comparators):
        if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
            yield from operand.elts
        else:
            yield operand


_EQUALITY = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


# What production does with a literal that is not deciding on it.
_PRODUCES = "produces"


def _looked_up(node: ast.AST) -> ast.expr | None:
    """What ``node`` looks up by a computed key ``k``: ``t[k]``, ``t.get(k)`` and the like."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and not isinstance(node.slice, ast.Constant)
    ):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DICT_ACCESS_METHODS
        and node.args
        and not isinstance(node.args[0], ast.Constant)
    ):
        return node.func.value
    return None


def _tables(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> set[int]:
    """The ids of the dict displays a module looks values up in by a computed key.

    The display itself (``{...}[mode]``), or one bound to a bare name the
    same module looks up (``HANDLERS = {...}`` and ``HANDLERS[mode]``). Any
    other dict display builds a record, so its keys are values production
    produces, not ones it decides on.
    """
    tables: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        target = _looked_up(node)
        if isinstance(target, ast.Dict):
            tables.add(id(target))
        elif isinstance(target, ast.Name):
            names.add(target.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        parent = parents.get(node)
        bound = []
        if isinstance(parent, ast.Assign):
            bound = parent.targets
        elif isinstance(parent, ast.AnnAssign):
            bound = [parent.target]
        if any(isinstance(target, ast.Name) and target.id in names for target in bound):
            tables.add(id(node))
    return tables


def _use(node: ast.expr, parents: dict[ast.AST, ast.AST], tables: set[int]) -> str:
    """What production does with one literal: decides on it (and how), or produces it."""
    parent = parents.get(node)
    holder = parent
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        holder = parents.get(parent)
    if isinstance(holder, ast.Compare) and any(isinstance(op, _EQUALITY) for op in holder.ops):
        return "compares"
    if isinstance(parent, ast.MatchValue):
        return "matches"
    if isinstance(parent, ast.Dict) and node in parent.keys and id(parent) in tables:
        return "keys on"
    return _PRODUCES


def _production_literals(ctx: AnalysisContext, tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    """Every distinctive literal in a production module, and what the module does with it.

    It decides on it as an operand of ``==``/``!=``/``in``/``not in`` (a
    display's items included), as a ``case`` value, or as a lookup table's
    key; any other use produces it (an argument, an assignment, a record's
    key, a named constant). A docstring or other bare string statement is
    neither.
    """
    parents = ctx.parent_map(tree)
    tables = _tables(tree, parents)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.UnaryOp)) or _key(node) is None:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Expr) or (
            isinstance(node, ast.Constant) and isinstance(parent, ast.UnaryOp)
        ):
            continue
        yield node, _use(node, parents, tables)


def _decided(ctx: AnalysisContext) -> dict[tuple[str, Any], list[_Site]]:
    """Each distinctive literal production only decides on, with every site that does.

    A literal production also produces anywhere (passes, assigns, returns,
    names as a constant, builds a record with) is left out: production has
    its own source for it.
    """
    uses: dict[tuple[str, Any], list[_Site]] = {}
    for filepath, tree in sorted(ctx.all_trees.items()):
        if filepath in ctx.roles.files:
            continue
        for node, how in _production_literals(ctx, tree):
            key = _key(node)
            if key is not None:
                uses.setdefault(key, []).append(_Site(str(filepath), node.lineno, how))
    return {
        key: sites
        for key, sites in uses.items()
        if not any(site.how == _PRODUCES for site in sites)
    }


def _statement(ctx: AnalysisContext, filepath: Path, node: ast.AST) -> ast.AST | None:
    """The statement holding ``node``."""
    parents = ctx.parent_map(ctx.all_trees[filepath])
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(current)
    return current


def _fed(ctx: AnalysisContext, call: dict) -> Iterator[tuple[tuple[str, Any], _Site]]:
    """The distinctive literals a test call feeds in, and those its ``assert`` checks it against."""
    node: ast.Call = call["node"]
    filepath = call["file"]
    for arg in (*node.args, *(keyword.value for keyword in node.keywords)):
        key = _key(arg)
        if key is not None:
            yield key, _Site(filepath, arg.lineno, "passes")
    statement = _statement(ctx, Path(filepath), node)
    if isinstance(statement, ast.Assert) and isinstance(statement.test, ast.Compare):
        for operand in _operands(statement.test):
            key = _key(operand)
            if key is not None:
                yield key, _Site(filepath, operand.lineno, "asserts")


@dataclass(frozen=True)
class _Taught:
    """One literal a test feeds a function, and where that function decides on it."""

    literal: Any
    tests: list[_Site]
    decisions: list[_Site]


def _taught(
    ctx: AnalysisContext, subject: _Subject, decided: dict[tuple[str, Any], list[_Site]]
) -> list[_Taught]:
    entry = subject.entry
    fed: dict[tuple[str, Any], list[_Site]] = {}
    for index in sorted(subject.tests):
        for key, site in _fed(ctx, entry.test_callers[index]):
            if key in decided:
                fed.setdefault(key, []).append(site)
    taught = []
    for key, tests in fed.items():
        inside = [site for site in decided[key] if entry.encloses(site.file, site.line)]
        if inside:
            taught.append(_Taught(key[1], tests, inside))
    return taught


def _site_text(sites: list[_Site]) -> str:
    """``app.py:4, :9 (compares), b.py:2 (keys on)`` — every site, by file, with what it does."""
    by_file: dict[str, list[_Site]] = {}
    for site in sites:
        by_file.setdefault(site.file, []).append(site)
    return ", ".join(
        filepath
        + ", ".join(f":{n}" for n in sorted({site.line for site in found}))
        + f" ({', '.join(sorted({site.how for site in found}))})"
        for filepath, found in sorted(by_file.items())
    )


def _literal_token(literal: Any) -> str | None:
    """The text ``git log -S`` traces a literal by: a one-line string, or a number as written.

    None for anything else (bytes, a string spanning lines), which gets no
    provenance line.
    """
    if isinstance(literal, str):
        return literal if literal and "\n" not in literal else None
    if _is_number(literal):
        return repr(literal)
    return None


def _taught_message(name: str, taught: list[_Taught]) -> str:
    cases = "; ".join(
        f"{item.literal!r}, decided at {_site_text(item.decisions)} and fed in by "
        f"{_site_text(item.tests)}"
        for item in taught
    )
    noun = "a value" if len(taught) == 1 else "values"
    return (
        f"{name}() special-cases {noun} only tests supply: {cases}. No production code "
        "produces it, so the special case serves the tests, not the input (is_even written "
        "for its fixtures)."
    )


@check(
    "fixture-literal-in-production",
    severity=Severity.MEDIUM,
    description="A function special-cases a literal only its tests supply (needs tests analyzed)",
    advice=Advice(
        fix=(
            "Replace the special case with the rule that covers every input, or, if the value"
            " belongs to the domain, name it as a constant production and tests both import."
        ),
        dismiss=(
            "Dismiss only if the value comes from outside the analyzed code (a protocol, file"
            " format or API) and the check is that contract's."
        ),
    ),
)
def check_fixture_literal_in_production(ctx: AnalysisContext) -> list[Finding]:
    """Find functions that decide on a literal a test feeds them (module docstring)."""
    if not ctx.roles.files:
        return []
    decided = _decided(ctx)
    findings = []
    for subject in _subjects(ctx):
        taught = _taught(ctx, subject, decided)
        if not taught:
            continue
        first = min((site.file, site.line) for item in taught for site in item.decisions)
        findings.append(
            Finding(
                file=first[0],
                line=first[1],
                check="fixture-literal-in-production",
                message=_taught_message(subject.entry.name, taught),
                severity=Severity.MEDIUM,
                subjects=literal_subjects(
                    subject.entry,
                    [
                        token
                        for item in taught
                        if (token := _literal_token(item.literal)) is not None
                    ],
                ),
            )
        )
    return findings
