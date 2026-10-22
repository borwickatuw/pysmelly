"""Tests for git history coupling checks."""

from __future__ import annotations

import ast
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from pysmelly.checks.helpers import filepath_to_module, filepath_to_package
from pysmelly.checks.history_coupling import (
    _imports_module,
    check_blast_radius,
    check_change_coupling,
    check_conscious_debt,
    check_emergency_hotspots,
    check_no_refactoring,
    check_test_erosion,
    check_yo_yo_code,
)
from pysmelly.checks.history_growth import check_churn_without_growth
from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo, FileStats

from .history_test_helpers import (
    _RECENT,
    bulk_commit,
    feat_commits,
    fix_commits,
    make_ctx,
    make_large_file,
)


def _make_commits_for_blast_radius(
    target_file: str, co_change_counts: list[int]
) -> list[CommitInfo]:
    """Create commits where target_file changes with N other files each time."""
    commits = []
    for i, n in enumerate(co_change_counts):
        files = [target_file] + [f"other_{i}_{j}.py" for j in range(n)]
        commits.append(
            CommitInfo(
                hash=f"abc{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit {i}",
                files=files,
            )
        )
    return commits


def _make_coupling_commits(
    file_a: str, file_b: str, together: int, a_alone: int = 0, b_alone: int = 0
) -> list[CommitInfo]:
    """Create commits for coupling analysis."""
    commits = []
    for i in range(together):
        commits.append(
            CommitInfo(
                hash=f"together{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"both {i}",
                files=[file_a, file_b],
            )
        )
    for i in range(a_alone):
        commits.append(
            CommitInfo(
                hash=f"alone_a{i:04d}",
                date=_RECENT - timedelta(days=together + i),
                message=f"just a {i}",
                files=[file_a],
            )
        )
    for i in range(b_alone):
        commits.append(
            CommitInfo(
                hash=f"alone_b{i:04d}",
                date=_RECENT - timedelta(days=together + a_alone + i),
                message=f"just b {i}",
                files=[file_b],
            )
        )
    return commits


def _emergency_commits(filepath: str, count: int) -> list[CommitInfo]:
    """Create emergency/hotfix commits touching a file."""
    return [
        CommitInfo(
            hash=f"emrg{i:04d}",
            date=_RECENT - timedelta(days=i),
            message=f"hotfix: urgent fix #{i}",
            files=[filepath],
        )
        for i in range(count)
    ]


# --- blast-radius tests ---


class TestBlastRadius:
    def test_high_blast_radius(self):
        """File with median >= 8 co-changes flagged."""
        files = {"services/payment.py": "x = 1"}
        # 7 commits, each touching 9+ other files — median exceeds threshold of 8
        commits = _make_commits_for_blast_radius("services/payment.py", [9, 10, 8, 11, 9, 10, 8])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1
        assert "payment.py" in findings[0].message
        assert findings[0].check == "blast-radius"

    def test_low_blast_radius_no_finding(self):
        """File with median < 8 co-changes not flagged."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [5, 6, 4, 5, 6, 5, 4])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_commits(self):
        """File with < 5 qualifying commits skipped."""
        files = {"services/payment.py": "x = 1"}
        commits = _make_commits_for_blast_radius("services/payment.py", [6, 7, 8, 6])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py is skipped."""
        files = {"pkg/__init__.py": ""}
        commits = _make_commits_for_blast_radius("pkg/__init__.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_bulk_commits_excluded(self):
        """Commits touching >= 20 files are excluded."""
        files = {"services/payment.py": "x = 1"}
        # All commits are bulk (>= 20 other files)
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [20, 25, 30, 22, 21, 20, 25]
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"tests/test_billing.py": "x = 1"}
        commits = _make_commits_for_blast_radius("tests/test_billing.py", [6, 7, 5, 8, 6, 7, 5])
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

    def test_co_changed_tests_are_not_dragged(self):
        """A test-role file changing with the file is the change's test, not blast."""
        files = {"services/payment.py": "x = 1"}
        commits = [
            CommitInfo(
                hash=f"t{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit {i}",
                files=[
                    "services/payment.py",
                    *(f"services/other_{i}_{j}.py" for j in range(5)),
                    *(f"tests/test_payment_{j}.py" for j in range(5)),
                ],
            )
            for i in range(7)
        ]
        ctx = make_ctx(files, commits=commits)
        assert check_blast_radius(ctx) == []

    def test_test_partners_not_listed(self):
        """The partner list names production files only."""
        files = {"services/payment.py": "x = 1"}
        commits = [
            CommitInfo(
                hash=f"t{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit {i}",
                files=[
                    "services/payment.py",
                    *(f"services/other_{j}.py" for j in range(9)),
                    "tests/test_payment.py",
                ],
            )
            for i in range(7)
        ]
        ctx = make_ctx(files, commits=commits)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1
        assert "median of 9 " in findings[0].message
        assert "test_payment.py" not in findings[0].message

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_blast_radius(ctx) == []

    def test_threshold_scales_with_project(self):
        """median_commit_size=5 means threshold=12, so median of 10 doesn't fire but 13 does."""
        files = {"services/payment.py": "x = 1"}

        # median of 10 < threshold of 12 -> no finding
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [10, 10, 10, 10, 10, 10, 10]
        )
        ctx = make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 0

        # median of 13 >= threshold of 12 -> finding
        commits = _make_commits_for_blast_radius(
            "services/payment.py", [13, 14, 12, 15, 13, 14, 12]
        )
        ctx = make_ctx(files, commits=commits, median_commit_size=5.0)
        findings = check_blast_radius(ctx)
        assert len(findings) == 1


# --- change-coupling tests ---


class TestChangeCoupling:
    def test_high_coupling_no_import(self):
        """Highly coupled files with no import -> finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "api/views.py" in findings[0].message
        assert "billing/invoice.py" in findings[0].message
        assert "hidden coupling" in findings[0].message

    def test_coupled_files_with_import_no_finding(self):
        """Highly coupled files WITH import relationship -> no finding."""
        files = {
            "api/views.py": "from billing.invoice import create_invoice\nx = 1",
            "billing/invoice.py": "def create_invoice(): pass",
        }
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_relative_import_detected(self):
        """Relative import (from .utils import ...) counts as import relationship."""
        files = {
            "myapp/works/models/file.py": "from .utils import generate_permalink\nx = 1",
            "myapp/works/models/utils.py": "def generate_permalink(): pass",
        }
        commits = _make_coupling_commits(
            "myapp/works/models/file.py",
            "myapp/works/models/utils.py",
            together=8,
            a_alone=2,
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_relative_parent_import_detected(self):
        """Relative parent import (from .. import X) counts as import relationship."""
        files = {
            "pkg/sub/views.py": "from ..models import User\nx = 1",
            "pkg/models.py": "class User: pass",
        }
        commits = _make_coupling_commits("pkg/sub/views.py", "pkg/models.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_src_layout_import_detected(self):
        """Under a src layout, the absolute import still counts as a relationship."""
        files = {
            "src/pkg/a.py": "from pkg.b import thing\nx = 1",
            "src/pkg/b.py": "def thing(): pass",
        }
        commits = _make_coupling_commits("src/pkg/a.py", "src/pkg/b.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert findings == []

    def test_src_layout_without_import_still_flagged(self):
        """Stripping the source root must not suppress genuinely unrelated files."""
        files = {
            "src/pkg/api/views.py": "x = 1",
            "src/pkg/billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "src/pkg/api/views.py", "src/pkg/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "hidden coupling" in findings[0].message

    def testfilepath_to_module_strips_source_root(self):
        """The path -> module mapping matches what a src-layout project imports."""
        packages = {("src", "pkg"), ("src", "pkg", "sub"), ("src", "pkg", "lib")}
        assert filepath_to_module("src/pkg/a.py", packages) == "pkg.a"
        assert filepath_to_module("pkg/a.py", packages) == "pkg.a"
        assert filepath_to_module("src/pkg/__init__.py", packages) == "pkg"
        assert filepath_to_package("src/pkg/sub/a.py", packages) == "pkg.sub"
        # A 'lib' package under the source root keeps its place.
        assert filepath_to_module("src/pkg/lib/a.py", packages) == "pkg.lib.a"

    def test_src_layout_module_unchanged_without_init(self):
        """A repo-root ``src/`` is a source root even when no ``__init__.py`` is analyzed."""
        assert filepath_to_module("src/pkg/mod.py", set()) == "pkg.mod"

    def test_workspace_member_source_root_stripped(self):
        """A uv workspace member's ``home/src/`` is a source root too."""
        packages = {("home", "src", "myproj_home")}
        assert filepath_to_module("home/src/myproj_home/build.py", packages) == "myproj_home.build"
        assert filepath_to_module("home/src/myproj_home/__init__.py", packages) == "myproj_home"
        assert filepath_to_package("home/src/myproj_home/__init__.py", packages) == "myproj_home"
        assert filepath_to_package("home/src/myproj_home/build.py", packages) == "myproj_home"

    def test_lib_package_keeps_its_place(self):
        """A ``lib`` directory with ``__init__.py`` is a package, not a source root."""
        packages = {("pkg",), ("pkg", "lib")}
        assert filepath_to_module("pkg/lib/mod.py", packages) == "pkg.lib.mod"
        assert filepath_to_package("pkg/lib/mod.py", packages) == "pkg.lib"

    def test_lib_dir_inside_package_keeps_its_place(self):
        """A ``lib`` without ``__init__.py`` inside a package is a namespace portion of it."""
        assert filepath_to_module("pkg/lib/mod.py", {("pkg",)}) == "pkg.lib.mod"

    def test_non_package_lib_dir_is_a_source_root(self):
        """A ``lib`` directory outside any package is a layout artifact."""
        assert filepath_to_module("tools/lib/helper.py", set()) == "helper"

    def test_module_named_lib_keeps_its_name(self):
        """Only directories are source roots; ``lib.py`` is a module."""
        assert filepath_to_module("lib.py", set()) == "lib"
        assert filepath_to_module("scripts/src.py", set()) == "scripts.src"

    def test_repo_root_init_does_not_make_src_a_package(self):
        """A stray root ``__init__.py`` names nothing, so ``src/`` stays a source root."""
        assert filepath_to_module("src/pkg/mod.py", {(), ("src", "pkg")}) == "pkg.mod"

    def test_deepest_source_root_wins(self):
        """With nested source roots, the import name starts below the innermost one."""
        assert (
            filepath_to_module("lib/member/src/pkg/mod.py", {("lib", "member", "src", "pkg")})
            == "pkg.mod"
        )

    def test_low_coupling_ratio(self):
        """Low coupling ratio (< 0.7) -> no finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        # 5 together, a has 10 alone, b has 5 alone
        # min(15, 10) = 10, ratio = 5/10 = 0.5 < 0.7
        commits = _make_coupling_commits(
            "api/views.py", "billing/invoice.py", together=5, a_alone=10, b_alone=5
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_fewer_than_5_co_changes(self):
        """Fewer than 5 co-changes -> no finding."""
        files = {
            "api/views.py": "x = 1",
            "billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits("api/views.py", "billing/invoice.py", together=4)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_test_source_pair_skipped(self):
        """Test file + source file pair is skipped."""
        files = {
            "app.py": "x = 1",
            "test_app.py": "y = 2",
        }
        commits = _make_coupling_commits("app.py", "test_app.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_init_py_skipped(self):
        """__init__.py files are excluded from coupling analysis."""
        files = {
            "pkg/__init__.py": "",
            "other/module.py": "x = 1",
        }
        commits = _make_coupling_commits(
            "pkg/__init__.py", "other/module.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_migration_files_skipped(self):
        """Migration files are excluded."""
        files = {
            "app/migrations/0001.py": "x = 1",
            "app/models.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "app/migrations/0001.py", "app/models.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_expected_coupling_suppressed(self):
        """Pairs matching expected-coupling config are suppressed."""
        files = {
            "app/settings.py": "x = 1",
            "app/urls.py": "y = 2",
        }
        commits = _make_coupling_commits("app/settings.py", "app/urls.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        # Without config, would find coupling
        findings = check_change_coupling(ctx)
        assert len(findings) == 1

        # With expected-coupling, suppressed
        ctx.expected_coupling = [["*/settings.py", "*/urls.py"]]
        findings = check_change_coupling(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        """No git_history -> empty."""
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_change_coupling(ctx) == []

    def test_symbol_import_from_package_does_not_explain_sibling_coupling(self):
        """`from pkg import VERSION` is not a dependency on pkg's other modules."""
        files = {
            "src/pkg/api/views.py": "from pkg import VERSION\nx = 1",
            "src/pkg/billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "src/pkg/api/views.py", "src/pkg/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "hidden coupling" in findings[0].message

    def test_plain_submodule_import_explains_coupling(self):
        """`import pkg.billing` credits the modules under pkg/billing/."""
        files = {
            "src/pkg/api/views.py": "import pkg.billing\nx = 1",
            "src/pkg/billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "src/pkg/api/views.py", "src/pkg/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        assert check_change_coupling(ctx) == []

    def test_bare_top_level_import_does_not_explain_coupling(self):
        """`import pkg` runs only pkg/__init__.py — it does not clear pkg/billing."""
        files = {
            "src/pkg/api/views.py": "import pkg\nx = 1",
            "src/pkg/billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "src/pkg/api/views.py", "src/pkg/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "hidden coupling" in findings[0].message

    def test_star_import_of_package_does_not_explain_sibling_coupling(self):
        """`from pkg import *` credits pkg, not every module under it."""
        files = {
            "src/pkg/api/views.py": "from pkg import *\nx = 1",
            "src/pkg/billing/invoice.py": "y = 2",
        }
        commits = _make_coupling_commits(
            "src/pkg/api/views.py", "src/pkg/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_change_coupling(ctx)
        assert len(findings) == 1
        assert "hidden coupling" in findings[0].message

    def test_dotted_string_naming_the_module_explains_coupling(self):
        """Django's `ROOT_URLCONF = "proj.urls"` loads proj/urls.py by name."""
        files = {
            "proj/settings.py": 'ROOT_URLCONF = "proj.urls"\n',
            "proj/urls.py": "urlpatterns = []\n",
        }
        commits = _make_coupling_commits("proj/settings.py", "proj/urls.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        assert check_change_coupling(ctx) == []

    def test_dotted_string_naming_an_attribute_explains_coupling(self):
        """`"proj.tasks.sweep"` and `"proj.tasks:sweep"` name proj/tasks.py's attribute."""
        for spelling in ("proj.tasks.sweep", "proj.tasks:sweep"):
            files = {
                "proj/celery.py": f'BEAT = {{"sweep": {{"task": "{spelling}"}}}}\n',
                "proj/tasks.py": "def sweep(): pass\n",
            }
            commits = _make_coupling_commits(
                "proj/celery.py", "proj/tasks.py", together=8, a_alone=2
            )
            ctx = make_ctx(files, commits=commits)
            assert check_change_coupling(ctx) == [], spelling

    def test_dotted_string_reaching_past_a_module_does_not_explain_it(self):
        """Only the module a string spells, or the one holding its last name, is
        credited: `"proj.billing.invoice.total.extra"` names no module."""
        files = {
            "proj/api.py": 'LABEL = "proj.billing.invoice.total.extra"\n',
            "proj/billing/invoice.py": "y = 2\n",
        }
        commits = _make_coupling_commits(
            "proj/api.py", "proj/billing/invoice.py", together=8, a_alone=2
        )
        ctx = make_ctx(files, commits=commits)
        assert len(check_change_coupling(ctx)) == 1

    def test_prose_string_does_not_explain_coupling(self):
        """A string that is not a dotted path as a whole credits nothing."""
        files = {
            "proj/api.py": 'NOTE = "see proj.billing for details"\n',
            "proj/billing.py": "y = 2\n",
        }
        commits = _make_coupling_commits("proj/api.py", "proj/billing.py", together=8, a_alone=2)
        ctx = make_ctx(files, commits=commits)
        assert len(check_change_coupling(ctx)) == 1


# --- _imports_module unit tests ---


def _credits(tree: ast.Module, module: str, package: str) -> bool:
    """Whether a module in ``package`` holding ``tree`` credits ``module``.

    The module's imports are read from the module graph, as change-coupling
    reads them.
    """
    path = Path(*package.split("."), "mod.py") if package else Path("mod.py")
    trees = {path: tree}
    if package:
        trees[path.parent / "__init__.py"] = ast.parse("")
    node = AnalysisContext(trees, verbose=False).module_graph.nodes[path]
    return _imports_module(node.imports, module)


def _workspace_member_repo(repo: Path, build_source: str) -> None:
    """Commit a uv-workspace-shaped repo where build.py and config.py co-change.

    The layout is a typical uv workspace's: a workspace member ``home/`` with its own
    ``src/`` holding the ``myproj_home`` package, plus a root ``pyproject.toml``
    declaring the workspace. Six commits touch both files together and one
    touches config.py alone, so the pair clears change-coupling's thresholds.
    """
    pkg = repo / "home" / "src" / "myproj_home"
    pkg.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["home"]\n')
    (repo / "home" / "pyproject.toml").write_text('[project]\nname = "myproj-home"\n')
    (pkg / "__init__.py").write_text("")
    for i in range(6):
        (pkg / "config.py").write_text(f"x = {i}\n")
        (pkg / "build.py").write_text(f"{build_source}\ny = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"feat: build step {i}")
    (pkg / "config.py").write_text("x = 99\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: config alone")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _run_change_coupling(repo: Path, capsys) -> str:
    with pytest.raises(SystemExit):
        main(["git-history", "--no-context", "--check", "change-coupling", str(repo)])
    return capsys.readouterr().out


class TestChangeCouplingWorkspaceMember:
    """End to end on a real git repo with a uv workspace member's src layout."""

    def test_member_import_explains_co_change(self, git_repo, capsys):
        _workspace_member_repo(git_repo, "from myproj_home.config import x")
        out = _run_change_coupling(git_repo, capsys)
        assert "hidden coupling" not in out
        assert "All checks passed." in out

    def test_member_pair_without_import_still_flagged(self, git_repo, capsys):
        _workspace_member_repo(git_repo, "import os")
        out = _run_change_coupling(git_repo, capsys)
        assert (
            "home/src/myproj_home/build.py and home/src/myproj_home/config.py changed together"
        ) in out
        assert "hidden coupling" in out


class TestImportsModule:
    """A symbol import must not clear a package; a submodule import must."""

    def test_symbol_import_does_not_credit_sibling_module(self):
        """`from pkg import VERSION` says nothing about pkg/checks/imports.py."""
        tree = ast.parse("from pkg import VERSION")
        assert _credits(tree, "pkg.checks.imports", "") is False

    def test_symbol_import_does_not_credit_nonexistent_module(self):
        """The reverse-prefix rule used to accept any path under the package."""
        tree = ast.parse("from pkg import VERSION")
        assert _credits(tree, "pkg.totally_unrelated", "") is False

    def test_plain_submodule_import_credits_modules_under_it(self):
        """`import pkg.checks` is a declared dependency on pkg/checks/*.py."""
        tree = ast.parse("import pkg.checks")
        assert _credits(tree, "pkg.checks.imports", "") is True

    def test_from_import_of_submodule_credits_modules_under_it(self):
        """The `from pkg import checks` spelling of the same dependency."""
        tree = ast.parse("from pkg import checks")
        assert _credits(tree, "pkg.checks.imports", "") is True

    def test_symbol_import_still_credits_the_package_itself(self):
        """`from pkg import VERSION` does run pkg/__init__.py."""
        tree = ast.parse("from pkg import VERSION")
        assert _credits(tree, "pkg", "") is True

    def test_deep_import_credits_every_package_it_runs(self):
        """Importing pkg.checks.imports executes pkg/__init__.py."""
        tree = ast.parse("from pkg.checks.imports import thing")
        assert _credits(tree, "pkg", "") is True

    def test_relative_symbol_import_does_not_credit_sibling_module(self):
        """Relative imports get the same alias rule, not the old bare-package one."""
        tree = ast.parse("from . import VERSION")
        assert _credits(tree, "pkg.checks.imports", "pkg") is False

    def test_relative_submodule_import_credits_modules_under_it(self):
        """`from . import checks` inside pkg names the pkg.checks package."""
        tree = ast.parse("from . import checks")
        assert _credits(tree, "pkg.checks.imports", "pkg") is True

    def test_bare_top_level_import_does_not_credit_modules_under_it(self):
        """`import pkg` runs pkg/__init__.py only — not pkg/checks/imports.py."""
        tree = ast.parse("import pkg")
        assert _credits(tree, "pkg.checks.imports", "") is False

    def test_bare_top_level_import_credits_the_package_itself(self):
        """The same `import pkg` still credits pkg, whose __init__ it runs."""
        tree = ast.parse("import pkg")
        assert _credits(tree, "pkg", "") is True

    def test_star_import_credits_the_package_but_not_modules_under_it(self):
        """`from pkg import *` builds no `pkg.*` path; the head pkg still credits."""
        tree = ast.parse("from pkg import *")
        assert _credits(tree, "pkg", "") is True
        assert _credits(tree, "pkg.checks.imports", "") is False

    def test_star_import_of_submodule_credits_that_submodule(self):
        """`from pkg.checks import *` credits pkg.checks, which it runs."""
        tree = ast.parse("from pkg.checks import *")
        assert _credits(tree, "pkg.checks", "") is True

    def test_name_rule_alone_does_not_credit_a_re_exports_source(self):
        """The dotted-name rules never reach a re-export's defining module.

        `from pkg.checks import thing` names pkg.checks, not
        pkg/checks/impl.py, which re-exports it; that credit comes from the
        module graph (`TestReExportCredit`), not from the import's path.
        """
        tree = ast.parse("from pkg.checks import thing")
        assert _credits(tree, "pkg.checks", "") is True
        assert _credits(tree, "pkg.checks.impl", "") is False


# --- yo-yo-code tests ---


class TestYoYoCode:
    def test_high_gross_churn(self):
        """File with gross churn >= 3x its size -> finding."""
        files = {"utils/parser.py": make_large_file(200)}
        file_stats = {
            # 350 ins + 340 del = 690 gross, 690/200 = 3.45x
            "utils/parser.py": FileStats(total_insertions=350, total_deletions=340, commit_count=12)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 1
        assert "3.5x turnover" in findings[0].message
        assert findings[0].check == "yo-yo-code"

    def test_low_gross_churn_no_finding(self):
        """File with gross churn < 3x -> no finding."""
        files = {"app.py": make_large_file(200)}
        file_stats = {
            # 200 ins + 100 del = 300 gross, 300/200 = 1.5x
            "app.py": FileStats(total_insertions=200, total_deletions=100, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 100 lines -> no finding."""
        files = {"tiny.py": make_large_file(50)}
        file_stats = {
            "tiny.py": FileStats(total_insertions=200, total_deletions=200, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 5 commits -> no finding."""
        files = {"app.py": make_large_file(200)}
        file_stats = {
            "app.py": FileStats(total_insertions=400, total_deletions=400, commit_count=4)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_parser.py": make_large_file(200)}
        file_stats = {
            "test_parser.py": FileStats(total_insertions=400, total_deletions=400, commit_count=10)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        findings = check_yo_yo_code(ctx)
        assert len(findings) == 0

    def test_different_from_churn_without_growth(self):
        """A file can trigger yo-yo but not churn-without-growth (and vice versa).

        yo-yo: high gross churn (ins + del) relative to size
        churn-without-growth: many commits, low net change (ins - del)
        A file growing fast has high net but could also have high gross.
        """
        files = {"app.py": make_large_file(200)}
        file_stats = {
            # Net: 400-300 = 100 (50% of 200, above 10% threshold -> no churn-without-growth)
            # Gross: 400+300 = 700 (3.5x of 200 -> yo-yo triggers)
            "app.py": FileStats(total_insertions=400, total_deletions=300, commit_count=15)
        }
        ctx = make_ctx(files, file_stats=file_stats)
        assert len(check_yo_yo_code(ctx)) == 1
        assert len(check_churn_without_growth(ctx)) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_yo_yo_code(ctx) == []


# --- conscious-debt tests ---


class TestConsciousDebt:
    def test_debt_marker_in_commit(self):
        """Commit with debt keyword -> finding."""
        files = {"models/cache.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="a1b2c3d4e5f6",
                date=_RECENT,
                message="Add temporary workaround for rate limiting",
                files=["models/cache.py"],
            )
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 1
        assert "workaround" in findings[0].message
        assert findings[0].message.endswith("— acknowledged debt")

    def test_multiple_debt_commits_grouped(self):
        """Multiple debt commits to same file are grouped."""
        files = {"app.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="aaaa1111",
                date=_RECENT,
                message="Add temporary hack for deploy",
                files=["app.py"],
            ),
            CommitInfo(
                hash="bbbb2222",
                date=_RECENT - timedelta(days=10),
                message="Quick fix workaround for auth",
                files=["app.py"],
            ),
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 1
        assert "2 debt commits" in findings[0].message

    def test_no_debt_keywords(self):
        """Normal commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = feat_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 0

    def test_file_not_in_analysis(self):
        """Debt commit touching file not in all_trees -> no finding."""
        files = {"app.py": "x = 1"}
        commits = [
            CommitInfo(
                hash="aaaa1111",
                date=_RECENT,
                message="Add temporary hack",
                files=["other.py"],
            )
        ]
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_conscious_debt(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_conscious_debt(ctx) == []


# --- emergency-hotspots tests ---


class TestEmergencyHotspots:
    def test_high_emergency_ratio(self):
        """File with >= 30% emergency commits and >= 3 -> finding."""
        files = {"services/payment.py": "x = 1", "other/safe.py": "y = 2"}
        # payment.py: 4 emergency + 5 feat = 9 commits, 44% emergency
        # safe.py: 10 feat commits (dilutes project-wide emergency rate)
        # project total: 4 emergency / 19 = 21% < 30% -> check runs
        commits = (
            _emergency_commits("services/payment.py", 4)
            + feat_commits("services/payment.py", 5)
            + feat_commits("other/safe.py", 10)
        )
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 1
        assert "emergency" in findings[0].check
        assert "emergency/hotfix" in findings[0].message

    def test_low_emergency_ratio_no_finding(self):
        """File with < 30% emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2) + feat_commits("app.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_few_emergency_commits(self):
        """File with < 3 emergency commits -> no finding."""
        files = {"app.py": "x = 1"}
        commits = _emergency_commits("app.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_project_wide_high_emergency_suppressed(self):
        """If project emergency rate > 30%, suppress all findings."""
        files = {"app.py": "x = 1"}
        # All commits are emergencies — project-wide rate is 100%
        commits = _emergency_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits)
        findings = check_emergency_hotspots(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_emergency_hotspots(ctx) == []


# --- no-refactoring tests ---


class TestNoRefactoring:
    def test_no_refactoring_detected(self):
        """File with many fixes/features but zero refactoring -> finding."""
        files = {"models/user.py": make_large_file(100)}
        commits = fix_commits("models/user.py", 5) + feat_commits("models/user.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 1
        assert "zero refactoring" in findings[0].message

    def test_has_refactoring_no_finding(self):
        """File with some refactoring -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = (
            fix_commits("app.py", 5)
            + feat_commits("app.py", 3)
            + [
                CommitInfo(
                    hash="refac001",
                    date=_RECENT,
                    message="refactor: simplify logic",
                    files=["app.py"],
                )
            ]
        )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_commits_skipped(self):
        """File with < 8 commits -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = fix_commits("app.py", 4) + feat_commits("app.py", 3)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_few_fix_feature_skipped(self):
        """File with < 6 fix+feature commits -> no finding."""
        files = {"app.py": make_large_file(100)}
        commits = (
            fix_commits("app.py", 2)
            + feat_commits("app.py", 2)
            + [
                CommitInfo(
                    hash=f"misc{i:04d}",
                    date=_RECENT - timedelta(days=i),
                    message=f"chore: cleanup #{i}",
                    files=["app.py"],
                )
                for i in range(6)
            ]
        )
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """File < 50 lines -> no finding."""
        files = {"small.py": make_large_file(30)}
        commits = fix_commits("small.py", 5) + feat_commits("small.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_test_file_skipped(self):
        """Test files are skipped."""
        files = {"test_user.py": make_large_file(100)}
        commits = fix_commits("test_user.py", 5) + feat_commits("test_user.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_low_message_quality_skipped(self):
        """Low message quality -> semantic checks skip."""
        files = {"app.py": make_large_file(100)}
        commits = fix_commits("app.py", 5) + feat_commits("app.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=0.3)
        findings = check_no_refactoring(ctx)
        assert len(findings) == 0

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_no_refactoring(ctx) == []

    @pytest.mark.parametrize("name", ["myapp/urls.py", "myapp/routes.py"])
    def test_urlconf_skipped_whatever_its_name(self, name):
        """A module assigning urlpatterns is a URLconf, named urls.py or not."""
        files = {name: make_large_file(100) + "urlpatterns = []\n"}
        commits = fix_commits(name, 5) + feat_commits(name, 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        assert check_no_refactoring(ctx) == []

    def test_urls_py_without_urlpatterns_still_reported(self):
        """The filename alone does not make a URLconf."""
        files = {"myapp/urls.py": make_large_file(100)}
        commits = fix_commits("myapp/urls.py", 5) + feat_commits("myapp/urls.py", 5)
        ctx = make_ctx(files, commits=commits, message_quality=1.0)
        findings = check_no_refactoring(ctx)
        assert [f.file for f in findings] == ["myapp/urls.py"]


# --- test-erosion tests ---

# A module's tests are the test-role files that import it.
BILLING_TEST = "import pkg.billing\n\ndef test_it(): pass"


class TestTestErosion:
    def test_eroding_tests(self):
        """Source file with 3x+ more commits than test -> finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": BILLING_TEST,
        }
        commits = feat_commits("pkg/billing.py", 9) + feat_commits("tests/test_billing.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "9 source commits" in findings[0].message
        assert "eroding" in findings[0].message

    def test_balanced_commits_no_finding(self):
        """Source and test with similar commit counts -> no finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": BILLING_TEST,
        }
        commits = feat_commits("pkg/billing.py", 6) + feat_commits("tests/test_billing.py", 4)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_no_test_file_no_finding(self):
        """Source file with no corresponding test -> no finding."""
        files = {"pkg/billing.py": make_large_file(100)}
        commits = feat_commits("pkg/billing.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_few_source_commits_skipped(self):
        """Source file with < 5 commits -> no finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": BILLING_TEST,
        }
        commits = feat_commits("pkg/billing.py", 4)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_small_file_skipped(self):
        """Source file < 50 lines -> no finding."""
        files = {
            "pkg/small.py": make_large_file(30),
            "tests/test_small.py": "import pkg.small\n\ndef test_it(): pass",
        }
        commits = feat_commits("pkg/small.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0

    def test_test_file_not_flagged_as_source(self):
        """Test files are not analyzed as source files."""
        files = {
            "test_billing.py": make_large_file(100),
            "billing.py": make_large_file(100),
        }
        commits = feat_commits("test_billing.py", 10)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        # test_billing.py itself should not be flagged
        assert all(f.file != "test_billing.py" for f in findings)

    def test_same_directory_test_file(self):
        """Test file in same directory as source is found."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "pkg/test_billing.py": BILLING_TEST,
        }
        commits = feat_commits("pkg/billing.py", 9) + feat_commits("pkg/test_billing.py", 2)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1

    def test_zero_test_commits(self):
        """Test file exists but has zero commits -> finding."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": BILLING_TEST,
        }
        commits = feat_commits("pkg/billing.py", 6)
        ctx = make_ctx(files, commits=commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 1
        assert "6.0x ratio" in findings[0].message

    def test_no_git_history(self):
        ctx = AnalysisContext({Path("a.py"): ast.parse("x=1")}, verbose=False)
        assert check_test_erosion(ctx) == []


# --- bulk commit filter tests (test-erosion) ---


class TestBulkCommitFilterTestErosion:
    def test_test_erosion_skips_bulk_commits(self):
        """Bulk commits excluded from test-erosion calculation."""
        files = {
            "pkg/billing.py": make_large_file(100),
            "tests/test_billing.py": BILLING_TEST,
        }
        # 6 normal source + 5 bulk source = 11 total source
        # 3 normal test commits
        # Without filter: 11/3 = 3.67x -> finding
        # With filter: 6/3 = 2.0x < 3.0 -> no finding
        normal_source = feat_commits("pkg/billing.py", 6)
        bulk_source = [bulk_commit("pkg/billing.py", i) for i in range(5)]
        test_commits = feat_commits("tests/test_billing.py", 3)
        ctx = make_ctx(files, commits=normal_source + bulk_source + test_commits)
        findings = check_test_erosion(ctx)
        assert len(findings) == 0


# --- reviewed suppression tests (blast-radius) ---


class TestReviewedSuppressionBlastRadius:
    def test_blast_radius_suppressed_after_review(self):
        """Pre-review high-blast commits excluded after review."""
        files = {"services/payment.py": "x = 1"}
        review_date = _RECENT - timedelta(days=5)
        # 7 old high-blast commits + 5 new low-blast commits
        old_commits = [
            CommitInfo(
                hash=f"old{i:04d}",
                date=_RECENT - timedelta(days=20 + i),
                message=f"commit {i}",
                files=["services/payment.py"] + [f"other_{i}_{j}.py" for j in range(10)],
            )
            for i in range(7)
        ]
        new_commits = [
            CommitInfo(
                hash=f"new{i:04d}",
                date=_RECENT - timedelta(days=i),
                message=f"commit new {i}",
                files=["services/payment.py", f"just_one_{i}.py"],
            )
            for i in range(5)
        ]
        ctx = make_ctx(files, commits=old_commits + new_commits)
        # Without review: median co-changes ~10 -> finding
        findings = check_blast_radius(ctx)
        assert len(findings) == 1

        # With review: only 5 new commits with 1 co-change each -> no finding
        ctx.git_history.reviewed_at = {"services/payment.py": review_date}
        findings = check_blast_radius(ctx)
        assert len(findings) == 0


class TestReExportCredit:
    """30-2: `from pkg.sub import X`, re-exported from pkg/sub/impl.py, credits impl."""

    PACKAGE = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "from .impl import thing\n",
        "pkg/sub/impl.py": "def thing(): ...\n",
    }

    def _findings(self, files: dict[str, str]) -> list[str]:
        commits = _make_coupling_commits("api/views.py", "pkg/sub/impl.py", together=8, a_alone=2)
        return [f.message for f in check_change_coupling(make_ctx(files, commits=commits))]

    def test_re_export_explains_the_co_change(self):
        files = {**self.PACKAGE, "api/views.py": "from pkg.sub import thing\n"}
        assert self._findings(files) == []

    def test_re_export_chain_credits_the_end_of_the_chain(self):
        files = {
            **self.PACKAGE,
            "pkg/__init__.py": "from .sub import thing\n",
            "api/views.py": "from pkg import thing\n",
        }
        assert self._findings(files) == []

    def test_guarded_re_export_is_credited(self):
        """30-1 caution 1: the except fallback does not hide the guarded import."""
        files = {
            **self.PACKAGE,
            "pkg/sub/__init__.py": (
                "try:\n    from .impl import thing\nexcept ImportError:\n    thing = None\n"
            ),
            "api/views.py": "from pkg.sub import thing\n",
        }
        assert self._findings(files) == []

    def test_star_import_credits_no_re_export(self):
        files = {**self.PACKAGE, "api/views.py": "from pkg.sub import *\n"}
        [message] = self._findings(files)
        assert "hidden coupling" in message

    def test_name_the_package_defines_itself_credits_only_the_package(self):
        files = {
            **self.PACKAGE,
            "pkg/sub/__init__.py": "def thing(): ...\n",
            "api/views.py": "from pkg.sub import thing\n",
        }
        [message] = self._findings(files)
        assert "hidden coupling" in message


class TestSharedParentCredit:
    """30-3: siblings that subclass one class their package defines have an explained co-change.

    A common import alone is not enough (DECISIONS.md, "Shared-parent credit
    is common base-class inheritance").
    """

    PACKAGE = {
        "handlers/__init__.py": "from .base import Handler\n",
        "handlers/base.py": "class Handler: ...\nclass Other: ...\ndef helper(): ...\n",
        "core.py": "class Plugin: ...\n",
    }

    def _findings(self, audio: str, video: str, *, pair=("handlers/audio.py", "handlers/video.py")):
        files = {**self.PACKAGE, pair[0]: audio, pair[1]: video}
        commits = _make_coupling_commits(*pair, together=8, a_alone=2)
        return [f.message for f in check_change_coupling(make_ctx(files, commits=commits))]

    def test_siblings_subclassing_one_base_are_explained(self):
        code = "from .base import Handler\nclass Audio(Handler): ...\n"
        assert self._findings(code, code.replace("Audio", "Video")) == []

    def test_base_reached_through_a_re_export_or_its_module(self):
        audio = "from handlers import Handler\nclass Audio(Handler): ...\n"
        video = "from . import base\nclass Video(base.Handler): ...\n"
        assert self._findings(audio, video) == []

    def test_base_in_a_subpackage_of_the_siblings_is_explained(self):
        files = {"handlers/common/__init__.py": "", "handlers/common/base.py": "class Root: ...\n"}
        code = "from .common.base import Root\nclass Audio(Root): ...\n"
        commits = _make_coupling_commits(
            "handlers/audio.py", "handlers/video.py", together=8, a_alone=2
        )
        ctx = make_ctx(
            {**self.PACKAGE, **files, "handlers/audio.py": code, "handlers/video.py": code},
            commits=commits,
        )
        assert check_change_coupling(ctx) == []

    def test_a_common_import_without_inheritance_is_not_enough(self):
        code = "from .base import helper\nhelper()\n"
        [message] = self._findings(code, code)
        assert "hidden coupling" in message

    def test_different_bases_from_one_module_are_not_enough(self):
        audio = "from .base import Handler\nclass Audio(Handler): ...\n"
        video = "from .base import Other\nclass Video(Other): ...\n"
        [message] = self._findings(audio, video)
        assert "hidden coupling" in message

    def test_a_base_outside_the_siblings_package_is_not_enough(self):
        code = "from core import Plugin\nclass Audio(Plugin): ...\n"
        [message] = self._findings(code, code)
        assert "hidden coupling" in message

    def test_files_in_different_directories_are_not_siblings(self):
        audio = "from handlers.base import Handler\nclass Audio(Handler): ...\n"
        video = "from handlers.base import Handler\nclass Video(Handler): ...\n"
        pair = ("handlers/audio.py", "plugins/video.py")
        [message] = self._findings(audio, video, pair=pair)
        assert "hidden coupling" in message

    def test_a_library_base_is_not_enough(self):
        code = "import abc\nclass Audio(abc.ABC): ...\n"
        [message] = self._findings(code, code)
        assert "hidden coupling" in message
