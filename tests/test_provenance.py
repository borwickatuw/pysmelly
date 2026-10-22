"""Provenance lines (Phase 29-2): `added in <sha> (<subject>), last production reader removed in ...`.

Each end-to-end test builds a real repository and runs `pysmelly.cli.main`,
so a line's absence is what fails where provenance does not exist. The
laziness tests count `git log -S` runs through the one place that issues
them, `GitQueries.token_commits`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pysmelly.cli import main
from pysmelly.git_history import (
    MAX_SUBJECT_WIDTH,
    CommitRef,
    GitQueries,
    Hunk,
    TokenCommit,
    display_text,
    trace_addition,
)

_HELPER = "def helper():\n    return 1\n"

_APP_CALLING = """\
from mod import helper


def main():
    return helper()


if __name__ == "__main__":
    main()
"""

_APP_NOT_CALLING = """\
def main():
    return 2


if __name__ == "__main__":
    main()
"""

_JOB_READING = """\
from app import main


def run(flag):
    if flag:
        print("verbose")
    return main()


if __name__ == "__main__":
    run(True)
"""

_JOB_NOT_READING = _JOB_READING.replace('    if flag:\n        print("verbose")\n', "")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    """Write ``files``, stage them by path, commit; return the abbreviated sha git prints."""
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "log", "-1", "--format=%h")


def _run(capsys, *args: str) -> str:
    """pysmelly's stdout without the closing tagline, which is random."""
    with pytest.raises(SystemExit):
        main(["--no-context", *args])
    out = capsys.readouterr().out
    return out.split("\n  -- ")[0]


def _provenance(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.startswith("    provenance of ")]


@pytest.fixture
def story_repo(git_repo: Path) -> dict[str, str]:
    """helper() added and called, then its one caller dropped; run(flag) loses its read of flag."""
    shas = {
        "add": _commit(
            git_repo,
            {"mod.py": _HELPER, "app.py": _APP_CALLING, "job.py": _JOB_READING},
            "feat: add helper and the flag",
        ),
        "drop": _commit(
            git_repo,
            {"app.py": _APP_NOT_CALLING, "job.py": _JOB_NOT_READING},
            "refactor: stop calling helper and reading the flag",
        ),
    }
    return shas


class TestPresence:
    def test_dead_code_tells_the_whole_story(self, git_repo, story_repo, capsys):
        out = _run(capsys, "--check", "dead-code", str(git_repo))
        assert _provenance(out) == [
            f"provenance of helper: added in {story_repo['add']} (feat: add helper and the "
            f"flag), last production reader removed in {story_repo['drop']} (refactor: stop "
            "calling helper and reading the flag)"
        ]
        lines = out.splitlines()
        anchor = next(i for i, line in enumerate(lines) if line.startswith("  mod.py:1: helper()"))
        assert lines[anchor + 1].startswith("    provenance of helper: ")

    def test_vestigial_param_traced_in_its_own_file(self, git_repo, story_repo, capsys):
        out = _run(capsys, "--check", "vestigial-params", str(git_repo))
        assert _provenance(out) == [
            f"provenance of flag: added in {story_repo['add']} (feat: add helper and the "
            f"flag), last production reader removed in {story_repo['drop']} (refactor: stop "
            "calling helper and reading the flag)"
        ]

    def test_dead_constant_with_no_reader_removed_says_only_where_it_was_added(
        self, git_repo, capsys
    ):
        sha = _commit(git_repo, {"limits.py": "RETRIES = 3\n"}, "feat: retries")
        out = _run(capsys, "--check", "dead-constants", str(git_repo))
        assert _provenance(out) == [f"provenance of RETRIES: added in {sha} (feat: retries)"]

    def test_readded_name_dates_from_its_latest_addition(self, git_repo, capsys):
        _commit(git_repo, {"limits.py": "LIMIT = 1\n"}, "feat: first limit")
        _commit(git_repo, {"limits.py": "OTHER = 1\n"}, "refactor: drop the limit")
        again = _commit(git_repo, {"limits.py": "OTHER = 1\nLIMIT = 2\n"}, "feat: limit again")
        out = _run(capsys, "--check", "dead-constants", str(git_repo))
        assert f"provenance of LIMIT: added in {again} (feat: limit again)" in _provenance(out)

    def test_a_subdirectory_target_reads_paths_under_it(self, git_repo, capsys):
        add = _commit(
            git_repo,
            {"pkg/mod.py": _HELPER, "pkg/app.py": _APP_CALLING},
            "feat: helper in a package",
        )
        drop = _commit(git_repo, {"pkg/app.py": _APP_NOT_CALLING}, "refactor: drop the caller")
        out = _run(capsys, "--check", "dead-code", str(git_repo / "pkg"))
        assert _provenance(out) == [
            f"provenance of helper: added in {add} (feat: helper in a package), "
            f"last production reader removed in {drop} (refactor: drop the caller)"
        ]

    def test_a_param_followed_through_a_rename(self, git_repo, capsys):
        add = _commit(git_repo, {"old.py": _JOB_READING, "app.py": _APP_NOT_CALLING}, "feat: job")
        _git(git_repo, "mv", "old.py", "job.py")
        _git(git_repo, "commit", "-q", "-m", "refactor: rename the job module")
        drop = _commit(git_repo, {"job.py": _JOB_NOT_READING}, "refactor: ignore the flag")
        out = _run(capsys, "--check", "vestigial-params", str(git_repo))
        assert _provenance(out) == [
            f"provenance of flag: added in {add} (feat: job), "
            f"last production reader removed in {drop} (refactor: ignore the flag)"
        ]


class TestAbsence:
    def test_no_line_for_an_uncommitted_definition(self, git_repo, story_repo, capsys):
        (git_repo / "fresh.py").write_text("def fresh():\n    return 0\n", encoding="utf-8")
        out = _run(capsys, "--check", "dead-code", str(git_repo))
        assert "  fresh.py:1: fresh()" in out
        assert not any(line.startswith("provenance of fresh") for line in _provenance(out))

    def test_no_line_before_the_first_commit(self, git_repo, capsys):
        (git_repo / "mod.py").write_text(_HELPER, encoding="utf-8")
        out = _run(capsys, "--check", "dead-code", str(git_repo))
        assert "  mod.py:1: helper()" in out
        assert _provenance(out) == []

    def test_no_line_when_the_param_name_is_used_outside_its_function(self, git_repo, capsys):
        job = _JOB_NOT_READING + "\n\ndef other():\n    flag = 1\n    return flag\n"
        _commit(git_repo, {"job.py": job, "app.py": _APP_NOT_CALLING}, "feat: job")
        out = _run(capsys, "--check", "vestigial-params", str(git_repo))
        assert "flag is declared but never used in run()" in out
        assert _provenance(out) == []

    def test_a_test_file_losing_its_call_is_not_a_production_reader(self, git_repo, capsys):
        add = _commit(
            git_repo,
            {"mod.py": _HELPER, "test_mod.py": "from mod import helper\n\nhelper()\n"},
            "feat: helper and its test",
        )
        _commit(git_repo, {"test_mod.py": "x = 1\n"}, "test: stop calling helper")
        out = _run(capsys, "--check", "dead-code", str(git_repo))
        assert _provenance(out) == [
            f"provenance of helper: added in {add} (feat: helper and its test)"
        ]

    def test_non_git_output_is_unchanged_and_runs_no_query(self, tmp_path, capsys, monkeypatch):
        calls = _count_queries(monkeypatch)
        (tmp_path / "mod.py").write_text(_HELPER, encoding="utf-8")
        out = _run(capsys, "--check", "dead-code", str(tmp_path))
        assert "  mod.py:1: helper()" in out
        assert _provenance(out) == []
        assert calls == []

    def test_a_check_without_subjects_runs_no_query(
        self, git_repo, story_repo, capsys, monkeypatch
    ):
        calls = _count_queries(monkeypatch)
        _run(capsys, "--check", "single-call-site", "--check", "env-fallbacks", str(git_repo))
        assert calls == []


def _count_queries(monkeypatch) -> list[str]:
    """Record the token of every ``git log -S`` the run issues."""
    calls: list[str] = []
    real = GitQueries.token_commits

    def counting(self, token, pathspecs, *, follow):
        calls.append(token)
        return real(self, token, pathspecs, follow=follow)

    monkeypatch.setattr(GitQueries, "token_commits", counting)
    return calls


class TestLaziness:
    @pytest.fixture
    def many_dead(self, git_repo: Path) -> Path:
        """Twelve dead functions, two more than the default display cap of ten."""
        source = "".join(
            f"def dead_{index:02d}():\n    return {index}\n\n\n" for index in range(12)
        )
        _commit(git_repo, {"mod.py": source}, "feat: twelve functions")
        return git_repo

    def test_summary_runs_no_query(self, many_dead, capsys, monkeypatch):
        calls = _count_queries(monkeypatch)
        out = _run(capsys, "--summary", "--check", "dead-code", str(many_dead))
        assert "dead-code" in out
        assert calls == []

    def test_only_displayed_findings_are_queried(self, many_dead, capsys, monkeypatch):
        calls = _count_queries(monkeypatch)
        out = _run(capsys, "--check", "dead-code", str(many_dead))
        assert "Showing top 10" in out
        shown = sorted(
            line.split(": ")[0].removeprefix("provenance of ") for line in _provenance(out)
        )
        assert sorted(calls) == shown
        assert len(calls) == 10

    def test_more_please_queries_every_finding(self, many_dead, capsys, monkeypatch):
        calls = _count_queries(monkeypatch)
        out = _run(capsys, "--more-please", "--check", "dead-code", str(many_dead))
        assert len(_provenance(out)) == 12
        assert len(calls) == 12


# A right-to-left override and the pop that ends it, built so no source line holds one.
_RLO = chr(0x202E)
_PDF = chr(0x202C)


class TestUntrustedSubjects:
    def test_control_characters_are_dropped_and_long_subjects_cut(self, git_repo, capsys):
        message = f"feat: \x1b[31mred\x1b[0m {_RLO}evil{_PDF} " + "x" * 100
        sha = _commit(git_repo, {"limits.py": "RETRIES = 3\n"}, message)
        out = _run(capsys, "--check", "dead-constants", str(git_repo))
        (line,) = _provenance(out)
        subject = line.removeprefix(f"provenance of RETRIES: added in {sha} (").removesuffix(")")
        assert "\x1b" not in line and _RLO not in line and _PDF not in line
        assert subject.startswith("feat: [31mred[0m evil xxx")
        assert len(subject) == MAX_SUBJECT_WIDTH
        assert subject.endswith("...")

    def test_display_text_keeps_ordinary_text(self):
        assert display_text("fix(api): handle é and 日本") == "fix(api): handle é and 日本"
        assert display_text("a b\rc\td") == "abcd"


def _hunk(added: list[str], removed: list[str], file: str = "a.py") -> Hunk:
    body = tuple([*(f"-{line}" for line in removed), *(f"+{line}" for line in added)])
    return Hunk(file, file, 1, len(removed), 1, len(added), body)


class TestTraceAddition:
    def test_a_substring_match_does_not_count(self):
        rename = TokenCommit(CommitRef("b", "rename"), (_hunk(["subtotal = 1"], []),))
        commits = [
            rename,
            TokenCommit(CommitRef("a", "add"), (_hunk(["total = 1", "x = total"], []),)),
        ]
        found = trace_addition("total", commits, lambda _path: True)
        assert found is not None
        assert found.added == CommitRef("a", "add")
        assert found.later == (rename,)

    def test_no_addition_is_no_answer(self):
        commits = [TokenCommit(CommitRef("a", "edit"), (_hunk(["subtotal"], []),))]
        assert trace_addition("total", commits, lambda _path: True) is None

    def test_an_addition_in_an_uncounted_file_is_no_answer(self):
        commits = [TokenCommit(CommitRef("a", "test"), (_hunk(["total()"], [], "test_a.py"),))]
        assert trace_addition("total", commits, lambda path: not path.startswith("test_")) is None

    def test_later_commits_come_newest_first(self):
        newest = TokenCommit(CommitRef("c", "drop"), (_hunk([], ["total()"]),))
        middle = TokenCommit(CommitRef("b", "call"), (_hunk(["total()"], []),))
        commits = [newest, middle, TokenCommit(CommitRef("a", "add"), (_hunk(["total"], []),))]
        found = trace_addition("total", commits, lambda _path: True)
        assert found is not None
        assert found.later == (newest, middle)


# A function every caller leaves at its default, so always-default-params
# reports ``timeout``, which the function still reads.
_CLEANUP_CALLER = "from sweep import cleanup\n\n\ndef main():\n    return cleanup()\n"

_CLEANUP_READING_THRICE = """\
def cleanup(timeout=None):
    if timeout is None:
        timeout = 30
    threshold = 100 - timeout
    print(f"older than {timeout}")
    return threshold
"""

_CLEANUP_READING_ONCE = """\
def cleanup(timeout=None):
    return 100 - (30 if timeout is None else timeout)
"""

_WAIT_CALLER = "from net import wait\n\n\ndef main():\n    return wait('a')\n"

# The callee shares the parameter's name, so it lives in another file:
# a parameter is traced only in a file that names it inside its function alone.
_CLOCK = (
    "def pause(seconds):\n    return seconds\n\n\ndef check(url, poll):\n    return (url, poll)\n"
)

_WAIT_SLEEPING = '''\
from clock import check, pause


def wait(url, poll=30.0):
    """Wait for url, polling every *poll* seconds."""
    pause(poll)
    return check(url, poll)
'''

_WAIT_PASSING = '''\
from clock import check


def wait(url, poll=30.0):
    """Wait for url."""
    return check(url, poll=poll)
'''

_APP_DOCUMENTED = '''\
"""The app. It used to call helper; see mod.py."""
from mod import helper


def main():
    return helper()


if __name__ == "__main__":
    main()
'''

_APP_DOCUMENTED_NOT_CALLING = '''\
"""The app. It used to call helper; see mod.py."""


def main():
    return 2


if __name__ == "__main__":
    main()
'''


class TestRemovalIsProven:
    """The removal clause names a commit only when the code proves it took the last reader."""

    def test_a_param_still_read_at_head_has_no_removal(self, git_repo, capsys):
        # A timeout_seconds: a refactor rewrote three reads as one, and the param is still read.
        add = _commit(
            git_repo,
            {"sweep.py": _CLEANUP_READING_THRICE, "app.py": _CLEANUP_CALLER},
            "feat: cleanup",
        )
        _commit(git_repo, {"sweep.py": _CLEANUP_READING_ONCE}, "types: tighten cleanup")
        out = _run(capsys, "--check", "always-default-params", str(git_repo))
        assert "timeout" in out
        assert _provenance(out) == [f"provenance of timeout: added in {add} (feat: cleanup)"]

    def test_a_read_moved_with_a_mention_dropped_has_no_removal(self, git_repo, capsys):
        # A poll_seconds: a docstring mention and one read went, a read moved.
        add = _commit(
            git_repo,
            {"clock.py": _CLOCK, "net.py": _WAIT_SLEEPING, "app.py": _WAIT_CALLER},
            "feat: wait",
        )
        _commit(git_repo, {"net.py": _WAIT_PASSING}, "refactor: one code path")
        out = _run(capsys, "--check", "always-default-params", str(git_repo))
        assert "poll" in out
        assert _provenance(out) == [f"provenance of poll: added in {add} (feat: wait)"]

    def test_a_mention_dropped_after_the_last_reader_is_not_the_removal(self, git_repo, capsys):
        add = _commit(git_repo, {"mod.py": _HELPER, "app.py": _APP_DOCUMENTED}, "feat: helper")
        drop = _commit(git_repo, {"app.py": _APP_DOCUMENTED_NOT_CALLING}, "refactor: no helper")
        _commit(git_repo, {"app.py": _APP_NOT_CALLING}, "docs: forget helper")
        out = _run(capsys, "--check", "dead-code", str(git_repo))
        assert _provenance(out) == [
            f"provenance of helper: added in {add} (feat: helper), "
            f"last production reader removed in {drop} (refactor: no helper)"
        ]

    def test_a_literal_has_no_readers_to_remove(self, git_repo, capsys):
        commented = _HALVES_APP.replace(
            "    if width == 1337:", "    # 1337 is the odd width.\n    if width == 1337:"
        )
        add = _commit(
            git_repo, {"app.py": commented, "tests/test_app.py": _HALVES_TEST}, "feat: halves"
        )
        _commit(git_repo, {"app.py": _HALVES_APP}, "docs: drop the comment")
        out = _run(capsys, "--check", "fixture-literal-in-production", str(git_repo))
        assert _provenance(out) == [f"provenance of 1337: added in {add} (feat: halves)"]


def test_a_carriage_return_in_a_subject_does_not_split_its_record(git_repo, capsys):
    sha = _commit(git_repo, {"limits.py": "RETRIES = 3\n"}, "feat:\rretries")
    out = _run(capsys, "--check", "dead-constants", str(git_repo))
    assert _provenance(out) == [f"provenance of RETRIES: added in {sha} (feat:retries)"]


def test_the_query_rejects_a_multiline_token(git_repo):
    with pytest.raises(ValueError, match="one-line token"):
        GitQueries(git_repo).token_commits("a\nb", ("*.py",), follow=False)


def test_shallow_clone_has_no_complete_history(git_repo, tmp_path_factory):
    _commit(git_repo, {"a.py": "A = 1\n"}, "first")
    _commit(git_repo, {"a.py": "A = 2\n"}, "second")
    clone = tmp_path_factory.mktemp("shallow") / "clone"
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": "file"}
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{git_repo}", str(clone)],
        capture_output=True,
        check=True,
        env=env,
    )
    assert GitQueries(git_repo).has_complete_history()
    assert not GitQueries(clone).has_complete_history()


_HALVES_APP = """\
def halves(width):
    if width == 1337:
        return (668, 669)
    return (width // 2, width // 2)


def layout(cells):
    return halves(len(cells))
"""

_HALVES_TEST = """\
from app import halves


def test_odd_width():
    assert halves(1337) == (668, 669)
"""

# One committed fixture per check that carries subjects: the check fires on
# it, and its subject is distinctive enough for git to place.
_EVERY_CHECK = {
    "dead-code": ("helper", {"mod.py": _HELPER}),
    "dead-constants": ("RETRIES", {"limits.py": "RETRIES = 3\n"}),
    "vestigial-params": ("flag", {"job.py": _JOB_NOT_READING, "app.py": _APP_NOT_CALLING}),
    "unused-defaults": (
        "timeout",
        {
            "net.py": "def fetch(url, timeout=None):\n    return (url, timeout)\n",
            "app.py": "from net import fetch\n\n\ndef main():\n    return fetch('a', 3)\n",
        },
    ),
    "always-default-params": (
        "retries",
        {
            "net.py": "def fetch(url, retries=3):\n    return (url, retries)\n",
            "app.py": "from net import fetch\n\n\ndef main():\n    return fetch('a')\n",
        },
    ),
    "dead-branch-by-callers": (
        "verbose",
        {
            "net.py": (
                "def fetch(url, verbose):\n    if verbose:\n        print(url)\n    return url\n"
            ),
            "app.py": "from net import fetch\n\n\ndef main():\n    return fetch('a', False)\n",
        },
    ),
    "lone-dissenter": (
        "mode",
        {
            "net.py": "def fetch(url, mode='r'):\n    return open(url, mode)\n",
            "app.py": (
                "from net import fetch\n\n\ndef a():\n    return fetch('a')\n\n\n"
                "def b():\n    return fetch('b')\n\n\ndef c():\n    return fetch('c')\n\n\n"
                "def d():\n    return fetch('d', mode='w')\n"
            ),
        },
    ),
    "test-only-default": (
        "encoding",
        {
            "net.py": "def read(url, encoding='utf-8'):\n    return (url, encoding)\n",
            "app.py": "from net import read\n\n\ndef main():\n    return read('a', 'latin-1')\n",
            "tests/test_net.py": "from net import read\n\n\ndef test_read():\n    read('a')\n",
        },
    ),
    "literal-dispatch-param": (
        "fmt",
        {
            "net.py": (
                "def render(fmt):\n    if fmt == 'json':\n        return 1\n"
                "    elif fmt == 'csv':\n        return 2\n    raise ValueError(fmt)\n"
            ),
            "app.py": (
                "from net import render\n\n\ndef a():\n    return render('json')\n\n\n"
                "def b():\n    return render('csv')\n"
            ),
        },
    ),
    "fixture-literal-in-production": (
        "1337",
        {
            "app.py": _HALVES_APP,
            "tests/test_app.py": _HALVES_TEST,
        },
    ),
    "singleton-collection-param": (
        "items",
        {
            "net.py": "def send(items: list[str]):\n    return len(items)\n",
            "app.py": (
                "from net import send\n\n\ndef a(x):\n    return send([x])\n\n\n"
                "def b(y):\n    return send([y])\n"
            ),
        },
    ),
}


@pytest.mark.parametrize("check", sorted(_EVERY_CHECK))
def test_every_subject_check_prints_provenance(check, git_repo, capsys):
    name, files = _EVERY_CHECK[check]
    sha = _commit(git_repo, files, f"feat: {check}")
    out = _run(capsys, "--check", check, str(git_repo))
    assert f"=== {check} (1 finding(s)) ===" in out
    assert _provenance(out) == [f"provenance of {name}: added in {sha} (feat: {check})"]


# Checks that name a parameter without calling it vestigial: it is still
# read, so it is traced by no query and printed with no line.
_STILL_READ = {
    "constant-args": {
        "net.py": "def fetch(url, mode):\n    return open(url, mode)\n",
        "app.py": (
            "from net import fetch\n\n\ndef a():\n    return fetch('a', 'r')\n\n\n"
            "def b():\n    return fetch('b', 'r')\n"
        ),
    },
    "pass-through-params": {
        "net.py": (
            "def outer(url, mode):\n    print(url)\n    return inner(url, mode)\n\n\n"
            "def inner(url, mode):\n    return open(url, mode)\n"
        ),
        "app.py": "from net import outer\n\n\ndef a(mode):\n    return outer('a', mode)\n",
    },
}


@pytest.mark.parametrize("check", sorted(_STILL_READ))
def test_a_parameter_a_check_does_not_call_vestigial_gets_no_provenance(
    check, git_repo, capsys, monkeypatch
):
    _commit(git_repo, _STILL_READ[check], f"feat: {check}")
    calls = _count_queries(monkeypatch)
    out = _run(capsys, "--check", check, str(git_repo))
    assert f"=== {check} (1 finding(s)) ===" in out
    assert "provenance of" not in out
    assert calls == []


def test_chain_members_print_their_provenance_in_the_chain(git_repo, capsys):
    # Distinct names, so each sits only inside its own function and git can
    # trace it (``_confined``).
    layers = (
        "def outer(path, level=None):\n    print(path)\n    return inner(path, level)\n\n\n"
        "def inner(path, mode=None):\n    print(path)\n    return mode.upper()\n"
    )
    app = "from layers import outer\n\n\ndef main():\n    outer('a', 'x')\n    outer('b', 'y')\n"
    sha = _commit(git_repo, {"layers.py": layers, "app.py": app}, "feat: layers")
    checks = ["--check", "unused-defaults", "--check", "pass-through-params"]
    out = _run(capsys, *checks, str(git_repo))
    lines = out.splitlines()
    start = lines.index("=== finding chains (1 work item(s), 3 finding(s)) ===")
    section = lines[start + 2 : lines.index("", start)]
    assert [line.split("] ")[0] for line in section] == [
        "    layers.py:1: [pass-through-params",
        "    layers.py:1: [unused-defaults",
        f"      provenance of level: added in {sha} (feat: layers)",
        "    layers.py:6: [unused-defaults",
        f"      provenance of mode: added in {sha} (feat: layers)",
    ]
