"""Function ratchets: fix-added-special-case and growing-dispatch-ladder, on real repositories."""

from __future__ import annotations

import ast
import os
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.checks.history_ratchets import (
    _branch_key,
    _guards,
    _hunk_changes,
    _ladders,
    check_fix_added_special_case,
    check_growing_dispatch_ladder,
)
from pysmelly.cli import main
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.git_history import Hunk
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, Severity


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


def _commit(
    repo: Path, files: dict[str, str], days_ago: int, message: str, allow_empty=False
) -> str:
    """Write ``files``, stage them by path, commit them dated ``days_ago``; return the sha."""
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "--", name)
    stamp = _when(days_ago).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    extra = ["--allow-empty"] if allow_empty else []
    _git(repo, "commit", "-q", *extra, "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").strip()


def _ctx(repo: Path) -> AnalysisContext:
    """The working tree's Python files, analyzed with the repository's history."""
    trees = {
        path.relative_to(repo): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(repo.rglob("*.py"))
        if ".git" not in path.parts
    }
    return AnalysisContext(trees, verbose=False, git=GitSettings(root=repo))


_KINDS = ["csv", "json", "xml", "yaml", "toml", "ini", "html", "md", "txt", "pdf", "rtf"]
_SPECIAL = ["N/A", "-", "unknown", "null"]


def _render(kinds: int, special: int = 0, name: str = "render", indent: str = "") -> str:
    """A function with an ``if``/``elif`` ladder on ``kind`` (8+ arms is a long-elif-chain).

    ``special`` early exits on ``value`` come first, each a special case.
    """
    lines = [f"def {name}(kind, value):"]
    for value in _SPECIAL[:special]:
        lines += [f"    if value == {value!r}:", "        return None"]
    for i, kind in enumerate(_KINDS[:kinds]):
        lines += [f"    {'if' if i == 0 else 'elif'} kind == {kind!r}:", f"        return {kind!r}"]
    lines.append("    raise ValueError(kind)")
    return textwrap.indent("\n".join(lines), indent) + "\n"


def _fixes(repo: Path, path: str, specials: range, start_days: int = 60, kinds: int = 8) -> list:
    """Create ``path``, then commit one more special case per fix commit."""
    _commit(repo, {path: _render(kinds)}, start_days + 10, "feat: render every format")
    return [
        _commit(
            repo,
            {path: _render(kinds, special)},
            start_days - special,
            f"fix: render crashed on {_SPECIAL[special - 1]!r}",
        )
        for special in specials
    ]


class TestFixAddedSpecialCase:
    def test_three_fixes_each_adding_an_early_exit_fire(self, git_repo):
        shas = _fixes(git_repo, "render.py", range(1, 4))
        (finding,) = check_fix_added_special_case(_ctx(git_repo))
        assert (finding.file, finding.line) == ("render.py", 1)
        assert finding.severity == Severity.MEDIUM
        assert "render(): 3 fix commits in the window each added an early exit" in (finding.message)
        for sha, value in zip(shas, _SPECIAL, strict=False):
            assert f"{sha[:8]} \"fix: render crashed on '{value}'\"" in finding.message
            assert f"`if value == '{value}'`" in finding.message
        assert finding.message.endswith(
            "— each fix handled the input that broke instead of the rule behind it"
        )

    def test_two_fixes_do_not_fire(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 3))
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_feature_commits_adding_early_exits_do_not_count(self, git_repo):
        _commit(git_repo, {"render.py": _render(8)}, 70, "feat: render every format")
        for special in range(1, 4):
            _commit(git_repo, {"render.py": _render(8, special)}, 60 - special, "feat: skip blanks")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_a_function_without_a_gate_finding_is_not_a_candidate(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 4), kinds=4)
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_a_literal_dispatch_param_finding_is_a_gate(self, git_repo):
        callers = "from render import render\n\nA = render('csv', 1)\nB = render('json', 2)\n"
        _commit(git_repo, {"use.py": callers}, 90, "feat: the callers of render")
        _fixes(git_repo, "render.py", range(1, 4), kinds=2)
        (finding,) = check_fix_added_special_case(_ctx(git_repo))
        assert "render(): 3 fix commits" in finding.message

    def test_the_same_history_without_the_callers_fires_nothing(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 4), kinds=2)
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_early_exits_the_function_was_created_with_do_not_count(self, git_repo):
        _commit(git_repo, {"render.py": _render(8, 4)}, 60, "fix: render rewritten whole")
        for step in range(3):
            _commit(git_repo, {"render.py": _render(8, 4) + f"\nX = {step}\n"}, 50 - step, "fix: x")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_an_early_exit_a_fix_moved_does_not_count(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 3))
        moved = (
            _render(8, 2)
            .replace("    if value == 'N/A':\n        return None\n", "")
            .replace(
                "    raise ValueError",
                "    if value == 'N/A':\n        return None\n    raise ValueError",
            )
        )
        _commit(git_repo, {"render.py": moved}, 20, "fix: check N/A last")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_an_early_exit_removed_since_does_not_count(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 4))
        without_first = _render(8, 3).replace("    if value == 'N/A':\n        return None\n", "")
        _commit(git_repo, {"render.py": without_first}, 10, "refactor: N/A is a kind now")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_a_non_literal_test_is_not_a_special_case(self, git_repo):
        _commit(git_repo, {"render.py": _render(8)}, 70, "feat: render every format")
        body = _render(8)
        for step, guard in enumerate(["value == other", "len(value) > 3", "callable(value)"]):
            body = body.replace(
                "(kind, value):\n", f"(kind, value):\n    if {guard}:\n        return None\n", 1
            )
            _commit(git_repo, {"render.py": body}, 60 - step, f"fix: guard {step}")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_fixes_before_a_review_do_not_count(self, git_repo):
        _fixes(git_repo, "render.py", range(1, 4), start_days=60)
        _commit(git_repo, {}, 20, "pysmelly: reviewed render.py", allow_empty=True)
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_history_follows_a_rename(self, git_repo):
        _fixes(git_repo, "old.py", range(1, 3))
        _git(git_repo, "mv", "old.py", "new.py")
        _commit(git_repo, {}, 30, "refactor: rename the module")
        _commit(git_repo, {"new.py": _render(8, 3)}, 20, "fix: render crashed on 'unknown'")
        (finding,) = check_fix_added_special_case(_ctx(git_repo))
        assert finding.file == "new.py"
        assert "3 fix commits" in finding.message

    def test_namesake_methods_are_told_apart(self, git_repo):
        stable = "class Plain:\n" + _render(8, 0, indent="    ")

        def growing(special: int) -> str:
            return "class Fancy:\n" + _render(8, special, indent="    ")

        _commit(git_repo, {"m.py": stable + "\n\n" + growing(0)}, 70, "feat: two renderers")
        for special in range(1, 4):
            _commit(
                git_repo,
                {"m.py": stable + "\n\n" + growing(special)},
                60 - special,
                "fix: fancy rendering crashed",
            )
        (finding,) = check_fix_added_special_case(_ctx(git_repo))
        assert "Fancy.render(): 3 fix commits" in finding.message

    def test_messages_that_do_not_classify_fire_nothing(self, git_repo):
        _commit(git_repo, {"render.py": _render(8)}, 70, "wip")
        for special in range(1, 4):
            _commit(git_repo, {"render.py": _render(8, special)}, 60 - special, "wip")
        assert check_fix_added_special_case(_ctx(git_repo)) == []

    def test_no_history_fires_nothing(self, tmp_path):
        (tmp_path / "render.py").write_text(_render(8, 3), encoding="utf-8")
        ctx = AnalysisContext({Path("render.py"): ast.parse(_render(8, 3))}, verbose=False)
        assert check_fix_added_special_case(ctx) == []


def _grow_ladder(repo: Path, path: str, kinds: list[int], message: str = "feat: render {}"):
    """Create the ladder with ``kinds[0]`` arms, then one commit per further count."""
    shas = []
    for step, count in enumerate(kinds):
        text = message.format(_KINDS[count - 1]) if step else "feat: the renderer"
        shas.append(_commit(repo, {path: _render(count)}, 60 - step, text))
    return shas


class TestGrowingDispatchLadder:
    def test_three_features_each_adding_a_branch_fire(self, git_repo):
        shas = _grow_ladder(git_repo, "render.py", [5, 6, 7, 8])
        (finding,) = check_growing_dispatch_ladder(_ctx(git_repo))
        assert (finding.file, finding.line) == ("render.py", 1)
        assert finding.severity == Severity.MEDIUM
        assert "render(): the 8-branch dispatch on 'kind' grew a branch in 3 feature" in (
            finding.message
        )
        for sha, kind in zip(shas[1:], ["ini", "html", "md"], strict=True):
            assert f"{sha[:8]} \"feat: render {kind}\" added `if kind == '{kind}'`" in (
                finding.message
            )
        assert finding.message.endswith("— every new case edits this ladder")

    def test_two_features_do_not_fire(self, git_repo):
        _grow_ladder(git_repo, "render.py", [6, 7, 8])
        assert check_growing_dispatch_ladder(_ctx(git_repo)) == []

    def test_fix_commits_adding_branches_do_not_count(self, git_repo):
        _grow_ladder(git_repo, "render.py", [5, 6, 7, 8], message="fix: {} was rejected")
        assert check_growing_dispatch_ladder(_ctx(git_repo)) == []

    def test_branches_the_ladder_was_created_with_do_not_count(self, git_repo):
        _commit(git_repo, {"render.py": _render(11)}, 60, "feat: the renderer")
        for step in range(3):
            _commit(git_repo, {"render.py": _render(11) + f"\nX = {step}\n"}, 50 - step, "feat: x")
        assert check_growing_dispatch_ladder(_ctx(git_repo)) == []

    def test_a_ladder_without_a_gate_finding_is_not_a_candidate(self, git_repo):
        _grow_ladder(git_repo, "render.py", [4, 5, 6, 7])  # 7 arms: no long-elif-chain
        assert check_growing_dispatch_ladder(_ctx(git_repo)) == []

    def test_an_isinstance_ladder_of_exiting_ifs_fires(self, git_repo):
        types = ["int", "float", "str", "bytes", "list", "dict", "set"]

        def describe(count: int) -> str:
            arms = "".join(
                f"    if isinstance(value, {t}):\n        return {t!r}\n" for t in types[:count]
            )
            return f"def describe(value):\n{arms}    return 'other'\n"

        _commit(git_repo, {"d.py": describe(4)}, 60, "feat: describe values")
        shas = [
            _commit(git_repo, {"d.py": describe(count)}, 60 - count, f"feat: describe {count}")
            for count in (5, 6, 7)
        ]
        (finding,) = check_growing_dispatch_ladder(_ctx(git_repo))
        assert "describe(): the 7-branch dispatch on 'value'" in finding.message
        assert all(sha[:8] in finding.message for sha in shas)

    def test_branches_added_before_the_window_do_not_count(self, git_repo):
        for step, count in enumerate([5, 6, 7, 8]):
            _commit(git_repo, {"render.py": _render(count)}, 400 - step, f"feat: render {count}")
        _commit(git_repo, {"render.py": _render(8) + "\nX = 1\n"}, 10, "feat: x")
        assert check_growing_dispatch_ladder(_ctx(git_repo)) == []

    def test_a_review_note_marks_growth_after_it(self, git_repo):
        _commit(git_repo, {"render.py": _render(5)}, 90, "feat: the renderer")
        _commit(git_repo, {}, 80, "pysmelly: reviewed render.py", allow_empty=True)
        for step, count in enumerate([6, 7, 8]):
            _commit(git_repo, {"render.py": _render(count)}, 70 - step, f"feat: render {count}")
        (finding,) = check_growing_dispatch_ladder(_ctx(git_repo))
        assert "pattern persists" in finding.message


class TestReadingFunctions:
    @staticmethod
    def _function(source: str) -> ast.FunctionDef:
        node = ast.parse(textwrap.dedent(source)).body[0]
        assert isinstance(node, ast.FunctionDef)
        return node

    def test_guards_are_literal_early_exits_without_else(self):
        node = self._function(
            """
            def f(x, y):
                if x == "a":
                    return 1
                if x is None or x == -1:
                    raise ValueError
                if x in ("b", "c"):
                    return 2
                if x == STATUS_DONE:
                    return 3
                if x == y:
                    return 4
                if x == "d":
                    log(x)
                if x == "e":
                    return 5
                else:
                    return 6
                if x == "f":
                    return 7
                elif x == "g":
                    return 8
                def inner(z):
                    if z == 1:
                        return 9
            """
        )
        assert _guards(node) == {
            ("if", "x == 'a'"),
            ("if", "x is None or x == -1"),
            ("if", "x in ('b', 'c')"),
            ("if", "x == STATUS_DONE"),
        }

    def test_a_match_with_four_cases_is_a_ladder(self):
        node = self._function(
            """
            def f(cmd):
                match cmd:
                    case "a":
                        return 1
                    case "b" | "c":
                        return 2
                    case "d":
                        return 3
                    case Point(x=0):
                        return 4
                    case _:
                        return 5
            """
        )
        (ladder,) = _ladders(node)
        assert ladder.subject == "cmd"
        assert ladder.branches == {
            ("case", "'a'"),
            ("case", "'b' | 'c'"),
            ("case", "'d'"),
            ("case", "Point(x=0)"),
        }

    def test_a_ladder_counts_branches_on_one_subject(self):
        node = self._function(
            """
            def f(x, y):
                if x == 1:
                    pass
                elif y == 2:
                    pass
                elif x == 3:
                    pass
                elif not isinstance(x, str):
                    pass
                elif x.kind == 4:
                    pass
                else:
                    pass
            """
        )
        assert _ladders(node) == []

    def test_sibling_ifs_join_only_after_an_exit(self):
        node = self._function(
            """
            def f(x):
                if x == 1:
                    y = 1
                if x == 2:
                    return 2
                if x == 3:
                    return 3
                if x == 4:
                    return 4
            """
        )
        assert _ladders(node) == []

    @pytest.mark.parametrize(
        ("line", "key"),
        [
            ("if x == 'a':", ("if", "x == 'a'")),
            ('elif x == "a":  # note', ("if", "x == 'a'")),
            ("if x == 1: return None", ("if", "x == 1")),
            ("case 'a' | 'b':", ("case", "'a' | 'b'")),
            ("if (x == 1 or", None),
            ("iffy = 3", None),
            ("case = 3", None),
            ("return x", None),
        ],
    )
    def test_branch_key(self, line, key):
        assert _branch_key(line) == key

    def test_hunk_lines_belong_to_the_innermost_def(self):
        body = (
            " def outer(x):",
            "+    if x == 1:",
            "+        return 1",
            "     def inner(y):",
            "+        if y == 2:",
            "+            return 2",
            "-    if x == 3:",
            "+    if x == 3:",
            "     return 0",
        )
        hunk = Hunk("a.py", "a.py", 1, 5, 1, 9, body)
        changes = _hunk_changes(hunk)
        assert changes.added == {
            ("outer", ("if", "x == 1")),
            ("inner", ("if", "y == 2")),
            ("outer", ("if", "x == 3")),
        }
        assert changes.removed == {("outer", ("if", "x == 3"))}
        assert changes.existed == {"outer", "inner"}


class TestRegistration:
    @pytest.mark.parametrize("name", ["fix-added-special-case", "growing-dispatch-ladder"])
    def test_registered_as_medium_git_history(self, name):
        assert CHECK_CATEGORIES[name] == "git-history"
        assert CHECK_SEVERITY[name] == Severity.MEDIUM

    def test_the_cli_runs_both(self, git_repo, monkeypatch, capsys):
        _fixes(git_repo, "render.py", range(1, 4))
        monkeypatch.chdir(git_repo)
        argv = ["git-history", "--no-context", "--check", "fix-added-special-case"]
        with pytest.raises(SystemExit):
            main([*argv, "--check", "growing-dispatch-ladder", "."])
        assert "render(): 3 fix commits" in capsys.readouterr().out

    def test_the_ast_run_does_not_include_them(self, git_repo, monkeypatch, capsys):
        _fixes(git_repo, "render.py", range(1, 4))
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["--no-context", "--more-please", "."])
        assert "fix-added-special-case" not in capsys.readouterr().out
