"""Optional record fields whose readers mostly turn None into an empty value.

``coalesced-optional-field``: a dataclass, pydantic or attrs field typed
``X | None`` (``Optional[X]``, ``Union[X, None]``) whose production reads
are mostly ``x.f or {}`` (or ``[]``, ``()``, ``dict()``, ``list()``,
``set()``...). Every reader re-decides that None means empty; declaring the
empty value as the default says it once. A ``Block.attrs`` is the
founding case: ``Provenance.attrs`` beside it defaults to ``{}``, while
``Block.attrs`` defaults to None and its readers coalesce it.

Reads are found by attribute name and tied to a record through
``records.attribute_reads``: a read whose receiver ties to another class
(``provenance.attrs`` beside ``block.attrs``) is not the field's, and a read
nothing ties is counted as the field's, by name. NamedTuple and TypedDict
fields are left out: a NamedTuple default is shared by every instance, and
a TypedDict has no defaults.

**Mostly.** At least ``MIN_COALESCING_READS`` coalescing reads, and more
than half of all the field's reads. Three is the smallest count that is a
habit rather than a precaution in one or two places; over half is the
item's own word, "mostly". A field whose readers mostly use it as it is
(``if x.f is None``, passing it on) keeps None for a reason.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from pysmelly.checks.helpers import annotation_arms, referenced_name
from pysmelly.checks.records import (
    RecordClass,
    RecordIndex,
    RecordKind,
    attribute_reads,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

MIN_COALESCING_READS = 3

_KINDS = frozenset({RecordKind.DATACLASS, RecordKind.PYDANTIC, RecordKind.ATTRS})
_EMPTY_CALLS = frozenset({"dict", "list", "tuple", "set", "frozenset"})
_EMPTY_DISPLAYS = {ast.Dict: "dict", ast.List: "list", ast.Tuple: "tuple"}
_OPTIONAL = frozenset({"Optional", "typing.Optional"})
_UNION = frozenset({"Union", "typing.Union"})
_SPECIFIER = {
    RecordKind.DATACLASS: "field(default_factory={})",
    RecordKind.PYDANTIC: "Field(default_factory={})",
    RecordKind.ATTRS: "attrs.field(factory={})",
}


@dataclass(frozen=True)
class OptionalField:
    """A record field whose annotation admits None."""

    record: RecordClass
    kind: RecordKind
    name: str
    line: int
    declared_type: ast.expr


def _is_none(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


def _admits_none(annotation: ast.expr) -> bool:
    """``X | None``, ``Optional[X]`` or ``Union[..., None]``, also as a string."""
    return any(_is_none(arm) or _optional_subscript(arm) for arm in annotation_arms(annotation))


def _optional_subscript(annotation: ast.expr) -> bool:
    """``Optional[X]`` or ``Union[..., None]``."""
    if not isinstance(annotation, ast.Subscript):
        return False
    name = referenced_name(annotation.value)
    if name in _OPTIONAL:
        return True
    if name in _UNION:
        members = annotation.slice
        elts = members.elts if isinstance(members, ast.Tuple) else [members]
        return any(_is_none(elt) for elt in elts)
    return False


def _optional_fields(index: RecordIndex) -> Iterator[OptionalField]:
    for kind, record in index.of_kinds(_KINDS):
        for name, item in record.declared(index.names[record.file]):
            if (
                not name.startswith("_")
                and isinstance(item, ast.AnnAssign)
                and _admits_none(item.annotation)
            ):
                yield OptionalField(record, kind, name, item.lineno, item.annotation)


def _empty_value(expr: ast.expr) -> str | None:
    """The container type an empty literal or a no-argument constructor call makes."""
    for display, kind in _EMPTY_DISPLAYS.items():
        if isinstance(expr, display) and not any(
            getattr(expr, part, None) for part in ("keys", "elts")
        ):
            return kind
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in _EMPTY_CALLS
        and not expr.args
        and not expr.keywords
    ):
        return expr.func.id
    return None


def _coalesced(node: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> str | None:
    """The empty container ``node or <empty>`` turns None into, or None for any other read."""
    parent = parents.get(node)
    if (
        isinstance(parent, ast.BoolOp)
        and isinstance(parent.op, ast.Or)
        and len(parent.values) == 2
        and parent.values[0] is node
    ):
        return _empty_value(parent.values[1])
    return None


@dataclass(frozen=True)
class _Coalescing:
    """One field's reads: how many there are, and the ones that coalesce, with the empty kind."""

    field: OptionalField
    total: int
    sites: tuple[tuple[Site, str], ...]


def _is_mostly(coalescing: int, total: int) -> bool:
    return coalescing >= MIN_COALESCING_READS and 2 * coalescing > total


def _annotation_without_none(annotation: ast.expr) -> str:
    text = ast.unparse(annotation)
    for spelling in (" | None", "None | "):
        text = text.replace(spelling, "")
    return text


def _message(found: _Coalescing, indices: tuple[int, ...]) -> str | None:
    """The finding over the coalescing sites at ``indices``; None below the bar."""
    if not _is_mostly(len(indices), found.total):
        return None
    shown = [found.sites[index] for index in indices]
    by_file: dict[str, list[int]] = defaultdict(list)
    for site, _kind in shown:
        by_file[site.file].append(site.line)
    files = sorted(by_file)
    cited = "; ".join(
        f"{path}:" + ", ".join(str(line) for line in sorted(by_file[path])) for path in files[:3]
    )
    if len(files) > 3:
        cited += f"; +{len(files) - 3} more files"
    kind = Counter(kind for _site, kind in shown).most_common(1)[0][0]
    optional = found.field
    spec = _SPECIFIER[optional.kind].format(kind)
    record = optional.record
    return (
        f"`{record.name}.{optional.name}` ({record.file}:{optional.line}) is typed "
        f"`{ast.unparse(optional.declared_type)}`, and {len(shown)} of its {found.total} reads "
        f"coalesce it to an empty {kind} with `or` ({cited}); declared without None it is "
        f"`{optional.name}: {_annotation_without_none(optional.declared_type)} = {spec}`"
    )


def find_coalesced_fields(ctx: AnalysisContext) -> list[_Coalescing]:
    """Every optional record field whose production reads mostly coalesce it."""
    index = RecordIndex(ctx)
    fields = list(_optional_fields(index))
    if not fields:
        return []
    reads = attribute_reads(ctx, index, frozenset(f.name for f in fields))
    declaring: Counter[str] = Counter(
        name
        for record in index.records
        for name, _item in record.declared(index.names[record.file])
    )
    parents: dict[Path, dict[ast.AST, ast.AST]] = {}
    found = []
    for optional in fields:
        total = 0
        sites: list[tuple[Site, str]] = []
        for read in reads:
            if read.node.attr != optional.name:
                continue
            if read.holders is not None and optional.record not in read.holders:
                continue
            total += 1
            if read.file not in parents:
                parents[read.file] = ctx.parent_map(ctx.all_trees[read.file])
            kind = _coalesced(read.node, parents[read.file])
            # An untied read of a name several records declare may be any of
            # theirs: it counts against the field, never for it.
            ambiguous = read.holders is None and declaring[optional.name] > 1
            if kind is not None and not ambiguous:
                sites.append((Site(str(read.file), read.node.lineno), kind))
        if _is_mostly(len(sites), total):
            sites.sort(key=lambda pair: (pair[0].file, pair[0].line))
            found.append(_Coalescing(optional, total, tuple(sites)))
    return found


@check(
    "coalesced-optional-field",
    severity=Severity.MEDIUM,
    description=(
        "Optional record fields (X | None) whose readers mostly coalesce them with"
        " `or {}`, `or []` and the like"
    ),
    advice=Advice(
        fix=(
            "Declare the field as the message shows, defaulting to the empty value, and"
            " delete the `or` coalescing at the reads cited."
        ),
        dismiss="Dismiss only if None means something its readers must tell apart from empty.",
    ),
)
def check_coalesced_optional_field(ctx: AnalysisContext) -> list[Finding]:
    """Flag an optional field that its readers mostly turn into an empty container.

    One finding per field, anchored on its declaration; each coalescing read
    is a site, so an ignore on one removes it, and the finding goes when the
    sites left fall below the bar.
    """
    findings = []
    for found in find_coalesced_fields(ctx):
        record = found.field.record
        findings.append(
            multi_site_finding(
                "coalesced-optional-field",
                Severity.MEDIUM,
                [site for site, _kind in found.sites],
                partial(_message, found),
                anchor=Site(str(record.file), found.field.line),
            )
        )
    return findings
