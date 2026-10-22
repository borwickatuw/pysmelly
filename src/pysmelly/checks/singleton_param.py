"""singleton-collection-param: a collection parameter that only ever holds one element.

It reads the caller-value index (``ctx.caller_values``, Phase 25-1) for the
functions lone-dissenter judges (``caller_split._subjects``: every caller
visible to the index, a method only by a unique name and calls tied to its
class), and reports a parameter whose collection type is a generality no
production caller uses. Two shapes:

**Wrapped by every caller.** Every production call site passes the parameter
as a one-element list, tuple or set display (``[x]``, ``(x,)``, ``{x}``).
Each caller builds a collection to hand over one value; the loop or
membership test inside serves a case production never sends. Test-role
callers do not decide; those that pass two elements or more are cited as
the only ones using the generality.

**Read only as its first element.** The body reads the parameter only as
``p[0]``: no other load, no rebinding. The caller hands over a whole
collection and the function uses its head, so the signature asks for more
than it needs and hides which element matters.

**Which parameters.** One the signature declares a collection: annotated
``list``, ``set``, ``frozenset``, ``Sequence``, ``Iterable`` and the like,
or ``tuple`` of any length (``tuple[T, ...]`` or bare). A fixed-shape
``tuple[A, B]`` is a record, not a collection, and a mapping's ``p[0]`` is a
key lookup, so neither is judged. An unannotated parameter is judged by its
name: a plural (ending in ``s``, but not ``ss``, ``us`` or ``is``). An
annotation that is not a collection overrides a plural name.

Measured on real projects before it was built (DECISIONS.md, Phase 25-6):
the wrapped shape in three of them, pysmelly itself among them, and the
first-element shape in a fourth. The message ends with the one condition
under which to dismiss it: code outside the analyzed tree calls the
function with a real collection.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pysmelly.checks.caller_decided import _explicit_production, _sites
from pysmelly.checks.caller_split import _Subject, _subjects
from pysmelly.checks.callers import PassedValue, param_subjects
from pysmelly.checks.helpers import annotation_arms, base_class_name, is_bare_name, is_ellipsis
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

# Annotation names that declare a variable-length collection.
_COLLECTIONS = frozenset(
    {
        "list",
        "set",
        "frozenset",
        "List",
        "Set",
        "FrozenSet",
        "Sequence",
        "MutableSequence",
        "Iterable",
        "Collection",
        "AbstractSet",
        "MutableSet",
    }
)
_TUPLES = frozenset({"tuple", "Tuple"})

# Singular words that end in "s".
_SINGULAR_ENDINGS = ("ss", "us", "is")

# How much of a caller's argument a message quotes.
_QUOTE_LIMIT = 60

_Displays = (ast.List, ast.Tuple, ast.Set)


def _variadic_tuple(node: ast.expr) -> bool:
    """Bare ``tuple``, or ``tuple[T, ...]``; not the fixed shape ``tuple[A, B]``."""
    if not isinstance(node, ast.Subscript):
        return True
    elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return len(elements) == 2 and is_ellipsis(elements[1])


def _declares_collection(annotation: ast.expr) -> bool:
    """Whether an annotation, or any arm of a ``|`` union, is a collection type."""
    return any(_collection_arm(arm) for arm in annotation_arms(annotation))


def _collection_arm(arm: ast.expr) -> bool:
    name = base_class_name(arm)
    if name == "Optional" and isinstance(arm, ast.Subscript):
        return _declares_collection(arm.slice)
    if name in _TUPLES:
        return _variadic_tuple(arm)
    return name in _COLLECTIONS


def _is_collection_param(arg: ast.arg) -> bool:
    """Whether the signature declares ``arg`` a collection (module docstring)."""
    if arg.annotation is not None:
        return _declares_collection(arg.annotation)
    name = arg.arg
    return name.endswith("s") and not name.endswith(_SINGULAR_ENDINGS)


def _one_element(argument: ast.expr | None) -> bool:
    """Whether a site's argument is a one-element list, tuple or set display."""
    return (
        isinstance(argument, _Displays)
        and len(argument.elts) == 1
        and not isinstance(argument.elts[0], ast.Starred)
    )


def _several(value: PassedValue) -> bool:
    """Whether a site passes a display of two elements or more."""
    argument = value.argument
    return isinstance(argument, _Displays) and len(argument.elts) > 1


def _first_element_reads(statements: list[ast.stmt], name: str) -> list[int] | None:
    """The lines reading ``name[0]``, when the body uses ``name`` no other way; else None.

    Every mention counts, nested scopes included: a store, a ``del`` or any
    other load means the body needs more than the head.
    """
    parents: dict[int, ast.AST] = {}
    mentions: list[ast.Name] = []
    for stmt in statements:
        for node in ast.walk(stmt):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
            if is_bare_name(node, name):
                mentions.append(node)
    for node in mentions:
        parent = parents.get(id(node))
        if not (
            isinstance(node.ctx, ast.Load)
            and isinstance(parent, ast.Subscript)
            and parent.value is node
            and isinstance(parent.slice, ast.Constant)
            and parent.slice.value == 0
        ):
            return None
    return sorted({node.lineno for node in mentions}) or None


def _quoted(value: PassedValue, argument: ast.expr) -> str:
    """``a.py:3 `[x]``` — a site and the argument it passes, shortened."""
    text = ast.unparse(argument)
    if len(text) > _QUOTE_LIMIT:
        text = text[: _QUOTE_LIMIT - 3] + "..."
    return f"{value.file}:{value.line} `{text}`"


@dataclass(frozen=True)
class _Singleton:
    """One parameter found: what each production site passes, and how it is a singleton.

    ``wrapped``: every site passes a one-element display. ``head_reads``:
    the lines of the body's ``p[0]`` reads, None when it reads ``p`` some
    other way. At least one of the two holds.
    """

    name: str
    sites: list[tuple[PassedValue, ast.expr]]
    wrapped: bool
    head_reads: list[int] | None
    several_in_tests: list[PassedValue]

    @property
    def quoted(self) -> str:
        return ", ".join(_quoted(value, argument) for value, argument in self.sites)


def _singletons(subject: _Subject) -> list[_Singleton]:
    """The judged function's collection parameters that only ever carry one element."""
    function = subject.function
    args = function.node.args
    declared = {arg.arg: arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    found = []
    for param in subject.entry.params:
        if not _is_collection_param(declared[param.name]):
            continue
        sites = _explicit_production(param)
        if sites is None:
            continue
        wrapped = all(_one_element(argument) for _value, argument in sites)
        head_reads = _first_element_reads(function.statements, param.name)
        if not wrapped and head_reads is None:
            continue
        several = [value for value in subject.test_values(param) if _several(value)]
        found.append(_Singleton(param.name, sites, wrapped, head_reads, several))
    return found


def _lines_text(lines: list[int]) -> str:
    return ("line " if len(lines) == 1 else "lines ") + ", ".join(str(n) for n in lines)


def _message(name: str, found: _Singleton) -> str:
    count = len(found.sites)
    pname = found.name
    if found.wrapped:
        message = (
            f"{name}(): each of its {count} production caller(s) passes '{pname}' as a "
            f"one-element collection ({found.quoted})"
        )
        if found.head_reads is not None:
            message += f", and the body reads only {pname}[0] ({_lines_text(found.head_reads)})"
        message += "."
        if found.several_in_tests:
            message += (
                f" Only tests pass more than one element, at {_sites(found.several_in_tests)}."
            )
        return message
    return (
        f"{name}(): the body reads '{pname}' only as {pname}[0] "
        f"({_lines_text(found.head_reads or [])}), yet its {count} production caller(s) "
        f"hand over a whole collection ({found.quoted})"
    )


@check(
    "singleton-collection-param",
    severity=Severity.LOW,
    description="A collection parameter every caller wraps one value in, or read only as p[0]",
    advice=Advice(
        fix=(
            "Take the one element as the parameter: callers that wrap a value pass it bare,"
            " callers that pass a collection index it, and the body builds any collection it"
            " needs."
        ),
        dismiss=(
            "Dismiss only if the function is a genuine public boundary that code outside the"
            " analyzed tree calls with a real collection."
        ),
    ),
)
def check_singleton_collection_param(ctx: AnalysisContext) -> list[Finding]:
    """Find collection parameters that only ever carry one element (module docstring)."""
    findings = []
    for subject in _subjects(ctx):
        entry = subject.entry
        for found in _singletons(subject):
            findings.append(
                Finding(
                    file=str(entry.file),
                    line=entry.line,
                    check="singleton-collection-param",
                    message=_message(entry.name, found),
                    severity=Severity.LOW,
                    subjects=param_subjects(entry, [found.name]),
                )
            )
    return findings
