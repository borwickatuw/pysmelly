"""Commit-shape checks: growing-signatures and same-change-multiple-files, on real repositories."""

from __future__ import annotations

import ast
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.checks.history_shape import (
    _signatures_in,
    _significant_lines,
    check_growing_signatures,
    check_same_change_multiple_files,
)
from pysmelly.cli import main
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, Severity


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


def _commit(
    repo: Path, files: dict[str, str], days_ago: int, message: str = "work", allow_empty=False
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


def _signature(params: list[str], name: str = "build") -> str:
    return f"def {name}({', '.join(params)}):\n    return None\n"


def _grow(
    repo: Path, path: str, counts: list[int], start_days: int = 60, name="build"
) -> list[str]:
    """Commit ``path`` once per count, its function taking that many parameters."""
    shas = []
    for step, count in enumerate(counts):
        params = [f"p{i}" for i in range(count)]
        shas.append(
            _commit(
                repo,
                {path: _signature(params, name)},
                start_days - step,
                f"step {step}: {count} params",
            )
        )
    return shas


class TestGrowingSignatures:
    def test_three_rises_to_four_params_fire(self, git_repo):
        shas = _grow(git_repo, "api.py", [1, 2, 3, 4])
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert (finding.file, finding.line) == ("api.py", 1)
        assert finding.severity == Severity.MEDIUM
        assert "build() takes 4 parameters and 3 commits in the window each lengthened it" in (
            finding.message
        )
        for before, after, sha in [(1, 2, shas[1]), (2, 3, shas[2]), (3, 4, shas[3])]:
            assert f'{before}→{after} {sha[:8]} "step' in finding.message

    def test_two_rises_do_not_fire(self, git_repo):
        _grow(git_repo, "api.py", [2, 3, 4])
        assert check_growing_signatures(_ctx(git_repo)) == []

    def test_three_params_at_head_are_not_candidates(self, git_repo):
        _grow(git_repo, "api.py", [0, 1, 2, 3])
        assert check_growing_signatures(_ctx(git_repo)) == []

    def test_rises_before_the_window_do_not_count(self, git_repo):
        _grow(git_repo, "api.py", [1, 2, 3], start_days=400)
        _commit(git_repo, {"api.py": _signature(["a", "b", "c", "d"])}, 10)
        _commit(git_repo, {"api.py": _signature(["a", "b", "c", "d"]) + "\nX = 1\n"}, 9)
        _commit(git_repo, {"api.py": _signature(["a", "b", "c", "d"]) + "\nX = 2\n"}, 8)
        assert check_growing_signatures(_ctx(git_repo)) == []

    def test_self_is_not_a_parameter(self, git_repo):
        for step, count in enumerate([1, 2, 3, 4]):
            params = ", ".join(["self", *(f"p{i}" for i in range(count))])
            source = f"class Client:\n    def send({params}):\n        return None\n"
            _commit(git_repo, {"client.py": source}, 60 - step)
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert "Client.send() takes 4 parameters" in finding.message
        assert "1→2" in finding.message

    def test_multi_line_signature_growth_fires(self, git_repo):
        for step, count in enumerate([3, 4, 5, 6]):
            params = "".join(f"    p{i}: int,\n" for i in range(count))
            _commit(
                git_repo,
                {"api.py": f"def build(\n{params}) -> None:\n    return None\n"},
                50 - step,
            )
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert "3→4" in finding.message and "5→6" in finding.message

    def test_namesakes_in_one_file_are_told_apart(self, git_repo):
        stable = (
            "class Store:\n    def __init__(self, url, user, password, timeout):\n        pass\n"
        )
        for step, count in enumerate([1, 2, 3, 4]):
            params = ", ".join(["self", *(f"opt{i}" for i in range(count))])
            growing = f"class Job:\n    def __init__({params}):\n        pass\n"
            _commit(git_repo, {"models.py": stable + "\n\n" + growing}, 40 - step)
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert "Job.__init__() takes 4 parameters" in finding.message

    def test_history_follows_a_rename(self, git_repo):
        _grow(git_repo, "old.py", [1, 2, 3])
        _git(git_repo, "mv", "old.py", "new.py")
        _commit(git_repo, {}, 30, "rename")
        _commit(git_repo, {"new.py": _signature(["a", "b", "c", "d"])}, 20, "fourth")
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert finding.file == "new.py"
        assert "1→2" in finding.message and "3→4" in finding.message

    def test_an_unparsable_revision_is_skipped_not_fatal(self, git_repo):
        _commit(git_repo, {"api.py": _signature(["a"])}, 50, "one")
        _commit(git_repo, {"api.py": _signature(["a", "b"])}, 49, "two")
        _commit(git_repo, {"api.py": "def build(a, b c):\n    return None\n"}, 48, "broken")
        _commit(git_repo, {"api.py": _signature(["a", "b", "c"])}, 47, "three")
        _commit(git_repo, {"api.py": _signature(["a", "b", "c", "d"])}, 46, "four")
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert "2→3" in finding.message and '"three"' in finding.message
        assert '"broken"' not in finding.message

    def test_rises_before_a_review_do_not_count(self, git_repo):
        _grow(git_repo, "api.py", [1, 2, 3, 4], start_days=60)
        _commit(git_repo, {}, 20, "pysmelly: reviewed api.py", allow_empty=True)
        assert check_growing_signatures(_ctx(git_repo)) == []

    def test_a_review_note_marks_growth_after_it(self, git_repo):
        _commit(git_repo, {"api.py": _signature(["a"])}, 90, "one")
        _commit(git_repo, {}, 80, "pysmelly: reviewed api.py", allow_empty=True)
        _grow(git_repo, "api.py", [2, 3, 4], start_days=70)
        _commit(git_repo, {"api.py": _signature(["a", "b", "c", "d", "e"])}, 50, "five")
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert "pattern persists" in finding.message

    def test_a_working_tree_edit_does_not_shift_the_range(self, git_repo):
        _grow(git_repo, "api.py", [1, 2, 3, 4])
        source = (git_repo / "api.py").read_text(encoding="utf-8")
        (git_repo / "api.py").write_text("import os\n\n\n" + source, encoding="utf-8")
        (finding,) = check_growing_signatures(_ctx(git_repo))
        assert finding.line == 1

    def test_tests_are_not_candidates(self, git_repo):
        _grow(git_repo, "test_api.py", [1, 2, 3, 4])
        assert check_growing_signatures(_ctx(git_repo)) == []

    def test_no_git_history_no_findings(self, tmp_path):
        (tmp_path / "a.py").write_text(_signature(["a", "b", "c", "d"]), encoding="utf-8")
        trees = {Path("a.py"): ast.parse((tmp_path / "a.py").read_text(encoding="utf-8"))}
        assert check_growing_signatures(AnalysisContext(trees, verbose=False)) == []


class TestSignaturesIn:
    def test_reads_one_line_multi_line_and_one_liner_signatures(self):
        lines = [
            "    def a(self, x, *args, y, **kw):",
            "        pass",
            "async def b(",
            "    p,  # first",
            "    q=1,",
            "):",
            "def c(z): return z",
        ]
        assert [(s.name, s.params) for s in _signatures_in(lines)] == [
            ("a", ("x", "args", "y", "kw")),
            ("b", ("p", "q")),
            ("c", ("z",)),
        ]

    def test_a_signature_that_never_closes_is_left_out(self):
        assert _signatures_in(["def a(x,", "    y"]) == []


_HANDLER = """\
def {name}(request):
    payload = request.json()
    if payload.get("{key}") is None:
        raise ValueError("missing {key}")
    return payload
"""

_BASE = "def existing():\n    return 1\n"


def _three_files(names: list[str]) -> dict[str, str]:
    return {f"{name}.py": _BASE for name in names}


class TestSameChangeMultipleFiles:
    def test_one_block_pasted_into_three_files_fires(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        pasted = {
            f"{name}.py": _BASE + "\n\n" + _HANDLER.format(name=f"handle_{name}", key=f"k_{name}")
            for name in ["a", "b", "c"]
        }
        sha = _commit(git_repo, pasted, 50, "add handlers")
        (finding,) = check_same_change_multiple_files(_ctx(git_repo))
        assert (finding.file, finding.line) == ("a.py", 5)
        assert finding.severity == Severity.MEDIUM
        assert f'commit {sha[:8]} "add handlers"' in finding.message
        assert "same 5-line change in 3 files" in finding.message
        assert "a.py:5, b.py:5, c.py:5" in finding.message
        assert finding.message.endswith('it starts "def handle_a(request):"')

    def test_an_ignore_on_one_copy_removes_only_that_copy(self, git_repo, monkeypatch, capsys):
        """The finding names every copy; an ignore speaks for its own copy alone."""
        _commit(git_repo, _three_files(["a", "b", "c", "d"]), 60)
        pasted = {
            f"{name}.py": _BASE + "\n\n" + _HANDLER.format(name=f"handle_{name}", key=f"k_{name}")
            for name in ["a", "b", "c", "d"]
        }
        _commit(git_repo, pasted, 50, "add handlers")
        ignored = pasted["a.py"].replace(
            "def handle_a", "# pysmelly: ignore[same-change-multiple-files]\ndef handle_a"
        )
        _commit(git_repo, {"a.py": ignored}, 40, "a's copy stays on purpose")
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--check", "same-change-multiple-files", "."])
        (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "same 5-line" in ln]
        assert line.startswith("  b.py:5: ")
        assert "change in 3 files, still there at HEAD: b.py:5, c.py:5, d.py:5 —" in line
        assert "(1 other site ignored). Fix: " in line

    def test_two_files_do_not_fire(self, git_repo):
        _commit(git_repo, _three_files(["a", "b"]), 60)
        pasted = {
            f"{name}.py": _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
            for name in ["a", "b"]
        }
        _commit(git_repo, pasted, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_different_logic_does_not_fire(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        bodies = {
            "a.py": "def f(x):\n    y = x.strip()\n    return y.lower()\n",
            "b.py": "def f(x):\n    for item in x:\n        print(item)\n",
            "c.py": "def f(x):\n    with open(x) as fh:\n        return fh.read()\n",
        }
        _commit(git_repo, {k: _BASE + "\n\n" + v for k, v in bodies.items()}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_two_line_edits_are_below_the_bar(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        added = "def g(request):\n    return request.json()\n"
        _commit(git_repo, {f"{n}.py": _BASE + "\n\n" + added for n in "abc"}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_imports_docstrings_and_version_bumps_are_trivial(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        added = (
            "import os\nimport sys\nfrom pathlib import Path\n"
            '__version__ = "1.2.3"\n'
            '"""Module notes.\n\nif this is prose, return it for now.\n"""\n'
            "# a comment line\n# another\n"
        )
        _commit(git_repo, {f"{n}.py": added + _BASE for n in "abc"}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_a_reformat_across_many_files_does_not_fire(self, git_repo):
        names = [f"m{i:02}" for i in range(25)]
        cramped = "def f(a,b,c):\n    return g(a,b,c)\n\n\ndef h(x):\n    return k(x,1,2)\n"
        _commit(git_repo, {f"{n}.py": cramped for n in names}, 60)
        spread = (
            "def f(\n    a,\n    b,\n    c,\n):\n    return g(\n        a,\n        b,\n        c,\n    )\n"
            "\n\ndef h(x):\n    return k(\n        x,\n        1,\n        2,\n    )\n"
        )
        _commit(git_repo, {f"{n}.py": spread for n in names}, 50, "style: black")
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_a_reformat_of_a_few_files_does_not_fire(self, git_repo):
        cramped = "def f(a,b,c):\n    return g(a,b,c)\n"
        _commit(git_repo, {f"{n}.py": cramped for n in "abcd"}, 60)
        spread = (
            "def f(\n    a, b, c\n):\n    return g(\n        a,\n        b,\n        c,\n    )\n"
        )
        _commit(git_repo, {f"{n}.py": spread for n in "abcd"}, 50, "style: black")
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_a_sweep_of_twenty_files_is_skipped(self, git_repo):
        names = [f"m{i:02}" for i in range(20)]
        _commit(git_repo, {f"{n}.py": _BASE for n in names}, 60)
        pasted = _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
        _commit(git_repo, {f"{n}.py": pasted for n in names}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_non_python_files_do_not_make_a_commit_a_sweep(self, git_repo):
        """The 20-file bar counts Python files: 3 .py copies beside 20 docs still fire."""
        docs = {f"docs/page{i:02}.md": "old\n" for i in range(20)}
        _commit(git_repo, {**_three_files(["a", "b", "c"]), **docs}, 60)
        pasted = {
            f"{name}.py": _BASE + "\n\n" + _HANDLER.format(name=f"handle_{name}", key=f"k_{name}")
            for name in ["a", "b", "c"]
        }
        _commit(git_repo, {**pasted, **{p: "new\n" for p in docs}}, 50, "handlers and docs")
        (finding,) = check_same_change_multiple_files(_ctx(git_repo))
        assert "a.py:5, b.py:5, c.py:5" in finding.message

    def test_copies_gone_from_head_do_not_fire(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        pasted = _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
        _commit(git_repo, {f"{n}.py": pasted for n in "abc"}, 50)
        _commit(git_repo, {"b.py": _BASE, "c.py": _BASE}, 40, "extract handler")
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_new_files_are_not_compared(self, git_repo):
        _commit(git_repo, {"seed.py": _BASE}, 60)
        pasted = _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
        _commit(git_repo, {f"{n}.py": pasted for n in "abc"}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_test_files_are_not_compared(self, git_repo):
        names = ["test_a", "test_b", "test_c"]
        _commit(git_repo, _three_files(names), 60)
        pasted = _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
        _commit(git_repo, {f"{n}.py": pasted for n in names}, 50)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []

    def test_a_file_reviewed_after_the_commit_drops_out(self, git_repo):
        _commit(git_repo, _three_files(["a", "b", "c"]), 60)
        pasted = _BASE + "\n\n" + _HANDLER.format(name="handle", key="k")
        _commit(git_repo, {f"{n}.py": pasted for n in "abc"}, 50)
        _commit(git_repo, {}, 40, "pysmelly: reviewed c.py", allow_empty=True)
        assert check_same_change_multiple_files(_ctx(git_repo)) == []


def _shape(lines: list[str]) -> tuple[str, ...]:
    return tuple(line.shape for line in _significant_lines(lines))


class TestHunkShape:
    def test_names_and_literals_are_masked_calls_and_attributes_kept(self):
        one = _shape(['    total = compute(order.items, 3, "x")'])
        other = _shape(["sum_ = compute(cart.items, 7, 'y')  # note"])
        assert one == other == ("N = compute ( N . items , 0 , S )",)

    def test_a_different_call_is_a_different_shape(self):
        assert _shape(["x = compute(a)"]) != _shape(["x = measure(a)"])

    def test_docstring_text_comments_and_layout_lines_are_dropped(self):
        lines = [
            '"""Start.',
            "if this were code, return it",
            '"""',
            "# note",
            ")",
            "name,",
            "x = 1",
        ]
        assert _shape(lines) == ("N = 0",)


class TestRegistration:
    @pytest.mark.parametrize("name", ["growing-signatures", "same-change-multiple-files"])
    def test_registered_as_medium_git_history(self, name):
        assert CHECK_CATEGORIES[name] == "git-history"
        assert CHECK_SEVERITY[name] == Severity.MEDIUM

    def test_the_cli_runs_both(self, git_repo, monkeypatch, capsys):
        _grow(git_repo, "api.py", [1, 2, 3, 4])
        monkeypatch.chdir(git_repo)
        argv = ["git-history", "--no-context", "--check", "growing-signatures"]
        with pytest.raises(SystemExit):
            main([*argv, "--check", "same-change-multiple-files", "."])
        assert "build() takes 4 parameters" in capsys.readouterr().out

    def test_the_ast_run_does_not_include_them(self, git_repo, monkeypatch, capsys):
        _grow(git_repo, "api.py", [1, 2, 3, 4])
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["--no-context", "--more-please", "."])
        assert "growing-signatures" not in capsys.readouterr().out


class TestConsolidation:
    def test_call_sites_of_a_freshly_extracted_helper_do_not_fire(self, git_repo):
        inline = (
            "def handle(request):\n"
            "    raw = request.body\n"
            "    if not raw:\n"
            '        raise ValueError("empty")\n'
            "    data = json.loads(raw)\n"
            "    if not isinstance(data, dict):\n"
            '        raise ValueError("not an object")\n'
            "    return data\n"
        )
        _commit(git_repo, {f"{n}.py": inline for n in "abc"}, 60)
        extracted = (
            "def handle(request):\n"
            "    data = parse_json_object(request)\n"
            "    log.debug(data.keys())\n"
            "    return data\n"
        )
        _commit(git_repo, {f"{n}.py": extracted for n in "abc"}, 50, "one parser")
        assert check_same_change_multiple_files(_ctx(git_repo)) == []
