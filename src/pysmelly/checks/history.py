"""Git history checks — detect evolutionary signals invisible to static analysis."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

# Files that are naturally stable and shouldn't be flagged
_SKIP_NAMES = frozenset({"__init__.py", "conftest.py"})

# Files that match these patterns are config-like and naturally stable
_SKIP_SUFFIXES = ("_config.py", "_settings.py", "settings.py", "config.py")

_MONTHS_STALE = 12
_MONTHS_ACTIVE = 6


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
    description="Files untouched 12+ months while directory peers keep evolving",
)
def check_abandoned_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    now = datetime.now(timezone.utc)
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

        # Collect last-modified dates for files that have git history
        file_dates: dict[str, datetime] = {}
        for f in files:
            last_mod = history.last_modified.get(f)
            if last_mod is not None:
                file_dates[f] = last_mod

        if not file_dates:
            continue

        # Compute median last-modified for the directory
        dates = list(file_dates.values())
        median_timestamp = median(d.timestamp() for d in dates)
        median_date = datetime.fromtimestamp(median_timestamp, tz=timezone.utc)

        # If the directory median is old (peers aren't active), skip
        months_since_median = (now - median_date).days / 30.44
        if months_since_median >= _MONTHS_ACTIVE:
            continue

        # Find stale files in active directories
        for f in files:
            name = Path(f).name
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue

            last_mod = file_dates.get(f)
            if last_mod is None:
                # File not in git history (new/untracked) — skip
                continue

            months_stale = (now - last_mod).days / 30.44
            if months_stale < _MONTHS_STALE:
                continue

            # Count active peers
            active_peers = sum(
                1
                for peer_date in file_dates.values()
                if (now - peer_date).days / 30.44 < _MONTHS_ACTIVE
            )
            total_peers = len(files)

            date_str = last_mod.strftime("%Y-%m-%d")
            months_ago = int(months_stale)
            findings.append(
                Finding(
                    file=f,
                    line=1,
                    check="abandoned-code",
                    message=(
                        f"{f} last modified {date_str} ({months_ago} months ago), "
                        f"but {active_peers} of {total_peers} peers in {dir_name}/ "
                        f"changed in last {_MONTHS_ACTIVE} months"
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
