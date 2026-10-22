"""The multi-site finding: its invariants, and how the suppression pass narrows it.

``tests/test_ignore_per_site.py`` drives each aggregating check end to end;
this module pins the shared mechanism in ``registry`` and
``cli._apply_suppression`` directly.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from pysmelly.cli import _apply_suppression
from pysmelly.registry import Finding, Severity, Site, SitesError, multi_site_finding

IGNORE = "# pysmelly: ignore[demo]\n"


def _names(sites: list[Site], indices: tuple[int, ...]) -> str:
    return "seen at " + ", ".join(f"{sites[i].file}:{sites[i].line}" for i in indices)


def _at_least(minimum: int, indices: tuple[int, ...]) -> str | None:
    return f"{len(indices)} sites" if len(indices) >= minimum else None


def _finding(sites: list[Site], anchor: Site | None = None) -> Finding:
    return multi_site_finding("demo", Severity.LOW, sites, partial(_names, sites), anchor=anchor)


def _write(base: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (base / name).write_text(content, encoding="utf-8")


class TestInvariants:
    def test_sites_without_render_are_refused(self):
        with pytest.raises(SitesError, match="demo"):
            Finding("a.py", 1, "demo", "m", Severity.LOW, sites=(Site("a.py", 1),))

    def test_render_without_sites_is_refused(self):
        with pytest.raises(SitesError, match="demo"):
            Finding("a.py", 1, "demo", "m", Severity.LOW, render=partial(_names, []))

    def test_a_render_that_refuses_every_site_is_refused(self):
        with pytest.raises(SitesError, match="refuses the full set"):
            multi_site_finding("demo", Severity.LOW, [Site("a.py", 1)], partial(_at_least, 2))

    def test_anchors_on_the_first_site_and_renders_every_site(self):
        finding = _finding([Site("a.py", 3), Site("b.py", 4)])
        assert (finding.file, finding.line) == ("a.py", 3)
        assert finding.message == "seen at a.py:3, b.py:4"

    def test_an_explicit_anchor_wins(self):
        finding = _finding([Site("a.py", 3), Site("b.py", 4)], anchor=Site("def.py", 1))
        assert (finding.file, finding.line) == ("def.py", 1)

    def test_sites_do_not_take_part_in_equality(self):
        """Findings compare as before: by anchor, check, message and severity."""
        plain = Finding("a.py", 3, "demo", "seen at a.py:3, b.py:4", Severity.LOW)
        assert _finding([Site("a.py", 3), Site("b.py", 4)]) == plain


class TestNarrowing:
    FILES = {"a.py": "x = 1\n", "b.py": "x = 1\n", "c.py": "x = 1\n"}

    @pytest.fixture
    def narrow(self, tmp_path, trees):
        def run(files: dict[str, str], finding: Finding) -> list[Finding]:
            _write(tmp_path, files)
            return _apply_suppression([finding], tmp_path, trees.files(files))

        return run

    def test_untouched_when_no_site_is_ignored(self, narrow):
        finding = _finding([Site(name, 1) for name in self.FILES])
        (kept,) = narrow(self.FILES, finding)
        assert kept is finding

    def test_a_narrowed_finding_narrows_again_from_its_own_sites(self, narrow):
        """The re-render maps the narrowed indices back to the original sites."""
        files = {**self.FILES, "a.py": IGNORE + "x = 1\n"}
        (narrowed,) = narrow(files, _finding([Site(n, 1) for n in files]))
        assert narrowed.sites == (Site("b.py", 1), Site("c.py", 1))
        assert narrowed.render is not None
        assert narrowed.render((1,)) == "seen at c.py:1"

    def test_re_anchoring_skips_a_test_role_site(self, narrow):
        """The new anchor obeys the rule ``run_check`` applies to every anchor."""
        files = {
            "a.py": IGNORE + "x = 1\n",
            "test_b.py": "def test_b():\n    pass\n",
            "c.py": "x = 1\n",
        }
        sites = [Site("a.py", 2), Site("test_b.py", 1), Site("c.py", 1)]
        (narrowed,) = narrow(files, _finding(sites))
        assert (narrowed.file, narrowed.line) == ("c.py", 1)
        assert narrowed.message == "seen at test_b.py:1, c.py:1 (1 other site ignored)"

    def test_dropped_when_only_test_role_sites_are_left(self, narrow):
        files = {"a.py": IGNORE + "x = 1\n", "test_b.py": "def test_b():\n    pass\n"}
        sites = [Site("a.py", 2), Site("test_b.py", 1)]
        assert narrow(files, _finding(sites)) == []

    def test_dropped_when_the_render_refuses_the_sites_left(self, narrow):
        """The check's own floor decides whether what is left is still a finding."""
        files = {"a.py": IGNORE + "x = 1\n", "b.py": "x = 1\n"}
        finding = multi_site_finding(
            "demo", Severity.LOW, [Site("a.py", 2), Site("b.py", 1)], partial(_at_least, 2)
        )
        assert narrow(files, finding) == []

    def test_removed_count_is_plural_past_one(self, narrow):
        files = {"a.py": IGNORE + "x = 1\n", "b.py": IGNORE + "x = 1\n", "c.py": "x = 1\n"}
        sites = [Site("a.py", 2), Site("b.py", 2), Site("c.py", 1)]
        (narrowed,) = narrow(files, _finding(sites))
        assert narrowed.message == "seen at c.py:1 (2 other sites ignored)"
