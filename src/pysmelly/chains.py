"""Finding chains: the findings one edit resolves, printed once as one work item.

One vestigial parameter surfaces as several findings: unused-defaults on
each layer that threads it, pass-through-params on each layer that only
forwards it, constant-args where every caller passes one literal. Deleting
one dead function resolves the findings inside everything its cascade
frees. Each of those is one edit, so the output prints them once, in the
convergence section, as one work item listing every touchpoint
(``output.format_text``). Every finding still counts on its own: the
totals and ``--summary`` read findings, never chains.

:func:`chain_findings` runs after suppression and filtering, so a chain
only holds findings that are reported, and sets ``Finding.chain`` on each
member. A chain needs two findings; a finding belongs to one chain at most.

**Cascade chains** come first. A dead-code finding's root is the unit its
subject (the dead function) is in ``ctx.unit_graph``, and removing it removes that unit and
every unit :func:`cascade.removal_cascade` frees. A finding is resolved by
that removal when its anchor and each of its sites lie in a top-level
statement whose units are all removed, so a dead-code finding inside
another's removal shares its chain.

**Parameter chains** are built from the findings left. A finding's nodes
are the parameters among its ``subjects`` (a ``registry.Subject`` with a
``parameter_of``), vestigial or not. Findings that name the
same parameter join, and so do the findings on two parameters when the
first one's function is a conduit for it (:func:`_forwards`): the
parameter is threaded from one layer to the next, and changing it is one
edit across the layers.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from pysmelly.checks.callers import DEAD_CODE, CallerValues
from pysmelly.checks.cascade import removal_cascade
from pysmelly.checks.reachability import Unit, UnitGraph
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, function_scope

# How many functions or removed definitions a chain's title names before
# counting the rest.
MAX_TITLE_LISTED = 10


@dataclass(frozen=True, eq=False)
class Chain:
    """One work item: the findings that carry it are its touchpoints.

    Compared by identity, so two chains are never merged by their titles.
    """

    title: str


class _Groups:
    """Union-find over finding indices."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, index: int) -> int:
        parent = self._parent.setdefault(index, index)
        if parent != index:
            parent = self._parent[index] = self.find(parent)
        return parent

    def union(self, first: int, second: int) -> None:
        self._parent[self.find(first)] = self.find(second)

    def components(self) -> list[list[int]]:
        """Every group of two or more indices, each sorted, ordered by its first."""
        by_root: dict[int, list[int]] = defaultdict(list)
        for index in self._parent:
            by_root[self.find(index)].append(index)
        return sorted(sorted(group) for group in by_root.values() if len(group) > 1)


def chain_findings(ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    """``findings`` in the same order, each member of a chain carrying it in ``chain``."""
    chained = list(findings)
    cascades = _cascade_chains(ctx, findings)
    taken = {index for group, _title in cascades for index in group}
    for group, title in [*cascades, *_parameter_chains(ctx, findings, taken)]:
        chain = Chain(title)
        for index in group:
            chained[index] = replace(findings[index], chain=chain)
    return chained


def _listing(labels: list[str]) -> str:
    listed = ", ".join(labels[:MAX_TITLE_LISTED])
    if len(labels) > MAX_TITLE_LISTED:
        listed += f" (+{len(labels) - MAX_TITLE_LISTED} more)"
    return listed


# --- cascade chains ---


class _StatementUnits:
    """The units of the top-level statement enclosing a line, per file."""

    def __init__(self, graph: UnitGraph) -> None:
        self._spans: dict[str, list[tuple[int, int, frozenset[Unit]]]] = {
            str(path): [
                (
                    min([stmt.lineno, *(d.lineno for d in getattr(stmt, "decorator_list", []))]),
                    stmt.end_lineno or stmt.lineno,
                    frozenset(units),
                )
                for stmt, units in statements
            ]
            for path, statements in graph.statements.items()
        }

    def at(self, file: str, line: int) -> frozenset[Unit]:
        """The units of the statement holding ``file:line``; empty outside any."""
        for first, last, units in self._spans.get(file, ()):
            if first <= line <= last:
                return units
        return frozenset()


@dataclass(frozen=True)
class _Removal:
    """What deleting one dead-code finding's function removes: ``root`` and what it frees."""

    root: Unit
    removed: frozenset[Unit]


def _removals(ctx: AnalysisContext, findings: list[Finding]) -> dict[int, _Removal]:
    """Each dead-code finding's removal, by finding index.

    The unit is the one ``check_dead_code`` cascades from: its subject's
    file and name. A function defined inside a top-level ``if``/``try``
    block is part of its module's top-level code, not a unit of its own, so
    it roots no chain.
    """
    removals = {}
    for index, finding in enumerate(findings):
        if finding.check != DEAD_CODE:
            continue
        (subject,) = finding.subjects
        unit = (Path(subject.file), subject.name)
        if unit in ctx.unit_graph.members:
            freed = removal_cascade(ctx, unit).freed
            removals[index] = _Removal(
                unit, frozenset({unit, *((member.file, member.name) for member in freed)})
            )
    return removals


def _cascade_chains(ctx: AnalysisContext, findings: list[Finding]) -> list[tuple[list[int], str]]:
    if not any(finding.check == DEAD_CODE for finding in findings):
        return []
    statements = _StatementUnits(ctx.unit_graph)
    removals = _removals(ctx, findings)
    groups = _Groups()
    for index, finding in enumerate(findings):
        locations = [(finding.file, finding.line), *((s.file, s.line) for s in finding.sites)]
        held = [statements.at(file, line) for file, line in locations]
        if not all(held):
            continue
        for root, removal in removals.items():
            if root != index and all(units <= removal.removed for units in held):
                groups.union(root, index)
    chains = []
    for group in groups.components():
        members = [removals[index] for index in group if index in removals]
        # The removals no other removal in the chain already makes; in a
        # cycle (two dead functions calling each other) every one.
        primary = [
            removal
            for removal in members
            if not any(other is not removal and removal.root in other.removed for other in members)
        ] or members
        chains.append((group, _cascade_title(ctx.unit_graph, primary)))
    return chains


def _cascade_title(graph: UnitGraph, removals: list[_Removal]) -> str:
    """``delete main() (app.py:4) and the 2 definition(s) its removal frees: a() (lib.py:1)``."""

    def labels(units: Iterable[Unit]) -> list[str]:
        members = sorted(
            (graph.members[unit] for unit in units),
            key=lambda member: (str(member.file), member.line, member.name),
        )
        return [f"{member.title()} ({member.file}:{member.line})" for member in members]

    roots = {removal.root for removal in removals}
    freed = frozenset().union(*(removal.removed for removal in removals)) - roots
    title = f"delete {_listing(labels(roots))}"
    if freed:
        title += f" and the {len(freed)} definition(s) its removal frees: {_listing(labels(freed))}"
    return title


# --- parameter chains ---

# A function, as its file and ``Subject.scope``.
_Function = tuple[str, tuple[int, int]]


@dataclass(frozen=True)
class _Node:
    """One parameter of one function, however the findings that name it judge it."""

    file: str
    scope: tuple[int, int]
    name: str

    @property
    def owner(self) -> _Function:
        return (self.file, self.scope)


def _nodes(finding: Finding) -> Iterator[_Node]:
    """The parameters among ``finding``'s subjects."""
    for subject in finding.subjects:
        if subject.parameter_of is None:
            continue
        if subject.scope is None:
            msg = f"{finding.check}: parameter {subject.name!r} of {subject.parameter_of} unscoped"
            raise ValueError(msg)
        yield _Node(subject.file, subject.scope, subject.name)


def _parameter_chains(
    ctx: AnalysisContext, findings: list[Finding], taken: set[int]
) -> list[tuple[list[int], str]]:
    holders: dict[_Node, list[int]] = defaultdict(list)
    for index, finding in enumerate(findings):
        if index not in taken:
            for node in _nodes(finding):
                holders[node].append(index)
    if not holders:
        return []
    groups = _Groups()
    for indices in holders.values():
        for index in indices[1:]:
            groups.union(indices[0], index)
    scopes = _Scopes(ctx)
    forwards = _forwards(scopes, holders)
    for caller, callee in forwards:
        groups.union(holders[caller][0], holders[callee][0])
    chains = []
    for group in groups.components():
        nodes = {node for index in group for node in _nodes(findings[index])}
        labels = _function_labels(scopes, _forward_order(nodes, forwards))
        chains.append(
            (group, f"parameter chain over {len(labels)} function(s): {_listing(labels)}")
        )
    return chains


def _function_labels(scopes: _Scopes, order: list[_Node]) -> list[str]:
    """``inner(mode, size) pkg/layers.py:9`` per function, in the order its first node comes.

    Each function is named once, with its parameters in ``order``, at its
    ``def`` line.
    """
    names: dict[_Function, list[str]] = {}
    for node in order:
        names.setdefault(node.owner, []).append(node.name)
    labels = []
    for owner, params in names.items():
        entry = scopes.entry(owner)
        labels.append(f"{entry.name}({', '.join(params)}) {owner[0]}:{entry.line}")
    return labels


def _forwards(scopes: _Scopes, holders: dict[_Node, list[int]]) -> set[tuple[_Node, _Node]]:
    """Each ``(caller, callee)`` pair of flagged parameters the caller threads to the callee.

    The caller's function passes its parameter, as a bare name, for the
    callee at a production call, and every read of it in that function is
    such a pass to a flagged parameter. The function is then a conduit:
    changing the parameter is changing the layers it is threaded to. One
    that also reads it for itself, or hands it to a parameter nothing
    flags (``git_failure(cmd, timeout)`` beside ``run_in(timeout=timeout)``),
    has a use of its own, and its findings stand apart.

    A function's calls are the ones that resolve to it. A method's calls
    are matched by name alone (``build_caller_values``), so for a method
    only ``self.m(...)`` or ``cls.m(...)`` from a method of the same class
    counts: ``body.read(size)`` on a library object is not a call to an
    analyzed ``read``.
    """
    flagged: dict[_Function, dict[str, _Node]] = defaultdict(dict)
    for node in holders:
        flagged[node.owner][node.name] = node
    # Per flagged caller parameter, each call (by identity) it is passed at
    # and the flagged parameter that pass fills.
    passes: dict[_Node, set[tuple[int, _Node]]] = defaultdict(set)
    owners: dict[_Node, CallerValues] = {}
    for key, by_name in flagged.items():
        entry = scopes.entry(key)
        for param, callee in by_name.items():
            for call, name in entry.names_passed(param):
                owner = scopes.binding(call["file"], call["line"], name)
                if owner is None or (entry.is_method and not _own_method_call(call, owner, entry)):
                    continue
                caller = flagged.get(_owner_of(owner), {}).get(name)
                if caller is not None and caller != callee:
                    passes[caller].add((id(call["node"]), callee))
                    owners[caller] = owner
    return {
        (caller, callee)
        for caller, filled in passes.items()
        if len(filled) == _reads(owners[caller], caller.name)
        for _call, callee in filled
    }


def _reads(entry: CallerValues, name: str) -> int:
    """How many times ``entry``'s function reads ``name``."""
    return sum(
        1
        for node in ast.walk(entry.node)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    )


def _own_method_call(call: dict, caller: CallerValues, callee: CallerValues) -> bool:
    """Whether ``call`` is ``self.m(...)``/``cls.m(...)`` in a method of ``callee``'s class."""
    func = call["node"].func
    return (
        caller.owner is callee.owner
        and isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in {"self", "cls"}
    )


class _Scopes:
    """Every function ``ctx.caller_values`` indexes, by file, innermost first."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self._by_file: dict[str, list[CallerValues]] = {
            str(path): sorted(entries, key=_span_size)
            for path, entries in ctx.caller_values.by_file.items()
        }
        self._by_scope = {
            _owner_of(entry): entry
            for entries in ctx.caller_values.by_file.values()
            for entry in entries
        }

    def entry(self, function: _Function) -> CallerValues:
        """The function at ``function``; a parameter subject always names one."""
        return self._by_scope[function]

    def binding(self, file: str, line: int, name: str) -> CallerValues | None:
        """The innermost function enclosing ``file:line`` that has a parameter ``name``."""
        for entry in self._by_file.get(file, ()):
            if entry.encloses(file, line) and any(param.name == name for param in entry.params):
                return entry
        return None


def _owner_of(entry: CallerValues) -> _Function:
    return (str(entry.file), function_scope(entry.node))


def _span_size(entry: CallerValues) -> int:
    return (entry.node.end_lineno or entry.line) - entry.line


def _forward_order(nodes: set[_Node], forwards: set[tuple[_Node, _Node]]) -> list[_Node]:
    """``nodes`` callers first: each after every node that forwards into it.

    Ties, and the members of a forwarding cycle, go by file, line and name.
    """
    inbound = {node: 0 for node in nodes}
    edges = [(caller, callee) for caller, callee in forwards if caller in nodes]
    for _caller, callee in edges:
        inbound[callee] += 1
    order: list[_Node] = []
    ready = sorted((node for node, count in inbound.items() if not count), key=_node_key)
    while ready:
        node = ready.pop(0)
        order.append(node)
        for caller, callee in edges:
            if caller == node:
                inbound[callee] -= 1
                if not inbound[callee]:
                    ready = sorted([*ready, callee], key=_node_key)
    return order + sorted(nodes - set(order), key=_node_key)


def _node_key(node: _Node) -> tuple[str, int, str]:
    return (node.file, node.scope[0], node.name)
