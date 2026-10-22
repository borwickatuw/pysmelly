"""Git history checks — function ratchets: special cases and branches added one commit at a time.

Both checks start from a function that already carries a long-elif-chain,
isinstance-chain or literal-dispatch-param finding: a dispatch the AST
alone has judged worth a look. The function's history in the window says
whether that dispatch is still being fed.

fix-added-special-case reads the function's early exits at HEAD, each an
``if subject == literal: return/raise`` with no ``else``: ``==``, ``is`` or
``in`` against literals (constants, or names in capitals such as
``EXIT_USAGE`` and ``Status.DONE``), ``or``-joined on one subject. It counts
the fix commits that added one. Three fixes each special-casing one more
input is a function patched symptom by symptom.

growing-dispatch-ladder reads the function's ladders at HEAD: four or more
branches testing one subject, as an ``if``/``elif`` chain, a run of sibling
``if`` statements that each exit, or a ``match``. A branch tests the
subject against literals as above, or with ``isinstance`` (either negated).
It counts the feature commits that added a branch. Three features each
adding a case is a ladder every change has to edit.

How a commit's additions are read: the function's history comes from one
``git log -L`` over every candidate (``LineHistory.ranges_history``). In
each hunk, a line belongs to the innermost ``def`` above it with less
indentation. A line the commit added counts when its test, normalized
through ``ast.unparse``, is one of the function's guards or branches at
HEAD, and the same commit did not remove a line with that test (a move,
a re-indent or an ``if``/``elif`` swap adds nothing). The commit that
created the function adds nothing either: the hunk's old side must hold
its ``def``. A test written over several lines is matched on its first line
only, so it never matches; that misses some additions and invents none.

Both read the window's commits, so both honour ``pysmelly: reviewed``
markers, and both need commit messages that classify
(``has_semantic_history``).

Cost (DECISIONS.md, "Function ratchets"). Running the gate checks first
cost more than everything else together (literal-dispatch-param builds the
caller index), so the gate is applied last, with the same result. A
candidate is a function in a file with 3+ window commits that has a guard
or a ladder at HEAD and meets some gate check's per-function condition
(``_may_carry_gate_finding``). One ``git log -L`` covers every candidate
and is shared by both checks (the same arguments, so the second reads
``_query``'s cache). The gate checks themselves run only for the functions
whose history reached a bar, cheapest first (``_gated``).
"""

from __future__ import annotations

import ast
import functools
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from pysmelly.checks.history_helpers import (
    after_review,
    commit_subject,
    commits_by_hash,
    has_semantic_history,
    head_path_of,
    notes_persisting_review,
    post_review_commit_counts,
    preview,
    window_commit,
)
from pysmelly.checks.literal_dispatch import check_literal_dispatch_param, literal_ladders
from pysmelly.checks.patterns_misc import (
    ISINSTANCE_MIN_CALLS,
    can_recur,
    check_isinstance_chain,
    function_dispatch_type_set,
    isinstance_call_count,
)
from pysmelly.checks.structure import (
    LONG_ELIF_MIN_BRANCHES,
    check_long_elif_chain,
    count_elif_chain,
)
from pysmelly.context import AnalysisContext
from pysmelly.git_history import (
    FEATURE_CATEGORY,
    FIX_CATEGORY,
    CommitInfo,
    GitHistory,
    Hunk,
    classify_commit,
)
from pysmelly.registry import (
    GIT_HISTORY_CATEGORY,
    Advice,
    CheckFn,
    Finding,
    Severity,
    check,
    may_anchor,
)

# The checks whose findings make a function a candidate (the plan item's gate).
GATE_CHECKS: dict[str, CheckFn] = {
    "long-elif-chain": check_long_elif_chain,
    "isinstance-chain": check_isinstance_chain,
    "literal-dispatch-param": check_literal_dispatch_param,
}

# The bars the plan item sets.
MIN_SPECIAL_CASE_COMMITS = 3
MIN_LADDER_BRANCHES = 4
MIN_LADDER_COMMITS = 3

_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
_DEF_LINE = re.compile(r"^\s*(?:async\s+)?def\s+([^\W\d]\w*)\s*\(")

# A branch key: ("if", the unparsed test) or ("case", the unparsed pattern).
_Key = tuple[str, str]


# --- reading a function at HEAD ---


def _is_literal(node: ast.expr) -> bool:
    """A literal value: a constant, a negated number, or a named constant.

    A named constant is spelled in capitals (``EXIT_USAGE``,
    ``Status.DONE``), as module constants and enum members are. A tuple,
    list or set of literals is one too (the right side of ``in``).
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Attribute):
        return node.attr.isupper()
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts) and all(_is_literal(elt) for elt in node.elts)
    return False


def _shared(subjects: Iterable[str | None]) -> str | None:
    """The one subject every operand tests, or None."""
    distinct = set(subjects)
    return distinct.pop() if len(distinct) == 1 else None


def _literal_subject(test: ast.expr) -> str | None:
    """The subject ``test`` compares with literals: ``x == 1``, ``x in ("a", "b")``, ``None is x``.

    The subject is the other side, whatever expression it is
    (``kind.get("type") == "Canvas"`` tests ``kind.get("type")``).
    ``or``-joined comparisons count when they share their subject.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return _shared(_literal_subject(value) for value in test.values)
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    if not isinstance(test.ops[0], (ast.Eq, ast.Is, ast.In)):
        return None
    left, right = test.left, test.comparators[0]
    if _is_literal(right) and not _is_literal(left):
        return ast.unparse(left)
    if _is_literal(left) and not _is_literal(right) and not isinstance(test.ops[0], ast.In):
        return ast.unparse(right)
    return None


def _branch_subject(test: ast.expr) -> str | None:
    """The subject a ladder branch tests.

    A literal comparison (``_literal_subject``) or ``isinstance(subject,
    ...)``, either negated, or several joined by ``or`` on one subject.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return _shared(_branch_subject(value) for value in test.values)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _branch_subject(test.operand)
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
        and len(test.args) == 2
    ):
        return ast.unparse(test.args[0])
    return _literal_subject(test)


def _elif(node: ast.If) -> ast.If | None:
    """The ``elif`` that continues ``node``'s chain, if it has one."""
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        return node.orelse[0]
    return None


def _chain(node: ast.If) -> list[ast.If]:
    """``node`` and each ``elif`` after it."""
    links = [node]
    while (following := _elif(links[-1])) is not None:
        links.append(following)
    return links


def _blocks_in(node: ast.AST) -> Iterator[list[ast.stmt]]:
    """The statement lists directly inside ``node``.

    An ``if``'s chain is flattened: each arm's body, then the final
    ``else``, so an ``elif`` is never a statement of a list of its own.
    ``except`` handlers and ``match`` cases are blocks holding a body.
    """
    if isinstance(node, ast.If):
        chain = _chain(node)
        yield from (link.body for link in chain)
        if chain[-1].orelse:
            yield chain[-1].orelse
        return
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value:
            yield value


def _own_blocks(function: _FunctionDef) -> Iterator[list[ast.stmt]]:
    """Every statement list in ``function``'s own scope (not nested defs, classes).

    Statements only: a dispatch is made of statements, so no expression is
    walked.
    """
    pending = [function.body]
    while pending:
        block = pending.pop()
        yield block
        for node in block:
            if not isinstance(node, _NESTED_SCOPES):
                pending.extend(_blocks_in(node))


def _functions(tree: ast.Module) -> list[tuple[str, _FunctionDef]]:
    """Every function and method by its qualname (``qualified_definitions``' names).

    Found through statement lists alone, since a ``def`` is a statement.
    """
    found = []
    pending: list[tuple[str, list[ast.stmt]]] = [("", tree.body)]
    while pending:
        prefix, block = pending.pop()
        for node in block:
            if isinstance(node, _NESTED_SCOPES):
                qualname = f"{prefix}{node.name}"
                if isinstance(node, _FunctionDef):
                    found.append((qualname, node))
                pending.append((f"{qualname}.", node.body))
            else:
                pending.extend((prefix, inner) for inner in _blocks_in(node))
    return found


def _guards(function: _FunctionDef) -> frozenset[_Key]:
    """The function's early-exit special cases: ``if subject == literal: return/raise``, no else."""
    return frozenset(
        ("if", ast.unparse(node.test))
        for block in _own_blocks(function)
        for node in block
        if isinstance(node, ast.If)
        and not node.orelse
        and isinstance(node.body[-1], (ast.Return, ast.Raise))
        and _literal_subject(node.test) is not None
    )


@dataclass(frozen=True)
class _Ladder:
    """A dispatch at HEAD: the subject its branches test, and each branch's key."""

    subject: str
    branches: frozenset[_Key]


def _exits(chain: list[ast.If]) -> bool:
    """Whether control leaves after every branch of a chain that has no ``else``."""
    return not chain[-1].orelse and all(isinstance(link.body[-1], _EXITS) for link in chain)


def _if_ladders(statements: list[ast.stmt]) -> Iterator[_Ladder]:
    """Ladders among one statement list's ``if`` chains.

    Adjacent ``if`` statements join one run when the one before exits from
    every branch, which makes them as exclusive as ``elif``.
    """
    run: list[ast.If] = []
    for statement in [*statements, None]:
        if isinstance(statement, ast.If) and (not run or _exits(_chain(run[-1]))):
            run.append(statement)
            continue
        yield from _ladder_of(run)
        run = [statement] if isinstance(statement, ast.If) else []


def _ladder_of(run: list[ast.If]) -> Iterator[_Ladder]:
    by_subject: dict[str, set[_Key]] = {}
    for node in run:
        for link in _chain(node):
            subject = _branch_subject(link.test)
            if subject is not None:
                by_subject.setdefault(subject, set()).add(("if", ast.unparse(link.test)))
    for subject, branches in by_subject.items():
        if len(branches) >= MIN_LADDER_BRANCHES:
            yield _Ladder(subject, frozenset(branches))


def _is_wildcard(pattern: ast.pattern) -> bool:
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None


def _ladders(function: _FunctionDef) -> list[_Ladder]:
    """The function's dispatches on one subject with ``MIN_LADDER_BRANCHES`` or more branches."""
    found = []
    for block in _own_blocks(function):
        found.extend(_if_ladders(block))
        for node in block:
            if not isinstance(node, ast.Match):
                continue
            branches = {
                ("case", ast.unparse(case.pattern))
                for case in node.cases
                if not _is_wildcard(case.pattern)
            }
            if len(branches) >= MIN_LADDER_BRANCHES:
                found.append(_Ladder(ast.unparse(node.subject), frozenset(branches)))
    return found


# --- candidates ---


@dataclass(frozen=True)
class _Candidate:
    """A gated function: its path, its def line in the analyzed tree, and its HEAD form."""

    path: str
    qualname: str
    line: int
    head_start: int
    head_end: int
    guards: frozenset[_Key]
    ladders: tuple[_Ladder, ...]

    @property
    def name(self) -> str:
        return self.qualname.rpartition(".")[2]

    @property
    def keys(self) -> frozenset[_Key]:
        return self.guards.union(*(ladder.branches for ladder in self.ladders))


def _enclosing(functions: list[tuple[str, _FunctionDef]], line: int) -> str | None:
    """The qualname of the innermost function whose lines hold ``line``."""
    holding = [
        (node.lineno, qualname)
        for qualname, node in functions
        if node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    return max(holding)[1] if holding else None


def _parameters(node: _FunctionDef) -> set[str]:
    args = node.args
    return {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _may_carry_gate_finding(node: _FunctionDef) -> bool:
    """Whether a function meets the per-function condition of some gate check.

    long-elif-chain: an ``if``/``elif`` chain of ``LONG_ELIF_MIN_BRANCHES``.
    isinstance-chain: ``ISINSTANCE_MIN_CALLS`` calls, or a type set that
    can recur (whether 3+ functions share it is a question about the whole
    codebase). literal-dispatch-param: a literal ladder on one of its
    parameters (whether the callers pass exactly its values is the caller
    index's question). Each condition is necessary for its check's finding,
    so a function that meets none carries no gate finding; ``_gated``
    answers the rest for the few functions whose history reaches a bar.
    """
    if any(
        isinstance(child, ast.If) and count_elif_chain(child)[0] >= LONG_ELIF_MIN_BRANCHES
        for child in ast.walk(node)
    ):
        return True
    if isinstance_call_count(node) >= ISINSTANCE_MIN_CALLS:
        return True
    if can_recur(function_dispatch_type_set(node)):
        return True
    params = _parameters(node)
    return any(ladder.param in params for ladder in literal_ladders(node.body))


def _gated(ctx: AnalysisContext, candidates: Iterable[_Candidate]) -> set[_Candidate]:
    """The candidates that carry a gate finding, by each gate check's own run.

    The checks run cheapest first, and the rest are skipped once every
    candidate is placed: literal-dispatch-param builds the caller index.
    A finding (or a site of one) is placed in the innermost function of
    the analyzed tree that holds its line.
    """
    pending = {(c.path, c.qualname): c for c in candidates}
    functions = {path: _functions(ctx.all_trees[Path(path)]) for path, _name in pending}
    gated: set[_Candidate] = set()
    for name, gate in GATE_CHECKS.items():
        if not pending:
            break
        for finding in gate(ctx):
            if not may_anchor(name, finding.file, ctx):
                continue
            for site in finding.places:
                if site.file not in functions:
                    continue
                qualname = _enclosing(functions[site.file], site.line)
                placed = pending.pop((site.file, qualname), None) if qualname else None
                if placed is not None:
                    gated.add(placed)
    return gated


def _eligible_files(ctx: AnalysisContext, history: GitHistory) -> dict[str, Path]:
    """Analyzed production files, committed at HEAD, with enough window commits to ratchet.

    A file's commits include those made under the names it had before a
    rename in the window, and only those after its review date count.
    """
    committed = history.line_history.head_paths()
    least = min(MIN_SPECIAL_CASE_COMMITS, MIN_LADDER_COMMITS)
    return {
        path: file_path
        for path, file_path, commits in post_review_commit_counts(ctx, history)
        if path in committed and commits >= least
    }


def _candidates(ctx: AnalysisContext, history: GitHistory) -> list[_Candidate]:
    """Functions that may carry a gate finding and have a guard or a ladder at HEAD.

    Functions are read at HEAD, the lines ``git log -L`` takes (the HEAD
    blob is parsed when the working tree differs), and anchored at their
    ``def`` in the analyzed tree. A qualname either side lacks (an
    uncommitted function) or holds twice (a property and its setter) is
    left out.
    """
    eligible = _eligible_files(ctx, history)
    if not eligible:
        return []
    lines = history.line_history
    changed = lines.head_changed_paths()
    found = []
    for path, file_path in sorted(eligible.items()):
        functions = _functions(ctx.all_trees[file_path])
        head_tree = lines.head_tree(path) if path in changed else None
        head = _functions(head_tree) if head_tree is not None else functions
        here = Counter(qualname for qualname, _node in functions)
        there = Counter(qualname for qualname, _node in head)
        analyzed = dict(functions)
        for qualname, node in head:
            if here[qualname] != 1 or there[qualname] != 1:
                continue
            guards, ladders = _guards(node), tuple(_ladders(node))
            if not (guards or ladders) or not _may_carry_gate_finding(node):
                continue
            found.append(
                _Candidate(
                    path,
                    qualname,
                    analyzed[qualname].lineno,
                    min([node.lineno, *(d.lineno for d in node.decorator_list)]),
                    node.end_lineno or node.lineno,
                    guards,
                    ladders,
                )
            )
    return found


# --- reading the history ---


@functools.lru_cache(maxsize=1 << 12)
def _branch_key(stripped: str) -> _Key | None:
    """The key of the ``if``/``elif``/``case`` a stripped source line opens, else None."""
    if stripped.startswith("elif"):
        stripped = stripped[2:]
    if stripped.startswith("if"):
        for text in (stripped, f"{stripped}\n pass"):
            try:
                node = ast.parse(text).body[0]
            except SyntaxError:
                continue
            return ("if", ast.unparse(node.test)) if isinstance(node, ast.If) else None
        return None
    if stripped.startswith("case"):
        try:
            match = ast.parse(f"match _:\n {stripped}\n  pass").body[0]
        except SyntaxError:
            return None
        if isinstance(match, ast.Match):
            return ("case", ast.unparse(match.cases[0].pattern))
    return None


class _Owners:
    """Follows a fragment line by line, naming the innermost ``def`` each line sits in."""

    def __init__(self) -> None:
        self._open: list[tuple[int, str]] = []
        self.defined: set[str] = set()

    def owner(self, line: str) -> str | None:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            indent = len(line) - len(line.lstrip())
            while self._open and indent <= self._open[-1][0]:
                self._open.pop()
            opened = _DEF_LINE.match(line)
            if opened is not None:
                self._open.append((indent, opened.group(1)))
                self.defined.add(opened.group(1))
        return self._open[-1][1] if self._open else None


@dataclass(frozen=True)
class _HunkChanges:
    """The branch keys a hunk added and removed, each with the ``def`` it sits in.

    ``existed`` names the defs the hunk's old side holds: the functions that
    were there before the commit.
    """

    added: frozenset[tuple[str, _Key]]
    removed: frozenset[tuple[str, _Key]]
    existed: frozenset[str]


def _hunk_changes(hunk: Hunk) -> _HunkChanges:
    """Read one hunk's lines on both sides, owner by owner (``_Owners``)."""
    old, new = _Owners(), _Owners()
    added: set[tuple[str, _Key]] = set()
    removed: set[tuple[str, _Key]] = set()
    for line in hunk.body:
        tag, text = line[:1], line[1:]
        if tag == "-":
            owner, into = old.owner(text), removed
        elif tag == "+":
            owner, into = new.owner(text), added
        else:
            old.owner(text)
            new.owner(text)
            continue
        key = _branch_key(text.strip())
        if owner is not None and key is not None:
            into.add((owner, key))
    return _HunkChanges(frozenset(added), frozenset(removed), frozenset(old.defined))


@dataclass(frozen=True)
class _Addition:
    """What one commit added to one candidate."""

    commit: CommitInfo
    keys: frozenset[_Key]


def _additions(
    history: GitHistory, candidates: Sequence[_Candidate]
) -> dict[_Candidate, list[_Addition]]:
    """Each candidate's commits in the window that added a guard or branch it has at HEAD.

    Newest first; commits on or before the file's review date are left out.
    A line is tied to the candidate of its file whose name its ``def`` has;
    namesakes (methods of two classes) are told apart by which holds the
    key at HEAD, and a key both hold is not counted.
    """
    by_name: dict[tuple[str, str], list[_Candidate]] = {}
    for candidate in candidates:
        by_name.setdefault((candidate.path, candidate.name), []).append(candidate)
    head_paths = head_path_of(history, {c.path for c in candidates})
    commits = commits_by_hash(history, head_paths)
    ranges = [(c.path, c.head_start, c.head_end) for c in candidates]
    found: dict[_Candidate, list[_Addition]] = {}
    for revision in history.line_history.ranges_history(ranges):
        commit = window_commit(commits, revision.sha)
        keys: dict[_Candidate, set[_Key]] = {}
        for hunk in revision.hunks:
            head_path = head_paths.get(hunk.file)
            if head_path is None:
                continue
            changes = _hunk_changes(hunk)
            for owner, key in changes.added - changes.removed:
                holders = [c for c in by_name.get((head_path, owner), []) if key in c.keys]
                if owner in changes.existed and len(holders) == 1:
                    keys.setdefault(holders[0], set()).add(key)
        for candidate, added_keys in keys.items():
            if after_review(history, candidate.path, commit):
                found.setdefault(candidate, []).append(_Addition(commit, frozenset(added_keys)))
    return found


def _ratchet_additions(ctx: AnalysisContext) -> dict[_Candidate, list[_Addition]]:
    """Both checks' shared start: the candidates and what the window's commits added to them."""
    history = ctx.git_history
    if not has_semantic_history(history):
        return {}
    candidates = _candidates(ctx, history)
    if not candidates:
        return {}
    return _additions(history, candidates)


def _labelled(additions: Iterable[_Addition], category: str) -> list[_Addition]:
    """The additions made by commits ``classify_commit`` gives ``category``, oldest first."""
    return [a for a in reversed(list(additions)) if category in classify_commit(a.commit.message)]


def _steps(additions: list[_Addition], keys: frozenset[_Key]) -> str:
    """``sha "subject" added `if test`; ...`` — each commit and what it added among ``keys``."""
    return "; ".join(_step(addition, keys) for addition in additions)


def _step(addition: _Addition, keys: frozenset[_Key]) -> str:
    tests = ", ".join(f"`{kind} {preview(text)}`" for kind, text in sorted(addition.keys & keys))
    return f'{addition.commit.hash[:8]} "{commit_subject(addition.commit)}" added {tests}'


# --- the checks ---


@check(
    "fix-added-special-case",
    # A function patched symptom by symptom, with a dispatch the AST already
    # flagged: fix it unless each value is a distinct case of an outside system.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description=(
        "3+ fix commits each added an `if x == literal: return/raise` special case "
        "to a function that already dispatches on literals or types"
    ),
    advice=Advice(
        fix=(
            "Find what the special-cased values have in common and handle that once,"
            " replacing the early exits the fix commits added."
        ),
        dismiss=(
            "Dismiss only if each value is a distinct documented exception of an outside system."
        ),
    ),
)
@notes_persisting_review
def check_fix_added_special_case(ctx: AnalysisContext) -> list[Finding]:
    """Flag a function whose fixes each special-cased one more input (module docstring)."""
    reached = []
    for candidate, additions in _ratchet_additions(ctx).items():
        fixes = [a for a in _labelled(additions, FIX_CATEGORY) if a.keys & candidate.guards]
        if len(fixes) >= MIN_SPECIAL_CASE_COMMITS:
            reached.append((candidate, fixes))
    gated = _gated(ctx, [candidate for candidate, _fixes in reached])
    return [
        Finding(
            file=candidate.path,
            line=candidate.line,
            check="fix-added-special-case",
            message=(
                f"{candidate.qualname}(): {len(fixes)} fix commits in the window each added "
                f"an early exit for one more literal value: {_steps(fixes, candidate.guards)} "
                f"— each fix handled the input that broke instead of the rule behind it"
            ),
            severity=Severity.MEDIUM,
        )
        for candidate, fixes in reached
        if candidate in gated
    ]


@check(
    "growing-dispatch-ladder",
    # A dispatch every feature has to edit, already flagged by the AST: the
    # weight of isinstance-chain, with the history that shows it still growing.
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description=(
        "A 4+-branch dispatch on one subject to which 3+ feature commits each added a branch"
    ),
    advice=Advice(
        fix=(
            "Move the cases into a table or onto the types (a dict of handlers,"
            " functools.singledispatch, a method per class) so a new case stops editing"
            " the ladder."
        ),
        dismiss="Dismiss only if each branch's code is too different for one table to hold.",
    ),
)
@notes_persisting_review
def check_growing_dispatch_ladder(ctx: AnalysisContext) -> list[Finding]:
    """Flag a ladder that each new feature extends by a branch (module docstring)."""
    reached = []
    for candidate, additions in _ratchet_additions(ctx).items():
        features = _labelled(additions, FEATURE_CATEGORY)
        for ladder in candidate.ladders:
            grew = [a for a in features if a.keys & ladder.branches]
            if len(grew) >= MIN_LADDER_COMMITS:
                reached.append((candidate, ladder, grew))
    gated = _gated(ctx, [candidate for candidate, _ladder, _grew in reached])
    return [
        Finding(
            file=candidate.path,
            line=candidate.line,
            check="growing-dispatch-ladder",
            message=(
                f"{candidate.qualname}(): the {len(ladder.branches)}-branch dispatch on "
                f"'{preview(ladder.subject)}' grew a branch in {len(grew)} feature commits "
                f"in the window: {_steps(grew, ladder.branches)} — every new case edits this "
                f"ladder"
            ),
            severity=Severity.MEDIUM,
        )
        for candidate, ladder, grew in reached
        if candidate in gated
    ]
