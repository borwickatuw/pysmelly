"""Git history checks — detect evolutionary signals invisible to static analysis."""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
from statistics import median

from pysmelly.context import AnalysisContext
from pysmelly.git_history import GitHistory, TimeSlice, classify_commit
from pysmelly.registry import Finding, Severity, check

_MIN_MESSAGE_QUALITY = 0.5


def _is_expected_coupling(file_a: str, file_b: str, patterns: list[list[str]]) -> bool:
    """Check if a file pair matches any expected-coupling pattern pair."""
    for pat_a, pat_b in patterns:
        if (fnmatch.fnmatch(file_a, pat_a) and fnmatch.fnmatch(file_b, pat_b)) or (
            fnmatch.fnmatch(file_a, pat_b) and fnmatch.fnmatch(file_b, pat_a)
        ):
            return True
    return False


# Files that are naturally stable and shouldn't be flagged
_SKIP_NAMES = frozenset({"__init__.py", "conftest.py", "apps.py"})

# Files that match these patterns are config-like and naturally stable
_SKIP_SUFFIXES = ("_config.py", "_settings.py", "settings.py", "config.py")


def _get_line_count(file_path: str, all_trees: dict) -> int:
    """Current line count from last AST statement's end_lineno."""
    tree = all_trees.get(Path(file_path))
    if tree is None or not tree.body:
        return 0
    return tree.body[-1].end_lineno


def _is_migration_file(filepath: str) -> bool:
    return "migrations" in Path(filepath).parts


def _is_test_file(filepath: str) -> bool:
    name = Path(filepath).name
    return name.startswith("test_") or name.endswith("_test.py")


@check(
    "abandoned-code",
    severity=Severity.LOW,
    category="git-history",
    description="Files on disk with no commits in the window while directory peers keep evolving",
)
def check_abandoned_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    # Group files by parent directory
    dir_files: dict[str, list[str]] = {}
    for file_path in ctx.all_trees:
        file_str = str(file_path)
        parent = str(file_path.parent)
        dir_files.setdefault(parent, []).append(file_str)

    for dir_name, files in dir_files.items():
        # Need >= 3 files to establish a meaningful peer group
        if len(files) < 3:
            continue

        # Split into active (has commits in window) vs untouched (on disk only)
        active_files = [f for f in files if f in history.last_modified]
        untouched_files = [f for f in files if f not in history.last_modified]

        if not untouched_files or not active_files:
            continue

        # Need a majority of peers to be active for this to be meaningful
        if len(active_files) < len(files) / 2:
            continue

        for f in untouched_files:
            name = Path(f).name
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue

            findings.append(
                Finding(
                    file=f,
                    line=1,
                    check="abandoned-code",
                    message=(
                        f"{f} has no commits in the last {history.window}, "
                        f"but {len(active_files)} of {len(files)} peers in "
                        f"{dir_name}/ have been actively modified"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings


@check(
    "blast-radius",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files whose changes drag many other files along",
)
def check_blast_radius(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)
        if file_path.name == "__init__.py":
            continue
        if _is_test_file(file_str):
            continue

        commits = history.commits_for_file.get(file_str, [])
        if not commits:
            continue

        # For each commit, count other .py files changed (excluding this file)
        co_change_counts: list[int] = []
        for commit in commits:
            py_files = [f for f in commit.files if f.endswith(".py") and f != file_str]
            # Skip bulk refactors (>= 20 files)
            if len(py_files) >= 20:
                continue
            co_change_counts.append(len(py_files))

        # Need >= 5 qualifying commits
        if len(co_change_counts) < 5:
            continue

        median_co_changes = median(co_change_counts)
        if median_co_changes >= 5:
            findings.append(
                Finding(
                    file=file_str,
                    line=1,
                    check="blast-radius",
                    message=(
                        f"{file_str} — changes touch a median of "
                        f"{int(median_co_changes)} other files per commit "
                        f"({len(co_change_counts)} commits in last "
                        f"{history.window}) — poor encapsulation"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


@check(
    "change-coupling",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files that always change together but have no import relationship",
)
def check_change_coupling(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    # Build co-change pair counts
    pair_counts: dict[tuple[str, str], int] = {}
    file_commit_counts: dict[str, int] = {}

    analyzed_files = {str(p) for p in ctx.all_trees}

    for commit in history._commits:
        py_files = sorted(
            f
            for f in commit.files
            if f.endswith(".py")
            and f in analyzed_files
            and not Path(f).name == "__init__.py"
            and not _is_migration_file(f)
        )
        # Skip bulk commits
        if len(py_files) >= 20:
            continue

        for f in py_files:
            file_commit_counts[f] = file_commit_counts.get(f, 0) + 1

        for i in range(len(py_files)):
            for j in range(i + 1, len(py_files)):
                pair = (py_files[i], py_files[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    findings: list[Finding] = []
    seen_pairs: set[tuple[str, str]] = set()

    for (file_a, file_b), co_changes in pair_counts.items():
        if co_changes < 5:
            continue

        # Skip test↔source pairs
        a_is_test = _is_test_file(file_a)
        b_is_test = _is_test_file(file_b)
        if a_is_test != b_is_test:
            continue

        count_a = file_commit_counts.get(file_a, 0)
        count_b = file_commit_counts.get(file_b, 0)
        min_count = min(count_a, count_b)
        if min_count == 0:
            continue

        coupling_ratio = co_changes / min_count
        if coupling_ratio < 0.7:
            continue

        # Check import relationship
        if _files_have_import(file_a, file_b, ctx):
            continue

        # Check expected-coupling config
        if _is_expected_coupling(file_a, file_b, ctx.expected_coupling):
            continue

        pair = (file_a, file_b)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        findings.append(
            Finding(
                file=file_a,
                line=1,
                check="change-coupling",
                message=(
                    f"{file_a} and {file_b} changed together in "
                    f"{co_changes}/{min_count} commits (last {history.window}) "
                    f"with no import relationship — hidden coupling"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _files_have_import(file_a: str, file_b: str, ctx: AnalysisContext) -> bool:
    """Check if either file imports the other."""
    mod_a = _filepath_to_module(file_a)
    mod_b = _filepath_to_module(file_b)

    tree_a = ctx.all_trees.get(Path(file_a))
    tree_b = ctx.all_trees.get(Path(file_b))

    if tree_a is not None and _tree_imports_module(tree_a, mod_b):
        return True
    if tree_b is not None and _tree_imports_module(tree_b, mod_a):
        return True
    return False


def _filepath_to_module(filepath: str) -> str:
    """Convert billing/invoice.py -> billing.invoice."""
    p = Path(filepath)
    parts = list(p.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = p.stem
    return ".".join(parts)


def _tree_imports_module(tree: ast.Module, module: str) -> bool:
    """Check if an AST tree imports from the given module (or a parent)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == module or node.module.startswith(module + "."):
                return True
            # Also check if module starts with the import (e.g., importing parent)
            if module.startswith(node.module + "."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(module + "."):
                    return True
    return False


@check(
    "growth-trajectory",
    severity=Severity.LOW,
    category="git-history",
    description="Files growing rapidly within the time window",
)
def check_growth_trajectory(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)
        current_lines = _get_line_count(file_str, ctx.all_trees)
        if current_lines < 100:
            continue

        stats = history.file_stats.get(file_str)
        if stats is None:
            continue

        net_change = stats.total_insertions - stats.total_deletions
        start_lines = current_lines - net_change

        # Skip new files (start <= 0)
        if start_lines <= 0:
            continue

        growth = current_lines - start_lines
        if growth < 200:
            continue

        ratio = current_lines / start_lines
        if ratio < 2.0:
            continue

        pct = int((ratio - 1) * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="growth-trajectory",
                message=(
                    f"{file_str} grew from ~{start_lines} to {current_lines} lines "
                    f"(+{pct}%) in last {history.window} — accelerating accumulation "
                    f"of responsibilities"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "churn-without-growth",
    severity=Severity.LOW,
    category="git-history",
    description="Many commits but stable/shrinking line count — wrong abstractions",
)
def check_churn_without_growth(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        # Skip test files
        if _is_test_file(file_str):
            continue

        current_lines = _get_line_count(file_str, ctx.all_trees)
        if current_lines < 50:
            continue

        stats = history.file_stats.get(file_str)
        if stats is None:
            continue

        if stats.commit_count < 10:
            continue

        net_growth = stats.total_insertions - stats.total_deletions
        # Flag if net growth <= 10% of current line count
        if net_growth > current_lines * 0.1:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="churn-without-growth",
                message=(
                    f"{file_str} — {stats.commit_count} commits but only "
                    f"+{net_growth} net lines ({current_lines} lines total) "
                    f"in last {history.window} — code is being rewritten, "
                    f"not extended"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "yo-yo-code",
    severity=Severity.MEDIUM,
    category="git-history",
    description="High gross churn — code being written, deleted, rewritten repeatedly",
)
def check_yo_yo_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        current_lines = _get_line_count(file_str, ctx.all_trees)
        if current_lines < 100:
            continue

        stats = history.file_stats.get(file_str)
        if stats is None:
            continue

        if stats.commit_count < 5:
            continue

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


# --- Semantic checks (Tier 2 — require structured commit messages) ---


def _semantic_guard(history: GitHistory | None) -> GitHistory | None:
    """Return the history object if semantic checks should run, else None."""
    if history is None:
        return None
    if history.message_quality < _MIN_MESSAGE_QUALITY:
        return None
    return history


@check(
    "bug-magnet",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files where a majority of commits are fixes — recurring problems",
)
def check_bug_magnet(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        commits = history.commits_for_file.get(file_str, [])
        if len(commits) < 5:
            continue

        fix_count = sum(1 for c in commits if "fix" in classify_commit(c.message))
        fix_ratio = fix_count / len(commits)
        if fix_ratio < 0.5:
            continue

        pct = int(fix_ratio * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="bug-magnet",
                message=(
                    f"{file_str} — {fix_count} of {len(commits)} commits "
                    f"({pct}%) are fixes — recurring problems suggest a "
                    f"structural issue worth redesigning"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "fix-propagation",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files that co-change in fix commits — fixing one tends to break the other",
)
def check_fix_propagation(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []

    analyzed_files = {str(p) for p in ctx.all_trees}

    # Count co-changes in fix commits only
    pair_counts: dict[tuple[str, str], int] = {}
    file_fix_counts: dict[str, int] = {}

    for commit in history._commits:
        if "fix" not in classify_commit(commit.message):
            continue

        py_files = sorted(
            f
            for f in commit.files
            if f.endswith(".py")
            and f in analyzed_files
            and Path(f).name != "__init__.py"
            and not _is_migration_file(f)
        )

        if len(py_files) >= 20:
            continue

        for f in py_files:
            file_fix_counts[f] = file_fix_counts.get(f, 0) + 1

        for i in range(len(py_files)):
            for j in range(i + 1, len(py_files)):
                pair = (py_files[i], py_files[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    findings: list[Finding] = []

    for (file_a, file_b), co_fixes in pair_counts.items():
        if co_fixes < 3:
            continue

        # Skip test↔source pairs
        if _is_test_file(file_a) != _is_test_file(file_b):
            continue

        count_a = file_fix_counts.get(file_a, 0)
        count_b = file_fix_counts.get(file_b, 0)
        min_count = min(count_a, count_b)
        if min_count == 0:
            continue

        ratio = co_fixes / min_count
        if ratio < 0.6:
            continue

        if _is_expected_coupling(file_a, file_b, ctx.expected_coupling):
            continue

        findings.append(
            Finding(
                file=file_a,
                line=1,
                check="fix-propagation",
                message=(
                    f"{file_a} and {file_b} — {co_fixes} of {min_count} fix "
                    f"commits touch both files — fixing one tends to break "
                    f"the other"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "conscious-debt",
    severity=Severity.LOW,
    category="git-history",
    description="Commits that explicitly acknowledge technical debt",
)
def check_conscious_debt(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []

    analyzed_files = {str(p) for p in ctx.all_trees}

    # Group debt commits by file, keeping the most recent
    file_debt: dict[str, list[tuple[str, str, str]]] = {}

    for commit in history._commits:
        if "debt" not in classify_commit(commit.message):
            continue

        for filepath in commit.files:
            if filepath not in analyzed_files:
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
            detail = (
                f"{count} debt commits, most recent: {commit_hash} " f'"{message}" ({date_str})'
            )

        findings.append(
            Finding(
                file=filepath,
                line=1,
                check="conscious-debt",
                message=(f"{filepath} — {detail} — acknowledged debt, is it " f"still needed?"),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "divergent-change",
    severity=Severity.MEDIUM,
    category="git-history",
    description="One file appearing in commits with very different purposes",
)
def check_divergent_change(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if file_path.name in _SKIP_NAMES or file_path.name.endswith(_SKIP_SUFFIXES):
            continue

        current_lines = _get_line_count(file_str, ctx.all_trees)
        if current_lines < 50:
            continue

        commits = history.commits_for_file.get(file_str, [])
        if not commits:
            continue

        # Collect scopes from conventional commit messages
        scopes: dict[str, int] = {}
        for commit in commits:
            scope = _extract_scope(commit.message)
            if scope:
                scopes[scope] = scopes.get(scope, 0) + 1

        # Need 4+ distinct scopes with 2+ commits each
        significant_scopes = {s: n for s, n in scopes.items() if n >= 2}
        if len(significant_scopes) < 4:
            continue

        scope_list = ", ".join(sorted(significant_scopes.keys()))
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="divergent-change",
                message=(
                    f"{file_str} appears in commits for "
                    f"{len(significant_scopes)} different concerns "
                    f"({scope_list}) — consider splitting responsibilities"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _extract_scope(message: str) -> str | None:
    """Extract scope from conventional commit: 'fix(auth): ...' -> 'auth'."""
    colon_pos = message.find(":")
    if colon_pos == -1:
        return None
    prefix = message[:colon_pos]
    paren_open = prefix.find("(")
    paren_close = prefix.find(")")
    if paren_open != -1 and paren_close > paren_open:
        return prefix[paren_open + 1 : paren_close].strip().lower()
    return None


# --- Phase 10e-f checks ---


@check(
    "knowledge-silo",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files where one author dominates all changes — bus-factor risk",
)
def check_knowledge_silo(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        authors = history.authors_for_file.get(file_str)
        if not authors:
            continue

        total = sum(authors.values())
        if total < 5:
            continue

        max_author = max(authors, key=authors.get)  # type: ignore[arg-type]
        max_count = authors[max_author]
        dominance = max_count / total

        if dominance < 0.8:
            continue

        pct = int(dominance * 100)
        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="knowledge-silo",
                message=(
                    f"{file_str} — {max_author} authored {max_count} of "
                    f"{total} commits ({pct}%) — bus-factor risk, "
                    f"consider knowledge sharing"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "emergency-hotspots",
    severity=Severity.LOW,
    category="git-history",
    description="Files that attract disproportionate emergency/hotfix activity",
)
def check_emergency_hotspots(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    # Compute project-wide emergency rate to skip if globally high
    total_commits = len(history._commits)
    if total_commits == 0:
        return []
    total_emergency = sum(1 for c in history._commits if "emergency" in classify_commit(c.message))
    project_rate = total_emergency / total_commits
    if project_rate > 0.3:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        commits = history.commits_for_file.get(file_str, [])
        if not commits:
            continue

        emergency_count = sum(1 for c in commits if "emergency" in classify_commit(c.message))
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
    category="git-history",
    description="Files with heavy fix/feature activity but zero refactoring",
)
def check_no_refactoring(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        current_lines = _get_line_count(file_str, ctx.all_trees)
        if current_lines < 50:
            continue

        commits = history.commits_for_file.get(file_str, [])
        if len(commits) < 8:
            continue

        fix_count = 0
        feature_count = 0
        refactor_count = 0
        for c in commits:
            cats = classify_commit(c.message)
            if "fix" in cats:
                fix_count += 1
            if "feature" in cats:
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


# --- Time-slice based checks ---


@check(
    "fix-follows-feature",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Features that reliably produce fix commits shortly after",
)
def check_fix_follows_feature(ctx: AnalysisContext) -> list[Finding]:
    history = _semantic_guard(ctx.git_history)
    if history is None:
        return []
    if history.is_coarse_grained:
        return []

    slices = history.time_slices
    if len(slices) < 4:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        # Find feature-then-fix pairs within 2 periods
        pairs = 0
        for i, ts in enumerate(slices):
            feature_files = ts.files_by_category.get("feature", set())
            if file_str not in feature_files:
                continue
            # Look ahead up to 2 slices for fix activity
            for j in range(i + 1, min(i + 3, len(slices))):
                fix_files = slices[j].files_by_category.get("fix", set())
                if file_str in fix_files:
                    pairs += 1
                    break

        if pairs < 3:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="fix-follows-feature",
                message=(
                    f"{file_str} — {pairs} feature→fix sequences in last "
                    f"{history.window} — features are reliably followed "
                    f"by fixes, consider more thorough testing or design review"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "stabilization-failure",
    severity=Severity.LOW,
    category="git-history",
    description="Files that repeatedly burst with activity, go quiet, then burst again",
)
def check_stabilization_failure(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    slices = history.time_slices
    if len(slices) < 6:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        commits = history.commits_for_file.get(file_str, [])
        if len(commits) < 8:
            continue

        # Mark which slices are active for this file
        active = [file_str in ts.files_touched for ts in slices]

        # Count bursts: 2+ consecutive active slices separated by 3+ inactive
        bursts = 0
        i = 0
        while i < len(active):
            # Find start of a burst (2+ active)
            if active[i]:
                burst_len = 0
                while i < len(active) and active[i]:
                    burst_len += 1
                    i += 1
                if burst_len >= 2:
                    bursts += 1
                    # Now look for gap (3+ inactive)
                    gap_len = 0
                    while i < len(active) and not active[i]:
                        gap_len += 1
                        i += 1
                    if gap_len < 3:
                        # Not a real gap — merge with next burst
                        continue
                else:
                    i += 1
            else:
                i += 1

        if bursts < 3:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="stabilization-failure",
                message=(
                    f"{file_str} — {bursts} activity bursts in last "
                    f"{history.window} — code is repeatedly destabilized "
                    f"rather than converging"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "hotspot-acceleration",
    severity=Severity.MEDIUM,
    category="git-history",
    description="Files whose change frequency is increasing over time",
)
def check_hotspot_acceleration(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    slices = history.time_slices
    if len(slices) < 4:
        return []

    mid = len(slices) // 2
    first_half = slices[:mid]
    second_half = slices[mid:]

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if _is_test_file(file_str):
            continue

        commits = history.commits_for_file.get(file_str, [])
        if len(commits) < 5:
            continue

        # Count commits per slice for this file
        first_counts = []
        for ts in first_half:
            count = sum(1 for c in ts.commits if file_str in c.files)
            first_counts.append(count)

        second_counts = []
        for ts in second_half:
            count = sum(1 for c in ts.commits if file_str in c.files)
            second_counts.append(count)

        first_median = median(first_counts) if first_counts else 0
        second_median = median(second_counts) if second_counts else 0

        # Need meaningful activity in second half
        second_total = sum(second_counts)
        if second_total < 3:
            continue

        # Second half must be >= 2x first half
        if first_median == 0:
            # If first half was silent but second half is active, that's acceleration
            if second_median < 2:
                continue
        elif second_median < first_median * 2:
            continue

        findings.append(
            Finding(
                file=file_str,
                line=1,
                check="hotspot-acceleration",
                message=(
                    f"{file_str} — change frequency accelerating: "
                    f"median {first_median:.0f}→{second_median:.0f} "
                    f"commits per period in last {history.window} — "
                    f"emerging hotspot"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
