"""Git history checks — team and ownership signals."""

from __future__ import annotations

from pathlib import Path

from pysmelly.checks.history_helpers import (
    MIN_LINES_MEDIUM,
    MIN_LINES_SMALL,
    get_line_count,
    has_semantic_history,
    is_naturally_stable,
    notes_persisting_review,
    source_files,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import GIT_HISTORY_CATEGORY, Advice, Finding, Severity, check

# Directories that represent project structure, not business concerns
# (used to filter the divergent-change directory fallback). Test code is
# left out by its role instead, wherever it lives.
_STRUCTURAL_DIRS = frozenset(
    {
        "docs",
        "docs_src",
        "documentation",
        "scripts",
        "bin",
        "tools",
        "examples",
        "samples",
        "benchmarks",
        "dev",
    }
)


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


@check(
    "abandoned-code",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Files on disk with no commits in the window while directory peers keep evolving",
    advice=Advice(
        fix=(
            "Check whether anything still uses the file: delete it if nothing does, or bring"
            " it in line with its peers' recent changes."
        ),
        dismiss=(
            "Dismiss only if none of its peers' commits in the window would have changed it"
            " had it been included; then mark the file reviewed."
        ),
    ),
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

        # Need a strong majority of peers to be active for this to be meaningful
        if len(active_files) < len(files) * 2 / 3:
            continue

        for f in untouched_files:
            if is_naturally_stable(Path(f)):
                continue
            if ctx.roles.is_test(f):
                continue

            line_count = get_line_count(f, ctx.all_trees)
            if line_count < MIN_LINES_SMALL:
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
    "knowledge-silo",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Files where one author dominates all changes — bus-factor risk",
    advice=Advice(
        fix=(
            "Have another contributor review or make the next change to the file, and write"
            " down what only its main author knows."
        ),
        dismiss=(
            "Dismiss only if the file is generated or vendored, so its authorship does not"
            " reflect knowledge."
        ),
    ),
)
@notes_persisting_review
def check_knowledge_silo(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    # Bus-factor is meaningless for solo or duo projects
    if history.distinct_authors < 3:
        return []

    findings: list[Finding] = []

    for _file_path, file_str in source_files(ctx):
        # Compute authors from post-review commits
        commits = history.commits_since_review(file_str)
        if not commits:
            continue
        authors: dict[str, int] = {}
        for c in commits:
            if c.author:
                authors[c.author] = authors.get(c.author, 0) + 1
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
                    f"{total} commits ({pct}%) — bus-factor risk"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


@check(
    "divergent-change",
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="One file appearing in commits with very different purposes",
    advice=Advice(
        fix="Split the file along the concerns listed, one module per concern.",
        dismiss=(
            "Dismiss only if the file is an integration point every concern must edit by"
            " design (a URL table, a settings module, a registry)."
        ),
    ),
)
@notes_persisting_review
def check_divergent_change(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if not has_semantic_history(history):
        return []

    findings: list[Finding] = []

    for file_path, file_str in source_files(ctx):
        if is_naturally_stable(file_path):
            continue

        current_lines = get_line_count(file_str, ctx.all_trees)
        if current_lines < MIN_LINES_MEDIUM:
            continue

        commits = history.commits_since_review(file_str)
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
            # Fallback: infer scope from co-changed file directories. A
            # co-changed test is the change's test, not another concern; a
            # deleted file takes the role its path has (AnalysisContext.file_test_role).
            own_top_dir = file_path.parts[0] if len(file_path.parts) >= 2 else ""
            dir_scopes: dict[str, int] = {}
            for commit in commits:
                dirs = set()
                for f in commit.python_files:
                    if f != file_str and not ctx.roles.is_test(f):
                        parts = Path(f).parts
                        if len(parts) >= 2:
                            d = parts[0]
                            if d != own_top_dir and d not in _STRUCTURAL_DIRS:
                                dirs.add(d)
                for d in dirs:
                    dir_scopes[d] = dir_scopes.get(d, 0) + 1
            significant_dir_scopes = {s: n for s, n in dir_scopes.items() if n >= 2}
            if len(significant_dir_scopes) >= 4:
                significant_scopes = significant_dir_scopes
            else:
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
                    f"({scope_list})"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
