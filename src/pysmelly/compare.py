"""`--compare REF`: run the same checks at another git ref and report the delta.

The ref is checked out into a temporary, detached `git worktree` that this
module creates and always removes, so the comparison writes no state file and
leaves nothing registered with the repository. Findings are matched across the
two trees on (check, file, enclosing definition), never on line number: an
edit above a finding moves its line without changing what it is about.
"""

import ast
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pysmelly.discovery import (
    GitNotFoundError,
    GitRefError,
    get_git_root,
    git_worktree,
    resolve_commit,
)
from pysmelly.registry import Finding

# The definition name used for a finding that sits outside every def and class.
MODULE_SCOPE = "<module>"


class CompareError(Exception):
    """The comparison cannot be made: no git repository, a bad ref, or a missing target."""


@dataclass(frozen=True)
class Analysis:
    """One run of the checks: the parsed trees, keyed as findings name files, and the findings."""

    trees: dict[Path, ast.Module]
    findings: list[Finding]


# Runs the selected checks over the given target roots.
Analyze = Callable[[list[Path]], Analysis]


@dataclass(frozen=True)
class Located:
    """A finding together with the definition it sits in."""

    finding: Finding
    definition: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.finding.check, self.finding.file, self.definition)


@dataclass(frozen=True)
class Comparison:
    """The checks' findings at REF and now, and which ones went away or appeared."""

    ref: str
    sha: str
    before: Analysis
    after: Analysis
    resolved: list[Located]
    new: list[Located]


@contextmanager
def ref_worktree(git_root: Path, sha: str) -> Iterator[Path]:
    """Check `sha` out into a temporary detached worktree; remove it on every exit.

    The directory is created here and nowhere else, so removing it with
    `git worktree remove --force` can only ever discard this checkout.
    The directory is deleted even when `git worktree remove` fails.

    Raises:
        GitRefError: the worktree could not be added or removed.
    """
    path = Path(tempfile.mkdtemp(prefix="pysmelly-compare-"))
    try:
        _add_worktree(git_root, path, sha)
        try:
            yield path
        finally:
            git_worktree(["remove", "--force", str(path)], git_root)
    finally:
        if path.exists():
            shutil.rmtree(path)


def _add_worktree(git_root: Path, path: Path, sha: str) -> None:
    """Add the detached worktree at `path`, leaving nothing registered if that is cut short.

    `add` can raise after git registered the worktree: an interrupt that
    lands once git is done but before the call returns. So on any exception
    git is asked whether `path` is registered, and a registration is removed
    before the exception goes on.
    """
    try:
        git_worktree(["add", "--detach", "--quiet", str(path), sha], git_root)
    except BaseException:
        if _is_registered(git_root, path):
            git_worktree(["remove", "--force", str(path)], git_root)
        raise


def _is_registered(git_root: Path, path: Path) -> bool:
    """Whether the repository at `git_root` lists `path` as one of its worktrees."""
    listing = git_worktree(["list", "--porcelain"], git_root)
    return f"worktree {path.resolve()}" in listing.splitlines()


def _ref_roots(roots: list[Path], git_root: Path, tree: Path, ref: str) -> list[Path]:
    """Map each target root onto the checkout of REF at `tree`."""
    mapped = []
    for root in roots:
        try:
            rel = root.relative_to(git_root)
        except ValueError as err:
            msg = f"{root} is not inside the git repository at {git_root}"
            raise CompareError(msg) from err
        ref_root = tree / rel
        if not ref_root.is_dir():
            msg = f"cannot compare against '{ref}': {rel or '.'} does not exist there"
            raise CompareError(msg)
        mapped.append(ref_root)
    return mapped


def run_comparison(ref: str, roots: list[Path], analyze: Analyze) -> Comparison:
    """Analyse `roots` now and at `ref`, and match the two sets of findings.

    Raises:
        CompareError: the roots are not in a git repository, `ref` names no
            commit, or a root does not exist at `ref`.
    """
    try:
        git_root = get_git_root(roots[0]).resolve()
    except GitNotFoundError as err:
        msg = f"--compare requires a git repository; {roots[0]} is not in one"
        raise CompareError(msg) from err
    try:
        sha = resolve_commit(ref, git_root)
        with ref_worktree(git_root, sha) as tree:
            before = analyze(_ref_roots(roots, git_root, tree, ref))
    except GitRefError as err:
        raise CompareError(str(err)) from err
    after = analyze(roots)
    resolved, new = match_findings(before, after)
    return Comparison(ref, sha, before, after, resolved, new)


def _definition_spans(tree: ast.Module) -> list[tuple[int, int, str]]:
    """(first line, last line, dotted name) of every def and class in `tree`.

    A definition starts at its first decorator, so a finding on a decorator
    line belongs to the definition it decorates.
    """
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                spans.append((start, child.end_lineno or child.lineno, name))
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return spans


def _locate(analysis: Analysis) -> list[Located]:
    """Pair each finding with the innermost definition containing its line."""
    spans_by_file = {str(path): _definition_spans(tree) for path, tree in analysis.trees.items()}
    located = []
    for finding in analysis.findings:
        containing = [
            (start, name)
            for start, end, name in spans_by_file.get(finding.file, [])
            if start <= finding.line <= end
        ]
        # Nested definitions start after the ones enclosing them.
        definition = max(containing)[1] if containing else MODULE_SCOPE
        located.append(Located(finding, definition))
    return located


def _sort_key(item: Located) -> tuple[str, str, str, int, str]:
    f = item.finding
    return (f.file, f.check, item.definition, f.line, f.message)


def match_findings(before: Analysis, after: Analysis) -> tuple[list[Located], list[Located]]:
    """Findings present only at REF (resolved) and only now (new).

    Findings match on (check, file, enclosing definition) as a multiset. Within
    one key, findings whose message is unchanged are paired first, so when a
    function had two findings and lost one, the one reported resolved is the
    one whose message no longer appears.
    """
    groups: dict[tuple[str, str, str], tuple[list[Located], list[Located]]] = {}
    for item in sorted(_locate(before), key=_sort_key):
        groups.setdefault(item.key, ([], []))[0].append(item)
    for item in sorted(_locate(after), key=_sort_key):
        groups.setdefault(item.key, ([], []))[1].append(item)

    resolved: list[Located] = []
    new: list[Located] = []
    for old_items, new_items in groups.values():
        unmatched_messages = Counter(i.finding.message for i in new_items)
        unmatched_messages.subtract(i.finding.message for i in old_items)
        old_left = _drop_matched(old_items, -unmatched_messages)
        new_left = _drop_matched(new_items, +unmatched_messages)
        paired = min(len(old_left), len(new_left))
        resolved.extend(old_left[paired:])
        new.extend(new_left[paired:])
    return sorted(resolved, key=_sort_key), sorted(new, key=_sort_key)


def _drop_matched(items: list[Located], surplus: Counter[str]) -> list[Located]:
    """The items whose message is in `surplus`, as many of each as it counts."""
    remaining = Counter(surplus)
    kept = []
    for item in items:
        if remaining[item.finding.message] > 0:
            remaining[item.finding.message] -= 1
            kept.append(item)
    return kept


def _count_rows(before: Counter[str], after: Counter[str]) -> list[tuple[str, int, int]]:
    """(name, count at REF, count now) for every name whose count changed."""
    return [
        (name, before[name], after[name])
        for name in sorted(before.keys() | after.keys())
        if before[name] != after[name]
    ]


def _direction(then: int, now: int) -> str:
    return f"down from {then}" if now < then else f"up from {then}"


def format_comparison(cmp: Comparison, *, summary: bool) -> list[str]:
    """The comparison report: totals, per-check and per-file deltas, then the findings.

    `summary` keeps the counts and drops the lists of resolved and new findings.
    """
    then, now = len(cmp.before.findings), len(cmp.after.findings)
    label = f"{cmp.ref} ({cmp.sha[:7]})"
    lines = [
        f"Parsed {len(cmp.after.trees)} Python files now, {len(cmp.before.trees)} at {label}",
        "",
    ]
    if not cmp.resolved and not cmp.new:
        lines.append(f"No change against {label}: {now} finding(s) at both.")
        return lines

    lines.append(f"Compared against {label}: {then} finding(s) then, {now} now ({now - then:+d})")
    lines.append("")

    check_rows = _count_rows(
        Counter(f.check for f in cmp.before.findings),
        Counter(f.check for f in cmp.after.findings),
    )
    if check_rows:
        lines.append("=== per check ===")
        for name, old, cur in check_rows:
            lines.append(f"  {name:<30} {old} -> {cur}  ({cur - old:+d})")
        lines.append("")

    file_rows = _count_rows(
        Counter(f.file for f in cmp.before.findings),
        Counter(f.file for f in cmp.after.findings),
    )
    if file_rows:
        lines.append("=== per file ===")
        for name, old, cur in file_rows:
            fixed = Counter(i.finding.check for i in cmp.resolved if i.finding.file == name)
            detail = ", ".join(f"{check} x{n}" for check, n in sorted(fixed.items()))
            suffix = f"; fixed: {detail}" if detail else ""
            lines.append(f"  {name}: {cur} finding(s), {_direction(old, cur)}{suffix}")
        lines.append("")

    if summary:
        lines.append(f"Resolved: {len(cmp.resolved)}, new: {len(cmp.new)}")
        return lines

    if cmp.resolved:
        lines.append(f"=== resolved since {cmp.ref} ({len(cmp.resolved)}) ===")
        for item in cmp.resolved:
            f = item.finding
            lines.append(
                f"  {f.file}: {f.check} in {item.definition} (was line {f.line}): {f.message}"
            )
        lines.append("")
    if cmp.new:
        lines.append(f"=== new since {cmp.ref} ({len(cmp.new)}) ===")
        for item in cmp.new:
            f = item.finding
            lines.append(f"  {f.file}:{f.line}: {f.check} in {item.definition}: {f.text}")
        lines.append("")
    return lines
