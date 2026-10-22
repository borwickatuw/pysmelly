"""dual-stack-dependency: both libraries of a catalog pair in production, on dated repositories."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.catalog import EMPTY_CATALOG, Catalog, CatalogPair, PairSide
from pysmelly.checks.dual_stack import _resolved_uses, check_dual_stack_dependency
from pysmelly.cli import main
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, GIT_HISTORY_CATEGORY, Severity

CHECK = "dual-stack-dependency"
_OLD = 400
_YOUNG = 30

# One os.path use, and one pathlib use per file.
OS_PATH = 'import os\n\n\ndef where(p):\n    return os.path.join(p, "x")\n'
PATHLIB = 'from pathlib import Path\n\n\ndef where(p):\n    return Path(p) / "x"\n'


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _commit(repo: Path, files: dict[str, str], days_ago: int) -> None:
    """Write ``files``, stage them by path, and commit them dated ``days_ago``."""
    _write(repo, files)
    for name in files:
        subprocess.run(["git", "add", "--", name], cwd=repo, capture_output=True, check=True)
    stamp = _when(days_ago).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "commit", "-q", "-m", "work"], cwd=repo, env=env, capture_output=True, check=True
    )


def _trees(repo: Path) -> dict[Path, ast.Module]:
    return {
        path.relative_to(repo): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(repo.rglob("*.py"))
        if ".git" not in path.parts
    }


def _ctx(repo: Path, user_catalog: Catalog = EMPTY_CATALOG) -> AnalysisContext:
    """The working tree's Python files, analyzed with the repository's history."""
    return AnalysisContext(
        _trees(repo), verbose=False, git=GitSettings(root=repo), user_catalog=user_catalog
    )


def _pathlib_files(count: int) -> dict[str, str]:
    return {f"new{i}.py": PATHLIB for i in range(count)}


class TestRegistration:
    def test_is_a_medium_git_history_check(self):
        assert CHECK_CATEGORIES[CHECK] == GIT_HISTORY_CATEGORY
        assert CHECK_SEVERITY[CHECK] == Severity.MEDIUM


class TestShareAndAge:
    def test_older_minority_under_30_percent_is_reported(self, git_repo):
        _commit(git_repo, {"old.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        (finding,) = check_dual_stack_dependency(_ctx(git_repo))
        assert (finding.file, finding.line, finding.severity) == ("old.py", 1, Severity.MEDIUM)
        assert finding.message == (
            "os.path and pathlib are both used for path manipulation in production. "
            "os.path has 1 use site(s) in 1 file(s) (old.py:1), 25% of the pair's "
            "against 3 for pathlib, and is the older stack "
            f"(imports from {_when(_OLD):%Y-%m-%d} by median, "
            f"pathlib's from {_when(_YOUNG):%Y-%m-%d})"
        )

    def test_minority_at_30_percent_or_more_is_not_reported(self, git_repo):
        """One use against two is 33%: both libraries are in real use."""
        _commit(git_repo, {"old.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(2), _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_newer_minority_is_a_migration_starting(self, git_repo):
        _commit(git_repo, _pathlib_files(3), _OLD)
        _commit(git_repo, {"old.py": OS_PATH}, _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_sides_from_one_commit_are_not_older(self, git_repo):
        _commit(git_repo, {"old.py": OS_PATH, **_pathlib_files(3)}, _OLD)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_one_side_alone_is_not_reported(self, git_repo):
        _commit(git_repo, _pathlib_files(3), _OLD)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_no_git_root_reports_nothing(self, git_repo):
        """Without blame the minority could be a migration just starting."""
        _commit(git_repo, {"old.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        assert check_dual_stack_dependency(AnalysisContext(_trees(git_repo), verbose=False)) == []

    def test_minority_is_measured_not_declared(self, git_repo):
        """pathlib is the older minority here, so it is the side reported."""
        _commit(git_repo, {"new0.py": PATHLIB}, _OLD)
        _commit(git_repo, {f"old{i}.py": OS_PATH for i in range(3)}, _YOUNG)
        (finding,) = check_dual_stack_dependency(_ctx(git_repo))
        assert finding.message.startswith("pathlib and os.path are both used for path manipulation")
        assert "against 3 for os.path" in finding.message

    def test_age_is_the_median_of_the_files(self, git_repo):
        """Two of three os.path files are recent, so os.path is not the older stack."""
        _commit(git_repo, {"a.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(9), _OLD - 100)
        _commit(git_repo, {"b.py": OS_PATH, "c.py": OS_PATH}, _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_every_minority_file_is_a_site(self, git_repo):
        _commit(git_repo, {"a.py": OS_PATH, "b.py": "\n" + OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(9), _YOUNG)
        (finding,) = check_dual_stack_dependency(_ctx(git_repo))
        assert [(site.file, site.line) for site in finding.sites] == [("a.py", 1), ("b.py", 2)]
        assert "2 use site(s) in 2 file(s) (a.py:1, b.py:2), 18% of the pair's" in finding.message


class TestWhatCounts:
    def test_test_role_uses_do_not_count(self, git_repo):
        """pathlib's only uses are in tests, so production uses one stack."""
        _commit(git_repo, {"old.py": OS_PATH}, _OLD)
        _commit(git_repo, {f"test_new{i}.py": PATHLIB for i in range(3)}, _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_test_role_minority_sites_do_not_count(self, git_repo):
        _commit(git_repo, {"test_old.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_no_substitute_uses_do_not_count(self, git_repo):
        """pathlib has no normpath, so that use cannot move."""
        normpath = "import os\n\n\ndef tidy(p):\n    return os.path.normpath(p)\n"
        _commit(git_repo, {"old.py": normpath}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_import_os_used_for_environ_only_is_not_os_path(self, git_repo):
        environ = 'import os\n\n\ndef home():\n    return os.environ["HOME"]\n'
        _commit(git_repo, {"old.py": environ}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    @pytest.mark.parametrize(
        ("source", "dotted"),
        [
            ("import os\nos.path.join('a')\n", ["os.path.join"]),
            ("import os.path\nos.path.isdir('a')\n", ["os.path.isdir"]),
            ("import os.path as osp\nosp.isdir('a')\n", ["os.path.isdir"]),
            ("from os import path\npath.exists('a')\n", ["os.path.exists"]),
            ("from os.path import join\njoin('a')\n", ["os.path.join"]),
            ("from pathlib import Path\nPath('a').resolve()\n", ["pathlib.Path"]),
            (
                "from dataclasses import dataclass\n@dataclass\nclass A: pass\n",
                ["dataclasses.dataclass"],
            ),
            ("from . import os\nos.path.join('a')\n", []),
            ("import os\nos.path.sep = '/'\n", []),
        ],
    )
    def test_resolved_uses(self, source, dotted):
        assert [use.dotted for use in _resolved_uses(ast.parse(source))] == dotted


class TestHeadAndWorkingTree:
    def test_uncommitted_minority_file_is_newest(self, git_repo):
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        _write(git_repo, {"old.py": OS_PATH})
        assert check_dual_stack_dependency(_ctx(git_repo)) == []

    def test_uncommitted_majority_file_is_newest(self, git_repo):
        _commit(git_repo, {"old.py": OS_PATH, "new0.py": PATHLIB}, _OLD)
        _write(git_repo, _pathlib_files(4))
        (finding,) = check_dual_stack_dependency(_ctx(git_repo))
        assert "pathlib's from" in finding.message

    def test_lines_added_above_the_import_do_not_misplace_the_blame(self, git_repo):
        """The import is blamed at its HEAD line; the site is its working-tree line."""
        _commit(git_repo, {"old.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        _write(git_repo, {"old.py": '"""Old."""\n\n' + OS_PATH})
        (finding,) = check_dual_stack_dependency(_ctx(git_repo))
        assert finding.line == 3
        assert "(old.py:3)" in finding.message
        assert f"imports from {_when(_OLD):%Y-%m-%d} by median" in finding.message

    def test_use_added_in_the_working_tree_is_newest(self, git_repo):
        """HEAD's old.py uses no os.path, so its use is not committed yet."""
        _commit(git_repo, {"old.py": "import os\n"}, _OLD)
        _commit(git_repo, _pathlib_files(3), _YOUNG)
        _write(git_repo, {"old.py": OS_PATH})
        assert check_dual_stack_dependency(_ctx(git_repo)) == []


PAIR_TOML = """\
[[{prefix}catalog.pairs]]
name = "acme-http"
description = "internal HTTP"
sides = [
    {{ label = "acme.legacy_http", imports = ["acme.legacy_http"] }},
    {{ label = "acme.http", imports = ["acme.http"] }},
]
"""

LEGACY = "from acme import legacy_http\n\n\ndef get(u):\n    return legacy_http.fetch(u)\n"
MODERN = "from acme import http\n\n\ndef get(u):\n    return http.fetch(u)\n"


class TestProjectPairs:
    def test_project_pair_is_checked(self, git_repo):
        pair = CatalogPair(
            "acme-http",
            "internal HTTP",
            (
                PairSide("acme.legacy_http", ("acme.legacy_http",)),
                PairSide("acme.http", ("acme.http",)),
            ),
        )
        _commit(git_repo, {"old.py": LEGACY}, _OLD)
        _commit(git_repo, {f"new{i}.py": MODERN for i in range(3)}, _YOUNG)
        assert check_dual_stack_dependency(_ctx(git_repo)) == []
        (finding,) = check_dual_stack_dependency(_ctx(git_repo, Catalog(pairs=(pair,))))
        assert finding.message.startswith(
            "acme.legacy_http and acme.http are both used for internal HTTP"
        )

    def test_project_pair_from_config_reaches_the_git_history_run(
        self, git_repo, monkeypatch, capsys
    ):
        _commit(git_repo, {"old.py": LEGACY}, _OLD)
        _commit(git_repo, {f"new{i}.py": MODERN for i in range(3)}, _YOUNG)
        (git_repo / ".pysmelly.toml").write_text(PAIR_TOML.format(prefix=""), encoding="utf-8")
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--check", CHECK, "."])
        assert "old.py:1: acme.legacy_http and acme.http are both used for internal HTTP" in (
            capsys.readouterr().out
        )


FINDING_LINE = re.compile(r"^  \S+\.py:\d+: ")
IGNORE = f"# pysmelly: ignore[{CHECK}]\n"


class TestPerSiteIgnore:
    def _run(self, repo: Path, monkeypatch, capsys) -> list[str]:
        monkeypatch.chdir(repo)
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--check", CHECK, "."])
        out = capsys.readouterr().out
        return [line.strip() for line in out.splitlines() if FINDING_LINE.match(line)]

    def test_ignore_on_one_minority_import_removes_only_that_file(
        self, git_repo, monkeypatch, capsys
    ):
        _commit(git_repo, {"a.py": IGNORE + OS_PATH, "b.py": OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(9), _YOUNG)
        (line,) = self._run(git_repo, monkeypatch, capsys)
        assert line.startswith("b.py:1: os.path and pathlib")
        assert "1 use site(s) in 1 file(s) (b.py:1), 10% of the pair's" in line
        assert "(1 other site ignored)" in line

    def test_every_minority_import_ignored_leaves_no_finding(self, git_repo, monkeypatch, capsys):
        _commit(git_repo, {"a.py": IGNORE + OS_PATH, "b.py": IGNORE + OS_PATH}, _OLD)
        _commit(git_repo, _pathlib_files(9), _YOUNG)
        assert self._run(git_repo, monkeypatch, capsys) == []

    def test_ignored_old_file_can_leave_the_rest_not_older(self, git_repo, monkeypatch, capsys):
        """With the oldest file's use accepted, the os.path left is not older than pathlib."""
        _commit(git_repo, {"a.py": OS_PATH}, _OLD)
        _commit(git_repo, {"b.py": OS_PATH}, _OLD - 20)
        _commit(git_repo, _pathlib_files(12), _OLD - 100)
        _commit(git_repo, {"c.py": OS_PATH, "d.py": OS_PATH}, _YOUNG)
        (line,) = self._run(git_repo, monkeypatch, capsys)
        assert line.startswith("a.py:1: ")
        _write(git_repo, {"a.py": IGNORE + OS_PATH})
        assert self._run(git_repo, monkeypatch, capsys) == []
