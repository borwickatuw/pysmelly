"""Provenance lines: where a vestigial name came from, as git tells it (Phase 29-2).

A finding about a vestigial thing names it as a ``Subject`` marked
``vestigial``. For each finding the output displays, ``ProvenanceReader``
runs one ``git log -S`` per such subject (``GitQueries.token_commits``) and
prints ``added in <sha>
(<subject>)`` under it when git has the answer, and nothing when it has not.
Nothing runs for a finding the output does not display, for a run printing
only counts, or outside a git repository.

``, last production reader removed in <sha> (<subject>)`` follows only when
it is proven from the code itself, not from how often the name's text
appears: the subject has no production reader at HEAD, and the named commit
is the newest one that changed how many readers it has, taking its parent's
readers down to none. A reader is a load of the name in the parsed code
(``_module_readers``, ``_param_readers``), so a docstring mention or a
comment is not one, and a commit that rewrites two reads as one removed
none. When any of this cannot be read (a revision that does not parse, a
literal, which has no readers), the line says where the subject was added
and nothing more.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pysmelly.discovery import GitNotFoundError, get_git_root
from pysmelly.git_history import (
    CommitRef,
    GitQueries,
    Provenance,
    TokenCommit,
    TokenTrace,
    display_text,
    qualified_definitions,
    trace_addition,
    whole_word_pattern,
)
from pysmelly.registry import function_scope

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext
    from pysmelly.registry import Finding, Subject


def _no_lines(findings: list[Finding]) -> list[list[str]]:
    return [[] for _ in findings]


def with_notes(
    findings: list[Finding], printed: list[Finding], notes: list[list[str]]
) -> list[Finding]:
    """``findings``, with each of ``printed`` (some of them, by identity) carrying its ``notes``.

    ``notes`` is the reader's answer for ``printed``, one list per finding.
    """
    if len(notes) != len(printed):
        msg = f"{len(notes)} lists of notes for {len(printed)} printed findings"
        raise ValueError(msg)
    added = {id(finding): tuple(lines) for finding, lines in zip(printed, notes, strict=True)}
    return [
        replace(finding, notes=added[id(finding)]) if added.get(id(finding)) else finding
        for finding in findings
    ]


# One ``git log -S``: the token, its pathspecs, and whether it follows one file's renames.
_Query = tuple[str, tuple[str, ...], bool]

# How many readers of a subject one file's text holds; None when that cannot be read.
_Readers = Callable[[str], int | None]

_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _parsed(source: str) -> ast.Module | None:
    """``source`` parsed, or None when it does not parse (an old revision, another dialect)."""
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _module_readers(name: str) -> _Readers:
    """Readers of a module-level name: each load of it, bare or as an attribute."""

    def count(source: str) -> int | None:
        tree = _parsed(source)
        if tree is None:
            return None
        return sum(
            1
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load))
            or (
                isinstance(node, ast.Attribute)
                and node.attr == name
                and isinstance(node.ctx, ast.Load)
            )
        )

    return count


def _param_readers(qualname: str, name: str) -> _Readers:
    """Readers of a parameter: each load of the bare name in the body of the function ``qualname``.

    A revision without that function holds none; one with two (a property
    and its setter) cannot say whose parameter it is.
    """

    def count(source: str) -> int | None:
        tree = _parsed(source)
        if tree is None:
            return None
        functions = [
            node
            for found, node in qualified_definitions(tree)
            if found == qualname and isinstance(node, _Function)
        ]
        if len(functions) > 1:
            return None
        return sum(
            1
            for function in functions
            for statement in function.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        )

    return count


def _parameter_names(function: _Function) -> set[str]:
    arguments = function.args
    named = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    starred = [arguments.vararg, arguments.kwarg]
    return {argument.arg for argument in [*named, *starred] if argument is not None}


class ProvenanceReader:
    """The provenance lines for the findings a run displays, over the analyzed root ``base``.

    Building it runs nothing. The first call that has a subject to trace
    finds the git root; a target outside git, before its first commit, or in
    a shallow clone gets no lines.
    """

    def __init__(self, base: Path, ctx: AnalysisContext) -> None:
        self.base = base
        self.ctx = ctx
        self._queries: GitQueries | None = None
        self._offset: PurePosixPath | None = None
        self._resolved = False

    def __call__(self, findings: list[Finding]) -> list[list[str]]:
        """One list of lines per finding: a line per subject git can place."""
        if not any(_traced(finding) for finding in findings):
            return _no_lines(findings)
        queries = self._git()
        if queries is None:
            return _no_lines(findings)
        planned = {
            subject: query
            for finding in findings
            for subject in _traced(finding)
            if (query := self._query(subject)) is not None
        }
        distinct = sorted(set(planned.values()))
        with ThreadPoolExecutor() as pool:
            commits = {
                query: pool.submit(queries.token_commits, query[0], query[1], follow=query[2])
                for query in distinct
            }
            traced = {
                subject: pool.submit(self._provenance, subject, query, commits[query].result())
                for subject, query in planned.items()
            }
            answers = {subject: future.result() for subject, future in traced.items()}
        lines = []
        for finding in findings:
            found = [(subject, answers.get(subject)) for subject in _traced(finding)]
            lines.append(
                [_line(subject, provenance) for subject, provenance in found if provenance]
            )
        return lines

    def _provenance(
        self, subject: Subject, query: _Query, commits: list[TokenCommit]
    ) -> Provenance | None:
        """``subject``'s provenance from its ``git log -S`` answer; None when git has none."""
        counts = self._counter(follow=query[2])
        trace = trace_addition(subject.name, commits, counts)
        if trace is None:
            return None
        readers = _subject_readers(subject, self.ctx.all_trees)
        if readers is None:
            return Provenance(trace.added, None)
        return Provenance(trace.added, self._last_reader_removal(trace, query, counts, readers))

    def _last_reader_removal(
        self,
        trace: TokenTrace,
        query: _Query,
        counts: Callable[[str], bool],
        readers: _Readers,
    ) -> CommitRef | None:
        """The commit that removed the subject's last production reader, when that is proven.

        HEAD must hold no reader. Walking back from HEAD through the commits
        that changed the name's count, the first that changed how many
        readers it has must have taken them down, and left none anywhere.
        Anything else (a reader at HEAD, a commit that added readers, one
        that left some, a revision that does not parse) proves nothing.
        """
        if self._readers_at("HEAD", query, counts, readers) != 0:
            return None
        for token_commit in trace.later:
            around = self._readers_around(token_commit, counts, readers)
            if around is None:
                return None
            before, after = around
            if before == after:
                continue
            if after > before:
                return None
            # A followed subject lives in the one file the commit changed.
            follow = query[2]
            left = (
                after
                if follow
                else self._readers_at(token_commit.commit.sha, query, counts, readers)
            )
            return token_commit.commit if left == 0 else None
        return None

    def _readers_around(
        self, token_commit: TokenCommit, counts: Callable[[str], bool], readers: _Readers
    ) -> tuple[int, int] | None:
        """Readers in the counted files a commit changed the token in: before it, and after."""
        queries = self._repository()
        sha = token_commit.commit.sha
        changed = {
            (hunk.old_file, hunk.new_file) for hunk in token_commit.hunks if counts(hunk.file)
        }
        before = after = 0
        for old, new in changed:
            then = 0 if old is None else readers(queries.blob(f"{sha}^", old))
            now = 0 if new is None else readers(queries.blob(sha, new))
            if then is None or now is None:
                return None
            before += then
            after += now
        return before, after

    def _readers_at(
        self, rev: str, query: _Query, counts: Callable[[str], bool], readers: _Readers
    ) -> int | None:
        """The subject's readers in every counted file ``rev`` holds within the query's paths."""
        queries = self._repository()
        token, pathspecs, _follow = query
        counted = [path for path in queries.files_with_word(rev, token, pathspecs) if counts(path)]
        if not counted:
            return 0
        found = [readers("\n".join(lines)) for lines in queries.lines_at(rev, counted).values()]
        if None in found:
            return None
        return sum(count for count in found if count is not None)

    def _git(self) -> GitQueries | None:
        if not self._resolved:
            self._resolved = True
            try:
                git_root = get_git_root(self.base)
            except GitNotFoundError:
                return None
            queries = GitQueries(git_root)
            if queries.has_complete_history():
                self._queries = queries
                self._offset = PurePosixPath(self.base.relative_to(git_root).as_posix())
        return self._queries

    def _repository(self) -> GitQueries:
        """The repository's queries, once ``_git`` has found complete history."""
        if self._queries is None:
            msg = "the git root is resolved before any history is read"
            raise RuntimeError(msg)
        return self._queries

    def _git_path(self, file: str) -> str:
        """An analyzed file's path from the git root."""
        if self._offset is None:
            msg = "the git root is resolved before any path is mapped"
            raise RuntimeError(msg)
        return (self._offset / file).as_posix()

    def _query(self, subject: Subject) -> _Query | None:
        """The ``git log -S`` that traces ``subject``, or None when history cannot tell it apart.

        A module-level name is looked for in every Python file under the
        analyzed root. A scoped one (a parameter, a literal) is looked for in
        its own file, through renames, and only when every whole-word
        occurrence of it in that file sits inside its scope: otherwise the
        file's history of the text is not this subject's.
        """
        name, file, scope = subject.name, subject.file, subject.scope
        if scope is None:
            return name, (self._python_files(),), False
        if not _confined(name, scope, self._source(file)):
            return None
        return name, (f":(literal){self._git_path(file)}",), True

    def _source(self, file: str) -> str:
        """An analyzed file's text, as the analysis read it."""
        return (self.base / file).read_text(encoding="utf-8")

    def _python_files(self) -> str:
        """The pathspec for every Python file under the analyzed root."""
        if self._offset == PurePosixPath():
            return "*.py"
        return f":(glob){self._offset}/**/*.py"

    def _counter(self, *, follow: bool) -> Callable[[str], bool]:
        """Which files a query's hunks count in: all of one followed file, else production only."""
        if follow:
            return lambda _path: True
        return self._is_production

    def _is_production(self, git_path: str) -> bool:
        """Whether a file git names, under the analyzed root, is production code."""
        if self._offset is None:
            msg = "the git root is resolved before any path is judged"
            raise RuntimeError(msg)
        path = PurePosixPath(git_path)
        if not path.is_relative_to(self._offset):
            return False
        return not self.ctx.roles.is_test(Path(path.relative_to(self._offset)))


def _traced(finding: Finding) -> list[Subject]:
    """The subjects of ``finding`` that get a provenance line: the ones it calls vestigial."""
    return [subject for subject in finding.subjects if subject.vestigial]


def _subject_readers(subject: Subject, trees: Mapping[Path, ast.Module]) -> _Readers | None:
    """How to count ``subject``'s readers in a file's text; None when it has none to count.

    ``trees`` are the analyzed files' parsed text. A literal (a scoped
    subject with no ``parameter_of``) has none; a parameter's readers are
    counted in the one function at its scope that declares it.
    """
    name, scope = subject.name, subject.scope
    if scope is None:
        return _module_readers(name)
    if subject.parameter_of is None:
        return None
    tree = trees.get(Path(subject.file))
    if tree is None:
        return None
    owners = [
        qualname
        for qualname, node in qualified_definitions(tree)
        if isinstance(node, _Function)
        and function_scope(node) == scope
        and name in _parameter_names(node)
    ]
    if len(owners) != 1:
        return None
    return _param_readers(owners[0], name)


def _confined(name: str, scope: tuple[int, int], text: str) -> bool:
    """Whether every line of ``text`` holding ``name`` as a whole word sits inside ``scope``."""
    first, last = scope
    pattern = whole_word_pattern(name)
    return all(
        first <= number <= last
        for number, line in enumerate(text.split("\n"), 1)
        if pattern.search(line)
    )


def _line(subject: Subject, provenance: Provenance) -> str:
    return f"provenance of {display_text(subject.name)}: {provenance.describe()}"
