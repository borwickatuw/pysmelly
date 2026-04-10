"""Git history checks — coupling, debt, and structural signals."""

from __future__ import annotations

import ast
from pathlib import Path
from statistics import median

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.history_helpers import (
    CATEGORY,
    COCHANGE_SKIP_THRESHOLD,
    MIN_LINES_MEDIUM,
    SKIP_NAMES,
    SKIP_SUFFIXES,
    churned_files,
    coupling_ratio,
    get_line_count,
    is_bulk_commit,
    is_expected_coupling,
    is_test_file,
    semantic_guard,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import classify_commit
from pysmelly.registry import Finding, Severity, check


def _collapse_by_directory(findings: list[Finding], min_cluster: int = 3) -> list[Finding]:
    """Collapse findings in the same directory into a single package-level finding.

    If 3+ files in the same directory are flagged, replace individual findings
    with one summary finding for the directory. Standalone findings are kept.
    """
    dir_findings: dict[str, list[Finding]] = {}
    for f in findings:
        parent = str(Path(f.file).parent)
        dir_findings.setdefault(parent, []).append(f)

    result: list[Finding] = []
    for dir_name, group in dir_findings.items():
        if len(group) >= min_cluster:
            files_list = ", ".join(Path(f.file).name for f in group)
            result.append(
                Finding(
                    file=dir_name,
                    line=1,
                    check=group[0].check,
                    message=(
                        f"{dir_name}/ — {len(group)} files in this package "
                        f"flagged ({files_list}) — tightly-coupled subsystem"
                    ),
                    severity=group[0].severity,
                )
            )
        else:
            result.extend(group)
    return result


def _collapse_coupling_by_directory(findings: list[Finding], min_cluster: int = 3) -> list[Finding]:
    """Collapse change-coupling findings where both files share a directory."""
    intra_dir: dict[str, list[Finding]] = {}
    cross_dir: list[Finding] = []
    for f in findings:
        # Extract the two files from the message (file_a is f.file)
        file_a = f.file
        # file_b is the second file — extract from the pair in the message
        parts = f.message.split(" and ", 1)
        if len(parts) < 2:
            cross_dir.append(f)
            continue
        file_b = parts[1].split(" changed")[0]
        dir_a = str(Path(file_a).parent)
        dir_b = str(Path(file_b).parent)
        if dir_a == dir_b:
            intra_dir.setdefault(dir_a, []).append(f)
        else:
            cross_dir.append(f)

    result = list(cross_dir)
    for dir_name, group in intra_dir.items():
        if len(group) >= min_cluster:
            # Collect all unique files in this directory's coupling cluster
            all_files: set[str] = set()
            for f in group:
                all_files.add(f.file)
                parts = f.message.split(" and ", 1)
                if len(parts) >= 2:
                    all_files.add(parts[1].split(" changed")[0])
            files_list = ", ".join(Path(f).name for f in sorted(all_files))
            result.append(
                Finding(
                    file=dir_name,
                    line=1,
                    check="change-coupling",
                    message=(
                        f"{dir_name}/ — {len(all_files)} files with "
                        f"{len(group)} coupling pairs ({files_list}) "
                        f"— tightly-coupled subsystem"
                    ),
                    severity=group[0].severity,
                )
            )
        else:
            result.extend(group)
    return result


def _files_have_import(file_a: str, file_b: str, ctx: AnalysisContext) -> bool:
    """Check if either file imports the other."""
    mod_a = _filepath_to_module(file_a)
    mod_b = _filepath_to_module(file_b)
    pkg_a = _filepath_to_package(file_a)
    pkg_b = _filepath_to_package(file_b)

    tree_a = ctx.all_trees.get(Path(file_a))
    tree_b = ctx.all_trees.get(Path(file_b))

    if tree_a is not None and _tree_imports_module(tree_a, mod_b, pkg_a):
        return True
    if tree_b is not None and _tree_imports_module(tree_b, mod_a, pkg_b):
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


def _filepath_to_package(filepath: str) -> str:
    """Convert billing/invoice.py -> billing (the containing package)."""
    p = Path(filepath)
    parts = list(p.parent.parts)
    return ".".join(parts) if parts else ""


def _resolve_relative_import(node_module: str | None, level: int, package: str) -> str:
    """Resolve a relative import to an absolute module path.

    E.g., `from .utils import x` in package `havoc.works.models`
    -> `havoc.works.models.utils`
    """
    if level == 0 or not package:
        return node_module or ""
    # Go up `level - 1` packages (level=1 means current package)
    parts = package.split(".")
    if level > 1:
        parts = parts[: -(level - 1)] if level - 1 < len(parts) else []
    base = ".".join(parts)
    if node_module:
        return f"{base}.{node_module}" if base else node_module
    return base


def _tree_imports_module(tree: ast.Module, module: str, package: str = "") -> bool:
    """Check if an AST tree imports from the given module (or a parent)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Resolve relative imports (from .utils import ...)
            if node.level and node.level > 0:
                resolved = _resolve_relative_import(node.module, node.level, package)
            elif node.module:
                resolved = node.module
            else:
                continue
            if resolved == module or resolved.startswith(module + "."):
                return True
            if module.startswith(resolved + "."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(module + "."):
                    return True
    return False


def _find_test_file(source_path: str, all_trees: dict) -> str | None:
    """Find the corresponding test file for a source file.

    Searches for test_{stem}.py and {stem}_test.py in several locations:
    the same directory, a sibling tests/ directory, and root tests/.
    """
    p = Path(source_path)
    stem = p.stem
    parent = p.parent

    candidates = [
        str(parent / f"test_{stem}.py"),
        str(parent / f"{stem}_test.py"),
        str(parent / "tests" / f"test_{stem}.py"),
        str(parent / "tests" / f"{stem}_test.py"),
        str(Path("tests") / f"test_{stem}.py"),
        str(Path("tests") / f"{stem}_test.py"),
    ]

    for candidate in candidates:
        if Path(candidate) in all_trees:
            return candidate
    return None


@check(
    "blast-radius",
    severity=Severity.MEDIUM,
    category=CATEGORY,
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
        if is_test_file(file_str):
            continue

        commits = history.commits_since_review(file_str)
        if not commits:
            continue

        # For each commit, count other .py files changed (excluding this file)
        co_change_counts: list[int] = []
        for commit in commits:
            py_files = [f for f in commit.files if f.endswith(".py") and f != file_str]
            # Skip bulk refactors
            if len(py_files) >= COCHANGE_SKIP_THRESHOLD:
                continue
            co_change_counts.append(len(py_files))

        # Need >= 5 qualifying commits
        if len(co_change_counts) < 5:
            continue

        median_co_changes = median(co_change_counts)
        threshold = max(8, int(history.median_commit_size * 2.5))
        if median_co_changes >= threshold:
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

    return _collapse_by_directory(findings)


@check(
    "change-coupling",
    severity=Severity.MEDIUM,
    category=CATEGORY,
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
            and Path(f).name != "__init__.py"
            and not is_migration_file(f)
        )
        # Skip bulk commits
        if len(py_files) >= COCHANGE_SKIP_THRESHOLD:
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
        a_is_test = is_test_file(file_a)
        b_is_test = is_test_file(file_b)
        if a_is_test != b_is_test:
            continue

        ratio = coupling_ratio(file_commit_counts, file_a, file_b, co_changes, threshold=0.7)
        if ratio is None:
            continue

        # Check import relationship
        if _files_have_import(file_a, file_b, ctx):
            continue

        # Check expected-coupling config
        if is_expected_coupling(file_a, file_b, ctx.expected_coupling):
            continue

        pair = (file_a, file_b)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        min_count = min(
            file_commit_counts.get(file_a, 0),
            file_commit_counts.get(file_b, 0),
        )
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

    return _collapse_coupling_by_directory(findings)


@check(
    "yo-yo-code",
    severity=Severity.MEDIUM,
    category=CATEGORY,
    description="High gross churn — code being written, deleted, rewritten repeatedly",
)
def check_yo_yo_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_str, current_lines, stats in churned_files(
        history, ctx, min_lines=100, min_commits=5
    ):
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
    category=CATEGORY,
    description="Commits that explicitly acknowledge technical debt",
)
def check_conscious_debt(ctx: AnalysisContext) -> list[Finding]:
    history = semantic_guard(ctx.git_history)
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
            detail = f'{count} debt commits, most recent: {commit_hash} "{message}" ({date_str})'

        findings.append(
            Finding(
                file=filepath,
                line=1,
                check="conscious-debt",
                message=(f"{filepath} — {detail} — acknowledged debt, is it still needed?"),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "emergency-hotspots",
    severity=Severity.LOW,
    category=CATEGORY,
    description="Files that attract disproportionate emergency/hotfix activity",
)
def check_emergency_hotspots(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    # Compute project-wide emergency rate to skip if globally high
    non_bulk = [c for c in history._commits if not is_bulk_commit(c)]
    total_commits = len(non_bulk)
    if total_commits == 0:
        return []
    total_emergency = sum(1 for c in non_bulk if "emergency" in classify_commit(c.message))
    project_rate = total_emergency / total_commits
    if project_rate > 0.3:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        commits = [c for c in history.commits_since_review(file_str) if not is_bulk_commit(c)]
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
    category=CATEGORY,
    description="Files with heavy fix/feature activity but zero refactoring",
)
def check_no_refactoring(ctx: AnalysisContext) -> list[Finding]:
    history = semantic_guard(ctx.git_history)
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if is_test_file(file_str):
            continue

        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < MIN_LINES_MEDIUM:
            continue

        commits = [c for c in history.commits_since_review(file_str) if not is_bulk_commit(c)]
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


@check(
    "test-erosion",
    severity=Severity.LOW,
    category=CATEGORY,
    description="Source files changing much more often than their tests",
)
def check_test_erosion(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    findings: list[Finding] = []

    for file_path in ctx.all_trees:
        file_str = str(file_path)

        if is_test_file(file_str):
            continue
        if file_path.name in SKIP_NAMES or file_path.name.endswith(SKIP_SUFFIXES):
            continue
        if is_migration_file(file_str):
            continue

        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < MIN_LINES_MEDIUM:
            continue

        source_commits = [
            c for c in history.commits_since_review(file_str) if not is_bulk_commit(c)
        ]
        if len(source_commits) < 5:
            continue

        test_file = _find_test_file(file_str, ctx.all_trees)
        if test_file is None:
            continue

        test_commits = [
            c for c in history.commits_since_review(test_file) if not is_bulk_commit(c)
        ]
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
