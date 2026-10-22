"""Git history checks — commit shape: what the window's commits did to signatures and files.

growing-signatures follows the signatures of functions that are already long
(4+ parameters at HEAD) back through the window and counts the commits that
lengthened them. same-change-multiple-files reads the hunks of commits that
edited three or more files and looks for one change made, shape for shape,
in each of them.

Both read the window's commits, so both honour ``pysmelly: reviewed``
markers: a commit made on or before a file's review date does not count
for that file, and a finding on a reviewed file carries the review note.

Cost (DECISIONS.md, "Commit-shape checks"): each check makes one batched git
call for all of its candidates, plus a ``git show HEAD:path`` for the few
files a same-change finding names. Neither runs git once per commit or per
function.
"""

from __future__ import annotations

import ast
import functools
import keyword
import re
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.history_helpers import (
    COCHANGE_SKIP_THRESHOLD,
    after_review,
    commit_subject,
    commits_by_hash,
    head_path_of,
    notes_persisting_review,
    post_review_commit_counts,
    preview,
    source_files,
    window_commit,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import (
    CommitInfo,
    FunctionRevision,
    GitHistory,
    Hunk,
)
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)

# --- growing-signatures ---

# The bars the plan item sets: 4+ parameters at HEAD, lengthened by 3+ commits.
MIN_HEAD_PARAMS = 4
MIN_GROWING_COMMITS = 3

# A signature longer than this many lines is not read (nothing real is).
_MAX_SIGNATURE_LINES = 25

_DEF_LINE = re.compile(r"^\s*(?:async\s+)?def\s+([^\W\d]\w*)\s*\(")
# A line that can end a ``def`` header: a colon, then at most a comment.
_BLOCK_OPENER = re.compile(r":\s*(?:#.*)?$")
_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _param_names(args: ast.arguments) -> list[str]:
    """Parameters a caller can pass: every named one, ``*args`` and ``**kwargs``.

    A leading ``self`` or ``cls`` is the receiver, not a parameter. It is
    dropped by name, because a signature read from a diff fragment does not
    say whether it was a method.
    """
    positional = [*args.posonlyargs, *args.args]
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    vararg = [args.vararg] if args.vararg is not None else []
    kwarg = [args.kwarg] if args.kwarg is not None else []
    return [arg.arg for arg in (*positional, *vararg, *args.kwonlyargs, *kwarg)]


@dataclass(frozen=True)
class Signature:
    """One function signature read from a fragment: its name and parameters."""

    name: str
    params: tuple[str, ...]


def _parsed_def(text: str) -> _FunctionDef | None:
    """The function ``text`` opens, read with or without its body; None if it does not parse."""
    for candidate in (text, f"{text}\n    pass"):
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        first = tree.body[0] if tree.body else None
        if isinstance(first, _FunctionDef):
            return first
    return None


def _signatures_in(lines: Sequence[str]) -> list[Signature]:
    """Every function signature ``lines`` hold, in order.

    ``lines`` is a fragment (one side of a hunk), so a signature is read
    from its ``def`` line to the first line ending in ``:`` at which it
    parses, body or not (the ``def`` line itself is tried first, for a
    one-line function). A ``def`` whose signature never parses inside the
    fragment is left out rather than guessed.
    """
    found = []
    for start, line in enumerate(lines):
        if _DEF_LINE.match(line) is None:
            continue
        stop = min(len(lines), start + _MAX_SIGNATURE_LINES)
        for end in range(start + 1, stop + 1):
            if end > start + 1 and _BLOCK_OPENER.search(lines[end - 1]) is None:
                continue
            node = _parsed_def(textwrap.dedent("\n".join(lines[start:end])))
            if node is not None:
                found.append(Signature(node.name, tuple(_param_names(node.args))))
                break
    return found


@dataclass(frozen=True)
class _Candidate:
    """A function at HEAD long enough for growing-signatures to follow."""

    path: str
    qualname: str
    line: int
    params: tuple[str, ...]
    signature_end: int

    @property
    def name(self) -> str:
        return self.qualname.rpartition(".")[2]


@dataclass(frozen=True)
class _Resize:
    """One commit's change to a signature: the parameter counts it left before and after."""

    sha: str
    before: int | None
    after: int


def _signature_end(node: _FunctionDef) -> int:
    """The last line before the body (decorators of a first nested definition included)."""
    first = node.body[0]
    body_start = min([first.lineno, *(d.lineno for d in getattr(first, "decorator_list", []))])
    return max(node.lineno, body_start - 1)


def _module_and_class_functions(tree: ast.Module) -> Iterator[tuple[str, _FunctionDef]]:
    """Functions defined at module level or in a class body (nested classes too), by qualname.

    Functions nested in functions are closures over their caller's state;
    their signatures are not an API anyone ratchets, and walking every body
    for them costs more than the rest of the check.
    """
    pending: list[tuple[str, list[ast.stmt]]] = [("", tree.body)]
    while pending:
        prefix, body = pending.pop()
        for node in body:
            if isinstance(node, _FunctionDef):
                yield f"{prefix}{node.name}", node
            elif isinstance(node, ast.ClassDef):
                pending.append((f"{prefix}{node.name}.", node.body))


def _growing_candidates(ctx: AnalysisContext, history: GitHistory) -> Iterator[_Candidate]:
    """Functions with 4+ parameters in files with 3+ (post-review) commits in the window.

    A file's commits include those made under the names it had before a
    rename in the window. Lines are HEAD's: the analyzed tree is used when the working tree
    matches HEAD, and the HEAD blob is parsed when it does not.
    """
    lines = history.line_history
    changed: frozenset[str] | None = None
    for path, file_path, commits in post_review_commit_counts(ctx, history):
        if commits < MIN_GROWING_COMMITS:
            continue
        if changed is None:
            changed = lines.head_changed_paths()
        tree = lines.head_tree(path) if path in changed else ctx.all_trees[file_path]
        if tree is None:
            continue
        for qualname, node in _module_and_class_functions(tree):
            params = _param_names(node.args)
            if len(params) >= MIN_HEAD_PARAMS:
                yield _Candidate(path, qualname, node.lineno, tuple(params), _signature_end(node))


def _attribute(signature: Signature, namesakes: list[_Candidate]) -> _Candidate | None:
    """The candidate a signature in a hunk belongs to, among those sharing its name in the file.

    One namesake is the answer. Several (``__init__`` in two classes) are
    told apart by the parameter names they share with the signature; a tie
    is left unattributed rather than guessed.
    """
    if len(namesakes) == 1:
        return namesakes[0]
    overlap = [len(set(signature.params) & set(c.params)) for c in namesakes]
    best = max(overlap)
    if overlap.count(best) > 1:
        return None
    return namesakes[overlap.index(best)]


def _resizes(
    revisions: Iterable[FunctionRevision],
    candidates: list[_Candidate],
    history: GitHistory,
) -> dict[_Candidate, list[_Resize]]:
    """Each candidate's signature changes, newest first.

    A hunk is attributed to a candidate by the ``def`` it holds. Hunks name
    the file as it was at their commit, and ``git log -L`` does not print a
    rename commit that left the range alone, so the window's renames map
    each older name to its HEAD path.
    """
    by_path: dict[str, dict[str, list[_Candidate]]] = {}
    for candidate in candidates:
        by_path.setdefault(candidate.path, {}).setdefault(candidate.name, []).append(candidate)
    head_paths = head_path_of(history, by_path)
    found: dict[_Candidate, list[_Resize]] = {}
    for revision in revisions:
        seen: set[_Candidate] = set()
        for hunk in revision.hunks:
            head_path = head_paths.get(hunk.file)
            if head_path is None:
                continue
            before = _signatures_in(hunk.old_side)
            for signature in _signatures_in(hunk.new_side):
                namesakes = by_path[head_path].get(signature.name)
                candidate = _attribute(signature, namesakes) if namesakes else None
                if candidate is None or candidate in seen:
                    continue
                seen.add(candidate)
                previous = [s for s in before if s.name == signature.name]
                old = _attribute_back(previous, signature)
                found.setdefault(candidate, []).append(
                    _Resize(revision.sha, old, len(signature.params))
                )
    return found


def _attribute_back(previous: list[Signature], signature: Signature) -> int | None:
    """The parameter count the hunk's old side gave the same function, if it shows it."""
    if not previous:
        return None
    if len(previous) == 1:
        return len(previous[0].params)
    overlap = [len(set(signature.params) & set(s.params)) for s in previous]
    best = max(overlap)
    if overlap.count(best) > 1:
        return None
    return len(previous[overlap.index(best)].params)


def _rises(resizes: list[_Resize]) -> list[tuple[int, _Resize]]:
    """The commits that lengthened a signature, oldest first, each with the count it grew from.

    A hunk whose old side shows no signature (git aligned the change as a
    pure insertion) is compared with the count the previous commit left.
    """
    rises = []
    previous: int | None = None
    for resize in reversed(resizes):
        before = resize.before if resize.before is not None else previous
        if before is not None and resize.after > before:
            rises.append((before, resize))
        previous = resize.after
    return rises


@check(
    "growing-signatures",
    # A design trend with a known fix (a parameter object, a split), the same
    # weight as param-clumps and boolean-param-explosion: fix it unless the
    # parameters are independent options of a public API.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="Functions with 4+ parameters whose signature grew in 3+ commits in the window",
    advice=Advice(
        fix=("Group the parameters that travel together into a dataclass, or split the function."),
        dismiss=(
            "Dismiss only if each parameter is an independent keyword option of a public API"
            " that callers set one at a time."
        ),
    ),
)
@notes_persisting_review
def check_growing_signatures(ctx: AnalysisContext) -> list[Finding]:
    """Flag a long signature that keeps getting longer.

    Each commit that left a function with more parameters than it found is
    one step of the ratchet; three in the window is a habit, not an
    accident. Revisions whose signature does not parse are skipped, not
    guessed at.
    """
    history = ctx.git_history
    if history is None:
        return []
    candidates = list(_growing_candidates(ctx, history))
    if not candidates:
        return []
    commits = commits_by_hash(history, head_path_of(history, {c.path for c in candidates}))
    findings = []
    ranges = [(c.path, c.line, c.signature_end) for c in candidates]
    revisions = history.line_history.ranges_history(ranges)
    for candidate, resizes in _resizes(revisions, candidates, history).items():
        counted = [
            resize
            for resize in resizes
            if after_review(history, candidate.path, window_commit(commits, resize.sha))
        ]
        rises = _rises(counted)
        if len(rises) < MIN_GROWING_COMMITS:
            continue
        steps = "; ".join(
            f"{before}→{resize.after} {resize.sha[:8]} "
            f'"{commit_subject(window_commit(commits, resize.sha))}"'
            for before, resize in rises
        )
        findings.append(
            Finding(
                file=candidate.path,
                line=candidate.line,
                check="growing-signatures",
                message=(
                    f"{candidate.qualname}() takes {len(candidate.params)} parameters and "
                    f"{len(rises)} commits in the window each lengthened it: {steps}"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings


# --- same-change-multiple-files ---

# The fewest files one shape must reach in one commit (the plan item's bar).
MIN_SAME_CHANGE_FILES = 3

# The fewest significant lines a hunk must add to count. A one- or two-line
# edit repeated across files is what a lockstep API change looks like at its
# call sites (a renamed function, one more argument): the change itself, not
# a copy of it. Three lines is the smallest block with logic of its own,
# a guard, an action and its result (DECISIONS.md, "Commit-shape checks").
MIN_SIGNIFICANT_LINES = 3

# Commits touching COCHANGE_SKIP_THRESHOLD (20) or more Python files are not
# read: a mass rename, reformat or license-header sweep, the same mechanical
# edits co-change analysis already skips.

# One alternative per kind of lexeme, scanned with findall: each match
# fills exactly one group. Whitespace matches nothing and is skipped.
_LEXEME = re.compile(
    r"""
    ([rRbBuUfF]{0,2}(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))
  | ((?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJ]?)
  | ([^\W\d]\w*)
  | (\#.*)
  | (\*\*=?|//=?|->|:=|<<=?|>>=?|[-+*/%@&|^<>=!]=|\S)
    """,
    re.VERBOSE,
)
_CLOSED_TRIPLE = re.compile(r"[rRbBuUfF]{0,2}(\"\"\"|''').*?\1")
_OPEN_TRIPLE = re.compile(r"[rRbBuUfF]{0,2}(\"\"\"|''')")

# Tokens that carry no logic: masked names and literals, and punctuation
# that lists or closes them. A line of nothing else (`)`, `foo,`,
# `"text",`) is layout or data, not a change of behaviour.
_INERT_TOKENS = frozenset({"N", "S", "0", "(", ")", "[", "]", "{", "}", ",", ":", "."})

_IMPORT_LINE = re.compile(r"^(?:from\s+\S+\s+)?import\s")
_VERSION_LINE = re.compile(r"^_*version_*\s*[:=]", re.IGNORECASE)


def _shape_tokens(line: str, open_triple: str | None) -> tuple[list[str], str | None]:
    """A line's tokens with names and literals masked, and the triple quote left open.

    Names become ``N``, strings ``S``, numbers ``0``. Keywords stay, and so
    do the names that say what the code does rather than what it holds: an
    attribute (after a ``.``) and a called name (before a ``(``), except the
    name a ``def`` or ``class`` introduces. Text inside a triple-quoted string
    (a docstring) is dropped; ``open_triple`` carries its delimiter from one
    line to the next.
    """
    lead: list[str] = []
    if open_triple is not None:
        close = line.find(open_triple)
        if close < 0:
            return lead, open_triple
        lead.append("S")
        line = line[close + 3 :]
    line = _CLOSED_TRIPLE.sub('""', line)
    opened = _OPEN_TRIPLE.search(line)
    if opened is not None:
        line = line[: opened.start()] + '""'
    lexemes = _LEXEME.findall(line)
    masked = list(lead)
    for i, (string, number, name, comment, op) in enumerate(lexemes):
        if comment:
            break
        if string:
            masked.append("S")
        elif number:
            masked.append("0")
        elif op:
            masked.append(op)
        else:
            previous = lexemes[i - 1][4] or lexemes[i - 1][2] if i else ""
            following = lexemes[i + 1][4] if i + 1 < len(lexemes) else ""
            keep = keyword.iskeyword(name) or previous == "." or following == "("
            if previous in {"def", "class"}:
                keep = False
            masked.append(name if keep else "N")
    return masked, (opened.group(1) if opened is not None else None)


@dataclass(frozen=True)
class _ShapedLine:
    """A significant line: where it sits in its fragment, its text and its shape."""

    index: int
    text: str
    shape: str


def _significant_lines(lines: Sequence[str]) -> list[_ShapedLine]:
    """The lines of a fragment that carry logic, each with its shape.

    A line is significant unless it is blank, a comment, docstring text, an
    import, a version assignment, or nothing but masked names, literals and
    list punctuation.
    """
    kept: list[_ShapedLine] = []
    open_triple: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        shape, open_triple = _line_shape(stripped, open_triple)
        if shape is not None:
            kept.append(_ShapedLine(index, stripped, shape))
    return kept


@functools.lru_cache(maxsize=1 << 16)
def _line_shape(stripped: str, open_triple: str | None) -> tuple[str | None, str | None]:
    """One stripped line's shape (None when it is not significant) and the triple quote left open.

    Cached: closing brackets, ``return`` lines and their like recur across
    every hunk of a window, so most lines are shaped once.
    """
    tokens, still_open = _shape_tokens(stripped, open_triple)
    if open_triple is not None or _IMPORT_LINE.match(stripped) or _VERSION_LINE.match(stripped):
        return None, still_open
    if not set(tokens) - _INERT_TOKENS:
        return None, still_open
    return " ".join(tokens), still_open


def _hunk_shape(hunk: Hunk, limit: int | None = None) -> tuple[str, ...]:
    """The shapes of the significant lines a hunk added, in order (the first ``limit``)."""
    return tuple(line.shape for line in _significant_lines(hunk.added))[:limit]


def _layout_only(hunk: Hunk) -> bool:
    """Whether the hunk re-lays out what it removed: same text bar whitespace, commas, quotes."""

    def squeeze(lines: list[str]) -> str:
        return re.sub(r"[\s,]", "", "".join(lines)).replace("'", '"')

    return bool(hunk.removed) and squeeze(hunk.removed) == squeeze(hunk.added)


def _line_at_head(present: list[_ShapedLine], added: Sequence[str]) -> int | None:
    """The HEAD line where a hunk's significant lines still stand together, in order.

    ``present`` is the HEAD file's significant lines. The lines are compared
    as text (stripped), so a copy edited since the commit no longer counts;
    lines that are not significant may sit between.
    """
    wanted = [line.text for line in _significant_lines(added)]
    width = len(wanted)
    for i in range(len(present) - width + 1):
        if all(present[i + j].text == wanted[j] for j in range(width)):
            return present[i].index + 1
    return None


@dataclass(frozen=True)
class _Copy:
    """One file's hunk carrying a shared shape."""

    path: str
    hunk: Hunk


def _same_change_commits(
    ctx: AnalysisContext, history: GitHistory
) -> list[tuple[CommitInfo, list[str]]]:
    """Window commits that edited 3+ eligible files, newest first, each with those files.

    Eligible: analyzed, not a test or migration, and edited after the
    file's review date. Commits touching COCHANGE_SKIP_THRESHOLD or more
    Python files are mechanical and skipped.
    """
    eligible = {path for _file_path, path in source_files(ctx) if not is_migration_file(path)}
    touching = sorted(
        commits_by_hash(history, eligible).values(), key=lambda c: c.date, reverse=True
    )
    picked = []
    for commit in touching:
        if len(commit.files) >= COCHANGE_SKIP_THRESHOLD:
            continue
        files = [
            path
            for path in commit.files
            if path in eligible and after_review(history, path, commit)
        ]
        if len(files) >= MIN_SAME_CHANGE_FILES:
            picked.append((commit, files))
    return picked


def _shared_shapes(hunks: list[Hunk], files: set[str]) -> dict[tuple[str, ...], list[_Copy]]:
    """Shapes one commit gave to 3+ files' hunks, each with the first such hunk per file.

    A hunk counts only when it adds more significant lines than it removes:
    one that replaces as much as it adds or more is consolidating (the call
    sites of a helper just extracted, a migration to a new API), and the
    duplication it shows was there before the commit. Hunks are first
    grouped by the shape of their first significant lines, which equal
    shapes share, so only a group reaching 3 files is shaped in full: most
    hunks of a commit are never read past their opening lines.
    """
    by_opening: dict[tuple[str, ...], dict[str, list[Hunk]]] = {}
    for hunk in hunks:
        if hunk.file not in files or _layout_only(hunk):
            continue
        opening = _hunk_shape(hunk, MIN_SIGNIFICANT_LINES)
        if len(opening) < MIN_SIGNIFICANT_LINES:
            continue
        by_opening.setdefault(opening, {}).setdefault(hunk.file, []).append(hunk)
    by_shape: dict[tuple[str, ...], dict[str, Hunk]] = {}
    for per_file in by_opening.values():
        if len(per_file) < MIN_SAME_CHANGE_FILES:
            continue
        for path, file_hunks in per_file.items():
            for hunk in file_hunks:
                shape = _hunk_shape(hunk)
                if len(shape) <= len(_significant_lines(hunk.removed)):
                    continue
                by_shape.setdefault(shape, {}).setdefault(path, hunk)
    return {
        shape: [_Copy(path, hunk) for path, hunk in sorted(per_file.items())]
        for shape, per_file in by_shape.items()
        if len(per_file) >= MIN_SAME_CHANGE_FILES
    }


@check(
    "same-change-multiple-files",
    # Duplicated code, verified still present at HEAD: the weight of
    # duplicate-blocks, whose AST view sees each copy but not that one
    # commit made them all.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="One commit made the same multi-line change in 3+ files, still present at HEAD",
    advice=Advice(
        fix=(
            "Extract the repeated change into one helper the files share, and replace each"
            " copy listed with a call."
        ),
        dismiss=(
            "Dismiss only if the copies must diverge: generated code, or per-plugin copies"
            " kept independent on purpose."
        ),
    ),
)
@notes_persisting_review
def check_same_change_multiple_files(ctx: AnalysisContext) -> list[Finding]:
    """Flag a change pasted into several files in one commit.

    Hunks are compared by shape: names and literals masked, calls and
    attributes kept, docstrings, comments, imports and layout dropped. A
    shared shape counts only where the lines are still at HEAD, so
    extracting the copies into one helper clears the finding.
    """
    history = ctx.git_history
    if history is None:
        return []
    picked = _same_change_commits(ctx, history)
    if not picked:
        return []
    lines = history.line_history
    paths = sorted({path for _commit, files in picked for path in files})
    hunks_by_commit = lines.edit_hunks([commit.hash for commit, _files in picked], paths)
    shared = [
        (commit, shape, copies)
        for commit, files in picked
        for shape, copies in _shared_shapes(hunks_by_commit[commit.hash], set(files)).items()
    ]
    if not shared:
        return []
    head = lines.lines_at(
        "HEAD", {copy.path for _commit, _shape, copies in shared for copy in copies}
    )
    present = {path: _significant_lines(text) for path, text in head.items()}
    findings = []
    for commit, shape, copies in shared:
        located = []
        for copy in copies:
            line = _line_at_head(present.get(copy.path, []), copy.hunk.added)
            if line is not None:
                located.append((copy.path, line))
        if len(located) < MIN_SAME_CHANGE_FILES:
            continue
        opening = _significant_lines(copies[0].hunk.added)[0].text
        sites = [Site(path, line) for path, line in located]
        findings.append(
            multi_site_finding(
                "same-change-multiple-files",
                Severity.MEDIUM,
                sites,
                functools.partial(
                    _same_change_message,
                    f'commit {commit.hash[:8]} "{commit_subject(commit)}" made the same '
                    f"{len(shape)}-line change",
                    preview(opening),
                    sites,
                ),
            )
        )
    return findings


def _same_change_message(
    change: str, preview: str, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """same-change-multiple-files' message over the copies at ``indices``;
    None below the file floor."""
    shown = [sites[index] for index in indices]
    if len(shown) < MIN_SAME_CHANGE_FILES:
        return None
    return (
        f"{change} in {len(shown)} files, still there at "
        f"HEAD: {', '.join(f'{site.file}:{site.line}' for site in shown)} — "
        f'it starts "{preview}"'
    )
