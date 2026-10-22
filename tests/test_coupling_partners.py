"""blast-radius partners, the expected-coupling hint, and per-site ignores.

blast-radius names the files a flagged file drags along; change-coupling and
blast-radius print the ``expected-coupling`` line that declares their pair;
both report a package-level cluster with its files as sites, so an ignore on
one file removes just that file.
"""

from __future__ import annotations

import re
import tomllib
from datetime import timedelta
from pathlib import Path

from pysmelly.checks.history_coupling import check_blast_radius, check_change_coupling
from pysmelly.checks.history_helpers import is_expected_coupling
from pysmelly.cli import _apply_suppression
from pysmelly.config import _validate_config
from pysmelly.context import AnalysisContext
from pysmelly.git_history import CommitInfo
from pysmelly.registry import Finding, Site

from .history_test_helpers import _RECENT, make_ctx

README = Path(__file__).resolve().parent.parent / "README.md"

_HINT = re.compile(r"`(expected-coupling = .*?)`")


def _commit(index: int, files: list[str]) -> CommitInfo:
    return CommitInfo(
        hash=f"c{index:04d}",
        date=_RECENT - timedelta(days=index),
        message=f"commit {index}",
        files=files,
    )


def _hint(message: str) -> list[list[str]]:
    """The expected-coupling pairs the message's config line declares, read
    the way pysmelly reads its own config."""
    match = _HINT.search(message)
    assert match, message
    config = _validate_config(tomllib.loads(match.group(1)), "hint", set())
    return config["expected-coupling"]


# A file dragging nine others along in each of seven commits: the same three
# partners every time, plus six files that differ per commit.
_BLAST_TARGET = "app/core.py"
_STEADY = ["app/models.py", "app/views.py", "app/forms.py"]


def _blast_commits() -> list[CommitInfo]:
    commits = []
    for i in range(7):
        partners = list(_STEADY)
        if i < 4:
            partners.append("app/admin.py")
        partners += [f"other/m{i}_{j}.py" for j in range(9 - len(partners))]
        commits.append(_commit(i, [_BLAST_TARGET, *partners]))
    return commits


def _blast_ctx() -> AnalysisContext:
    return make_ctx({_BLAST_TARGET: "x = 1"}, commits=_blast_commits())


class TestBlastRadiusPartners:
    def test_names_the_most_frequent_partners_with_shared_commits(self):
        [finding] = check_blast_radius(_blast_ctx())
        assert (
            "most frequent co-change partners (shared commits): "
            "app/forms.py (7), app/models.py (7), app/views.py (7), app/admin.py (4), "
            "other/m0_0.py (1)"
        ) in finding.message

    def test_lists_at_most_five_partners(self):
        [finding] = check_blast_radius(_blast_ctx())
        listed = finding.message.split("(shared commits): ")[1].split("; ")[0]
        assert len(listed.split(", ")) == 5

    def test_hint_declares_the_first_partner(self):
        [finding] = check_blast_radius(_blast_ctx())
        assert _hint(finding.message) == [[_BLAST_TARGET, "app/forms.py"]]

    def test_a_declared_partner_leaves_the_list(self):
        ctx = _blast_ctx()
        ctx.expected_coupling = _hint(check_blast_radius(ctx)[0].message)
        [finding] = check_blast_radius(ctx)
        assert "app/forms.py" not in finding.message
        assert "app/models.py (7), app/views.py (7), app/admin.py (4)" in finding.message

    def test_declared_partners_stop_counting_toward_the_median(self):
        """Declaring every steady partner drops the median from 9 to 6, under
        the floor of 8, so the file is no longer flagged."""
        ctx = _blast_ctx()
        assert "median of 9" in check_blast_radius(ctx)[0].message
        ctx.expected_coupling = [[_BLAST_TARGET, partner] for partner in _STEADY]
        assert check_blast_radius(ctx) == []


def _cluster_ctx() -> AnalysisContext:
    """Three files in pkg/, each changing with nine others in five commits."""
    members = ["pkg/a.py", "pkg/b.py", "pkg/c.py"]
    commits = []
    for m, member in enumerate(members):
        for i in range(5):
            dragged = ["shared/hub.py"] + [f"x/{member[4]}{i}_{j}.py" for j in range(8)]
            commits.append(_commit(m * 10 + i, [member, *dragged]))
    return make_ctx(dict.fromkeys(members, "x = 1"), commits=commits)


class TestBlastRadiusCluster:
    def test_cluster_names_partners_besides_its_members(self):
        [finding] = check_blast_radius(_cluster_ctx())
        assert finding.file == "pkg"
        assert "3 files in this package flagged (a.py, b.py, c.py)" in finding.message
        assert "partners besides these (shared commits): shared/hub.py (15)" in finding.message

    def test_cluster_sites_are_its_files(self):
        [finding] = check_blast_radius(_cluster_ctx())
        assert finding.sites == (Site("pkg/a.py", 1), Site("pkg/b.py", 1), Site("pkg/c.py", 1))


def _pair_ctx(file_a: str, file_b: str) -> AnalysisContext:
    commits = [_commit(i, [file_a, file_b]) for i in range(8)]
    commits += [_commit(10 + i, [file_a]) for i in range(2)]
    commits += [_commit(20 + i, [file_b]) for i in range(2)]
    return make_ctx({file_a: "x = 1", file_b: "y = 2"}, commits=commits)


class TestChangeCouplingHint:
    def test_hint_parses_as_config_and_declares_the_pair(self):
        ctx = _pair_ctx("api/views.py", "billing/invoice.py")
        [finding] = check_change_coupling(ctx)
        pairs = _hint(finding.message)
        assert pairs == [["api/views.py", "billing/invoice.py"]]
        ctx.expected_coupling = pairs
        assert check_change_coupling(ctx) == []

    def test_hint_escapes_wildcards_in_a_path(self):
        """A path holding fnmatch syntax is declared as itself, not as a
        pattern that also matches its neighbours."""
        ctx = _pair_ctx("a/[x]*.py", 'b/q"?.py')
        [finding] = check_change_coupling(ctx)
        pairs = _hint(finding.message)
        assert is_expected_coupling("a/[x]*.py", 'b/q"?.py', pairs)
        assert not is_expected_coupling("a/x_other.py", "b/qz.py", pairs)
        ctx.expected_coupling = pairs
        assert check_change_coupling(ctx) == []

    def test_hint_escapes_what_toml_forbids_raw(self):
        """Backslashes and control characters (DEL included) in a path come
        out as TOML escapes, and the line still declares that pair."""
        file_a, file_b = "a/t\tb\\c.py", "d/\x7f\x01.py"
        ctx = _pair_ctx(file_a, file_b)
        [finding] = check_change_coupling(ctx)
        assert _hint(finding.message) == [[file_a, file_b]]

    def test_cluster_hint_declares_every_pair(self):
        files = ["mod/a.py", "mod/b.py", "mod/c.py"]
        commits = [_commit(i, files) for i in range(6)]
        ctx = make_ctx(dict.fromkeys(files, "x = 1"), commits=commits)
        [finding] = check_change_coupling(ctx)
        assert finding.file == "mod"
        pairs = _hint(finding.message)
        assert sorted(map(tuple, pairs)) == [
            ("mod/a.py", "mod/b.py"),
            ("mod/a.py", "mod/c.py"),
            ("mod/b.py", "mod/c.py"),
        ]
        ctx.expected_coupling = pairs
        assert check_change_coupling(ctx) == []

    def test_readme_example_is_the_real_message(self):
        """The README's worked example is what the check prints for it."""
        ctx = _pair_ctx("api/views.py", "billing/invoice.py")
        [finding] = check_change_coupling(ctx)
        assert f"api/views.py:1: {finding.text}\n" in README.read_text(encoding="utf-8")


def _ignored(tmp_path: Path, ignore_in: set[str], files: list[str]) -> None:
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        first = "# pysmelly: ignore[change-coupling,blast-radius]\n" if name in ignore_in else ""
        path.write_text(f"{first}x = 1\n", encoding="utf-8")


def _suppressed(findings: list[Finding], tmp_path: Path, ctx: AnalysisContext) -> list[Finding]:
    return _apply_suppression(findings, tmp_path, ctx)


class TestPerSiteIgnores:
    def test_ignore_on_the_second_file_of_a_pair_drops_it(self, tmp_path):
        files = ["api/views.py", "billing/invoice.py"]
        _ignored(tmp_path, {"billing/invoice.py"}, files)
        ctx = _pair_ctx(*files)
        assert _suppressed(check_change_coupling(ctx), tmp_path, ctx) == []

    def test_ignore_on_one_cluster_file_keeps_the_other_pairs(self, tmp_path):
        files = ["mod/a.py", "mod/b.py", "mod/c.py", "mod/d.py"]
        _ignored(tmp_path, {"mod/d.py"}, files)
        commits = [_commit(i, files) for i in range(6)]
        ctx = make_ctx(dict.fromkeys(files, "x = 1"), commits=commits)
        [finding] = _suppressed(check_change_coupling(ctx), tmp_path, ctx)
        assert "3 files with 3 coupling pairs (a.py, b.py, c.py)" in finding.message
        assert "(1 other site ignored)" in finding.message
        assert "mod/d.py" not in finding.message

    def test_ignore_on_one_blast_cluster_file_removes_just_it(self, tmp_path):
        _ignored(tmp_path, {"pkg/b.py"}, ["pkg/a.py", "pkg/b.py", "pkg/c.py"])
        ctx = _cluster_ctx()
        [finding] = _suppressed(check_blast_radius(ctx), tmp_path, ctx)
        assert "2 files in this package flagged (a.py, c.py)" in finding.message
        assert "shared/hub.py (10)" in finding.message


class TestCountsUnchanged:
    """Pins of behaviour 31-1 keeps: only the messages changed, so which
    findings appear, and how clusters collapse, is as before."""

    def test_two_same_directory_pairs_stay_separate(self):
        commits = [_commit(i, ["mod/a.py", "mod/b.py"]) for i in range(6)]
        commits += [_commit(10 + i, ["mod/c.py", "mod/d.py"]) for i in range(6)]
        files = ["mod/a.py", "mod/b.py", "mod/c.py", "mod/d.py"]
        ctx = make_ctx(dict.fromkeys(files, "x = 1"), commits=commits)
        assert sorted(f.file for f in check_change_coupling(ctx)) == ["mod/a.py", "mod/c.py"]

    def test_three_same_directory_pairs_collapse_to_one(self):
        files = ["mod/a.py", "mod/b.py", "mod/c.py"]
        ctx = make_ctx(dict.fromkeys(files, "x = 1"), commits=[_commit(i, files) for i in range(6)])
        assert [f.file for f in check_change_coupling(ctx)] == ["mod"]

    def test_cross_directory_pairs_never_collapse(self):
        files = ["a/x.py", "b/y.py", "c/z.py"]
        ctx = make_ctx(dict.fromkeys(files, "x = 1"), commits=[_commit(i, files) for i in range(6)])
        assert len(check_change_coupling(ctx)) == 3

    def test_blast_cluster_collapses_at_three_files(self):
        assert [f.file for f in check_blast_radius(_cluster_ctx())] == ["pkg"]

    def test_same_directory_pair_is_reported(self):
        """No same-directory auto-suppression (DECISIONS.md, Phase 31-1)."""
        ctx = _pair_ctx("mod/a.py", "mod/b.py")
        [finding] = check_change_coupling(ctx)
        assert "hidden coupling" in finding.message
