"""The dead-code cascade: what else dies when a dead definition is removed.

dead-code reports a function nothing calls. Deleting it often leaves its
private helpers, the constants only they read and the classes only they
build with no production referrer either, and each of those is a separate
finding later, or none. :func:`removal_cascade` names that closure up front,
so one finding is one work item.

**The graph.** The cascade reads ``ctx.unit_graph``, the graph
unreachable-from-entry-points walks (``checks/reachability.py``): a unit is a
top-level function, class or simple assignment of a production file (methods
and nested functions fold into their top-level unit), or a module's remaining
top-level code. Its edges are every reference the reachability walk follows:
resolved calls, names and attribute chains, dotted strings, ``getattr``
literals, imports, and a module's own top-level code, which reaches what
``__all__``, ``globals()`` and registering decorators expose. Module constants
are assignment units, so they are covered by the same edges.

**The rule.** Removing a unit *frees* a definition when every path of
production references into it starts at the removed unit or passes only
through other freed definitions. Concretely:

1. The candidates are the definitions reachable forward from the removed
   unit through other definitions. A module's top-level code is never a
   candidate (deleting a function does not delete its module), and neither is
   an entry-point root, which the program runs whoever refers to it.
2. A candidate is kept alive when a production unit other than the removed
   one and the candidates refers to it, and so is every candidate a kept one
   refers to.
3. The rest are freed.

Both walks visit each unit once, so a cycle (two helpers calling each other)
terminates and is freed whole when only the removed unit reaches it. A
test-role file's references are never edges (``UnitGraph.test_edges``), so a
test calling a helper does not keep it alive, as in dead-code and
reachability. A helper that live code also uses is kept.

**Unreachable clusters.** An unreachable-from-entry-points cluster is already
its own cascade: a definition everything outside the cluster has stopped
referring to is unreached too, and it is joined to the cluster by the edge
that refers to it. So removing any member frees only other members, which
the finding already lists, and only dead-code prints a cascade
(DECISIONS.md, Phase 26-3).

**Reuse.** Phase 29's finding chains collapse findings that share a cascade;
they call :func:`removal_cascade` with the unit a finding removes and read
:attr:`Cascade.freed`, rather than parsing messages. It takes one unit
because every caller removes one (Phase 25-6, singleton-collection-param).
"""

from __future__ import annotations

from dataclasses import dataclass

from pysmelly.checks.reachability import Member, Unit
from pysmelly.context import AnalysisContext

# How many freed definitions a cascade lists before counting the rest.
MAX_CASCADE_LISTED = 10


@dataclass(frozen=True)
class Cascade:
    """What removing ``removed`` frees: ``freed``, sorted by file, line and name."""

    removed: Unit
    freed: tuple[Member, ...]

    def describe(self, subject: str, reported: frozenset[Unit]) -> str | None:
        """``Removing <subject> also frees 2: _a() (lib.py:5), LIMIT (lib.py:1)``.

        None when nothing is freed. A freed definition in ``reported`` is
        marked, since it is also the subject of a finding of its own; the
        listing stops at :data:`MAX_CASCADE_LISTED` and counts the rest.
        """
        if not self.freed:
            return None
        listed = self.freed[:MAX_CASCADE_LISTED]
        listing = ", ".join(
            f"{member.title()} ({member.file}:{member.line}"
            f"{', reported separately' if (member.file, member.name) in reported else ''})"
            for member in listed
        )
        if len(self.freed) > len(listed):
            listing += f" (+{len(self.freed) - len(listed)} more)"
        return f"Removing {subject} also frees {len(self.freed)}: {listing}"


def removal_cascade(ctx: AnalysisContext, removed: Unit) -> Cascade:
    """The definitions that lose their last production referrer when ``removed`` goes.

    ``removed`` names a unit of ``ctx.unit_graph`` (``(file, name)`` for a
    top-level definition, ``(file, None)`` for a module's top-level code).
    See the module docstring for the rule.
    """
    graph = ctx.unit_graph
    gone = frozenset({removed})
    candidates: set[Unit] = set()
    pending = list(gone)
    while pending:
        for target in graph.edges.get(pending.pop(), ()):
            if (
                target in gone
                or target in candidates
                or target not in graph.members
                or target in graph.roots
            ):
                continue
            candidates.add(target)
            pending.append(target)
    alive = {
        unit for unit in candidates if graph.referrers.get(unit, frozenset()) - gone - candidates
    }
    pending = list(alive)
    while pending:
        for target in graph.edges.get(pending.pop(), ()):
            if target in candidates and target not in alive:
                alive.add(target)
                pending.append(target)
    freed = sorted(
        (graph.members[unit] for unit in candidates - alive),
        key=lambda member: (str(member.file), member.line, member.name),
    )
    return Cascade(removed, tuple(freed))
