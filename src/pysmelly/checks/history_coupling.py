"""Git history checks — coupling, debt, and structural signals."""

from __future__ import annotations

import ast
import functools
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from pysmelly.checks.framework import is_django_urlconf, is_migration_file
from pysmelly.checks.helpers import INIT_FILE, dotted_strings
from pysmelly.checks.history_helpers import (
    COCHANGE_SKIP_THRESHOLD,
    MIN_LINES_MEDIUM,
    after_review,
    category_count,
    churned_files,
    counted_commits,
    coupled_pairs,
    get_line_count,
    has_semantic_history,
    is_bulk_commit,
    is_expected_coupling,
    is_naturally_stable,
    notes_persisting_pair_review,
    notes_persisting_review,
    source_files,
)
from pysmelly.checks.module_graph import ImportedPath
from pysmelly.config import expected_coupling_line
from pysmelly.context import AnalysisContext
from pysmelly.git_history import FEATURE_CATEGORY, FIX_CATEGORY, classify_commit
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)

# blast-radius and change-coupling report this many or more findings in one
# directory as one package-level finding: a tightly-coupled subsystem.
MIN_CLUSTER = 3

# blast-radius names at most this many of a file's co-change partners.
PARTNERS_SHOWN = 5


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _declare_hint(line: str, *, several: bool) -> str:
    """The closing clause that hands the reader the config line to paste."""
    if several:
        return f"the expected-coupling line for the pairs: `{line}`"
    return f"the expected-coupling line for them: `{line}`"


def _top_partners(
    partners: Mapping[str, Collection[str]], exclude: Collection[str] = ()
) -> list[tuple[str, int]]:
    """The ``PARTNERS_SHOWN`` most frequent co-change partners with their
    shared-commit counts, most first, ties by path. ``partners`` maps each
    co-changed file to the commits it shared; ``exclude`` drops files."""
    ranked = sorted(
        ((path, len(commits)) for path, commits in partners.items() if path not in exclude),
        key=lambda item: (-item[1], item[0]),
    )
    return ranked[:PARTNERS_SHOWN]


def _partners_text(top: list[tuple[str, int]]) -> str:
    return ", ".join(f"{path} ({count})" for path, count in top)


@dataclass(frozen=True)
class _Blast:
    """A file over blast-radius's threshold, with the evidence its message names."""

    file: str
    median: int
    commits: int
    # Each co-changed file -> the hashes of the counted commits it shared.
    partners: Mapping[str, frozenset[str]]


def _blast_message(blast: _Blast, window: str) -> str:
    """blast-radius's message for one file: the partners it drags along, and
    the config line for the first of them. Declaring one pair takes that
    partner out of the count; the line is a template for the others."""
    top = _top_partners(blast.partners)
    first, _count = top[0]
    hint = expected_coupling_line([(blast.file, first)])
    return (
        f"{blast.file} — changes touch a median of {blast.median} other files "
        f"per commit ({blast.commits} commits in last {window}) — poor "
        f"encapsulation; most frequent co-change partners (shared commits): "
        f"{_partners_text(top)}; the expected-coupling line for the first: `{hint}`"
    )


def _blast_cluster_message(
    directory: str, members: Sequence[_Blast], indices: tuple[int, ...]
) -> str:
    """The package-level blast-radius message over the members at ``indices``.

    Partners are counted across the members' commits, each commit once, and
    leave out the members themselves: what the subsystem drags along.
    """
    kept = [members[index] for index in indices]
    names = {blast.file for blast in kept}
    shared: dict[str, set[str]] = {}
    for blast in kept:
        for partner, commits in blast.partners.items():
            shared.setdefault(partner, set()).update(commits)
    return (
        f"{directory}/ — {_plural(len(kept), 'file')} in this package flagged "
        f"({', '.join(Path(blast.file).name for blast in kept)}) — tightly-coupled "
        f"subsystem; most frequent co-change partners besides these (shared commits): "
        f"{_partners_text(_top_partners(shared, exclude=names))}"
    )


def _blast_findings(blasts: list[_Blast], window: str) -> list[Finding]:
    """One finding per file, or one per directory holding ``MIN_CLUSTER`` or
    more, whose sites are the files so an ignore on one removes just it."""
    by_directory: dict[str, list[_Blast]] = {}
    for blast in blasts:
        by_directory.setdefault(str(Path(blast.file).parent), []).append(blast)

    findings: list[Finding] = []
    for directory, members in by_directory.items():
        if len(members) < MIN_CLUSTER:
            findings.extend(
                Finding(
                    file=blast.file,
                    line=1,
                    check="blast-radius",
                    message=_blast_message(blast, window),
                    severity=Severity.MEDIUM,
                )
                for blast in members
            )
            continue
        findings.append(
            multi_site_finding(
                "blast-radius",
                Severity.MEDIUM,
                [Site(blast.file, 1) for blast in members],
                functools.partial(_blast_cluster_message, directory, members),
                anchor=Site(directory, 1),
            )
        )
    return findings


@dataclass(frozen=True)
class _CoupledPair:
    """Two files change-coupling reports, with the counts its message names."""

    file_a: str
    file_b: str
    co_changes: int
    min_count: int


def _pair_message(pair: _CoupledPair, window: str, indices: tuple[int, ...]) -> str | None:
    """change-coupling's message for one pair; None once either file is
    ignored, since one file is not a pair."""
    if len(indices) < 2:
        return None
    hint = expected_coupling_line([(pair.file_a, pair.file_b)])
    return (
        f"{pair.file_a} and {pair.file_b} changed together in "
        f"{pair.co_changes}/{pair.min_count} commits (last {window}) "
        f"with no import relationship — hidden coupling — "
        f"{_declare_hint(hint, several=False)}"
    )


def _coupling_cluster_message(
    directory: str,
    files: Sequence[str],
    pairs: Sequence[_CoupledPair],
    indices: tuple[int, ...],
) -> str | None:
    """The package-level change-coupling message over the pairs whose files
    are both among ``files`` at ``indices``; None when no pair is left."""
    kept = {files[index] for index in indices}
    left = [pair for pair in pairs if pair.file_a in kept and pair.file_b in kept]
    if not left:
        return None
    in_pairs = sorted({path for pair in left for path in (pair.file_a, pair.file_b)})
    hint = expected_coupling_line((pair.file_a, pair.file_b) for pair in left)
    return (
        f"{directory}/ — {_plural(len(in_pairs), 'file')} with "
        f"{_plural(len(left), 'coupling pair')} "
        f"({', '.join(Path(path).name for path in in_pairs)}) — tightly-coupled "
        f"subsystem — {_declare_hint(hint, several=len(left) > 1)}"
    )


def _coupling_findings(pairs: list[_CoupledPair], window: str) -> list[Finding]:
    """One finding per pair, sited on both files, except that ``MIN_CLUSTER``
    or more pairs inside one directory make one finding sited on their files."""
    same_directory: dict[str, list[_CoupledPair]] = {}
    findings: list[Finding] = []
    for pair in pairs:
        directory = str(Path(pair.file_a).parent)
        if directory == str(Path(pair.file_b).parent):
            same_directory.setdefault(directory, []).append(pair)
        else:
            findings.append(_pair_finding(pair, window))

    for directory, group in same_directory.items():
        if len(group) < MIN_CLUSTER:
            findings.extend(_pair_finding(pair, window) for pair in group)
            continue
        files = sorted({path for pair in group for path in (pair.file_a, pair.file_b)})
        findings.append(
            multi_site_finding(
                "change-coupling",
                Severity.MEDIUM,
                [Site(path, 1) for path in files],
                functools.partial(_coupling_cluster_message, directory, files, group),
                anchor=Site(directory, 1),
            )
        )
    return findings


def _pair_finding(pair: _CoupledPair, window: str) -> Finding:
    return multi_site_finding(
        "change-coupling",
        Severity.MEDIUM,
        [Site(pair.file_a, 1), Site(pair.file_b, 1)],
        functools.partial(_pair_message, pair, window),
    )


def _files_have_import(file_a: str, file_b: str, ctx: AnalysisContext) -> bool:
    """Whether either file imports the other, read from ``ctx.module_graph``.

    An import credits the module it names (``_imports_module``) and, through
    a re-export, the module that defines what it binds: ``from pkg.sub
    import X``, with ``pkg/sub/__init__.py`` re-exporting ``X`` from
    ``impl``, credits ``pkg/sub/impl.py`` too (``ModuleGraph.defining_files``).
    A dotted string that names the other module loads it as surely as an
    import does (``_string_loads``): Django's ``ROOT_URLCONF = "proj.urls"``.
    A file git history names that is not analyzed (deleted, excluded or
    unparseable) has no node in the graph, so it imports nothing; it can
    still be imported, by its path's module name.
    """
    graph = ctx.module_graph
    for importer, imported in ((file_a, file_b), (file_b, file_a)):
        path = Path(importer)
        if path in graph.nodes and (
            _imports_module(graph.nodes[path].imports, graph.module_of(imported))
            or Path(imported) in graph.defining_files(path)
            or Path(imported) in _string_loads(path, ctx)
        ):
            return True
    return False


def _string_loads(path: Path, ctx: AnalysisContext) -> set[Path]:
    """The other analyzed files a dotted string in ``path`` names.

    A string names the module it spells (``"proj.urls"``) and the module
    holding the name it ends in (``"proj.tasks.sweep"``, ``"proj.tasks:sweep"``),
    which is how Django settings, URLconfs and Celery schedules load code.
    Every name the module graph knows a file by counts, its ``sys.path``
    names included (``ModuleGraph.by_module``). A string reaching deeper
    than one name past a module credits nothing: it is not a load of it.
    """
    by_module = ctx.module_graph.by_module
    named: set[Path] = set()
    for node in ast.walk(ctx.all_trees[path]):
        for dotted in dotted_strings(node):
            for module in (dotted, dotted.rpartition(".")[0]):
                named.update(by_module.get(module, ()))
    named.discard(path)
    return named


def _share_parent_base(file_a: str, file_b: str, ctx: AnalysisContext) -> bool:
    """Whether two sibling files subclass one class their package defines.

    The files sit in one directory, and a module-level class in each
    inherits directly from the same analyzed class, defined in a file of
    that directory or below it (``ModuleGraph.base_classes``):
    ``handlers/audio.py`` and ``handlers/video.py`` both subclassing
    ``handlers/base.py``'s ``ContentHandler``. Each changes when the
    contract they implement does, so their co-change is explained. Sharing
    an import of a common module is not enough on its own (DECISIONS.md,
    "Shared-parent credit is common base-class inheritance").
    """
    graph = ctx.module_graph
    path_a, path_b = Path(file_a), Path(file_b)
    if path_a.parent != path_b.parent or not {path_a, path_b} <= graph.nodes.keys():
        return False
    shared = graph.base_classes(path_a) & graph.base_classes(path_b)
    return any(base.path is not None and base.path.is_relative_to(path_a.parent) for base in shared)


def _import_reaches_module(imported: str, module: str) -> bool:
    """True when importing ``imported`` runs ``module``'s own code.

    Either the paths are equal, or ``imported`` sits inside ``module`` —
    ``from billing.invoice.tax import x`` executes ``billing/invoice.py``'s
    package ``__init__`` chain, so it depends on ``billing``.
    """
    return imported == module or imported.startswith(module + ".")


def _import_names_package_of(imported: str, module: str) -> bool:
    """True when ``imported`` is a *dotted* package path that contains ``module``.

    ``import billing.invoice`` — or the equivalent ``from billing import
    invoice`` — declares a dependency on the ``billing.invoice`` package, so
    co-changes with anything under it are explained by that import.

    A bare, undotted top-level name is the exception and never credits anything
    below it: ``import billing`` runs ``billing/__init__.py`` and nothing else,
    so it says nothing about ``billing/invoice.py``. That distinction is what
    keeps the rule usable in a single-package src-layout repo, where every file
    lives under one top-level name and a bare ``import billing`` would otherwise
    clear the file against the whole codebase. Crediting the package the bare
    import *does* run is ``_import_reaches_module``'s job, not this one's.
    """
    return "." in imported and module.startswith(imported + ".")


def _imports_module(imports: Iterable[ImportedPath], module: str) -> bool:
    """Whether a module's imports (its ``ModuleNode.imports``) credit ``module``.

    An import credits ``module`` under two rules: it *runs* ``module``'s code
    (``_import_reaches_module``), or it *names a dotted package* ``module``
    lives under (``_import_names_package_of``). The ``S`` of ``from S import
    n`` (``from_source``) is certainly a module, while ``S.n`` is only *maybe*
    one (``from billing import VERSION`` names a symbol), so only the first
    rule applies to ``S``. The settled semantics:

    - ``import billing.invoice`` and ``from billing import invoice`` credit
      everything under ``billing/invoice/`` — naming a subpackage is a declared
      dependency on it.
    - ``import billing`` credits only ``billing`` itself. A bare, undotted
      top-level import runs ``billing/__init__.py`` and no more, so it is not
      evidence of a dependency on ``billing/invoice.py``. Without that
      exception a single-package src-layout repo — where every file is under
      one top-level name — would have nearly every co-changing pair explained
      away, leaving ``change-coupling`` inert.
    - ``from billing import VERSION`` credits ``billing`` only: an alias may
      name a symbol rather than a module, so it must not stand in for a
      dependency on every module under ``billing/``.
    - ``from billing.invoice import X`` where ``billing/invoice/__init__.py``
      re-exports ``X`` from ``billing/invoice/impl.py`` credits
      ``billing.invoice`` here; ``_files_have_import`` adds the credit for
      ``billing.invoice.impl`` from the module graph's re-export origins.
      See DECISIONS.md, "What counts as an import for change-coupling".
    - ``from billing import *`` credits ``billing`` and nothing below it: the
      star names no path of its own (``ImportedPath``).
    """
    return any(
        _import_reaches_module(imported.dotted, module)
        or (not imported.from_source and _import_names_package_of(imported.dotted, module))
        for imported in imports
    )


def _tests_importing(source: Path, ctx: AnalysisContext) -> list[str]:
    """The test-role files that import ``source`` directly, sorted.

    These are the module's tests by evidence: the code that loads it to
    exercise it, wherever it lives and whatever it is called.
    """
    return sorted(
        str(importer)
        for importer, loaded in ctx.import_graph.items()
        if source in loaded and ctx.roles.is_test(importer)
    )


@check(
    "blast-radius",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Files whose changes drag many other files along",
    advice=Advice(
        fix=(
            "Move what the file and its partners keep changing for into one module, so a"
            " change stops touching the partners listed."
        ),
        dismiss=(
            "Dismiss only if a partner changes with it by design (a schema and its"
            " migration); then declare that pair with the expected-coupling line given."
        ),
    ),
)
@notes_persisting_review
def check_blast_radius(ctx: AnalysisContext) -> list[Finding]:
    """Flag a file whose commits drag a median of many other files along.

    A co-changed file that ``expected-coupling`` pairs with the file does not
    count: that pair changes together by design. Nor does a test-role file:
    a test changing with the code is the change's test, as divergent-change
    and change-coupling read it, and test drag is test-erosion's subject.
    The bulk-commit cut still counts every ``.py`` file. The message names
    the files dragged along most often, and the config line declaring them.
    """
    history = ctx.git_history
    if history is None:
        return []

    threshold = max(8, int(history.median_commit_size * 2.5))
    blasts: list[_Blast] = []

    for file_path, file_str in source_files(ctx):
        if file_path.name == INIT_FILE:
            continue

        commits = history.commits_since_review(file_str)
        if not commits:
            continue

        # For each commit, count other .py files changed (excluding this file)
        co_change_counts: list[int] = []
        partners: dict[str, set[str]] = {}
        for commit in commits:
            py_files = [f for f in commit.python_files if f != file_str]
            # Skip bulk refactors
            if len(py_files) >= COCHANGE_SKIP_THRESHOLD:
                continue
            dragged = [
                f
                for f in py_files
                if not ctx.roles.is_test(f)
                and not is_expected_coupling(file_str, f, ctx.expected_coupling)
            ]
            co_change_counts.append(len(dragged))
            for f in dragged:
                partners.setdefault(f, set()).add(commit.hash)

        # Need >= 5 qualifying commits
        if len(co_change_counts) < 5:
            continue

        median_co_changes = median(co_change_counts)
        if median_co_changes >= threshold:
            blasts.append(
                _Blast(
                    file=file_str,
                    median=int(median_co_changes),
                    commits=len(co_change_counts),
                    partners={path: frozenset(hashes) for path, hashes in partners.items()},
                )
            )

    return _blast_findings(blasts, history.window)


@check(
    "change-coupling",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Files that always change together with no import or shared base class",
    advice=Advice(
        fix=(
            "Make the dependency explicit: move what both files change for into one module"
            " they import, or have one import it from the other."
        ),
        dismiss=(
            "Dismiss only if the files change together by design; then declare the pair"
            " with the expected-coupling line the message gives."
        ),
    ),
)
@notes_persisting_pair_review
def check_change_coupling(ctx: AnalysisContext) -> list[Finding]:
    """Flag file pairs that change together with nothing in the code to say why.

    A pair whose two files both carry a ``pysmelly: reviewed`` marker is
    judged on the commits after the later marker (``coupled_pairs``).
    """
    history = ctx.git_history
    if history is None:
        return []

    pairs: list[_CoupledPair] = []

    for counted in coupled_pairs(ctx, history, history.commits, min_co_changes=5, min_ratio=0.7):
        file_a, file_b = counted.file_a, counted.file_b
        if _files_have_import(file_a, file_b, ctx) or _share_parent_base(file_a, file_b, ctx):
            continue

        pairs.append(_CoupledPair(file_a, file_b, counted.co_changes, counted.min_count))

    return _coupling_findings(pairs, history.window)


@check(
    "yo-yo-code",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="High gross churn — code being written, deleted, rewritten repeatedly",
    advice=Advice(
        fix=(
            "Find the part the commits keep rewriting and settle its design: name the concept"
            " it circles and give it one stable interface."
        ),
        dismiss=(
            "Dismiss only if the churn is mechanical (formatting, renames, generated code)"
            " rather than rework."
        ),
    ),
)
@notes_persisting_review
def check_yo_yo_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_str, current_lines, stats in churned_files(history, ctx, min_lines=100, min_commits=5):
        gross_churn = stats.total_insertions + stats.total_deletions
        churn_ratio = gross_churn / current_lines
        if churn_ratio < 3.0:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="yo-yo-code",
                message=(
                    f"{file_str} — {gross_churn} lines churned across "
                    f"{current_lines} lines of code ({churn_ratio:.1f}x "
                    f"turnover) in last {history.window} — abstractions "
                    f"are being reworked repeatedly"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "conscious-debt",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Commits that explicitly acknowledge technical debt",
    advice=Advice(
        fix=(
            "Pay the debt the commit acknowledged (remove the workaround), or, if it is now"
            " the design, say so in the code."
        ),
        dismiss=(
            "Dismiss only if the debt waits on an external fix the commit names (an upstream"
            " bug, a vendor release) that has not shipped."
        ),
    ),
)
@notes_persisting_review
def check_conscious_debt(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_semantic_history(history):
        return []

    analyzed_files = {str(p) for p in ctx.all_trees}

    # Group debt commits by file, keeping the most recent
    file_debt: dict[str, list[tuple[str, str, str]]] = {}

    for commit in history.commits:
        if "debt" not in classify_commit(commit.message):
            continue

        for filepath in commit.files:
            if filepath not in analyzed_files or not after_review(history, filepath, commit):
                continue
            file_debt.setdefault(filepath, []).append(
                (commit.hash[:7], commit.date.strftime("%Y-%m-%d"), commit.message)
            )

    findings: list[Finding] = []

    for filepath, debt_commits in file_debt.items():
        # Use the most recent debt commit for the message
        commit_hash, date_str, message = debt_commits[0]
        count = len(debt_commits)

        if count == 1:
            detail = f'commit {commit_hash} "{message}" ({date_str})'
        else:
            detail = f'{count} debt commits, most recent: {commit_hash} "{message}" ({date_str})'

        findings.append(
            Finding(
                file=filepath,
                line=1,
                check="conscious-debt",
                message=f"{filepath} — {detail} — acknowledged debt",
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "emergency-hotspots",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Files that attract disproportionate emergency/hotfix activity",
    advice=Advice(
        fix=(
            "Read the emergency commits for their shared cause and fix it at the root: add"
            " the validation, test or error handling they patched around."
        ),
        dismiss=(
            "Dismiss only if the emergencies came from outside the file (an upstream outage,"
            " a data feed change) and none patched its logic."
        ),
    ),
)
@notes_persisting_review
def check_emergency_hotspots(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    # Compute project-wide emergency rate to skip if globally high
    non_bulk = [c for c in history.commits if not is_bulk_commit(c)]
    total_commits = len(non_bulk)
    if total_commits == 0:
        return []
    total_emergency = category_count(non_bulk, "emergency")
    project_rate = total_emergency / total_commits
    if project_rate > 0.3:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        commits = counted_commits(history, file_str)
        emergency_count = category_count(commits, "emergency")
        if emergency_count < 3:
            continue

        emergency_ratio = emergency_count / len(commits)
        if emergency_ratio < 0.3:
            continue

        pct = int(emergency_ratio * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="emergency-hotspots",
                message=(
                    f"{file_str} — {emergency_count} of {len(commits)} commits "
                    f"({pct}%) are emergency/hotfix changes — fragile code "
                    f"that breaks under pressure"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "no-refactoring",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Files with heavy fix/feature activity but zero refactoring",
    advice=Advice(
        fix=(
            "Refactor the code the fixes and features keep touching (extract, rename,"
            " simplify) before adding more to it."
        ),
        dismiss=(
            "Dismiss only if refactors were committed under messages the classifier does not"
            " recognize; name those commits in the ignore."
        ),
    ),
)
@notes_persisting_review
def check_no_refactoring(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_semantic_history(history):
        return []

    findings: list[Finding] = []

    for file_path, file_str in source_files(ctx):
        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < MIN_LINES_MEDIUM:
            continue
        # A URLconf grows one route per feature and is never refactored as
        # such; zero refactoring there is the framework's shape, not debt.
        if is_django_urlconf(ctx.all_trees[file_path]):
            continue

        commits = counted_commits(history, file_str)
        if len(commits) < 8:
            continue

        fix_count = 0
        feature_count = 0
        refactor_count = 0
        for c in commits:
            cats = classify_commit(c.message)
            if FIX_CATEGORY in cats:
                fix_count += 1
            if FEATURE_CATEGORY in cats:
                feature_count += 1
            if "refactor" in cats:
                refactor_count += 1

        if refactor_count > 0:
            continue
        if (fix_count + feature_count) < 6:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="no-refactoring",
                message=(
                    f"{file_str} — {len(commits)} commits "
                    f"({fix_count} fixes, {feature_count} features) but "
                    f"zero refactoring — accumulated complexity likely "
                    f"needs cleanup"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "test-erosion",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Source files changing much more often than their tests",
    advice=Advice(
        fix="Add or update the module's tests to cover what the recent source commits changed.",
        dismiss=(
            "Dismiss only if the source commits changed no behavior (comments,"
            " formatting, renames)."
        ),
    ),
    about_tests=True,
)
@notes_persisting_review
def check_test_erosion(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path, file_str in source_files(ctx):
        if is_naturally_stable(file_path):
            continue
        if is_migration_file(file_str):
            continue

        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < MIN_LINES_MEDIUM:
            continue

        source_commits = counted_commits(history, file_str)
        if len(source_commits) < 5:
            continue

        test_files = _tests_importing(file_path, ctx)
        if not test_files:
            continue

        # A commit touching several of the tests counts once.
        test_commits = list(
            {
                c.hash: c for test_file in test_files for c in counted_commits(history, test_file)
            }.values()
        )
        test_count = max(len(test_commits), 1)
        ratio = len(source_commits) / test_count

        if ratio < 3.0:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="test-erosion",
                message=(
                    f"{file_str} — {len(source_commits)} source commits but "
                    f"only {len(test_commits)} test commits in last "
                    f"{history.window} ({ratio:.1f}x ratio) — test coverage "
                    f"may be eroding"
                ),
                severity=Severity.LOW,
            )
        )

    return findings
