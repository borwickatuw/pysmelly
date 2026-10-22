"""Migrations left half done: the old way is still called beside its replacement.

Three checks over one "B arrived, A is still called" story:

- ``half-finished-migration`` (AST): two definitions are an old/new pair by
  name, and production code still calls both. The pair is linked by a
  version or age marker on the name (``parse``/``parse_v2``,
  ``Client``/``ClientV2``, ``fetch_legacy``/``fetch``,
  ``old_render``/``render``, ``parse``/``parse2``) or on the module
  (``client.py``/``client_v2.py``, ``legacy/views.py``/``views.py``).
- ``stalled-migration`` (git-history): the same pairs, when git shows the
  migration has stopped: the new member is 90+ days old and no commit in
  the last 90 days removed a production call of the old one. The AST run
  carries no git history (DECISIONS.md, "Blame-age checks"), so the aged
  finding is its own check, as frozen-feature-flag is fossilized-toggles'.
- ``self-deprecated`` (AST): a definition that deprecates itself
  (``warnings.warn(..., DeprecationWarning)`` as a statement of its body,
  or ``@deprecated``) that production code still calls.

Callers are resolved, not matched by name: a top-level function or class
through the imports of each calling module (``callers._resolving_calls``),
a method by its name only when no other definition shares it. Test-role
callers are counted apart and never make a pair.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from pysmelly.checks.callers import (
    _Callers,
    _callers_clause,
    _CallTarget,
    _cite_sites,
    _reaches_module,
    _resolving_calls,
    _split_callers,
)
from pysmelly.checks.helpers import (
    filepath_to_module,
    import_bindings,
    referenced_name,
    source_trees,
)
from pysmelly.checks.module_graph import ModuleGraph
from pysmelly.context import AnalysisContext
from pysmelly.git_history import Hunk, LineHistory
from pysmelly.registry import GIT_HISTORY_CATEGORY, Advice, Finding, Severity, check

_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

# How long a migration may sit with no caller of the old member removed
# before stalled-migration calls it stalled. The new member must be at least
# this old too: a replacement added last month has had no time to be adopted.
STALL_BAR = timedelta(days=90)

_DEPRECATION_CATEGORIES = frozenset({"DeprecationWarning", "PendingDeprecationWarning"})
# ``@deprecated`` as PEP 702 spells it: the typing_extensions backport and
# the stdlib's from 3.13.
_DEPRECATED_DECORATORS = frozenset({"typing_extensions.deprecated", "warnings.deprecated"})
_WARN = "warnings.warn"


@dataclass(frozen=True)
class Definition:
    """A function, class or method an old/new pair or a deprecation can name.

    ``owner`` is the enclosing class's name for a method, None at module level.
    """

    file: Path
    node: _DefNode
    owner: str | None

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def line(self) -> int:
        return self.node.lineno

    @property
    def label(self) -> str:
        """``Client.parse()`` for a method, ``parse()`` or ``Client`` otherwise."""
        if isinstance(self.node, ast.ClassDef):
            return self.name
        prefix = f"{self.owner}." if self.owner else ""
        return f"{prefix}{self.name}()"

    @property
    def kind(self) -> str:
        return "class" if isinstance(self.node, ast.ClassDef) else "function"

    def call_target(self, graph: ModuleGraph) -> _CallTarget:
        """What ``callers._resolving_calls`` resolves calls of this definition against."""
        return _CallTarget.defined_in(graph, self.name, self.file)

    def contains(self, filepath: str, line: int) -> bool:
        """Whether ``filepath:line`` falls inside this definition."""
        end = self.node.end_lineno if self.node.end_lineno is not None else self.node.lineno
        return filepath == str(self.file) and self.node.lineno <= line <= end


class NameLink(NamedTuple):
    """What a name says about its place in a migration.

    ``stem`` is the name less its marker; ``generation`` orders the members
    of one stem (0 for old or legacy, 1 for the bare name or ``_v1``, N for
    ``_vN``); ``marker`` is how the name says so, for the message; a
    ``loose`` marker (a bare digit, ``new_``/``old_``) links only within one
    module and only to the bare stem, because those words name other things
    too: ``old_lines``/``new_lines`` are a diff's two sides and
    ``step2``/``step3`` a sequence, not a migration.
    """

    stem: str
    generation: int
    marker: str
    loose: bool


_NAME_RULES: tuple[tuple[re.Pattern[str], int | None, str, bool], ...] = (
    # (pattern over the name less leading underscores, generation or None
    #  for the captured number, marker template, loose)
    (re.compile(r"(?P<stem>[A-Za-z]\w*?)_v(?P<n>\d+)"), None, "'_v{n}' suffix", False),
    (re.compile(r"(?P<stem>[A-Z]\w*?[a-z])V(?P<n>\d+)"), None, "'V{n}' suffix", False),
    (re.compile(r"(?P<stem>[a-z]\w*?)_legacy"), 0, "'_legacy' suffix", False),
    (re.compile(r"legacy_(?P<stem>[a-z]\w*)"), 0, "'legacy_' prefix", False),
    (re.compile(r"Legacy(?P<stem>[A-Z]\w*)"), 0, "'Legacy' prefix", False),
    (re.compile(r"(?P<stem>[A-Z]\w*?[a-z])Legacy"), 0, "'Legacy' suffix", False),
    (re.compile(r"(?P<stem>[a-z]\w*?)_old"), 0, "'_old' suffix", True),
    (re.compile(r"old_(?P<stem>[a-z]\w*)"), 0, "'old_' prefix", True),
    (re.compile(r"Old(?P<stem>[A-Z]\w*)"), 0, "'Old' prefix", True),
    (re.compile(r"(?P<stem>[a-z]\w*?)_new"), 2, "'_new' suffix", True),
    (re.compile(r"new_(?P<stem>[a-z]\w*)"), 2, "'new_' prefix", True),
    (re.compile(r"New(?P<stem>[A-Z]\w*)"), 2, "'New' prefix", True),
    # A bare digit: ``parse2`` beside ``parse``. One digit, after a letter,
    # so ``sha256``, ``utf8`` and ``int32`` style names never link.
    (re.compile(r"(?P<stem>[A-Za-z]\w*?[A-Za-z])(?P<n>[2-9])"), None, "'{n}' suffix", True),
)


def _name_link(name: str) -> NameLink:
    """The stem, generation and marker of ``name``; a bare name is generation 1."""
    core = name.lstrip("_")
    lead = name[: len(name) - len(core)]
    for pattern, generation, marker, loose in _NAME_RULES:
        match = pattern.fullmatch(core)
        if match is None:
            continue
        number = match.groupdict().get("n")
        gen = int(match["n"]) if generation is None else generation
        return NameLink(lead + match["stem"], gen, marker.format(n=number), loose=loose)
    return NameLink(name, 1, "", loose=False)


def _module_link(filepath: Path) -> NameLink:
    """The module's path less its one marked part, as a NameLink over the path.

    A directory named ``legacy``/``old``/``new`` or ``vN``, or a file stem
    carrying a name marker (``client_v2``, ``legacy_views``), marks the
    module. A path with no marked part, or more than one, is bare.
    """
    parts = list(filepath.with_suffix("").parts)
    marked: list[tuple[int, str | None, int, str]] = []
    for index, part in enumerate(parts):
        version = re.fullmatch(r"v(\d+)", part)
        if version is not None:
            marked.append((index, None, int(version[1]), f"'{part}/' package"))
        elif part in {"legacy", "old"}:
            marked.append((index, None, 0, f"'{part}/' package"))
        elif part == "new":
            marked.append((index, None, 2, "'new/' package"))
        else:
            link = _name_link(part)
            # Only an explicit marker names a module's generation: ``new_``
            # and digits say too little about a file (``base64.py``).
            if link.marker and not link.loose:
                marked.append((index, link.stem, link.generation, f"{link.marker} on the module"))
    if len(marked) != 1:
        return NameLink("/".join(parts), 1, "", loose=False)
    index, stem, generation, marker = marked[0]
    if stem is None:
        del parts[index]
    else:
        parts[index] = stem
    return NameLink("/".join(parts), generation, marker, loose=False)


@dataclass(frozen=True)
class MigrationPair:
    """An old/new pair still called in production, and how its names link it."""

    old: Definition
    new: Definition
    marker: str
    old_callers: _Callers
    new_callers: _Callers


def _definitions(tree: ast.Module, filepath: Path) -> Iterator[Definition]:
    """Module-level functions and classes, and the methods of module-level classes."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield Definition(filepath, node, None)
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield Definition(filepath, item, node.name)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


class _CallerResolver:
    """The call sites of a definition, resolved as far as the call index allows.

    A module-level function or class: every call that resolves to it through
    the calling module's imports. A method: every ``x.name(...)`` call, but
    only when no other definition anywhere shares its name; otherwise its
    callers cannot be told apart and the answer is None.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        counts: dict[str, int] = defaultdict(int)
        for filepath, tree in ctx.all_trees.items():
            for definition in _definitions(tree, filepath):
                counts[definition.name] += 1
        self._name_counts = counts

    def calls(self, definition: Definition) -> list[dict] | None:
        if definition.owner is None:
            target = definition.call_target(self.ctx.module_graph)
            return [
                {"file": str(path), "line": call.lineno, "node": call}
                for path, _matcher, call in _resolving_calls(self.ctx, target)
            ]
        name = definition.name
        if self._name_counts[name] != 1:
            return None
        return [
            entry
            for entry in self.ctx.call_index.get(name, [])
            if entry["receiver_kind"] == "instance"
        ]

    def callers(self, definition: Definition, outside: Iterable[Definition]) -> _Callers | None:
        """The definition's callers split by role, leaving out calls inside ``outside``."""
        calls = self.calls(definition)
        if calls is None:
            return None
        excluded = list(outside)
        kept = [
            call
            for call in calls
            if not any(d.contains(call["file"], call["line"]) for d in excluded)
        ]
        return _split_callers(self.ctx, kept)


def _linked_by_name(definitions: list[Definition]) -> Iterator[tuple[Definition, Definition, str]]:
    """Old/new pairs whose names share a stem and differ in generation.

    Members are of one kind, at module level or in one class. A loose marker
    (a bare digit, ``new_``/``old_``) links only within one module and only
    to the bare stem. The new member is the stem's highest generation.
    """
    groups: dict[tuple[str, str | None, str], list[tuple[Definition, NameLink]]] = defaultdict(list)
    for definition in definitions:
        if _is_dunder(definition.name):
            continue
        link = _name_link(definition.name)
        groups[(definition.kind, definition.owner, link.stem)].append((definition, link))
    for members in groups.values():
        if len(members) < 2:
            continue
        top = max(link.generation for _, link in members)
        for new, new_link in members:
            if new_link.generation != top:
                continue
            for old, old_link in members:
                if old_link.generation >= top:
                    continue
                if (new_link.loose or old_link.loose) and old.file != new.file:
                    continue
                if new.owner is not None and old.file != new.file:
                    continue
                if (new_link.loose and old_link.marker) or (old_link.loose and new_link.marker):
                    continue
                marker = new_link.marker or old_link.marker
                yield old, new, marker


def _linked_by_module(
    definitions: list[Definition],
) -> Iterator[tuple[Definition, Definition, str]]:
    """Same-named public module-level definitions in an old and a new module.

    The modules are linked by a marked path part (``client.py`` and
    ``client_v2.py``, ``legacy/views.py`` and ``views.py``); the new module
    is the higher generation.
    """
    by_module: dict[str, list[tuple[Path, NameLink]]] = defaultdict(list)
    files = sorted({d.file for d in definitions}, key=str)
    for filepath in files:
        link = _module_link(filepath)
        by_module[link.stem].append((filepath, link))
    top_level: dict[tuple[Path, str], Definition] = {
        (d.file, d.name): d for d in definitions if d.owner is None and not d.name.startswith("_")
    }
    for members in by_module.values():
        for old_file, old_link in members:
            for new_file, new_link in members:
                if new_link.generation <= old_link.generation:
                    continue
                marker = new_link.marker or old_link.marker
                names = {name for (f, name) in top_level if f == old_file}
                for name in sorted(names):
                    new = top_level.get((new_file, name))
                    if new is None or new.kind != top_level[(old_file, name)].kind:
                        continue
                    yield top_level[(old_file, name)], new, f"{marker} ({old_file} → {new_file})"


def _find_migration_pairs(ctx: AnalysisContext) -> list[MigrationPair]:
    """Every name-linked old/new pair whose members both have production callers.

    A call inside either member does not count for the other (a new
    function delegating to the old one is how many migrations start).
    """
    definitions = [d for path, tree in source_trees(ctx) for d in _definitions(tree, path)]
    resolver = _CallerResolver(ctx)
    pairs: list[MigrationPair] = []
    seen: set[tuple[Path, int, Path, int]] = set()
    candidates = [*_linked_by_name(definitions), *_linked_by_module(definitions)]
    for old, new, marker in candidates:
        key = (old.file, old.line, new.file, new.line)
        if key in seen:
            continue
        seen.add(key)
        old_callers = resolver.callers(old, (old, new))
        new_callers = resolver.callers(new, (old, new))
        if old_callers is None or new_callers is None:
            continue
        if not old_callers.production or not new_callers.production:
            continue
        pairs.append(MigrationPair(old, new, marker, old_callers, new_callers))
    return sorted(pairs, key=lambda p: (str(p.old.file), p.old.line, str(p.new.file), p.new.line))


def _where(pair: MigrationPair) -> str:
    if pair.new.file == pair.old.file:
        return f"line {pair.new.line}"
    return f"{pair.new.file}:{pair.new.line}"


def _pair_clause(pair: MigrationPair) -> str:
    return (
        f"{pair.old.label} and {pair.new.label} ({_where(pair)}) are an old/new pair by name "
        f"({pair.marker}), and production still calls both: {pair.old.label} has "
        f"{_callers_clause(pair.old_callers)}; {pair.new.label} has "
        f"{len(pair.new_callers.production)} production caller(s)"
    )


# Both migration checks report the same pairs, so they share the edit and the dismissal.
_MIGRATION_ADVICE = Advice(
    fix=(
        "Finish the migration: move the old member's production callers to the new member,"
        " then delete the old member."
    ),
    dismiss=(
        "Dismiss only if the old member is a deliberate compatibility surface, kept on purpose"
        " for callers outside this codebase."
    ),
)


@check(
    "half-finished-migration",
    severity=Severity.LOW,
    description="Old/new pairs by name (_v2, legacy_, new_ ...) both still called in production",
    advice=_MIGRATION_ADVICE,
)
def check_half_finished_migration(ctx: AnalysisContext) -> list[Finding]:
    """Flag the old member of a name-linked old/new pair that production still calls.

    The AST run has no history, so this reports every such pair without its
    age; ``stalled-migration`` is the same pairs, aged, in the git-history run.
    """
    return [
        Finding(
            file=str(pair.old.file),
            line=pair.old.line,
            check="half-finished-migration",
            message=_pair_clause(pair),
            severity=Severity.LOW,
        )
        for pair in _find_migration_pairs(ctx)
    ]


def _head_definition_line(lines: LineHistory, definition: Definition) -> int | None:
    """The line of ``definition`` in its file as committed at HEAD, or None if HEAD lacks it."""
    tree = lines.head_tree(str(definition.file))
    if tree is None:
        return None
    for found in _definitions(tree, definition.file):
        if (found.name, found.owner, found.kind) == (
            definition.name,
            definition.owner,
            definition.kind,
        ):
            return found.line
    return None


def _call_pattern(definition: Definition) -> re.Pattern[str]:
    """A call of ``definition`` in a source line: ``.name(`` for a method, else ``name(``."""
    if definition.owner is not None:
        return re.compile(rf"\.{definition.name}\s*\(")
    return re.compile(rf"(?<![\w]){definition.name}\s*\(")


def _is_definition_line(text: str, name: str) -> bool:
    return re.match(rf"\s*(?:async\s+)?(?:def|class)\s+{name}\b", text) is not None


def _calls_removed(ctx: AnalysisContext, hunks: list[Hunk], definition: Definition) -> bool:
    """Whether one commit's hunks remove more production calls of ``definition`` than they add."""
    pattern = _call_pattern(definition)
    net = 0
    for hunk in hunks:
        if ctx.roles.is_test(hunk.file):
            continue
        for text in hunk.removed:
            if not _is_definition_line(text, definition.name):
                net += len(pattern.findall(text))
        for text in hunk.added:
            if not _is_definition_line(text, definition.name):
                net -= len(pattern.findall(text))
    return net > 0


@check(
    "stalled-migration",
    # One level above half-finished-migration (LOW), whose pairs this ages.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Old/new pairs by name where no old-member caller moved in 90+ days",
    advice=_MIGRATION_ADVICE,
)
def check_stalled_migration(ctx: AnalysisContext) -> list[Finding]:
    """Raise a half-finished migration that git shows has stopped.

    A pair qualifies when its new member was written 90+ days ago (blame of
    its ``def``/``class`` line at HEAD) and no commit in the last 90 days
    removed more production calls of the old member than it added. One
    ``git log -G`` over every old member's name answers the second question
    for all pairs.
    """
    history = ctx.git_history
    if history is None:
        return []
    lines = history.line_history
    now = datetime.now(UTC)
    aged: list[tuple[MigrationPair, datetime]] = []
    for pair in _find_migration_pairs(ctx):
        head_line = _head_definition_line(lines, pair.new)
        if head_line is None:
            continue
        written = lines.line_ages(str(pair.new.file), [head_line])[head_line]
        if now - written >= STALL_BAR:
            aged.append((pair, written))
    if not aged:
        return []
    names = sorted({pair.old.name for pair, _ in aged})
    commits = lines.changes_matching(rf"({'|'.join(names)})[[:space:]]*\(", now - STALL_BAR)
    findings = []
    for pair, written in aged:
        if any(_calls_removed(ctx, hunks, pair.old) for _sha, hunks in commits):
            continue
        findings.append(
            Finding(
                file=str(pair.old.file),
                line=pair.old.line,
                check="stalled-migration",
                message=(
                    f"{_pair_clause(pair)}. {pair.new.label} was written {written:%Y-%m-%d} "
                    f"({(now - written).days} days ago), and no commit in the last "
                    f"{STALL_BAR.days} days removed a production call of {pair.old.label}"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings


class _Bindings:
    """What the names of one module refer to, from its imports (last binding wins)."""

    def __init__(self, ctx: AnalysisContext, filepath: Path) -> None:
        self.bound = import_bindings(ctx.module_imports.get(filepath, []))

    def resolve(self, expr: ast.expr) -> str | None:
        """The dotted path ``expr`` names through an import, or None if it names no import.

        ``name`` and ``module.name`` resolve, which is every spelling of
        ``warnings.warn``, a warning category and ``@deprecated``; a longer
        chain (``pkg.mod.Name``) is not followed.
        """
        match expr:
            case ast.Name(id=name):
                return self.bound.get(name)
            case ast.Attribute(value=ast.Name(id=root), attr=attr) if root in self.bound:
                return f"{self.bound[root]}.{attr}"
        return None


def _deprecation_classes(ctx: AnalysisContext) -> dict[str, set[str]]:
    """Analyzed warning classes that subclass a deprecation category: name → defining modules.

    Bases are matched by their last name, to a fixed point, so a project's
    ``RemovedInV3Warning(DeprecationWarning)`` and its subclasses count.
    """
    classes: list[tuple[str, str, set[str | None]]] = []
    for filepath, tree in ctx.all_trees.items():
        module = filepath_to_module(str(filepath), ctx.package_dirs)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = {referenced_name(base) for base in node.bases}
                classes.append((node.name, module, bases))
    known: set[str | None] = set(_DEPRECATION_CATEGORIES)
    found: dict[str, set[str]] = defaultdict(set)
    changed = True
    while changed:
        changed = False
        for name, module, bases in classes:
            if module not in found.get(name, set()) and known.intersection(bases):
                found[name].add(module)
                known.add(name)
                changed = True
    return found


class _DeprecationReader:
    """Reads one module's deprecation markers: the warn call, its category, the decorator."""

    def __init__(
        self, ctx: AnalysisContext, filepath: Path, project_classes: dict[str, set[str]]
    ) -> None:
        self.bindings = _Bindings(ctx, filepath)
        self.module = filepath_to_module(str(filepath), ctx.package_dirs)
        self.project_classes = project_classes

    def is_category(self, expr: ast.expr) -> bool:
        """Whether ``expr`` names DeprecationWarning, PendingDeprecationWarning or a subclass."""
        if isinstance(expr, ast.Call):
            expr = expr.func
        resolved = self.bindings.resolve(expr)
        if resolved is None:
            if not isinstance(expr, ast.Name):
                return False
            if expr.id in _DEPRECATION_CATEGORIES:
                return True
            return self.module in self.project_classes.get(expr.id, set())
        module, _, name = resolved.rpartition(".")
        if module == "builtins":
            return name in _DEPRECATION_CATEGORIES
        return any(_reaches_module(module, m) for m in self.project_classes.get(name, set()))

    def warn_category(self, stmt: ast.stmt) -> ast.expr | None:
        """The category of a ``warnings.warn(...)`` statement, or None for any other statement."""
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            return None
        call = stmt.value
        if self.bindings.resolve(call.func) != _WARN:
            return None
        for keyword in call.keywords:
            if keyword.arg == "category":
                return keyword.value
        if len(call.args) >= 2:
            return call.args[1]
        # ``warnings.warn(DeprecationWarning("..."))``: the message is the warning.
        if call.args and isinstance(call.args[0], ast.Call):
            return call.args[0]
        return None

    def how(self, node: _DefNode) -> str | None:
        """How ``node`` deprecates itself, or None when it does not.

        The warning must be a statement of the body itself: one under an
        ``if`` deprecates an argument or a path, not every call.
        """
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if self.bindings.resolve(target) in _DEPRECATED_DECORATORS:
                return f"@deprecated at line {decorator.lineno}"
        if isinstance(node, ast.ClassDef):
            return None
        for stmt in node.body:
            category = self.warn_category(stmt)
            if category is not None and self.is_category(category):
                name = ast.unparse(category.func if isinstance(category, ast.Call) else category)
                return f"warnings.warn(..., {name}) at line {stmt.lineno}"
        return None


def _deprecated_definitions(ctx: AnalysisContext) -> Iterator[tuple[Definition, str]]:
    """Every definition that deprecates itself, with how.

    A class's ``__init__`` or ``__new__`` deprecating itself deprecates the
    class, whose callers are what construct it.
    """
    project_classes = _deprecation_classes(ctx)
    for filepath, tree in source_trees(ctx):
        reader = _DeprecationReader(ctx, filepath, project_classes)
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        for definition in _definitions(tree, filepath):
            how = reader.how(definition.node)
            if how is None:
                continue
            if definition.owner is not None and definition.name in {"__init__", "__new__"}:
                yield Definition(filepath, classes[definition.owner], None), how
            else:
                yield definition, how


@check(
    "self-deprecated",
    severity=Severity.MEDIUM,
    description="Deprecated functions and classes (DeprecationWarning, @deprecated) still called",
    advice=Advice(
        fix=(
            "Move each production caller listed to the replacement the deprecation names,"
            " then delete the deprecated definition."
        ),
        dismiss=(
            "Dismiss only if the deprecation names no replacement yet, so the callers listed"
            " have nothing to move to."
        ),
    ),
)
def check_self_deprecated(ctx: AnalysisContext) -> list[Finding]:
    """Flag a definition that deprecates itself while production code still calls it.

    A call from inside another deprecated definition does not count: it
    goes when that one does.
    """
    deprecated = list(_deprecated_definitions(ctx))
    resolver = _CallerResolver(ctx)
    outside = [definition for definition, _ in deprecated]
    findings = []
    for definition, how in deprecated:
        callers = resolver.callers(definition, outside)
        if callers is None or not callers.production:
            continue
        findings.append(
            Finding(
                file=str(definition.file),
                line=definition.line,
                check="self-deprecated",
                message=(
                    f"{definition.label} is deprecated ({how}) but production still calls it: "
                    f"{len(callers.production)} caller(s) to migrate — "
                    f"{_cite_sites(callers.production, ())}"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings
