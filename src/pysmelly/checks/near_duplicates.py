"""Near-duplicates with drift — code copied once that has since diverged.

``duplicate-blocks`` matches blocks whose normalized shape is *identical*.
A copy that was edited in one place and not the other falls out of that
match the moment it drifts, which is exactly when it starts to matter: the
copies now disagree, and at most one of them can be right.

This module pairs code units in different files — function bodies, nested
statement lists, the short heads and tails of both, and single statements —
whose normalized token streams are similar but not identical.
:func:`find_near_duplicate_pairs`, memoized as
``AnalysisContext.near_duplicate_pairs``, is the reusable half (the pairs
and their aligned line ranges); the check groups the pairs into clusters
and prints a bounded unified diff of the drift.
"""

from __future__ import annotations

import ast
import bisect
import copy
import difflib
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import body_without_docstring, source_trees
from pysmelly.checks.structure import _shorten_paths
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

# A pair is reported when difflib's ratio over the two normalized token
# streams reaches this. Measured on a real project (DECISIONS.md, Phase
# 28-1): the ink-coverage fallback copies pair at 0.80-0.98, and 0.85 loses
# two of them.
SIMILARITY_THRESHOLD = 0.8

# Shape alone cannot tell a copy from two functions written to one pattern:
# every check's ``findings.append(Finding(file=..., line=..., check=...))``
# tail has the same shape and none of the same content. Where two units'
# token streams line up, the local names and literals the shape erased must
# agree this often for the pair to be a copy. Measured (DECISIONS.md, Phase
# 28-1): that project's ink-coverage copies agree at 0.67-1.00, and the drifted
# tesseract paragraph block reaches the cluster at 0.76 and 0.79; pysmelly's
# check-registration and visitor skeletons agree at 0.70 at most.
MIN_CONTENT_AGREEMENT = 0.75

# The size floor is in normalized tokens, not statements: the copies this
# check exists for are three-statement functions whose drift lives inside
# one large ``Block(...)`` call, which a statement floor of 8 never sees.
MIN_TOKENS = 40
# Heads and tails of a statement list are units up to this many statements.
MAX_WINDOW = 8

# Candidate generation: units sharing too few token k-grams are never scored.
SHINGLE_SIZE = 5
MIN_SHARED_SHINGLES = 0.3
# A k-gram that occurs in more functions than this is boilerplate, not evidence.
MAX_SHINGLE_POSTINGS = 400

# The diff printed in a finding is capped so one large cluster cannot drown
# the report; the message says how much was left out.
MAX_DIFF_LINES = 40
RENDER_WIDTH = 72

_BODY_FIELDS = ("body", "orelse", "finalbody")
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# The node types that carry a source position (``lineno``).
_LOCATED = (
    ast.stmt,
    ast.expr,
    ast.excepthandler,
    ast.arg,
    ast.keyword,
    ast.alias,
    ast.pattern,
    ast.type_param,
)

# The compound statements with an ``else:`` clause rendered after their body.
_WITH_ELSE = (ast.If, ast.For, ast.AsyncFor, ast.While)


@dataclass(frozen=True)
class CodeSpan:
    """A contiguous run of statements: file, enclosing function, lines."""

    file: str
    scope: str
    line_start: int
    line_end: int

    def contains(self, other: CodeSpan) -> bool:
        return (
            self.file == other.file
            and self.line_start <= other.line_start
            and other.line_end <= self.line_end
        )

    def overlaps(self, other: CodeSpan) -> bool:
        return (
            self.file == other.file
            and self.line_start <= other.line_end
            and other.line_start <= self.line_end
        )


@dataclass(frozen=True)
class LineAlignment:
    """One stretch of the two copies, in order: code they share, or drift.

    Each side is an inclusive line range, or ``None`` where that copy has
    nothing in the stretch (code only the other copy has). A line can sit
    in a shared stretch and a drift stretch both when only part of it moved.
    """

    first: tuple[int, int] | None
    second: tuple[int, int] | None
    same: bool


@dataclass(frozen=True)
class NearDuplicatePair:
    """Two similar-but-not-identical code spans and how their lines align.

    ``aligned`` walks both copies in order, shared stretches and drift
    stretches alternating, so a line in one copy maps to the lines that
    play its part in the other.
    """

    first: CodeSpan
    second: CodeSpan
    similarity: float
    aligned: tuple[LineAlignment, ...]
    first_stmts: tuple[ast.stmt, ...]
    second_stmts: tuple[ast.stmt, ...]


@dataclass
class _Unit:
    span: CodeSpan
    stmts: list[ast.stmt]
    tokens: list[str]
    lines: list[int]
    content: list[str | None]


# --- tokens -------------------------------------------------------------------


def _label(node: ast.AST) -> str:
    """The normalized token for one node: its kind plus any API-level name.

    Local names and literal values are erased so renames and new literals do
    not hide a copy; called names, attributes and keyword names are kept as
    the anchors that make two blocks *the same code* rather than the same
    shape.
    """
    if isinstance(node, ast.Attribute):
        return f".{node.attr}"
    if isinstance(node, ast.keyword):
        return f"kw:{node.arg or '**'}"
    if isinstance(node, ast.Constant):
        return f"const:{type(node.value).__name__}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return f"call:{node.func.id}"
    return type(node).__name__


def _erased(node: ast.AST) -> str | None:
    """What :func:`_label` erased from *node*: a local name or a literal value."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return None


@dataclass
class _Tokens:
    """A token stream with, per token, its source line and erased content."""

    tokens: list[str]
    lines: list[int]
    content: list[str | None]

    def extend(self, other: _Tokens) -> None:
        self.tokens.extend(other.tokens)
        self.lines.extend(other.lines)
        self.content.extend(other.content)

    def append(self, token: str, line: int, content: str | None) -> None:
        self.tokens.append(token)
        self.lines.append(line)
        self.content.append(content)


def _tokens(stmts: list[ast.stmt], memo: dict[int, _Tokens]) -> _Tokens:
    """Preorder tokens of *stmts*, with the source line and erased content of each."""
    result = _Tokens([], [], [])
    for stmt in stmts:
        result.extend(_stmt_tokens(stmt, memo))
    return result


def _stmt_tokens(stmt: ast.stmt, memo: dict[int, _Tokens]) -> _Tokens:
    """One statement's tokens, memoized: nested statement lists reuse them.

    A nested function or class contributes one token: it is its own unit.
    """
    if id(stmt) in memo:
        return memo[id(stmt)]
    result = _Tokens([], [], [])
    stack: list[tuple[ast.AST, int]] = [(stmt, stmt.lineno)]
    while stack:
        node, line = stack.pop()
        if isinstance(node, ast.expr_context):
            continue
        if isinstance(node, _LOCATED):
            line = node.lineno
        if isinstance(node, _NESTED_SCOPES):
            result.append(type(node).__name__, line, None)
            continue
        if node is not stmt and isinstance(node, ast.stmt):
            result.extend(_stmt_tokens(node, memo))
            continue
        result.append(_label(node), line, _erased(node))
        children = list(ast.iter_child_nodes(node))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            children = children[1:]
        stack.extend((child, line) for child in reversed(children))
    memo[id(stmt)] = result
    return result


# --- units --------------------------------------------------------------------


def _statement_lists(node: ast.AST) -> Iterator[list[ast.stmt]]:
    """Every statement list nested under *node*, not crossing into nested scopes."""
    for attr in _BODY_FIELDS:
        body = getattr(node, attr, None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            yield body
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
            yield from _statement_lists(child)


def _scoped_functions(
    tree: ast.Module,
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield ``(qualified name, function)`` for every function, methods and nested ones included."""
    stack: list[tuple[str, ast.AST]] = [("", tree)]
    while stack:
        prefix, node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    yield name, child
                stack.append((f"{name}.", child))
            elif not isinstance(child, (ast.expr, ast.expr_context)):
                stack.append((prefix, child))


def _make_unit(
    filepath: str, scope: str, stmts: list[ast.stmt], memo: dict[int, _Tokens]
) -> _Unit | None:
    stream = _tokens(stmts, memo)
    if len(stream.tokens) < MIN_TOKENS:
        return None
    end = stmts[-1].end_lineno or stmts[-1].lineno
    return _Unit(
        CodeSpan(filepath, scope, stmts[0].lineno, end),
        stmts,
        stream.tokens,
        stream.lines,
        stream.content,
    )


def _extract_units(filepath: str, tree: ast.Module) -> list[_Unit]:
    """Every statement list in every function, its short heads and tails, each statement.

    Heads and tails catch a copied block that opens or closes a function
    whose other half is its own; any window in the middle would make the unit
    count quadratic in the body length.
    """
    units: dict[tuple[int, int], _Unit] = {}
    memo: dict[int, _Tokens] = {}
    for scope, func in _scoped_functions(tree):
        body = body_without_docstring(func.body)
        if not body:
            continue
        candidates: list[list[ast.stmt]] = []
        for stmts in [body, *(n for s in body for n in _statement_lists(s))]:
            candidates.append(stmts)
            candidates.extend([s] for s in stmts)
            for size in range(2, min(len(stmts), MAX_WINDOW + 1)):
                candidates.extend((stmts[:size], stmts[-size:]))
        for stmts in candidates:
            unit = _make_unit(filepath, scope, stmts, memo)
            if unit is not None:
                units.setdefault((unit.span.line_start, unit.span.line_end), unit)
    return list(units.values())


# --- pairing ------------------------------------------------------------------


def _shingles(tokens: list[str]) -> frozenset[int]:
    return frozenset(
        hash(tuple(tokens[i : i + SHINGLE_SIZE])) for i in range(len(tokens) - SHINGLE_SIZE + 1)
    )


def _candidate_pairs(units: list[_Unit]) -> Iterator[tuple[_Unit, _Unit]]:
    """Unit pairs in different files worth scoring: enough shared k-grams, compatible length.

    Copies within one module are left out: two functions side by side in a
    file are usually a deliberate pair (a horizontal and a vertical split,
    a predicate and its negation) that a reader editing one already sees.

    Two passes keep this near-linear. An inverted index over each
    function's k-grams finds the function pairs that share enough uncommon
    ones to hold any candidate at all — a function's nested units repeat its
    k-grams, so indexing per function rather than per unit keeps the
    postings short. Only units of those function pairs are then compared,
    by set intersection.
    """
    by_scope: dict[tuple[str, str], list[tuple[_Unit, frozenset[int]]]] = defaultdict(list)
    for unit in sorted(units, key=lambda u: len(u.tokens)):
        by_scope[(unit.span.file, unit.span.scope)].append((unit, _shingles(unit.tokens)))
    scopes = list(by_scope)
    scope_shingles = [frozenset().union(*(s for _, s in by_scope[k])) for k in scopes]
    postings: dict[int, list[int]] = defaultdict(list)
    for index, shingles in enumerate(scope_shingles):
        for shingle in shingles:
            postings[shingle].append(index)
    floor = MIN_SHARED_SHINGLES * (MIN_TOKENS - SHINGLE_SIZE + 1)
    for index, shingles in enumerate(scope_shingles):
        shared: Counter[int] = Counter()
        for shingle in shingles:
            posting = postings[shingle]
            if len(posting) <= MAX_SHINGLE_POSTINGS:
                shared.update(other for other in posting if other > index)
        for other, count in shared.items():
            if count >= floor and scopes[index][0] != scopes[other][0]:
                yield from _unit_candidates(by_scope[scopes[index]], by_scope[scopes[other]])


def _unit_candidates(
    first: list[tuple[_Unit, frozenset[int]]], second: list[tuple[_Unit, frozenset[int]]]
) -> Iterator[tuple[_Unit, _Unit]]:
    """Pairs across two functions' units whose lengths and k-grams allow a match.

    ``2 * shorter / (shorter + longer) >= threshold`` bounds the ratio from
    lengths alone, so each unit is only checked against the length band
    that could reach it; *second* arrives sorted by token count.
    """
    lengths = [len(unit.tokens) for unit, _ in second]
    stretch = (2 - SIMILARITY_THRESHOLD) / SIMILARITY_THRESHOLD
    for a, a_shingles in first:
        low = bisect.bisect_left(lengths, len(a.tokens) / stretch)
        high = bisect.bisect_right(lengths, len(a.tokens) * stretch)
        for b, b_shingles in second[low:high]:
            smaller = min(len(a_shingles), len(b_shingles))
            if len(a_shingles & b_shingles) >= MIN_SHARED_SHINGLES * smaller:
                yield a, b


def _alignment(a: _Unit, b: _Unit, matcher: difflib.SequenceMatcher) -> tuple[LineAlignment, ...]:
    return tuple(
        LineAlignment(
            first=_line_range(a.lines[i1:i2]),
            second=_line_range(b.lines[j1:j2]),
            same=tag == "equal",
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    )


def _line_range(lines: list[int]) -> tuple[int, int] | None:
    return (min(lines), max(lines)) if lines else None


def _content_agreement(a: _Unit, b: _Unit, matcher: difflib.SequenceMatcher) -> float:
    """How often the names and literals agree where the two shapes line up.

    A copy keeps its variable names and string literals in the stretches it
    did not edit; two bodies written to one skeleton share the skeleton and
    fill it differently. With nothing erased in the shared stretches there
    is nothing to disagree, which counts as agreement.
    """
    same = total = 0
    for i, j, size in matcher.get_matching_blocks():
        for first, second in zip(a.content[i : i + size], b.content[j : j + size], strict=True):
            if first is not None:
                total += 1
                same += first == second
    return same / total if total else 1.0


def _score(a: _Unit, b: _Unit) -> tuple[NearDuplicatePair, bool] | None:
    """The pair if it reaches the threshold, and whether its shapes are identical.

    Identical pairs are duplicate-blocks' finding, never reported here, but
    they are returned so that the windows inside them (a head of one copy
    against all of the other) are recognized as covered rather than drift.
    """
    identical = a.tokens == b.tokens
    matcher = difflib.SequenceMatcher(None, a.tokens, b.tokens, autojunk=False)
    similarity = 1.0
    if not identical:
        if matcher.quick_ratio() < SIMILARITY_THRESHOLD:
            return None
        similarity = round(matcher.ratio(), 2)
        if similarity < SIMILARITY_THRESHOLD:
            return None
        if _content_agreement(a, b, matcher) < MIN_CONTENT_AGREEMENT:
            return None
    first, second = sorted((a, b), key=lambda u: (u.span.file, u.span.line_start))
    if first is not a:
        matcher = difflib.SequenceMatcher(None, first.tokens, second.tokens, autojunk=False)
    pair = NearDuplicatePair(
        first=first.span,
        second=second.span,
        similarity=similarity,
        aligned=_alignment(first, second, matcher),
        first_stmts=tuple(first.stmts),
        second_stmts=tuple(second.stmts),
    )
    return pair, identical


def _dominated(pair: NearDuplicatePair, others: list[NearDuplicatePair]) -> bool:
    """Whether a larger pair between the same two files covers both spans of *pair*."""
    return any(
        other is not pair
        and other.first.contains(pair.first)
        and other.second.contains(pair.second)
        for other in others
    )


def find_near_duplicate_pairs(ctx: AnalysisContext) -> list[NearDuplicatePair]:
    """Every maximal near-duplicate pair in the production code.

    A pair is maximal when no other reported pair contains both of its
    spans; the sub-blocks of a near-duplicate function are therefore not
    reported again. Test files and Django migrations are excluded. Callers
    read the memoized ``AnalysisContext.near_duplicate_pairs``.
    """
    units: list[_Unit] = []
    for filepath, tree in source_trees(ctx):
        if is_migration_file(filepath):
            continue
        units.extend(_extract_units(str(filepath), tree))
    scored = []
    identical_pairs = set()
    for a, b in _candidate_pairs(units):
        result = _score(a, b)
        if result is not None:
            pair, identical = result
            scored.append(pair)
            if identical:
                identical_pairs.add(id(pair))
    # first.file < second.file in every pair, so one key per file pair.
    by_files: dict[tuple[str, str], list[NearDuplicatePair]] = defaultdict(list)
    for pair in scored:
        by_files[(pair.first.file, pair.second.file)].append(pair)
    maximal = [
        p
        for p in scored
        if id(p) not in identical_pairs
        and not _dominated(p, by_files[(p.first.file, p.second.file)])
    ]
    return sorted(maximal, key=lambda p: (_span_key(p.first), _span_key(p.second)))


# --- clusters -----------------------------------------------------------------


def _cluster(pairs: list[NearDuplicatePair]) -> list[list[NearDuplicatePair]]:
    """Group pairs whose spans overlap into clusters of copies of one block."""
    spans = sorted({s for pair in pairs for s in (pair.first, pair.second)}, key=_span_key)
    parent = {span: span for span in spans}

    def find(span: CodeSpan) -> CodeSpan:
        while parent[span] != span:
            parent[span] = parent[parent[span]]
            span = parent[span]
        return span

    def union(a: CodeSpan, b: CodeSpan) -> None:
        parent[find(a)] = find(b)

    for pair in pairs:
        union(pair.first, pair.second)
    by_file: dict[str, list[CodeSpan]] = defaultdict(list)
    for span in spans:
        by_file[span.file].append(span)
    for same_file in by_file.values():
        for index, span in enumerate(same_file):
            for later in same_file[index + 1 :]:
                if span.overlaps(later):
                    union(span, later)
    groups: dict[CodeSpan, list[NearDuplicatePair]] = defaultdict(list)
    for pair in pairs:
        groups[find(pair.first)].append(pair)
    return list(groups.values())


def _span_key(span: CodeSpan) -> tuple[str, int, int]:
    return (span.file, span.line_start, span.line_end)


def _members(cluster: list[NearDuplicatePair]) -> list[CodeSpan]:
    """Each copy in the cluster once — overlapping spans merged — in file/line order."""
    merged: list[CodeSpan] = []
    for span in sorted({s for p in cluster for s in (p.first, p.second)}, key=_span_key):
        if merged and merged[-1].overlaps(span):
            last = merged[-1]
            merged[-1] = CodeSpan(
                last.file, last.scope, last.line_start, max(last.line_end, span.line_end)
            )
        else:
            merged.append(span)
    return merged


def _diff_order(cluster: list[NearDuplicatePair]) -> list[NearDuplicatePair]:
    """A spanning tree of the cluster's pairs, most similar first.

    Each copy is diffed once, against the copy it most resembles, so the
    printed drift is the smallest set of diffs that reaches every copy.
    """
    remaining = sorted(
        cluster, key=lambda p: (-p.similarity, _span_key(p.first), _span_key(p.second))
    )
    reached: list[CodeSpan] = []
    tree: list[NearDuplicatePair] = []

    def seen(span: CodeSpan) -> bool:
        return any(span.overlaps(r) for r in reached)

    while remaining:
        bridge = next(
            (p for p in remaining if not reached or seen(p.first) != seen(p.second)), None
        )
        if bridge is None:
            break  # every pair left joins two copies already diffed
        remaining.remove(bridge)
        tree.append(bridge)
        reached.extend((bridge.first, bridge.second))
    return tree


# --- rendering ----------------------------------------------------------------


def _explode(expr: ast.expr, indent: str) -> list[str]:
    """*expr* as source lines, one argument or dict entry per line once it is long."""
    text = ast.unparse(expr)
    if len(indent) + len(text) <= RENDER_WIDTH:
        return [indent + text]
    inner = indent + "    "
    if isinstance(expr, ast.Call):
        items = [_explode(a, inner) for a in expr.args]
        for kw in expr.keywords:
            items.append(_entry(f"{kw.arg}=" if kw.arg else "**", kw.value, inner))
        return _bracketed(f"{indent}{ast.unparse(expr.func)}(", items, f"{indent})")
    if isinstance(expr, ast.Dict):
        items = []
        for key, value in zip(expr.keys, expr.values, strict=True):
            head = f"{ast.unparse(key)}: " if key is not None else "**"
            items.append(_entry(head, value, inner))
        return _bracketed(f"{indent}{{", items, f"{indent}}}")
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        brackets = {ast.List: "[]", ast.Tuple: "()", ast.Set: "{}"}[type(expr)]
        items = [_explode(e, inner) for e in expr.elts]
        return _bracketed(indent + brackets[0], items, indent + brackets[1])
    return [indent + text]


_ATOMS = (
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.Subscript,
    ast.Constant,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.DictComp,
    ast.ListComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.JoinedStr,
)


def _entry(head: str, value: ast.expr, indent: str) -> list[str]:
    """One argument or dict entry: ``head`` then *value*, parenthesized after ``**``."""
    if head == "**" and not isinstance(value, _ATOMS):
        return [f"{indent}**({ast.unparse(value)})"]
    lines = _explode(value, indent)
    return [indent + head + lines[0].lstrip(), *lines[1:]]


def _bracketed(open_line: str, items: list[list[str]], close_line: str) -> list[str]:
    lines = [open_line]
    for item in items:
        lines.extend(item[:-1])
        lines.append(item[-1] + ",")
    lines.append(close_line)
    return lines


def _render(stmts: tuple[ast.stmt, ...] | list[ast.stmt], indent: str = "") -> list[str]:
    """Readable source for *stmts*, rebuilt from the AST (comments are not code)."""
    lines: list[str] = []
    for stmt in stmts:
        lines.extend(_render_stmt(stmt, indent))
    return lines


def _render_stmt(stmt: ast.stmt, indent: str) -> list[str]:
    if isinstance(stmt, (ast.Return, ast.Assign, ast.Expr)) and stmt.value is not None:
        value = stmt.value
        if isinstance(stmt, ast.Return):
            prefix = "return "
        elif isinstance(stmt, ast.Assign):
            prefix = " = ".join(ast.unparse(t) for t in stmt.targets) + " = "
        else:
            prefix = ""
        value_lines = _explode(value, indent)
        return [indent + prefix + value_lines[0].lstrip(), *value_lines[1:]]
    if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        header = copy.copy(stmt)
        header.body = [ast.Pass()]
        if isinstance(header, _WITH_ELSE):
            header.orelse = []
        lines = [indent + ast.unparse(header).splitlines()[0]]
        lines.extend(_render(stmt.body, indent + "    "))
        if isinstance(stmt, _WITH_ELSE) and stmt.orelse:
            lines.append(indent + "else:")
            lines.extend(_render(stmt.orelse, indent + "    "))
        return lines
    if isinstance(stmt, ast.Try):
        lines = [indent + "try:", *_render(stmt.body, indent + "    ")]
        for handler in stmt.handlers:
            header = copy.copy(handler)
            header.body = [ast.Pass()]
            lines.append(indent + ast.unparse(header).splitlines()[0])
            lines.extend(_render(handler.body, indent + "    "))
        for label, body in (("else:", stmt.orelse), ("finally:", stmt.finalbody)):
            if body:
                lines.append(indent + label)
                lines.extend(_render(body, indent + "    "))
        return lines
    return [indent + line for line in ast.unparse(stmt).splitlines()]


def _location(span: CodeSpan, short: dict[str, str]) -> str:
    return f"{short[span.file]}:{span.scope}():{span.line_start}-{span.line_end}"


def _drift_lines(ranges: list[tuple[int, int] | None]) -> str:
    """The source lines a copy's drift stretches cover, as ``857, 860-861``."""
    lines = sorted({n for r in ranges if r is not None for n in range(r[0], r[1] + 1)})
    runs: list[list[int]] = []
    for line in lines:
        if runs and line == runs[-1][1] + 1:
            runs[-1][1] = line
        else:
            runs.append([line, line])
    return ", ".join(str(s) if s == e else f"{s}-{e}" for s, e in runs)


def _diff_header(span: CodeSpan, drift: list[tuple[int, int] | None], short: dict) -> str:
    named = _drift_lines(drift)
    return f"{_location(span, short)} (drift at {named})" if named else _location(span, short)


def _drift_diff(order: list[NearDuplicatePair], short: dict[str, str]) -> tuple[list[str], int]:
    """Unified diffs along the spanning tree, capped; returns lines and how many were cut."""
    lines: list[str] = []
    for pair in order:
        drift = [a for a in pair.aligned if not a.same]
        diff = difflib.unified_diff(
            _render(pair.first_stmts),
            _render(pair.second_stmts),
            fromfile=_diff_header(pair.first, [a.first for a in drift], short),
            tofile=_diff_header(pair.second, [a.second for a in drift], short),
            n=1,
            lineterm="",
        )
        # The renderer's own line numbers mean nothing in the source: keep
        # the hunk marker, drop its ranges.
        lines.extend("@@" if line.startswith("@@") else line for line in diff)
    return lines[:MAX_DIFF_LINES], max(0, len(lines) - MAX_DIFF_LINES)


@check(
    "near-duplicate-with-drift",
    severity=Severity.MEDIUM,
    description=(
        "Similar-but-not-identical code in different files — copies that have drifted, "
        "with a unified diff of the drift"
    ),
    advice=Advice(
        fix=(
            "Decide which side of the diff is right, extract that version into one function,"
            " and call it from every copy listed."
        ),
        dismiss=(
            "Dismiss only if the diff shows the copies must behave differently and would"
            " not change together, and say so beside one of them."
        ),
    ),
)
def check_near_duplicate_with_drift(ctx: AnalysisContext) -> list[Finding]:
    """Report each cluster of drifted copies once, at its first copy."""
    findings = []
    clusters = _cluster(ctx.near_duplicate_pairs)
    for cluster in clusters:
        members = _members(cluster)
        short = _shorten_paths(
            [s.file for s in members] + [s.file for p in cluster for s in (p.first, p.second)]
        )
        order = _diff_order(cluster)
        diff, cut = _drift_diff(order, short)
        if cut:
            diff.append(f"... diff capped at {MAX_DIFF_LINES} lines; {cut} more not shown")
        lowest = min(p.similarity for p in cluster)
        highest = max(p.similarity for p in cluster)
        similarity = f"{lowest:.2f}" if lowest == highest else f"{lowest:.2f}-{highest:.2f}"
        anchor = members[0]
        message = (
            f"{len(members)} copies of one block have drifted apart "
            f"(similarity {similarity}): "
            + ", ".join(_location(s, short) for s in members)
            + "\n"
            + "\n".join(f"      {line}" for line in diff)
            + "\n      Fold the copies into one helper so the next fix lands once; "
            "if they are meant to evolve independently, say so with "
            f"`# pysmelly: ignore[near-duplicate-with-drift]` on line {anchor.line_start}."
        )
        findings.append(
            Finding(
                file=anchor.file,
                line=anchor.line_start,
                check="near-duplicate-with-drift",
                message=message,
                severity=Severity.MEDIUM,
            )
        )
    return sorted(findings, key=lambda f: (f.file, f.line))
