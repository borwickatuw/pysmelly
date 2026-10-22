"""Tests for the line-level git primitives: commit hunks, function history, line ages."""

from __future__ import annotations

import ast
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pysmelly.git_history as git_history_module
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.git_history import BlamedLine, GitHistory, GitHistoryQueryError, LineHistory

_PRICING_V1 = """\
def keep():
    return 0


def compute_total(order_lines, discount_rate):
    subtotal = sum(line.price * line.quantity for line in order_lines)
    return subtotal * (1 - discount_rate)


class Cart:
    @property
    def total(self):
        return 0
"""

# fix: Cart.total reads the items (body edit, lines 13).
_PRICING_V2 = _PRICING_V1.replace("        return 0\n", "        return sum(self.items)\n")

# rename pricing.py -> billing.py, plus an edit to Cart.total (line 13).
_BILLING_V3 = _PRICING_V2.replace("sum(self.items)", "sum(self.items) + self.shipping")

# move compute_total out of billing.py into totals.py.
_BILLING_V4 = """\
def keep():
    return 0


class Cart:
    @property
    def total(self):
        return sum(self.items) + self.shipping
"""

_TOTALS_V4 = """\
import math


def compute_total(order_lines, discount_rate):
    subtotal = sum(line.price * line.quantity for line in order_lines)
    return subtotal * (1 - discount_rate)
"""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str, when: datetime | None = None) -> str:
    """Write ``files``, stage them by path, commit, and return the new sha."""
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "--", name)
    env = None
    if when is not None:
        stamp = when.isoformat()
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _days_ago(days: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0)


# Inside a 6m window, ten days apart, so each commit's author time is its own.
_WHEN = {"add": _days_ago(60), "fix": _days_ago(50), "rename": _days_ago(40), "move": _days_ago(30)}


@pytest.fixture
def pricing_repo(git_repo):
    """Four commits: add, fix, rename with an edit, move a function to another file."""
    shas = {
        "add": _commit(git_repo, {"pricing.py": _PRICING_V1}, "feat: add pricing", _WHEN["add"])
    }
    shas["fix"] = _commit(
        git_repo, {"pricing.py": _PRICING_V2}, "fix: cart total reads items", _WHEN["fix"]
    )
    _git(git_repo, "mv", "pricing.py", "billing.py")
    shas["rename"] = _commit(
        git_repo, {"billing.py": _BILLING_V3}, "refactor: rename to billing", _WHEN["rename"]
    )
    shas["move"] = _commit(
        git_repo,
        {"billing.py": _BILLING_V4, "totals.py": _TOTALS_V4},
        "refactor: move compute_total to totals",
        _WHEN["move"],
    )
    return git_repo, shas


def _lines(repo: Path) -> LineHistory:
    return GitHistory(repo, window="6m").line_history


class TestCommitHunks:
    def test_edit_gives_file_ranges_and_added_lines(self, pricing_repo):
        repo, shas = pricing_repo
        hunks = _lines(repo).commit_hunks(shas["fix"])
        assert len(hunks) == 1
        hunk = hunks[0]
        assert (hunk.file, hunk.old_file, hunk.new_file) == (
            "pricing.py",
            "pricing.py",
            "pricing.py",
        )
        assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (13, 1, 13, 1)
        assert hunk.old_lines == range(13, 14)
        assert hunk.new_lines == range(13, 14)
        assert hunk.added == ["        return sum(self.items)"]
        assert hunk.body == ("-        return 0", "+        return sum(self.items)")

    def test_first_commit_adds_the_whole_file(self, pricing_repo):
        repo, shas = pricing_repo
        (hunk,) = _lines(repo).commit_hunks(shas["add"])
        assert hunk.old_file is None
        assert hunk.new_file == "pricing.py"
        assert hunk.old_lines == range(0)
        assert hunk.new_lines == range(1, 14)
        assert hunk.added == _PRICING_V1.splitlines()

    def test_rename_with_edit_names_both_paths(self, pricing_repo):
        repo, shas = pricing_repo
        (hunk,) = _lines(repo).commit_hunks(shas["rename"])
        assert hunk.file == "billing.py"
        assert hunk.old_file == "pricing.py"
        assert hunk.added == ["        return sum(self.items) + self.shipping"]

    def test_function_moved_between_files(self, pricing_repo):
        repo, shas = pricing_repo
        hunks = _lines(repo).commit_hunks(shas["move"])
        by_file = {hunk.file: hunk for hunk in hunks}
        assert set(by_file) == {"billing.py", "totals.py"}
        removed = by_file["billing.py"]
        assert removed.new_count == 0
        assert removed.added == []
        assert removed.old_lines == range(5, 10)
        added = by_file["totals.py"]
        assert added.old_file is None
        assert added.added == _TOTALS_V4.splitlines()

    def test_deleted_file_is_named_by_its_old_path(self, pricing_repo):
        repo, _shas = pricing_repo
        _git(repo, "rm", "-q", "totals.py")
        _git(repo, "commit", "-m", "chore: drop totals")
        (hunk,) = _lines(repo).commit_hunks(_git(repo, "rev-parse", "HEAD"))
        assert (hunk.file, hunk.new_file) == ("totals.py", None)
        assert hunk.old_lines == range(1, 7)

    def test_non_ascii_and_quoted_paths(self, git_repo):
        sha = _commit(
            git_repo,
            {"café.py": "x = 1\n", "tab\tname.py": "y = 2\n", "sp ace.py": "z = 3\n"},
            "feat: odd names",
        )
        files = {hunk.file for hunk in _lines(git_repo).commit_hunks(sha)}
        assert files == {"café.py", "tab\tname.py", "sp ace.py"}

    def test_missing_newline_marker_is_not_a_line(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1"}, "feat: no newline")
        sha = _commit(git_repo, {"a.py": "x = 2"}, "fix: still no newline")
        (hunk,) = _lines(git_repo).commit_hunks(sha)
        assert hunk.body == ("-x = 1", "+x = 2")

    def test_non_python_files_are_left_out(self, git_repo):
        sha = _commit(git_repo, {"a.py": "x = 1\n", "notes.txt": "hi\n"}, "feat: add")
        assert [hunk.file for hunk in _lines(git_repo).commit_hunks(sha)] == ["a.py"]

    def test_merge_with_a_resolved_conflict_is_rejected(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n"}, "feat: add")
        trunk = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        _git(git_repo, "checkout", "-q", "-b", "side")
        _commit(git_repo, {"a.py": "x = 2\n"}, "feat: side")
        _git(git_repo, "checkout", "-q", trunk)
        _commit(git_repo, {"a.py": "x = 3\n"}, "feat: trunk")
        subprocess.run(["git", "merge", "side"], cwd=git_repo, capture_output=True, check=False)
        merge = _commit(git_repo, {"a.py": "x = 4\n"}, "merge side")
        with pytest.raises(GitHistoryQueryError, match="combined diff"):
            _lines(git_repo).commit_hunks(merge)

    def test_unknown_commit_fails_fast(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n"}, "feat: add")
        with pytest.raises(GitHistoryQueryError, match="failed"):
            _lines(git_repo).commit_hunks("0" * 40)


class TestFunctionHistory:
    def test_follows_a_rename_back_to_the_first_commit(self, pricing_repo):
        repo, shas = pricing_repo
        revisions = _lines(repo).function_history("billing.py", "Cart.total")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [shas["rename"], shas["fix"], shas["add"]]
        (rename_hunk,) = revisions[0].hunks
        assert (rename_hunk.old_file, rename_hunk.new_file) == ("pricing.py", "billing.py")
        assert revisions[0].added == ["        return sum(self.items) + self.shipping"]
        assert revisions[1].added == ["        return sum(self.items)"]
        assert revisions[-1].hunks[0].old_file is None

    def test_range_is_the_ast_definition_at_head(self, pricing_repo):
        repo, _shas = pricing_repo
        revisions = _lines(repo).function_history("billing.py", "Cart.total")
        assert revisions is not None
        (node,) = [
            n
            for n in ast.walk(ast.parse(_BILLING_V4))
            if isinstance(n, ast.FunctionDef) and n.name == "total"
        ]
        assert node.end_lineno is not None
        definition = _BILLING_V4.splitlines()[node.decorator_list[0].lineno - 1 : node.end_lineno]
        assert revisions[0].source == "\n".join(definition)
        assert revisions[-1].source == "\n".join(_PRICING_V1.splitlines()[10:13])

    def test_function_moved_between_files_starts_at_the_move(self, pricing_repo):
        repo, shas = pricing_repo
        revisions = _lines(repo).function_history("totals.py", "compute_total")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [shas["move"]]
        assert revisions[0].added == _TOTALS_V4.splitlines()[3:6]

    def test_commits_outside_the_window_are_left_out(self, git_repo):
        _commit(git_repo, {"a.py": "def f():\n    return 1\n"}, "feat: add f", _days_ago(400))
        recent = _commit(git_repo, {"a.py": "def f():\n    return 2\n"}, "fix: f")
        revisions = _lines(git_repo).function_history("a.py", "f")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [recent]

    def test_absent_definition_or_file_has_no_history(self, pricing_repo):
        repo, _shas = pricing_repo
        lines = _lines(repo)
        assert lines.function_history("billing.py", "compute_total") is None
        assert lines.function_history("pricing.py", "Cart.total") is None

    def test_uncommitted_edits_do_not_shift_the_range(self, pricing_repo):
        repo, shas = pricing_repo
        (repo / "billing.py").write_text("\n\n\n" + _BILLING_V4, encoding="utf-8")
        revisions = _lines(repo).function_history("billing.py", "Cart.total")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [shas["rename"], shas["fix"], shas["add"]]

    def test_nested_qualname(self, git_repo):
        source = "def outer():\n    def inner():\n        return 1\n\n    return inner\n"
        sha = _commit(git_repo, {"n.py": source}, "feat: add outer")
        revisions = _lines(git_repo).function_history("n.py", "outer.inner")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [sha]
        assert revisions[0].source == "    def inner():\n        return 1"

    def test_ambiguous_qualname_fails_fast(self, git_repo):
        source = (
            "class C:\n"
            "    @property\n"
            "    def v(self):\n"
            "        return 1\n"
            "\n"
            "    @v.setter\n"
            "    def v(self, value):\n"
            "        pass\n"
        )
        _commit(git_repo, {"c.py": source}, "feat: add C")
        with pytest.raises(ValueError, match="names 2 definitions"):
            _lines(git_repo).function_history("c.py", "C.v")

    def test_file_that_does_not_parse_at_head_fails_fast(self, git_repo):
        _commit(git_repo, {"b.py": "def f(:\n"}, "feat: broken")
        with pytest.raises(GitHistoryQueryError, match="does not parse"):
            _lines(git_repo).function_history("b.py", "f")

    def test_non_ascii_path(self, git_repo):
        sha = _commit(git_repo, {"café.py": "def f():\n    return 1\n"}, "feat: add f")
        revisions = _lines(git_repo).function_history("café.py", "f")
        assert revisions is not None
        assert [rev.sha for rev in revisions] == [sha]
        assert revisions[0].hunks[0].file == "café.py"


class TestLineAges:
    def test_each_line_is_as_old_as_the_commit_that_last_wrote_it(self, pricing_repo):
        repo, _shas = pricing_repo
        ages = _lines(repo).line_ages("billing.py", [1, 7, 8])
        assert ages == {1: _WHEN["add"], 7: _WHEN["add"], 8: _WHEN["rename"]}

    def test_function_moved_between_files_keeps_its_age(self, pricing_repo):
        repo, _shas = pricing_repo
        ages = _lines(repo).line_ages("totals.py", range(1, 7))
        assert ages[1] == _WHEN["move"]
        assert [ages[line] for line in (4, 5, 6)] == [_WHEN["add"]] * 3

    def test_renamed_file_keeps_its_age(self, pricing_repo):
        repo, _shas = pricing_repo
        assert _lines(repo).line_ages("billing.py", [7]) == {7: _WHEN["add"]}

    def test_whitespace_only_change_does_not_make_a_line_young(self, git_repo):
        written = _days_ago(90)
        _commit(git_repo, {"a.py": "x = [1,2]\n"}, "feat: add x", written)
        _commit(git_repo, {"a.py": "x = [1, 2]\n"}, "style: reformat", _days_ago(10))
        assert _lines(git_repo).line_ages("a.py", [1]) == {1: written}

    def test_non_ascii_path(self, git_repo):
        when = _days_ago(20)
        _commit(git_repo, {"café.py": "x = 1\ny = 2\n"}, "feat: add", when)
        assert _lines(git_repo).line_ages("café.py", [2]) == {2: when}

    def test_empty_line_set_runs_no_git(self, git_repo, monkeypatch):
        lines = _lines(git_repo)
        calls = _count_git(monkeypatch)
        assert lines.line_ages("a.py", []) == {}
        assert calls == []

    def test_line_past_the_end_fails_fast(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n"}, "feat: add")
        with pytest.raises(GitHistoryQueryError, match="failed"):
            _lines(git_repo).line_ages("a.py", [5])


class TestFilesLineAges:
    def test_answers_each_file_as_line_ages_does(self, pricing_repo):
        repo, _shas = pricing_repo
        lines = _lines(repo)
        ages = lines.files_line_ages({"billing.py": [1, 8], "totals.py": [1, 4]})
        assert ages == {
            "billing.py": lines.line_ages("billing.py", [1, 8]),
            "totals.py": lines.line_ages("totals.py", [1, 4]),
        }

    def test_one_failing_file_fails_the_batch(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n", "b.py": "y = 2\n"}, "feat: add")
        with pytest.raises(GitHistoryQueryError, match="failed"):
            _lines(git_repo).files_line_ages({"a.py": [1], "b.py": [5]})


class TestHeadPaths:
    def test_lists_committed_files_only(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n", "café.py": "y = 2\n"}, "feat: add")
        (git_repo / "untracked.py").write_text("z = 3\n", encoding="utf-8")
        assert _lines(git_repo).head_paths() == {"a.py", "café.py"}


def _count_git(monkeypatch) -> list[list[str]]:
    """Record every git argument list git_history runs, still running git."""
    calls: list[list[str]] = []
    real_run_git = git_history_module.run_git

    def counting_run_git(args, cwd):
        calls.append(list(args))
        return real_run_git(args, cwd)

    monkeypatch.setattr(git_history_module, "run_git", counting_run_git)
    return calls


class TestCaching:
    def test_reaching_the_layer_runs_no_git(self, pricing_repo, monkeypatch):
        repo, _shas = pricing_repo
        history = GitHistory(repo, window="6m")
        calls = _count_git(monkeypatch)
        assert history.line_history is history.line_history
        assert calls == []

    def test_repeated_queries_run_git_once(self, pricing_repo, monkeypatch):
        repo, shas = pricing_repo
        lines = _lines(repo)
        calls = _count_git(monkeypatch)

        first_hunks = lines.commit_hunks(shas["fix"])
        first_history = lines.function_history("billing.py", "Cart.total")
        first_ages = lines.line_ages("billing.py", [8, 7, 1])
        assert [args[0] for args in calls] == ["show", "ls-tree", "cat-file", "log", "blame"]
        runs_after_first = len(calls)

        assert lines.commit_hunks(shas["fix"]) == first_hunks
        assert lines.function_history("billing.py", "Cart.total") == first_history
        assert lines.line_ages("billing.py", [1, 7, 8, 8]) == first_ages
        assert len(calls) == runs_after_first

    def test_a_different_query_runs_git_again(self, pricing_repo, monkeypatch):
        repo, shas = pricing_repo
        lines = _lines(repo)
        calls = _count_git(monkeypatch)
        lines.commit_hunks(shas["fix"])
        lines.commit_hunks(shas["rename"])
        assert len(calls) == 2

    def test_scattered_lines_are_one_blame(self, pricing_repo, monkeypatch):
        repo, _shas = pricing_repo
        lines = _lines(repo)
        calls = _count_git(monkeypatch)
        lines.line_ages("billing.py", [1, 2, 7, 8])
        (blame_args,) = calls
        assert "-L1,2" in blame_args
        assert "-L7,8" in blame_args


class TestReachability:
    def test_reachable_from_the_context_in_a_git_root(self, pricing_repo):
        repo, shas = pricing_repo
        ctx = AnalysisContext({}, verbose=False, git=GitSettings(root=repo))
        assert ctx.git_history is not None
        assert ctx.git_history.line_history.commit_hunks(shas["fix"])

    def test_absent_outside_a_git_root(self):
        ctx = AnalysisContext({}, verbose=False)
        assert ctx.git_history is None


class TestHeadQueries:
    def test_head_source_is_the_committed_text(self, pricing_repo):
        repo, _ = pricing_repo
        (repo / "billing.py").write_text("x = 1\n", encoding="utf-8")
        assert _lines(repo).head_source("billing.py") == _BILLING_V4

    def test_head_source_and_tree_are_none_for_a_path_head_lacks(self, pricing_repo):
        repo, _ = pricing_repo
        (repo / "draft.py").write_text("x = 1\n", encoding="utf-8")
        lines = _lines(repo)
        assert lines.head_source("pricing.py") is None
        assert lines.head_tree("draft.py") is None

    def test_head_tree_parses_the_committed_text(self, pricing_repo):
        repo, _ = pricing_repo
        tree = _lines(repo).head_tree("totals.py")
        assert tree is not None
        assert [type(node).__name__ for node in tree.body] == ["Import", "FunctionDef"]

    def test_head_tree_that_does_not_parse_raises(self, git_repo):
        _commit(git_repo, {"broken.py": "def (\n"}, "feat: add broken")
        with pytest.raises(GitHistoryQueryError, match="broken.py at HEAD does not parse"):
            _lines(git_repo).head_tree("broken.py")

    def test_head_files_matching_reads_head_case_insensitively(self, pricing_repo):
        repo, _ = pricing_repo
        (repo / "billing.py").write_text("# SHIPPING\n", encoding="utf-8")
        lines = _lines(repo)
        assert lines.head_files_matching("SHIPPING") == {"billing.py"}
        assert lines.head_files_matching("subtotal|^import") == {"totals.py"}

    def test_head_files_matching_nothing_is_empty_not_an_error(self, pricing_repo):
        repo, _ = pricing_repo
        assert _lines(repo).head_files_matching("no such text anywhere") == frozenset()

    def test_head_files_matching_a_bad_pattern_raises(self, pricing_repo):
        repo, _ = pricing_repo
        with pytest.raises(GitHistoryQueryError, match="failed"):
            _lines(repo).head_files_matching("(unclosed")


class TestBatchedQueries:
    def test_ranges_history_tracks_ranges_in_several_files_in_one_git_call(
        self, pricing_repo, monkeypatch
    ):
        repo, shas = pricing_repo
        lines = _lines(repo)
        calls = _count_git(monkeypatch)
        revisions = lines.ranges_history([("billing.py", 7, 8), ("totals.py", 4, 6)])
        assert len(calls) == 1
        assert [r.sha for r in revisions] == [
            shas["move"],
            shas["rename"],
            shas["fix"],
            shas["add"],
        ]
        (moved,) = [r for r in revisions if r.sha == shas["move"]]
        assert {hunk.file for hunk in moved.hunks} == {"totals.py"}
        (renamed,) = [r for r in revisions if r.sha == shas["rename"]]
        assert renamed.hunks[0].old_side == [
            "    def total(self):",
            "        return sum(self.items)",
        ]
        assert renamed.hunks[0].removed == ["        return sum(self.items)"]

    def test_ranges_history_matches_function_history_for_one_range(self, pricing_repo):
        repo, _ = pricing_repo
        lines = _lines(repo)
        assert lines.ranges_history([("billing.py", 6, 8)]) == lines.function_history(
            "billing.py", "Cart.total"
        )

    def test_ranges_history_needs_a_range(self, pricing_repo):
        repo, _ = pricing_repo
        with pytest.raises(ValueError, match="at least one range"):
            _lines(repo).ranges_history([])

    def test_edit_hunks_keeps_modified_and_renamed_files_only(self, pricing_repo, monkeypatch):
        repo, shas = pricing_repo
        lines = _lines(repo)
        calls = _count_git(monkeypatch)
        hunks = lines.edit_hunks(
            [shas["add"], shas["fix"], shas["rename"], shas["move"]],
            ["pricing.py", "billing.py", "totals.py"],
        )
        assert len(calls) == 1
        assert hunks[shas["add"]] == []
        assert [h.file for h in hunks[shas["fix"]]] == ["pricing.py"]
        assert {h.file for h in hunks[shas["move"]]} == {"billing.py"}

    def test_edit_hunks_takes_paths_literally(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\n", "b.py": "y = 1\n"}, "feat: add")
        sha = _commit(git_repo, {"a.py": "x = 2\n", "b.py": "y = 2\n"}, "fix: bump")
        hunks = _lines(git_repo).edit_hunks([sha], ["?.py"])
        assert hunks == {sha: []}

    def test_edit_hunks_needs_shas_and_paths(self, pricing_repo):
        repo, shas = pricing_repo
        with pytest.raises(ValueError, match="at least one sha and one path"):
            _lines(repo).edit_hunks([shas["fix"]], [])

    def test_lines_at_reads_several_files_in_one_git_call(self, pricing_repo, monkeypatch):
        repo, _ = pricing_repo
        (repo / "billing.py").write_text("x = 1\n", encoding="utf-8")
        lines = _lines(repo)
        calls = _count_git(monkeypatch)
        found = lines.lines_at("HEAD", ["billing.py", "totals.py", "pricing.py"])
        assert len(calls) == 1
        assert found == {
            "billing.py": _BILLING_V4.split("\n")[:-1],
            "totals.py": _TOTALS_V4.split("\n")[:-1],
        }

    def test_lines_at_of_paths_head_lacks_is_empty(self, pricing_repo):
        repo, _ = pricing_repo
        assert _lines(repo).lines_at("HEAD", ["pricing.py"]) == {}

    def test_lines_at_keeps_a_lone_carriage_return_inside_its_line(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\ry = 2\nz = 3\n"}, "add a")
        found = _lines(git_repo).lines_at("HEAD", ["a.py"])
        assert found == {"a.py": ["x = 1\ry = 2", "z = 3"]}

    def test_head_changed_paths_names_working_tree_edits(self, pricing_repo):
        repo, _ = pricing_repo
        (repo / "billing.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "notes.txt").write_text("hi\n", encoding="utf-8")
        assert _lines(repo).head_changed_paths() == {"billing.py"}


class TestRenames:
    def test_renames_come_from_the_lifecycle_query(self, pricing_repo, monkeypatch):
        repo, _ = pricing_repo
        history = GitHistory(repo, window="6m")
        calls = _count_git(monkeypatch)
        assert history.renames == [("pricing.py", "billing.py")]
        assert history.lifecycle_by_commit
        assert len(calls) == 1


class TestEmptyPatches:
    @pytest.mark.parametrize("text", ["", "\n", "\n\n"])
    def test_a_commit_with_no_diff_has_no_hunks(self, text):
        assert git_history_module._parse_patch(text) == []


class TestLineCommits:
    def test_names_the_commit_its_time_and_subject(self, git_repo):
        written = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        sha = _commit(git_repo, {"a.py": "x = 1\n"}, "add x", written)
        blamed = _lines(git_repo).line_commits("a.py", [1])
        assert blamed == {1: BlamedLine(sha, written, "add x")}

    def test_a_carriage_return_in_the_subject_is_kept(self, git_repo):
        written = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        sha = _commit(git_repo, {"a.py": "x = 1\n"}, "add\rx", written)
        blamed = _lines(git_repo).line_commits("a.py", [1])
        assert blamed == {1: BlamedLine(sha, written, "add\rx")}

    def test_line_ages_reads_the_same_blame(self, git_repo):
        _commit(git_repo, {"a.py": "x = 1\ny = 2\n"}, "add", datetime(2025, 3, 4, tzinfo=UTC))
        lines = _lines(git_repo)
        commits = lines.files_line_commits({"a.py": [1, 2]})["a.py"]
        assert lines.line_ages("a.py", [1, 2]) == {n: c.written for n, c in commits.items()}
