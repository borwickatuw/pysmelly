"""return-shape-unused: return values, tuple elements and record fields no caller reads.

A function computes a value and returns it, and the code that calls it
throws the value away. Either the return is vestigial (a status, a count or
a result some earlier caller needed) or a caller that should be checking it
is not. The partial form is the same remains at a finer grain: a function
returns a pair and most callers keep only the first half, or it returns a
record whose field none of its callers reads.

**Which functions.** Every function defined at module level, and every
method whose name exactly one analyzed class defines, in production code.
A method's class must reach no base outside the project (``object``,
``ABC``, ``Generic`` and ``Protocol`` aside), since a library base can
declare the method and read what an override returns. The function must
return something other than ``None`` somewhere in its own body (a bare
``return``, ``return None`` and falling off the end are all ``None``), and
must not be a generator. Skipped:

- a method that returns ``self`` anywhere (a fluent API: the return is
  there to chain, and dropping it is the normal use);
- a dunder method or a framework hook method (``FRAMEWORK_HOOK_METHODS``);
- a decorated function, except for ``staticmethod``, ``classmethod``,
  ``cache``, ``lru_cache`` and ``override``, which leave the return as it
  is. The framework dispatch decorators (``FRAMEWORK_DISPATCH_DECORATORS``:
  ``@receiver``, ``@task``, ``@hookimpl``...) fall under this, as do route
  and command registrations, ``property`` and ``contextmanager``;
- a callback or a registered handler: a function whose name production code
  loads anywhere other than as the callee of a call (``map(f, ...)``,
  ``{"k": f}``, ``callback=f``, ``obj.method`` unbound), names in a dotted
  string, or uses as a decorator, and a function called from an
  ``if __name__ == "__main__":`` block (an entry point, whose console-script
  wrapper reads the return as the exit status).

**Callers.** ``ctx.caller_values``: a module-level function's calls are the
ones that resolve to it under the Phase 21-3 rule, a method's are the
attribute calls of its name. Only production callers decide; test callers
are named in the message.

**What a caller does with the value.** Each call site is one of:

- a *discard*: an expression statement (``f()``, ``await f()``), a name
  bound to it that its function never reads (``_`` included), or an
  unpacking whose every name is unread;
- a *partial read*: ``f()[i]``, ``f()["k"]``, ``f().attr``, an unpacking
  ``a, _ = f()`` (each name read counts its position), or a name bound once
  to the result whose every read is one of those (``r[0]``, ``r.attr``,
  ``r.get("k")``);
- a *whole use*: anything else (returned, passed on, compared, iterated,
  bound at module level, a name bound more than once, a method called on the
  result). A whole use reads every part.

**Findings.**

- MEDIUM: every production caller discards the return.
- LOW, a tuple position: the function returns tuples of one length ``n``
  (every non-``None`` return is a tuple display of that length, or the
  return annotation is ``tuple[...]`` of ``n`` types), and at least
  ``MIN_PARTIAL_SITES`` production callers read some element but not
  element ``i``, more than ``PARTIAL_SHARE`` of all production callers.
- LOW, a record field: every non-``None`` return builds one record class
  (``records.RecordIndex``: dataclass, pydantic, attrs, NamedTuple), every
  production caller reads its parts (or discards it), at least
  ``MIN_PARTIAL_SITES`` of them read some part, and no caller reads a field.
  A NamedTuple's positions read the fields they hold. A dict display is left
  to write-only-record-keys, which follows returned dicts to their callers.

The dismissal condition is a public API: callers outside the analyzed code
may read the value, and the message says so.
"""

from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path

from pysmelly.checks.callers import (
    CallerValues,
    _Callers,
    _callers_clause,
    _is_none_literal,
    _method_owners,
)
from pysmelly.checks.framework import FRAMEWORK_HOOK_METHODS
from pysmelly.checks.helpers import (
    ProjectClasses,
    _find_main_entry_points,
    base_class_name,
    is_bare_name,
    is_referenced_as_dotted_string,
    is_used_as_decorator,
    iter_functions,
    loaded_names,
    nested_function_ids,
    own_scope_nodes,
    referenced_name,
    source_trees,
)
from pysmelly.checks.records import _KEY_READ_METHODS, RecordClass, RecordIndex
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

CHECK_NAME = "return-shape-unused"

# The partial case needs at least this many production callers that read
# part of the value without the part in question...
MIN_PARTIAL_SITES = 3
# ...and they must be more than this share of all production callers.
PARTIAL_SHARE = 0.6

# Decorators that hand the function's return value through unchanged.
_TRANSPARENT_DECORATORS = frozenset(
    {"staticmethod", "classmethod", "cache", "lru_cache", "override"}
)
# Bases that declare no method a project class could be overriding.
_TRANSPARENT_BASES = frozenset({"object", "ABC", "Generic", "Protocol"})

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

# A part of a returned value one site reads: ("pos", i), ("attr", name) or ("key", k).
_Part = tuple[str, int | str]


class _Use(Enum):
    DISCARD = "discard"
    PARTS = "parts"
    WHOLE = "whole"


@dataclass(frozen=True)
class _SiteUse:
    """What one call site does with the returned value.

    ``parts`` holds what a ``PARTS`` site reads; ``unpacked`` is the length
    of the unpacking that took the value apart, None when none did. ``how``
    says, for a discard, the way it was dropped.
    """

    file: str
    line: int
    use: _Use
    parts: frozenset[_Part] = frozenset()
    unpacked: int | None = None
    how: str = ""


def _returned_values(func: _FunctionNode) -> list[ast.expr] | None:
    """The non-``None`` values ``func`` returns; None for a generator."""
    values: list[ast.expr] = []
    for node in own_scope_nodes(func.body):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return None
        if (
            isinstance(node, ast.Return)
            and node.value is not None
            and not _is_none_literal(node.value)
        ):
            values.append(node.value)
    return values


def _receiver(func: _FunctionNode, owner: ast.ClassDef | None) -> str | None:
    """The name of a method's receiver parameter; None for a function or staticmethod."""
    if owner is None or any(referenced_name(d) == "staticmethod" for d in func.decorator_list):
        return None
    positional = [*func.args.posonlyargs, *func.args.args]
    return positional[0].arg if positional else None


def _tuple_length(values: list[ast.expr], returns: ast.expr | None) -> int | None:
    """``n`` when the function returns ``n``-tuples, by its displays or its annotation."""
    displays = {
        len(value.elts)
        for value in values
        if isinstance(value, ast.Tuple) and not any(isinstance(e, ast.Starred) for e in value.elts)
    }
    if len(displays) == 1 and all(isinstance(value, ast.Tuple) for value in values):
        length = displays.pop()
        return length if length >= 2 else None
    if (
        isinstance(returns, ast.Subscript)
        and referenced_name(returns.value) in {"tuple", "Tuple"}
        and isinstance(returns.slice, ast.Tuple)
        and len(returns.slice.elts) >= 2
        and not any(isinstance(e, ast.Constant) and e.value is Ellipsis for e in returns.slice.elts)
    ):
        return len(returns.slice.elts)
    return None


# --- what a call site does with the value ---


def _part_read(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> _Part | None:
    """The part of ``node``'s value its parent reads; None when it uses the value otherwise."""
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript) and parent.value is node:
        if not isinstance(parent.ctx, ast.Load):
            return None
        index = parent.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return ("key", index.value)
        if isinstance(index, ast.Constant) and type(index.value) is int:
            return ("pos", index.value)
        if (
            isinstance(index, ast.UnaryOp)
            and isinstance(index.op, ast.USub)
            and isinstance(index.operand, ast.Constant)
            and type(index.operand.value) is int
        ):
            return ("pos", -index.operand.value)
        return None
    if isinstance(parent, ast.Attribute) and parent.value is node:
        if not isinstance(parent.ctx, ast.Load):
            return None
        grand = parents.get(parent)
        if isinstance(grand, ast.Call) and grand.func is parent:
            first = grand.args[0] if grand.args else None
            if (
                parent.attr in _KEY_READ_METHODS
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                return ("key", first.value)
            return None
        return ("attr", parent.attr)
    return None


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The function or lambda ``node`` sits in; None at module or class level."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return current
        if isinstance(current, ast.ClassDef):
            return None
    return None


class _NameUse:
    """How a function reads one local name: unread, read in parts, or read whole."""

    def __init__(self, scope: ast.AST, name: str, parents: dict[ast.AST, ast.AST]) -> None:
        loads: list[ast.Name] = []
        stores = 0
        # Read without a Name load: declared global/nonlocal, updated in
        # place (``x += 1`` reads x), or reachable through ``locals()``/``vars()``.
        opaque = False
        for node in ast.walk(scope):
            if is_bare_name(node, name):
                if isinstance(node.ctx, ast.Load):
                    loads.append(node)
                elif isinstance(node.ctx, ast.Store):
                    stores += 1
            elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                opaque = True
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                opaque = opaque or node.target.id == name
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                opaque = opaque or node.func.id in {"locals", "vars"}
            elif isinstance(node, ast.arg) and node.arg == name:
                stores += 1
        self.unread = not loads and not opaque
        parts: set[_Part] = set()
        whole = opaque or stores > 1
        for load in loads:
            part = _part_read(load, parents)
            if part is None:
                whole = True
            else:
                parts.add(part)
        self.whole = whole
        self.parts = frozenset(parts)


def _site_use(path: str, call: ast.Call, parents: dict[ast.AST, ast.AST]) -> _SiteUse:
    """Classify what the code around ``call`` does with its value."""
    node: ast.expr = call
    parent = parents.get(node)
    if isinstance(parent, ast.Await):
        node, parent = parent, parents.get(parent)
    if isinstance(parent, ast.Expr):
        return _SiteUse(path, call.lineno, _Use.DISCARD, how="statement")
    part = _part_read(node, parents)
    if part is not None:
        return _SiteUse(path, call.lineno, _Use.PARTS, frozenset({part}))
    target: ast.expr | None = None
    if isinstance(parent, ast.Assign) and parent.value is node and len(parent.targets) == 1:
        target = parent.targets[0]
    elif isinstance(parent, ast.AnnAssign) and parent.value is node:
        target = parent.target
    scope = _enclosing_scope(node, parents)
    if target is None or scope is None:
        return _SiteUse(path, call.lineno, _Use.WHOLE)
    if isinstance(target, ast.Name):
        return _bound_use(path, call.lineno, scope, target.id, parents)
    if isinstance(target, (ast.Tuple, ast.List)):
        return _unpacked_use(path, call.lineno, scope, target.elts, parents)
    return _SiteUse(path, call.lineno, _Use.WHOLE)


def _bound_use(
    path: str, line: int, scope: ast.AST, name: str, parents: dict[ast.AST, ast.AST]
) -> _SiteUse:
    use = _NameUse(scope, name, parents)
    if use.unread:
        return _SiteUse(path, line, _Use.DISCARD, how=f"unread {name}")
    if use.whole:
        return _SiteUse(path, line, _Use.WHOLE)
    return _SiteUse(path, line, _Use.PARTS, use.parts)


def _unpacked_use(
    path: str, line: int, scope: ast.AST, elts: list[ast.expr], parents: dict[ast.AST, ast.AST]
) -> _SiteUse:
    if any(isinstance(elt, ast.Starred) for elt in elts):
        return _SiteUse(path, line, _Use.WHOLE)
    read: set[_Part] = set()
    for position, elt in enumerate(elts):
        if not isinstance(elt, ast.Name) or not _NameUse(scope, elt.id, parents).unread:
            read.add(("pos", position))
    if not read:
        return _SiteUse(path, line, _Use.DISCARD, how="unpacked, all unread")
    return _SiteUse(path, line, _Use.PARTS, frozenset(read), unpacked=len(elts))


# --- which functions are judged ---


@dataclass(frozen=True)
class _Subject:
    """A judged function or method, its class if a method, and the calls that reach it.

    ``production`` holds the production call sites that decide; ``clause`` is
    the caller clause the message prints over the same calls.
    """

    file: Path
    node: _FunctionNode
    owner: ast.ClassDef | None
    production: list[dict]
    clause: str

    @property
    def label(self) -> str:
        prefix = f"{self.owner.name}." if self.owner is not None else ""
        return f"{prefix}{self.node.name}()"

    @property
    def anchor(self) -> Site:
        return Site(str(self.file), self.node.lineno)

    @property
    def total(self) -> int:
        """How many production callers it has."""
        return len(self.production)

    def tuple_length(self, values: list[ast.expr]) -> int | None:
        return _tuple_length(values, self.node.returns)

    def returned(self) -> list[ast.expr]:
        """The non-``None`` values it returns; empty when there is nothing to judge.

        Nothing is judged without a production caller, for a generator, for a
        function that only returns ``None``, or for a method that returns its
        receiver (a fluent API).
        """
        values = _returned_values(self.node)
        if not values or not self.production:
            return []
        receiver = _receiver(self.node, self.owner)
        if receiver is not None and any(is_bare_name(value, receiver) for value in values):
            return []
        return values


class _Candidates:
    """The functions and methods whose callers the check can see, in production code."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.classes = ProjectClasses(ctx)
        self.method_names = Counter(
            item.name
            for classes in self.classes.by_name.values()
            for node in classes
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        self.entry_points = _find_main_entry_points(ctx.all_trees)
        self.uncalled = _names_loaded_uncalled(ctx)

    def __iter__(self) -> Iterator[_Subject]:
        """Each judged function, in file order and then ``ast.walk`` order.

        The definitions and their classes come from the tree; the calls that
        reach each one come from ``ctx.caller_values``.
        """
        for path, tree in source_trees(self.ctx):
            nested = nested_function_ids(tree)
            owners = _method_owners(tree)
            for node in iter_functions(tree):
                owner = owners.get(id(node))
                if id(node) in nested or not self._judged(node, owner):
                    continue
                values = self.ctx.caller_values.of(node)
                if not values.is_method:
                    yield _Subject(
                        path, node, None, values.production_callers, values.callers_clause
                    )
                    continue
                subject = self._method_subject(path, node, owners[id(node)], values)
                if subject is not None:
                    yield subject

    def _method_subject(
        self, path: Path, node: _FunctionNode, owner: ast.ClassDef, values: CallerValues
    ) -> _Subject | None:
        """The method with the calls that reach it through its class; None when a
        production call of its name has a receiver that cannot be tied to the class.

        ``Cls.m()``, and ``self.m()`` or ``cls.m()`` inside the class or a
        project subclass, are tied. Any other receiver (``obj.m()``,
        ``super().m()``) may be this class's instance or a library object,
        so the method is not judged when production code has one. A test's
        untied call is left out of the count.
        """
        production = []
        for call in values.production_callers:
            if not self.classes.tied(call, owner):
                return None
            production.append(call)
        tests = [call for call in values.test_callers if self.classes.tied(call, owner)]
        clause = _callers_clause(_Callers(production, tests, frozenset()))
        return _Subject(path, node, owner, production, clause)

    def _judged(self, node: _FunctionNode, owner: ast.ClassDef | None) -> bool:
        name = node.name
        if any(referenced_name(d) not in _TRANSPARENT_DECORATORS for d in node.decorator_list):
            return False
        if (
            name in self.uncalled
            or name in self.entry_points
            or is_referenced_as_dotted_string(name, self.ctx)
            or is_used_as_decorator(name, self.ctx)
        ):
            return False
        if owner is None:
            return True
        if name.startswith("__") and name.endswith("__"):
            return False
        if name in FRAMEWORK_HOOK_METHODS or self.method_names[name] != 1:
            return False
        return self._project_ancestry(owner, set())

    def _project_ancestry(self, cls: ast.ClassDef, seen: set[int]) -> bool:
        """Whether every base ``cls`` reaches is a project class or a transparent one."""
        seen.add(id(cls))
        for base in cls.bases:
            name = base_class_name(base)
            if name in _TRANSPARENT_BASES:
                continue
            parents = [] if name is None else self.classes.by_name.get(name, [])
            if not parents:
                return False
            for parent in parents:
                if id(parent) not in seen and not self._project_ancestry(parent, seen):
                    return False
        return True


def _names_loaded_uncalled(ctx: AnalysisContext) -> set[str]:
    """Every name production code loads other than as a call's callee.

    A bare name or an attribute (``f``, ``obj.method``) loaded as a value is
    a function handed to something else to call, whose use of the return
    the call graph cannot see.
    """
    found: set[str] = set()
    for _path, tree in source_trees(ctx):
        callees = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        found.update(loaded_names(tree, callees))
    return found


# --- findings ---


@dataclass(frozen=True)
class _Render(ABC):
    """Rebuilds one finding's message from the sites still reported (``SiteRender``)."""

    subject: _Subject
    sites: list[_SiteUse]

    @abstractmethod
    def __call__(self, indices: tuple[int, ...]) -> str | None:
        """The message over the sites at ``indices``; None below the check's floor."""

    def finding(self, severity: Severity) -> Finding:
        """The finding over every site, anchored on the function's definition."""
        return multi_site_finding(
            CHECK_NAME,
            severity,
            [Site(use.file, use.line) for use in self.sites],
            self,
            anchor=self.subject.anchor,
        )

    def cite(self, indices: tuple[int, ...]) -> str:
        shown = [self.sites[i] for i in indices]
        return ", ".join(
            f"{use.file}:{use.line}" + (f" ({use.how})" if use.how else "") for use in shown
        )

    def tail(self, indices: tuple[int, ...]) -> str:
        """The sites and the caller clause."""
        return f"{self.cite(indices)} ({self.subject.clause})"


@dataclass(frozen=True)
class _DiscardRender(_Render):
    line: int

    def __call__(self, indices: tuple[int, ...]) -> str | None:
        if len(indices) != len(self.sites):
            return None
        return (
            f"{self.subject.label} returns a value (line {self.line}) and every production "
            f"caller discards it: " + self.tail(indices)
        )


@dataclass(frozen=True)
class _PositionRender(_Render):
    length: int
    position: int

    def __call__(self, indices: tuple[int, ...]) -> str | None:
        total = self.subject.total
        if not _partial_floor(total, len(indices)):
            return None
        return (
            f"{self.subject.label} returns a {self.length}-tuple and {len(indices)} of {total} "
            f"production callers never read element {self.position}: " + self.tail(indices)
        )


@dataclass(frozen=True)
class _FieldRender(_Render):
    record: str
    unread: tuple[str, ...]

    def __call__(self, indices: tuple[int, ...]) -> str | None:
        if len(indices) < MIN_PARTIAL_SITES:
            return None
        return (
            f"{self.subject.label} returns a {self.record} and none of its "
            f"{self.subject.total} production callers reads "
            f"{', '.join(self.unread)}: " + self.tail(indices)
        )


def _reads_positions(use: _SiteUse, length: int) -> bool:
    """Whether a partial read takes only elements of a ``length``-tuple, as one."""
    if use.unpacked is not None and use.unpacked != length:
        return False
    return all(
        kind == "pos" and isinstance(part, int) and -length <= part < length
        for kind, part in use.parts
    )


def _partial_floor(total: int, count: int) -> bool:
    return count >= MIN_PARTIAL_SITES and count > PARTIAL_SHARE * total


def _position_findings(subject: _Subject, length: int, uses: list[_SiteUse]) -> Iterator[Finding]:
    """One LOW finding per tuple element most production callers never read."""
    reading = [use for use in uses if use.use is _Use.PARTS]
    if not all(_reads_positions(use, length) for use in reading):
        return
    for position in range(length):
        lacking = [
            use
            for use in reading
            if ("pos", position) not in use.parts and ("pos", position - length) not in use.parts
        ]
        if not _partial_floor(len(uses), len(lacking)):
            continue
        yield _PositionRender(subject, lacking, length, position).finding(Severity.LOW)


class _Judge:
    """Turns one function's call-site uses into findings."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx

    def uses(self, subject: _Subject) -> list[_SiteUse]:
        found = []
        for call in subject.production:
            tree = self.ctx.all_trees[Path(call["file"])]
            found.append(_site_use(call["file"], call["node"], self.ctx.parent_map(tree)))
        return sorted(found, key=lambda use: (use.file, use.line))

    def findings(self, subject: _Subject) -> Iterator[Finding]:
        values = subject.returned()
        if not values:
            return
        uses = self.uses(subject)
        if all(use.use is _Use.DISCARD for use in uses):
            yield _DiscardRender(subject, uses, values[0].lineno).finding(Severity.MEDIUM)
            return
        length = subject.tuple_length(values)
        if length is not None:
            yield from _position_findings(subject, length, uses)
            return
        record = self._record(subject, values)
        if record is not None:
            yield from self._fields(subject, record, uses)

    @cached_property
    def records(self) -> RecordIndex:
        return RecordIndex(self.ctx)

    def _record(self, subject: _Subject, values: list[ast.expr]) -> RecordClass | None:
        """The one record class every returned value builds; None for anything else."""
        names = self.records.names[subject.file]
        found: set[RecordClass] = set()
        for value in values:
            if not isinstance(value, ast.Call):
                return None
            resolved = self.records.resolve(value.func, names)
            if len(resolved) != 1:
                return None
            found |= resolved
        return found.pop() if len(found) == 1 else None

    def _fields(
        self, subject: _Subject, record: RecordClass, uses: list[_SiteUse]
    ) -> Iterator[Finding]:
        if any(use.use is _Use.WHOLE for use in uses):
            return
        reading = [use for use in uses if use.use is _Use.PARTS]
        if len(reading) < MIN_PARTIAL_SITES:
            return
        read = self._fields_read(record, reading)
        if read is None:
            return
        fields = {
            name
            for ancestor in self.records.ancestors(record)
            for name, _item in ancestor.declared(self.records.names[ancestor.file])
            if not name.startswith("_")
        }
        unread = tuple(sorted(fields - read))
        if not unread:
            return
        yield _FieldRender(subject, reading, record.name, unread).finding(Severity.LOW)

    def _fields_read(self, record: RecordClass, reading: list[_SiteUse]) -> set[str] | None:
        """The fields the sites read; None when one reads the record some other way.

        An attribute names its field. A position names the field a NamedTuple
        holds there (no other record kind takes positions). A key read, which
        a TypedDict would take, is some other way.
        """
        positional = record.positional_fields(self.records)
        read: set[str] = set()
        for use in reading:
            for kind, part in use.parts:
                if kind == "attr":
                    read.add(str(part))
                elif kind == "pos" and isinstance(part, int):
                    if not -len(positional) <= part < len(positional):
                        return None
                    read.add(positional[part])
                else:
                    return None
        return read


@check(
    CHECK_NAME,
    severity=Severity.MEDIUM,
    description=(
        "Every production caller discards a function's return value (MEDIUM), "
        "or most never read one tuple element or record field (LOW)"
    ),
    advice=Advice(
        fix=(
            "Return only what callers read: stop returning a value every caller discards (or"
            " make the caller that should check it do so), and drop or split off the unread"
            " tuple element or record fields."
        ),
        dismiss=(
            "Dismiss only if the function is public API whose callers outside this"
            " code read the value."
        ),
    ),
)
def check_return_shape_unused(ctx: AnalysisContext) -> list[Finding]:
    """Find returned values, tuple elements and record fields no production caller reads."""
    judge = _Judge(ctx)
    return [finding for subject in _Candidates(ctx) for finding in judge.findings(subject)]
