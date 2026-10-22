"""Git history checks — blame age: code that promised to change and has not.

Both checks start from candidates the AST already found (a fossilized toggle,
a comment that calls itself temporary) and blame only those lines, so their
cost is one ``git blame`` per file that holds a candidate. Line numbers come
from the file as committed at HEAD (``LineHistory.head_source``), which is
what ``line_ages`` blames, so an uncommitted edit cannot misplace a line.

Neither check reads the window's commits: a line is as old as it is. They
therefore ignore ``pysmelly: reviewed`` markers: acknowledging the line
means changing it.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from datetime import UTC, datetime, timedelta

from pysmelly.checks.helpers import iter_uppercase_assigns
from pysmelly.checks.patterns_misc import FossilizedToggle, find_fossilized_toggles
from pysmelly.context import AnalysisContext
from pysmelly.git_history import GitHistoryQueryError, LineHistory
from pysmelly.registry import GIT_HISTORY_CATEGORY, Advice, Finding, Severity, check

# Six months as the rest of pysmelly counts them (30-day months, as in
# ``--window 6m``). frozen-feature-flag's bar is "180+ days"; fossil-temporary's
# is "older than 6 months". They are the same span.
AGE_BAR = timedelta(days=180)

# What "legacy" or "temporary" must name for the comment to call the code
# itself temporary. Bare, both words mostly describe data, a mode or a
# caller's behaviour ("legacy rows", "fallback to legacy name", "temporarily
# switch profile"); DECISIONS.md, "32-7: fossil-temporary marker calibration".
_CODE_NOUN = (
    r"(?:\s+[\w-]+)?\s+"
    r"(?:support|shim|wrapper|alias|function|method|code|path|api|branch"
    r"|fallback|fix|workaround|hack|solution|patch|measure)\b"
)

# The vocabulary of a comment that promises its own removal, matched against
# the comment's text without its ``#``. It differs from conscious-debt's
# commit-message words on purpose (DECISIONS.md, "Blame-age checks"): "legacy"
# and "for now" describe code, not a commit, and "workaround"/"stopgap"/
# "kludge" describe a commit's act. TODO, FIXME and XXX are tags, matched in
# capitals only (lowercase they are words: "Mongolian todo", a
# ``xxx-xx-xxxx`` placeholder) and only in a tag's place: opening the comment
# or followed by ``:`` or ``(``. Mid-sentence they are a word ("ships with
# FIXME placeholders"). A word joined to a hyphen is part of a name, not a
# marker.
_TEMPORARY_MARKER = re.compile(
    r"(?-i:^(?:TODO|FIXME|XXX)\b|\b(?:TODO|FIXME|XXX)(?=[:(]))"
    r"|\bhack\b"
    r"|\btemporary" + _CODE_NOUN + r"|\bfor now\b"
    r"|\bremove after\b"
    r"|\bonce\b[^.;]*?\bland(?:s|ed)?\b"
    r"|(?<![-\w])legacy" + _CODE_NOUN,
    re.IGNORECASE,
)

# A superset of _TEMPORARY_MARKER after a ``#`` on the same line, for one
# ``git grep`` over HEAD (POSIX ERE, case-insensitive), so only the files
# that can hold a marked comment are read.
_MARKER_PREFILTER = "#.*(todo|fixme|xxx|hack|temporary|for now|remove after|once.*land|legacy)"

_COMMENT_PREVIEW = 80


def _same_constant(a: object, b: object) -> bool:
    """Equal and of one type, so ``True`` at HEAD is not taken for ``1`` on disk."""
    return type(a) is type(b) and a == b


def _head_definition_lines(lines: LineHistory, toggle: FossilizedToggle) -> range | None:
    """The lines of the toggle's assignment as committed at HEAD.

    None when HEAD has no such file, or no module-level assignment of the
    name to the same value: the toggle is new or is being changed in the
    working tree, so it is not frozen.
    """
    tree = lines.head_tree(str(toggle.file))
    if tree is None:
        return None
    found = None
    for name, lineno, value_node in iter_uppercase_assigns(tree):
        if (
            name == toggle.name
            and isinstance(value_node, ast.Constant)
            and _same_constant(value_node.value, toggle.value)
        ):
            end = value_node.end_lineno if value_node.end_lineno is not None else lineno
            found = range(lineno, end + 1)
    return found


@check(
    "frozen-feature-flag",
    # MEDIUM like fossilized-toggles, whose finding this ages, until a fleet
    # finding earns HIGH (away-run audit, 2026-10-21: none fired yet).
    severity=Severity.MEDIUM,
    category=GIT_HISTORY_CATEGORY,
    description="A fossilized toggle unchanged for 180+ days, both branches still present",
    advice=Advice(
        fix=(
            "Delete the flag and the branch it never takes, keeping the live branch"
            " unconditionally."
        ),
        dismiss="Dismiss only if it is a kill switch kept to be flipped during an incident.",
    ),
)
def check_frozen_feature_flag(ctx: AnalysisContext) -> list[Finding]:
    """Raise a fossilized toggle that blame shows has not moved in six months.

    fossilized-toggles is an AST check, and AST runs carry no git history,
    so this is its git-history counterpart under its own name: the registry
    gives each check name one category. A toggle qualifies when at least one
    conditional it decides still carries code on both sides, so the path it
    never takes is in the source beside the one it always does.
    """
    history = ctx.git_history
    if history is None:
        return []
    now = datetime.now(UTC)
    findings = []
    for toggle in find_fossilized_toggles(ctx):
        if not any(use.two_sided for use in toggle.uses):
            continue
        definition = _head_definition_lines(history.line_history, toggle)
        if definition is None:
            continue
        written = max(history.line_history.line_ages(str(toggle.file), definition).values())
        if now - written < AGE_BAR:
            continue
        frozen_days = (now - written).days
        findings.append(
            Finding(
                file=str(toggle.file),
                line=toggle.line,
                check="frozen-feature-flag",
                message=(
                    f"{toggle.describe()} — frozen for {frozen_days} days "
                    f"(unchanged since {written:%Y-%m-%d}) with both branches still "
                    f"present"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings


def _temporary_comments(path: str, source: str) -> dict[int, tuple[str, str]]:
    """Comment lines that call themselves temporary: {line: (comment, marker)}.

    Only ``#`` comments, read with tokenize, so a ``#`` inside a string is not
    one and docstrings are never read: a docstring's "legacy" or "for now" is
    documentation, not a promise (DECISIONS.md, "Blame-age checks").
    """
    found: dict[int, tuple[str, str]] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            match = _TEMPORARY_MARKER.search(token.string.lstrip("#").strip())
            if match is not None:
                found[token.start[0]] = (token.string, match.group(0))
    except (tokenize.TokenError, SyntaxError) as err:
        msg = f"{path} at HEAD does not tokenize: {err}"
        raise GitHistoryQueryError(msg) from err
    return found


def _preview(comment: str) -> str:
    text = comment.lstrip("#").strip()
    if len(text) > _COMMENT_PREVIEW:
        return text[: _COMMENT_PREVIEW - 1] + "…"
    return text


@check(
    "fossil-temporary",
    severity=Severity.LOW,
    category=GIT_HISTORY_CATEGORY,
    description="Comments marked temporary (TODO, HACK, for now, legacy shim...) over 6 months old",
    advice=Advice(
        fix="Do what the comment promised, or reword it if the code is now permanent.",
        dismiss=(
            "Dismiss only if the promised change waits on an external event the comment"
            " names (a dependency release, a cut-over date) that has not happened."
        ),
    ),
)
def check_fossil_temporary(ctx: AnalysisContext) -> list[Finding]:
    """Flag a comment that promises its own removal and is six months old.

    The comments are read from each analyzed file as committed at HEAD, and
    one ``git blame`` per file dates the marked lines. One ``git grep`` first
    picks the files whose HEAD text could hold a marker, so a file without
    one costs no git call of its own.
    """
    history = ctx.git_history
    if history is None:
        return []
    lines = history.line_history
    candidates = lines.head_files_matching(_MARKER_PREFILTER)
    now = datetime.now(UTC)
    findings = []
    for filepath in sorted(ctx.all_trees, key=str):
        path = str(filepath)
        if path not in candidates:
            continue
        source = lines.head_source(path)
        if source is None:
            msg = f"git grep found {path} at HEAD, but git ls-tree does not list it"
            raise GitHistoryQueryError(msg)
        marked = _temporary_comments(path, source)
        if not marked:
            continue
        ages = lines.line_ages(path, marked)
        for line, (comment, marker) in sorted(marked.items()):
            written = ages[line]
            if now - written < AGE_BAR:
                continue
            findings.append(
                Finding(
                    file=path,
                    line=line,
                    check="fossil-temporary",
                    message=(
                        f'"{_preview(comment)}" — marked {marker!r}, unchanged for '
                        f"{(now - written).days} days (since {written:%Y-%m-%d})"
                    ),
                    severity=Severity.LOW,
                )
            )
    return findings
