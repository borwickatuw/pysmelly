"""The test role in use: the import graph, findings in test-role files, and history paths."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from pysmelly.checks.helpers import build_module_imports, package_dirs
from pysmelly.checks.history_coupling import check_change_coupling, check_test_erosion
from pysmelly.checks.history_team import check_divergent_change
from pysmelly.checks.module_graph import build_module_graph
from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo
from pysmelly.pytest_config import load_pytest_config
from pysmelly.registry import CHECKS, TEST_SUBJECT_CHECKS, Finding, Severity, run_check

from .history_test_helpers import _RECENT, feat_commits, make_ctx, make_large_file


def _graph(files: dict[str, str], import_roots: tuple[Path, ...] = ()) -> dict[Path, set[str]]:
    trees = {Path(name): ast.parse(code) for name, code in files.items()}
    packages = package_dirs(trees)
    graph = build_module_graph(trees, packages, import_roots, build_module_imports(trees, packages))
    return {path: {str(p) for p in loaded} for path, loaded in graph.loads.items()}


class TestImportGraph:
    def test_dotted_import_loads_the_module_and_its_packages(self):
        graph = _graph(
            {"main.py": "import pkg.sub.mod\n", "pkg/__init__.py": "", "pkg/sub/mod.py": ""}
        )
        assert graph[Path("main.py")] == {"pkg/__init__.py", "pkg/sub/mod.py"}

    def test_from_import_of_a_submodule_and_of_a_name(self):
        graph = _graph(
            {
                "main.py": "from pkg import mod\nfrom pkg.other import func\n",
                "pkg/__init__.py": "",
                "pkg/mod.py": "",
                "pkg/other.py": "def func(): ...\n",
            }
        )
        assert graph[Path("main.py")] == {"pkg/__init__.py", "pkg/mod.py", "pkg/other.py"}

    def test_relative_import_resolves_against_the_package(self):
        graph = _graph({"pkg/__init__.py": "", "pkg/a.py": "from .b import x\n", "pkg/b.py": ""})
        assert graph[Path("pkg/a.py")] == {"pkg/__init__.py", "pkg/b.py"}

    def test_src_layout_strips_the_source_root(self):
        graph = _graph({"tests/test_a.py": "import app.core\n", "src/app/core.py": ""})
        assert graph[Path("tests/test_a.py")] == {"src/app/core.py"}

    def test_unknown_top_level_name_is_tried_beside_the_importer(self):
        graph = _graph(
            {"tests/test_a.py": "import os\nfrom helpers import make\n", "tests/helpers.py": ""}
        )
        assert graph[Path("tests/test_a.py")] == {"tests/helpers.py"}

    def test_pytest_pythonpath_entries_are_import_roots(self):
        files = {"tests/test_g.py": "import gallery\n", "tools/gallery.py": ""}
        assert _graph(files)[Path("tests/test_g.py")] == set()
        assert _graph(files, (Path("tools"),))[Path("tests/test_g.py")] == {"tools/gallery.py"}

    def test_pythonpath_is_read_from_the_pytest_config(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\npythonpath = ["src", "tools", "../elsewhere"]\n',
            encoding="utf-8",
        )
        config = load_pytest_config(tmp_path, tmp_path)
        assert config.pythonpath == ("src", "tools", "../elsewhere")
        assert config.import_roots() == (Path("src"), Path("tools"))
        trees = {
            Path("tests/test_g.py"): ast.parse("import gallery\n"),
            Path("tools/gallery.py"): ast.parse(""),
        }
        ctx = AnalysisContext(trees, verbose=False, pytest_config=config)
        assert ctx.import_graph[Path("tests/test_g.py")] == frozenset({Path("tools/gallery.py")})

    def test_a_module_never_loads_itself(self):
        graph = _graph({"pkg/__init__.py": "from pkg import x\n"})
        assert graph[Path("pkg/__init__.py")] == set()


def _probe_tree(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "def unused_helper():\n    return 1\n\n\ndef test_x():\n    pass\n", encoding="utf-8"
    )
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")


class TestFindingsInTestRoleFiles:
    def test_checks_about_tests_are_marked(self):
        assert {"orphaned-test-helpers", "test-erosion"} <= TEST_SUBJECT_CHECKS

    def test_run_check_drops_findings_anchored_in_test_role_files(self, monkeypatch):
        ctx = AnalysisContext(
            {Path("tests/test_a.py"): ast.parse(""), Path("app.py"): ast.parse("")}, verbose=False
        )

        def fake(_ctx: AnalysisContext) -> list[Finding]:
            return [
                Finding("tests/test_a.py", 1, "fake", "in a test", Severity.LOW),
                Finding("app.py", 1, "fake", "in production", Severity.LOW),
            ]

        monkeypatch.setitem(CHECKS, "fake", fake)
        assert [f.file for f in run_check("fake", ctx)] == ["app.py"]
        monkeypatch.setattr("pysmelly.registry.TEST_SUBJECT_CHECKS", {"fake"})
        assert [f.file for f in run_check("fake", ctx)] == ["tests/test_a.py", "app.py"]

    def test_cli_reports_production_only_for_ordinary_checks(self, tmp_path, monkeypatch, capsys):
        _probe_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--more-please", "--check", "dead-code"])
        out = capsys.readouterr().out
        assert "app.py:1: run(): no production callers, no test callers" in out
        assert "tests/test_a.py" not in out

    def test_a_check_about_tests_still_fires_in_a_test_file(self, tmp_path, monkeypatch, capsys):
        _probe_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "orphaned-test-helpers"])
        assert "tests/test_a.py:1: unused_helper() in test_a.py has no callers" in (
            capsys.readouterr().out
        )


def _fixture_tree(root: Path) -> None:
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef old_client():\n    return 1\n", encoding="utf-8"
    )
    (root / "tests" / "test_api.py").write_text(
        "class TestApi:\n    def test_index(self):\n        pass\n", encoding="utf-8"
    )


class TestOrphanedTestHelpersWithTheRole:
    def test_fires_when_the_tests_are_parsed(self, tmp_path, monkeypatch, capsys):
        _fixture_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "orphaned-test-helpers"])
        assert (
            "tests/conftest.py:5: old_client fixture in conftest.py is never requested"
            in capsys.readouterr().out
        )

    def test_skips_fixtures_when_the_tests_are_excluded(self, tmp_path, monkeypatch, capsys):
        """DECISIONS.md: with the consumers excluded, fixture usage cannot be judged."""
        _fixture_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exit_info:
            main(
                [
                    str(tmp_path),
                    "--no-context",
                    "--check",
                    "orphaned-test-helpers",
                    "--exclude",
                    "test_*",
                ]
            )
        assert exit_info.value.code == 0
        assert "old_client" not in capsys.readouterr().out


def _co_change(file_str: str, others: list[str], count: int, tag: str) -> list[CommitInfo]:
    return [
        CommitInfo(
            hash=f"{tag}{i:04d}",
            date=_RECENT - timedelta(days=i),
            message=f"feat: change #{i}",
            files=[file_str, *others],
        )
        for i in range(count)
    ]


class TestHistoryPathsTakeTheRole:
    def test_deleted_test_files_are_not_a_concern_in_divergent_change(self):
        """qa/test_old.py is gone from the tree; pytest's default still makes it a test."""
        files = {"core/user.py": make_large_file(100)}
        commits = []
        for tag, other in [
            ("a", "auth/login.py"),
            ("b", "billing/invoice.py"),
            ("n", "notify/mail.py"),
            ("q", "qa/test_old.py"),
        ]:
            commits += _co_change("core/user.py", [other], 2, tag)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        assert check_divergent_change(ctx) == []

    def test_deleted_production_files_still_count_in_divergent_change(self):
        files = {"core/user.py": make_large_file(100)}
        commits = []
        for tag, other in [
            ("a", "auth/login.py"),
            ("b", "billing/invoice.py"),
            ("n", "notify/mail.py"),
            ("r", "reports/old.py"),
        ]:
            commits += _co_change("core/user.py", [other], 2, tag)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        assert len(check_divergent_change(ctx)) == 1

    def test_change_coupling_skips_a_test_support_module_paired_with_production(self):
        files = {
            "app.py": make_large_file(60),
            "tests/helpers.py": "import pytest\n\n\ndef make():\n    return 1\n",
            "tests/test_app.py": "import app\nfrom helpers import make\n",
        }
        commits = _co_change("app.py", ["tests/helpers.py"], 6, "c")
        ctx = make_ctx(files, commits=commits)
        assert ctx.roles.is_test("tests/helpers.py")
        assert check_change_coupling(ctx) == []

    def test_test_erosion_counts_the_tests_that_import_the_module(self):
        """The test is found by its import, whatever its name."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_invoices.py": "import pkg.billing\n\ndef test_it(): pass",
        }
        commits = feat_commits("pkg/billing.py", 9) + _co_change(
            "tests/test_invoices.py", [], 2, "t"
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert [f.file for f in findings] == ["pkg/billing.py"]
        assert "only 2 test commits" in findings[0].message

    def test_test_erosion_counts_a_commit_touching_two_tests_once(self):
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_a.py": "import pkg.billing\n",
            "tests/test_b.py": "import pkg.billing\n",
        }
        commits = feat_commits("pkg/billing.py", 9) + _co_change(
            "tests/test_a.py", ["tests/test_b.py"], 2, "t"
        )
        ctx = make_ctx(files, commits=commits)
        assert "only 2 test commits" in check_test_erosion(ctx)[0].message
