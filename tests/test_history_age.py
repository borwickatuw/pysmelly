"""Blame-age checks: frozen-feature-flag and fossil-temporary, on real dated repositories."""

from __future__ import annotations

import ast
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.checks.history_age import (
    _temporary_comments,
    check_fossil_temporary,
    check_frozen_feature_flag,
)
from pysmelly.checks.patterns_misc import check_fossilized_toggles
from pysmelly.cli import main
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.git_history import GitHistoryQueryError
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, Severity

_OLD = 400
_YOUNG = 30


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _commit(repo: Path, files: dict[str, str], days_ago: int, message: str = "work") -> None:
    """Write ``files``, stage them by path, and commit them dated ``days_ago``."""
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "--", name], cwd=repo, capture_output=True, check=True)
    stamp = _when(days_ago).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, capture_output=True, check=True
    )


def _ctx(repo: Path) -> AnalysisContext:
    """The working tree's Python files, analyzed with the repository's history."""
    trees = {
        path.relative_to(repo): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(repo.glob("*.py"))
    }
    return AnalysisContext(trees, verbose=False, git=GitSettings(root=repo))


_TOGGLE = """\
USE_V2 = False


def handle(request):
    if USE_V2:
        return "v2"
    else:
        return "v1"
"""

_ONE_SIDED = """\
USE_V2 = False


def handle(request):
    if USE_V2:
        return "v2"
    return "v1"
"""

_TERNARY = """\
USE_V2 = False


def handle(request):
    return "v2" if USE_V2 else "v1"
"""


class TestFrozenFeatureFlag:
    def test_old_toggle_with_both_branches_is_raised(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE}, _OLD)
        findings = check_frozen_feature_flag(_ctx(git_repo))
        assert len(findings) == 1
        finding = findings[0]
        assert (finding.file, finding.line, finding.severity) == ("flags.py", 1, Severity.MEDIUM)
        assert finding.message.startswith("USE_V2 = False is never reassigned")
        assert f"frozen for {_OLD} days" in finding.message
        assert finding.message.endswith(
            f"unchanged since {_when(_OLD):%Y-%m-%d}) with both branches still present"
        )

    def test_ternary_has_both_branches(self, git_repo):
        _commit(git_repo, {"flags.py": _TERNARY}, _OLD)
        assert len(check_frozen_feature_flag(_ctx(git_repo))) == 1

    def test_young_toggle_is_not_raised(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE}, _YOUNG)
        assert check_frozen_feature_flag(_ctx(git_repo)) == []

    def test_value_flipped_recently_is_not_frozen(self, git_repo):
        """Blame dates the definition line, so flipping the value resets the age."""
        _commit(git_repo, {"flags.py": _TOGGLE.replace("False", "True", 1)}, _OLD)
        _commit(git_repo, {"flags.py": _TOGGLE}, _YOUNG)
        assert check_frozen_feature_flag(_ctx(git_repo)) == []

    def test_other_lines_changing_does_not_thaw_it(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE}, _OLD)
        _commit(git_repo, {"flags.py": _TOGGLE.replace('"v1"', '"legacy"')}, _YOUNG)
        assert len(check_frozen_feature_flag(_ctx(git_repo))) == 1

    def test_one_sided_toggle_stays_with_fossilized_toggles(self, git_repo):
        _commit(git_repo, {"flags.py": _ONE_SIDED}, _OLD)
        ctx = _ctx(git_repo)
        assert check_frozen_feature_flag(ctx) == []
        assert len(check_fossilized_toggles(ctx)) == 1

    def test_uncommitted_value_change_is_not_frozen(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE}, _OLD)
        (git_repo / "flags.py").write_text(_TOGGLE.replace("False", "True", 1), encoding="utf-8")
        assert check_frozen_feature_flag(_ctx(git_repo)) == []

    def test_uncommitted_lines_above_do_not_misplace_the_blame(self, git_repo):
        """The definition is found at its HEAD line, not its working-tree line."""
        _commit(git_repo, {"flags.py": _TOGGLE}, _OLD)
        (git_repo / "flags.py").write_text('"""Flags."""\n\n' + _TOGGLE, encoding="utf-8")
        findings = check_frozen_feature_flag(_ctx(git_repo))
        assert [(f.line, f"frozen for {_OLD} days" in f.message) for f in findings] == [(3, True)]

    def test_uncommitted_file_is_not_frozen(self, git_repo):
        _commit(git_repo, {"other.py": "x = 1\n"}, _OLD)
        (git_repo / "flags.py").write_text(_TOGGLE, encoding="utf-8")
        assert check_frozen_feature_flag(_ctx(git_repo)) == []

    def test_no_history_no_findings(self):
        ctx = AnalysisContext({Path("flags.py"): ast.parse(_TOGGLE)}, verbose=False)
        assert check_frozen_feature_flag(ctx) == []

    def test_head_that_does_not_parse_fails_loudly(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE + "if (\n"}, _OLD)
        (git_repo / "flags.py").write_text(_TOGGLE, encoding="utf-8")
        with pytest.raises(GitHistoryQueryError, match="flags.py at HEAD does not parse"):
            check_frozen_feature_flag(_ctx(git_repo))

    def test_same_level_as_fossilized_toggles_until_the_fleet_earns_high(self):
        assert CHECK_SEVERITY["fossilized-toggles"] is Severity.MEDIUM
        assert CHECK_SEVERITY["frozen-feature-flag"] is Severity.MEDIUM
        assert CHECK_CATEGORIES["frozen-feature-flag"] == "git-history"

    def test_fossilized_toggles_message_is_unchanged(self, git_repo):
        _commit(git_repo, {"flags.py": _TOGGLE}, _OLD)
        [finding] = check_fossilized_toggles(_ctx(git_repo))
        assert finding.message == (
            "USE_V2 = False is never reassigned — `if` at line 5 is always False (dead branch)"
        )
        assert finding.severity is Severity.MEDIUM


@pytest.mark.parametrize(
    ("comment", "marker"),
    [
        ("# TODO: read the limit from settings", "TODO"),
        ("# FIXME off by one at the boundary", "FIXME"),
        ("# XXX: assumes UTC", "XXX"),
        ("# hack around the vendor's broken header", "hack"),
        ("# temporary shim until the API is fixed", "temporary shim"),
        ("# keep the old path for now", "for now"),
        ("# remove after the March migration", "remove after"),
        ("# drop this once the v2 client lands", "once the v2 client lands"),
        ("# Legacy function for backward compatibility", "Legacy function"),
    ],
)
def test_every_marker_fires_when_old(git_repo, comment, marker):
    _commit(git_repo, {"mod.py": f"x = 1\n{comment}\ny = 2\n"}, _OLD)
    findings = check_fossil_temporary(_ctx(git_repo))
    assert [(f.file, f.line, f.severity) for f in findings] == [("mod.py", 2, Severity.LOW)]
    assert f"marked {marker!r}, unchanged for {_OLD} days" in findings[0].message
    assert comment.lstrip("# ") in findings[0].message


def _marker(comment: str) -> str | None:
    """The marker _temporary_comments reads in a one-line source, or None."""
    found = _temporary_comments("mod.py", f"x = 1  {comment}\n")
    return found[1][1] if found else None


class TestMarkerCalibration:
    """32-7: tags only in a tag's place; legacy/temporary only naming code; no compat.

    Each negative is a fleet comment the pre-32-7 vocabulary flagged
    (DECISIONS.md, "32-7: fossil-temporary marker calibration").
    """

    @pytest.mark.parametrize(
        ("comment", "marker"),
        [
            ("# TODO abstract this out", "TODO"),
            ("# TODO(acl): per-bucket ACL", "TODO"),
            ("# FIXME metadata endpoint", "FIXME"),
            ("# see the parser. XXX: assumes UTC", "XXX"),
            ("# the reader, FIXME(bob) later", "FIXME"),
            ("# Legacy dict support - inline the logic", "Legacy dict support"),
            ("# Legacy function for backward compatibility", "Legacy function"),
            ("# legacy shim for the v1 client", "legacy shim"),
            ("# legacy code path, delete with v3", "legacy code path"),
            ("# temporary fix until the vendor patches it", "temporary fix"),
            ("# a temporary workaround for the broken header", "temporary workaround"),
        ],
    )
    def test_kept_phrasings(self, comment, marker):
        assert _marker(comment) == marker

    @pytest.mark.parametrize(
        "comment",
        [
            # From a real project, verbatim: a caller's behaviour
            "# callers handle the None case explicitly (fallback to legacy name, create",
            # From a real project's tests: FIXME as a word
            "# .env.example ships with FIXME placeholders; the script should",
            "# Legacy: no active sync, return items without a sync",
            "# Build secrets (modules + legacy)",
            "# Support component-based config (production) or DATABASE_URL (legacy/testing)",
            "# Legacy Office documents are OLE compound files.",
            "# initialises tesseract in legacy mode, which the fast set cannot serve.",
            "# legacy callers pass a dict",
            "# Save current profile and temporarily switch to infra profile",
            "# Temporary slug, will be regenerated",
            "# Not a requires-python compat shim: `src/s3_bagit/_version.py` is",
            "# py2 compat",
        ],
    )
    def test_dropped_phrasings(self, comment):
        assert _marker(comment) is None


_NOT_MARKERS = '''\
"""Module docstring: the legacy format is kept for now. TODO in prose."""


def parse(text):
    """Changelog-style docstring: temporary shim, remove after v3, HACK."""
    note = "# TODO inside a string is not a comment"
    # Mongolian todo soft hyphen, the script not the tag
    # a xxx-xx-xxxx placeholder
    # pysmelly: ignore[compat-shims] — the shim is the public API
    # compatible with the 2019 schema
    return note
'''


class TestFossilTemporary:
    def test_young_comment_is_left_alone(self, git_repo):
        _commit(git_repo, {"mod.py": "# TODO: tidy\nx = 1\n"}, _YOUNG)
        assert check_fossil_temporary(_ctx(git_repo)) == []

    def test_docstrings_strings_and_non_markers_do_not_count(self, git_repo):
        _commit(git_repo, {"mod.py": _NOT_MARKERS}, _OLD)
        assert check_fossil_temporary(_ctx(git_repo)) == []

    def test_age_is_the_comment_line_not_the_file(self, git_repo):
        _commit(git_repo, {"mod.py": "x = 1\ny = 2\n"}, _OLD)
        _commit(git_repo, {"mod.py": "x = 1\n# TODO: tidy\ny = 2\n"}, _YOUNG)
        assert check_fossil_temporary(_ctx(git_repo)) == []

    def test_reworded_comment_is_young_again(self, git_repo):
        _commit(git_repo, {"mod.py": "# TODO: tidy\nx = 1\n"}, _OLD)
        _commit(git_repo, {"mod.py": "# TODO: tidy the loop\nx = 1\n"}, _YOUNG)
        assert check_fossil_temporary(_ctx(git_repo)) == []

    def test_lines_are_read_at_head(self, git_repo):
        """An uncommitted comment is not old; a committed one keeps its HEAD line."""
        _commit(git_repo, {"mod.py": "x = 1\n# FIXME: overflow\n"}, _OLD)
        (git_repo / "mod.py").write_text("# TODO: new\nx = 1\n# FIXME: overflow\n", "utf-8")
        findings = check_fossil_temporary(_ctx(git_repo))
        assert [(f.line, "FIXME" in f.message) for f in findings] == [(2, True)]

    def test_one_finding_per_marked_line_in_order(self, git_repo):
        source = "# TODO: a\nx = 1  # for now\n\n# plain comment\ny = 2  # legacy shim\n"
        _commit(git_repo, {"b.py": source, "a.py": "# HACK\n"}, _OLD)
        findings = check_fossil_temporary(_ctx(git_repo))
        assert [(f.file, f.line) for f in findings] == [
            ("a.py", 1),
            ("b.py", 1),
            ("b.py", 2),
            ("b.py", 5),
        ]

    def test_long_comment_is_shortened(self, git_repo):
        _commit(git_repo, {"mod.py": "# TODO " + "word " * 40 + "\n"}, _OLD)
        [finding] = check_fossil_temporary(_ctx(git_repo))
        assert '…" — marked' in finding.message

    def test_no_history_no_findings(self):
        ctx = AnalysisContext({Path("mod.py"): ast.parse("# TODO\n")}, verbose=False)
        assert check_fossil_temporary(ctx) == []

    def test_uncommitted_file_is_skipped(self, git_repo):
        _commit(git_repo, {"other.py": "x = 1\n"}, _OLD)
        (git_repo / "mod.py").write_text("# TODO: tidy\n", encoding="utf-8")
        assert check_fossil_temporary(_ctx(git_repo)) == []

    def test_head_that_does_not_tokenize_fails_loudly(self, git_repo):
        _commit(git_repo, {"mod.py": '# TODO\nx = """unterminated\n'}, _OLD)
        (git_repo / "mod.py").write_text("# TODO\nx = 1\n", encoding="utf-8")
        with pytest.raises(GitHistoryQueryError, match="mod.py at HEAD does not tokenize"):
            check_fossil_temporary(_ctx(git_repo))


class TestFromTheCli:
    def test_git_history_run_reports_both(self, git_repo, monkeypatch, capsys):
        _commit(git_repo, {"flags.py": "# TODO: drop the flag\n" + _TOGGLE}, _OLD)
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--check", "frozen-feature-flag", "."])
        assert "frozen for" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            main(["git-history", "--no-context", "--check", "fossil-temporary", "."])
        assert "marked 'TODO'" in capsys.readouterr().out

    def test_ast_run_does_not_include_them(self, git_repo, monkeypatch, capsys):
        _commit(git_repo, {"flags.py": "# TODO: drop the flag\n" + _TOGGLE}, _OLD)
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["--no-context", "--more-please", "."])
        out = capsys.readouterr().out
        assert "fossilized-toggles" in out
        assert "frozen-feature-flag" not in out
        assert "fossil-temporary" not in out
