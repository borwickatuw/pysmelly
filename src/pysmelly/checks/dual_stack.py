"""Git history check — dual-stack-dependency: two libraries doing one job.

A catalog pair (``catalog.toml``'s ``pairs``, plus the project's own) names two
libraries that substitute for each other: ``requests``/``httpx``,
``os.path``/``pathlib``. When production code uses both, one of them is
usually the stack a migration left behind. Two facts separate that from a
migration just starting: the left-behind side is the minority of the use
sites, and it was there first.

The use sites are an AST question and the "there first" is a blame question,
so this is a git-history check: the AST run carries no git root (DECISIONS.md,
"Blame-age checks"). Outside a git root it does not run; DECISIONS.md
("dual-stack-dependency (Phase 33-6)") records why the share alone is not
reported.

A use site is an expression that resolves, through the module's own import
statements, to a name under one side's modules: ``os.path.join(...)`` after
``import os``, ``Path(...)`` after ``from pathlib import Path``, ``@dataclass``.
Each maximal ``a.b.c`` chain counts once. Uses in test-role files do not
count. A file's age on a side is the oldest of its import statements that
bind a name the side's uses go through, blamed at HEAD
(``LineHistory.line_ages``); a side's age is the median of its files' ages.
Like the other blame-age checks it reads no commits, so it ignores
``pysmelly: reviewed`` markers.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from statistics import median_low

from pysmelly.catalog import CatalogPair, PairSide, shipped_catalog
from pysmelly.checks.helpers import source_trees
from pysmelly.context import AnalysisContext
from pysmelly.git_history import LineHistory
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)

# The minority side is reported only below this share of the pair's
# production use sites. At or above it, both libraries are in real use.
MINORITY_SHARE = 0.30

# The age of an import that HEAD does not have yet: newer than any commit.
UNCOMMITTED = datetime.max.replace(tzinfo=UTC)

CHECK_NAME = "dual-stack-dependency"


@dataclass(frozen=True)
class ResolvedUse:
    """One use site: the dotted name it resolves to and the import line it goes through."""

    dotted: str
    import_line: int


@dataclass(frozen=True)
class FileUse:
    """One file's use of one side of a pair."""

    file: Path
    import_lines: tuple[int, ...]
    uses: int


@dataclass(frozen=True)
class DualStack:
    """A pair whose two sides production code both uses, the minority under the share bar."""

    pair: CatalogPair
    minority: PairSide
    majority: PairSide
    minority_files: tuple[FileUse, ...]
    majority_files: tuple[FileUse, ...]

    @property
    def majority_uses(self) -> int:
        return sum(use.uses for use in self.majority_files)


def _import_bindings(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """Each name the module's absolute imports bind: the dotted path and the import's line.

    Imports anywhere in the module count, in source order, a later binding of
    a name winning. Relative and star imports never name a library.
    """
    statements = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))),
        key=lambda node: (node.lineno, node.col_offset),
    )
    bindings: dict[str, tuple[str, int]] = {}
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = (
                        f"{node.module}.{alias.name}",
                        node.lineno,
                    )
        else:
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = (alias.name, node.lineno)
                else:
                    root = alias.name.partition(".")[0]
                    bindings[root] = (root, node.lineno)
    return bindings


def _resolved_uses(tree: ast.Module) -> list[ResolvedUse]:
    """Every maximal ``name`` or ``name.attr...`` load whose root an import binds."""
    bindings = _import_bindings(tree)
    if not bindings:
        return []
    inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    uses = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, (ast.Name, ast.Attribute))
            or id(node) in inner
            or not isinstance(node.ctx, ast.Load)
        ):
            continue
        attrs: list[str] = []
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            attrs.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in bindings:
            dotted, line = bindings[root.id]
            uses.append(ResolvedUse(".".join([dotted, *reversed(attrs)]), line))
    return uses


def _side_uses(
    uses: list[ResolvedUse], pair: CatalogPair
) -> tuple[tuple[frozenset[int], int], tuple[frozenset[int], int]]:
    """Per side of ``pair``: the import lines its uses go through, and how many uses."""
    per_side: list[tuple[frozenset[int], int]] = []
    for side in pair.sides:
        lines = [use.import_line for use in uses if side.covers(use.dotted)]
        per_side.append((frozenset(lines), len(lines)))
    return per_side[0], per_side[1]


def _find_dual_stacks(ctx: AnalysisContext, pairs: tuple[CatalogPair, ...]) -> list[DualStack]:
    """The pairs production uses both sides of, with the minority under ``MINORITY_SHARE``.

    The age condition is the check's; this is the share alone.
    """
    uses_by_file = {filepath: _resolved_uses(tree) for filepath, tree in source_trees(ctx)}
    stacks = []
    for pair in pairs:
        files: tuple[list[FileUse], list[FileUse]] = ([], [])
        for filepath in sorted(uses_by_file):
            for found, (lines, count) in zip(
                files, _side_uses(uses_by_file[filepath], pair), strict=True
            ):
                if count:
                    found.append(FileUse(filepath, tuple(sorted(lines)), count))
        totals = [sum(use.uses for use in found) for found in files]
        if not all(totals) or totals[0] == totals[1]:
            continue
        low = 0 if totals[0] < totals[1] else 1
        if totals[low] / sum(totals) >= MINORITY_SHARE:
            continue
        stacks.append(
            DualStack(
                pair=pair,
                minority=pair.sides[low],
                majority=pair.sides[1 - low],
                minority_files=tuple(files[low]),
                majority_files=tuple(files[1 - low]),
            )
        )
    return stacks


def _head_import_lines(
    lines: LineHistory, stacks: list[DualStack]
) -> dict[tuple[str, str, str], tuple[int, ...]]:
    """Per (path, pair, side label), the side's import lines in the file at HEAD.

    A file unchanged since HEAD has the analyzed tree's lines. A changed one
    (uncommitted edits are few) is re-read at HEAD and its uses found again
    there. A file HEAD lacks, or whose HEAD text uses the side through no
    import, has no entry: its use of the side is not committed yet.
    """
    committed = lines.head_paths()
    changed = lines.head_changed_paths()
    head_uses: dict[str, list[ResolvedUse]] = {}
    for stack in stacks:
        for use in (*stack.minority_files, *stack.majority_files):
            path = str(use.file)
            if path in changed and path not in head_uses:
                tree = lines.head_tree(path)
                head_uses[path] = _resolved_uses(tree) if tree is not None else []
    found: dict[tuple[str, str, str], tuple[int, ...]] = {}
    for stack in stacks:
        for side, uses in (
            (stack.minority, stack.minority_files),
            (stack.majority, stack.majority_files),
        ):
            index = stack.pair.sides.index(side)
            for use in uses:
                path = str(use.file)
                if path not in committed:
                    head_lines: tuple[int, ...] = ()
                elif path in changed:
                    head_lines = tuple(sorted(_side_uses(head_uses[path], stack.pair)[index][0]))
                else:
                    head_lines = use.import_lines
                if head_lines:
                    found[(path, stack.pair.name, side.label)] = head_lines
    return found


def _file_ages(lines: LineHistory, stacks: list[DualStack]) -> dict[tuple[str, str, str], datetime]:
    """Per (path, pair, side label), when the file took the side up: its oldest import's blame.

    One ``git blame`` per file, over every import line any pair asks about, the
    files blamed concurrently.
    A file whose use is not committed yet is ``UNCOMMITTED``.
    """
    import_lines = _head_import_lines(lines, stacks)
    wanted: dict[str, set[int]] = {}
    for (path, _pair, _side), head_lines in import_lines.items():
        wanted.setdefault(path, set()).update(head_lines)
    blamed = lines.files_line_ages(wanted)
    ages: dict[tuple[str, str, str], datetime] = {}
    for stack in stacks:
        for side, uses in (
            (stack.minority, stack.minority_files),
            (stack.majority, stack.majority_files),
        ):
            for use in uses:
                key = (str(use.file), stack.pair.name, side.label)
                head_lines = import_lines.get(key, ())
                ages[key] = (
                    min(blamed[key[0]][line] for line in head_lines) if head_lines else UNCOMMITTED
                )
    return ages


def _side_age(
    ages: dict[tuple[str, str, str], datetime],
    stack: DualStack,
    side: PairSide,
    uses: tuple[FileUse, ...],
) -> datetime:
    """The median of the files' ages on ``side``: when the side typically arrived."""
    return median_low(ages[(str(use.file), stack.pair.name, side.label)] for use in uses)


def _date(when: datetime) -> str:
    return "uncommitted" if when == UNCOMMITTED else f"{when:%Y-%m-%d}"


def _dual_stack_message(
    stack: DualStack,
    ages: dict[tuple[str, str, str], datetime],
    majority_age: datetime,
    indices: tuple[int, ...],
) -> str | None:
    """The finding over the minority files at ``indices``.

    None when those files no longer make the minority the older side: an
    ignored file is a use the author chose, so it leaves the comparison.
    """
    kept = tuple(stack.minority_files[index] for index in indices)
    minority_age = _side_age(ages, stack, stack.minority, kept)
    if not minority_age < majority_age:
        return None
    uses = sum(use.uses for use in kept)
    share = uses / (uses + stack.majority_uses)
    sites = ", ".join(f"{use.file}:{use.import_lines[0]}" for use in kept)
    minority, majority = stack.minority.label, stack.majority.label
    return (
        f"{minority} and {majority} are both used for {stack.pair.description} in production. "
        f"{minority} has {uses} use site(s) in {len(kept)} file(s) ({sites}), "
        f"{share:.0%} of the pair's against {stack.majority_uses} for {majority}, "
        f"and is the older stack (imports from {_date(minority_age)} by median, "
        f"{majority}'s from {_date(majority_age)})"
    )


@check(
    CHECK_NAME,
    # As stalled-migration: a migration git shows was left behind.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Both libraries of a catalog pair in production; the older one under 30% of uses",
    advice=Advice(
        fix=(
            "Finish the move: rewrite the minority library's sites listed with the majority"
            " library, then drop its imports."
        ),
        dismiss=(
            "Dismiss only for a site that needs something the majority library cannot do,"
            " with a reasoned ignore on that import."
        ),
    ),
)
def check_dual_stack_dependency(ctx: AnalysisContext) -> list[Finding]:
    """Report the minority side of a pair when it is also the older one.

    One finding per pair, listing the minority's files, each sited on its
    first import of the side; an ignore there removes that file.
    """
    history = ctx.git_history
    if history is None:
        return []
    stacks = _find_dual_stacks(ctx, (*shipped_catalog().pairs, *ctx.user_catalog.pairs))
    if not stacks:
        return []
    ages = _file_ages(history.line_history, stacks)
    findings = []
    for stack in stacks:
        majority_age = _side_age(ages, stack, stack.majority, stack.majority_files)
        render = partial(_dual_stack_message, stack, ages, majority_age)
        if render(tuple(range(len(stack.minority_files)))) is None:
            continue
        sites = [Site(str(use.file), use.import_lines[0]) for use in stack.minority_files]
        findings.append(multi_site_finding(CHECK_NAME, Severity.MEDIUM, sites, render))
    return findings
