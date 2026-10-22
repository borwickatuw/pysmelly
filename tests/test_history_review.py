"""The `pysmelly: reviewed` marker: which checks honor it, and the persistence note.

The end-to-end tests build a real repository with dated commits so the marker
is parsed from the log exactly as a user's acknowledgment commit would be.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pysmelly.checks.history_bugs import check_fix_propagation
from pysmelly.checks.history_coupling import check_change_coupling, check_conscious_debt
from pysmelly.checks.history_growth import check_hotspot_acceleration
from pysmelly.checks.history_helpers import notes_persisting_pair_review, notes_persisting_review
from pysmelly.cli import GUIDANCE_CONTENT, main
from pysmelly.git_history import CommitInfo
from pysmelly.registry import CHECK_CATEGORIES, CHECKS, GIT_HISTORY_CATEGORY, Finding, Severity

from .history_test_helpers import (
    _RECENT,
    commits_from_slices,
    make_ctx,
    make_time_slices,
)

_NOW = datetime.now(UTC).replace(microsecond=0)
_REVIEW_DATE = _NOW - timedelta(days=40)
_NOTE = f"reviewed {_REVIEW_DATE:%Y-%m-%d}, pattern persists"


def _commit(repo: Path, message: str, when: datetime, allow_empty: bool = False) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    args = ["git", "commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    subprocess.run(args, cwd=repo, env=env, capture_output=True, check=True)


def _touch(repo: Path, names: list[str], line: str) -> None:
    for name in names:
        with (repo / name).open("a", encoding="utf-8") as fh:
            fh.write(line)
    subprocess.run(["git", "add", *names], cwd=repo, capture_output=True, check=True)


@pytest.fixture
def reviewed_repo(git_repo: Path) -> Path:
    """billing.py is reviewed 40 days ago and keeps drawing fixes afterwards.

    ledger.py takes the same commits but carries no marker. Before the review:
    one feature and five fixes; after it: six fixes. bug-magnet fires on both
    files with or without the review window.
    """
    files = ["billing.py", "ledger.py"]
    _touch(git_repo, files, "x = 0\n")
    _commit(git_repo, "feat: add billing and ledger", _NOW - timedelta(days=60))
    for i in range(5):
        _touch(git_repo, files, f"a{i} = {i}\n")
        _commit(git_repo, f"fix: correct rounding case {i}", _NOW - timedelta(days=50 - i))
    _commit(
        git_repo,
        "chore: acknowledge pysmelly finding\n\npysmelly: reviewed billing.py",
        _REVIEW_DATE,
        allow_empty=True,
    )
    for i in range(6):
        _touch(git_repo, files, f"b{i} = {i}\n")
        _commit(git_repo, f"fix: correct currency case {i}", _NOW - timedelta(days=30 - i))
    return git_repo


def _bug_magnet_lines(repo: Path, monkeypatch, capsys, *extra: str) -> dict[str, str]:
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit):
        main(["git-history", "--no-context", "--check", "bug-magnet", *extra, "."])
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines() if "commits" in line and ".py" in line]
    return {line.split(":", 1)[0]: line for line in lines}


class TestReviewedPatternPersists:
    def test_reviewed_file_finding_carries_the_note(self, reviewed_repo, monkeypatch, capsys):
        lines = _bug_magnet_lines(reviewed_repo, monkeypatch, capsys)
        billing = lines["billing.py"]
        # Counted over the post-review commits only, and says so.
        assert "6 of 6 commits" in billing
        assert f"— {_NOTE}. Fix: " in billing

    def test_unreviewed_file_has_no_note(self, reviewed_repo, monkeypatch, capsys):
        lines = _bug_magnet_lines(reviewed_repo, monkeypatch, capsys)
        ledger = lines["ledger.py"]
        assert "11 of 12 commits" in ledger
        assert "pattern persists" not in ledger

    def test_ignore_reviewed_drops_window_and_note(self, reviewed_repo, monkeypatch, capsys):
        lines = _bug_magnet_lines(reviewed_repo, monkeypatch, capsys, "--ignore-reviewed")
        billing = lines["billing.py"]
        assert "11 of 12 commits" in billing
        assert "pattern persists" not in billing


class TestNotesPersistingReview:
    def test_note_appended_only_for_reviewed_file(self):
        @notes_persisting_review
        def fake_check(ctx):
            return [
                Finding(
                    file=str(path),
                    line=1,
                    check="fake",
                    message=f"{path} smells",
                    severity=Severity.LOW,
                )
                for path in ctx.all_trees
            ]

        ctx = make_ctx({"a.py": "x = 1", "b.py": "y = 2"})
        ctx.git_history.reviewed_at = {"a.py": datetime(2026, 9, 3, 12, 0, tzinfo=UTC)}
        messages = {f.file: f.message for f in fake_check(ctx)}
        assert messages == {
            "a.py": "a.py smells — reviewed 2026-09-03, pattern persists",
            "b.py": "b.py smells",
        }

    def test_slice_based_check_gets_the_note(self):
        """hotspot-acceleration filters slices, not commits, and still notes the review."""
        pattern = ["inactive"] * 4 + ["active", "inactive"] + ["active"] * 6
        slices = make_time_slices("hot.py", pattern)
        for ts in slices[6:]:
            extra = CommitInfo(
                hash=f"x{ts.start:%j}",
                date=ts.start + timedelta(days=2),
                message="chore: more",
                files=["hot.py"],
            )
            ts.commits.append(extra)
        ctx = make_ctx(
            {"hot.py": "x = 1"},
            commits=commits_from_slices(slices),
            time_slices=slices,
            commits_per_slice=2.0,
        )
        ctx.git_history.reviewed_at = {"hot.py": slices[0].start}
        findings = check_hotspot_acceleration(ctx)
        assert len(findings) == 1
        assert findings[0].message.endswith(
            f"reviewed {slices[0].start:%Y-%m-%d}, pattern persists"
        )


_EARLY = _RECENT - timedelta(days=15)  # evidence before the markers below
_LATE = _RECENT + timedelta(days=10)  # evidence after them


def _pair_commits(when: datetime, count: int, message: str) -> list[CommitInfo]:
    """``count`` commits on both alpha.py and beta.py, a day apart from ``when``."""
    return [
        CommitInfo(
            hash=f"{when:%j}{i:04d}pair",
            date=when + timedelta(days=i),
            message=f"{message} {i}",
            files=["alpha.py", "beta.py"],
        )
        for i in range(count)
    ]


def _solo_commits(when: datetime, count: int, message: str) -> list[CommitInfo]:
    """``count`` commits on each of alpha.py and beta.py alone, a day apart from ``when``."""
    return [
        CommitInfo(
            hash=f"{when:%j}{i:04d}{Path(name).stem}",
            date=when + timedelta(days=i),
            message=f"{message} {i}",
            files=[name],
        )
        for name in ("alpha.py", "beta.py")
        for i in range(count)
    ]


_PAIR_FILES = {"alpha.py": "x = 1", "beta.py": "y = 2"}


class TestConsciousDebtHonoursTheMarker:
    """Only debt commits after the file's marker count, as for bug-magnet."""

    def _debt_ctx(self, review: datetime | None):
        debt = CommitInfo(
            hash="debt0001",
            date=_RECENT,
            message="hack: workaround for vendor bug",
            files=["vendor.py"],
        )
        ctx = make_ctx({"vendor.py": "x = 1"}, commits=[debt], message_quality=1.0)
        if review is not None:
            ctx.git_history.reviewed_at = {"vendor.py": review}
        return ctx

    def test_marker_after_the_debt_quiets_it(self):
        assert check_conscious_debt(self._debt_ctx(_RECENT + timedelta(days=1))) == []

    def test_marker_before_the_debt_keeps_it_with_the_note(self):
        review = _RECENT - timedelta(days=1)
        findings = check_conscious_debt(self._debt_ctx(review))
        assert len(findings) == 1
        assert findings[0].message.endswith(f"reviewed {review:%Y-%m-%d}, pattern persists")

    def test_no_marker_unchanged(self):
        findings = check_conscious_debt(self._debt_ctx(None))
        assert len(findings) == 1
        assert "pattern persists" not in findings[0].message


class TestPairChecksHonourTheMarker:
    """change-coupling and fix-propagation judge a pair of files.

    A pair is narrowed to the commits after the later of its two markers only
    when both files carry one; a marker on one file leaves the pair as it was.
    """

    @pytest.fixture(
        params=[
            (check_change_coupling, "feat: change", {}),
            (check_fix_propagation, "fix: resolve issue", {"message_quality": 1.0}),
        ],
        ids=["change-coupling", "fix-propagation"],
    )
    def pair_check(self, request):
        return request.param

    def _run(self, pair_check, commits, reviewed_at):
        check_fn, _message, extra = pair_check
        ctx = make_ctx(_PAIR_FILES, commits=commits, **extra)
        ctx.git_history.reviewed_at = reviewed_at
        return check_fn(ctx)

    def test_both_reviewed_after_the_evidence_quiets_the_pair(self, pair_check):
        commits = _pair_commits(_EARLY, 6, pair_check[1])
        reviewed = {"alpha.py": _RECENT, "beta.py": _RECENT}
        assert self._run(pair_check, commits, reviewed) == []

    def test_one_file_reviewed_leaves_the_pair_as_it_was(self, pair_check):
        commits = _pair_commits(_EARLY, 6, pair_check[1])
        findings = self._run(pair_check, commits, {"alpha.py": _RECENT})
        assert len(findings) == 1
        assert "pattern persists" not in findings[0].message

    def test_the_later_marker_sets_the_cutoff(self, pair_check):
        """alpha.py's marker predates the evidence, beta.py's follows it."""
        commits = _pair_commits(_EARLY, 6, pair_check[1])
        reviewed = {"alpha.py": _EARLY - timedelta(days=1), "beta.py": _RECENT}
        assert self._run(pair_check, commits, reviewed) == []

    def test_post_review_evidence_fires_with_the_later_date(self, pair_check):
        commits = _pair_commits(_EARLY, 6, pair_check[1]) + _pair_commits(_LATE, 6, pair_check[1])
        later = _RECENT + timedelta(days=1)
        findings = self._run(pair_check, commits, {"alpha.py": _RECENT, "beta.py": later})
        assert len(findings) == 1
        finding = findings[0]
        assert "6/6 commits" in finding.message or "6 of 6 fix commits" in finding.message
        assert finding.message.endswith(f"reviewed {later:%Y-%m-%d}, pattern persists")
        assert {site.file for site in finding.sites} == {"alpha.py", "beta.py"}

    def test_per_file_counts_narrow_with_the_pair(self, pair_check):
        """Solo commits before the cutoff dilute the ratio only while they count."""
        commits = _solo_commits(_EARLY - timedelta(days=12), 12, pair_check[1]) + _pair_commits(
            _LATE, 6, pair_check[1]
        )
        assert len(self._run(pair_check, commits, {})) == 0
        reviewed = {"alpha.py": _RECENT, "beta.py": _RECENT}
        assert len(self._run(pair_check, commits, reviewed)) == 1

    def test_note_survives_rerender(self, pair_check):
        commits = _pair_commits(_LATE, 6, pair_check[1])
        reviewed = {"alpha.py": _RECENT, "beta.py": _RECENT}
        (finding,) = self._run(pair_check, commits, reviewed)
        assert finding.render is not None
        assert finding.render((0, 1)) == finding.message
        assert finding.render((0,)) is None


def _passes_review_note(name: str) -> bool:
    """Whether the registered check ``name`` is wrapped by notes_persisting_review
    or, for a check that judges pairs of files, notes_persisting_pair_review."""
    markers = {
        notes_persisting_review(lambda _ctx: []).__code__,
        notes_persisting_pair_review(lambda _ctx: []).__code__,
    }
    return CHECKS[name].__code__ in markers


def _scope_text(text: str, start: str, end: str) -> str:
    """The span of ``text`` from the first ``start`` to the next ``end``."""
    head = text.index(start)
    return text[head : text.index(end, head)]


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Each place that states which git-history checks ignore the marker, as the
# span that statement occupies: (source text, first words, words after it).
_SCOPE_STATEMENTS = {
    "README.md": (
        (_REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        "The `reviewed` command acknowledges findings",
        "\n",
    ),
    "init guidance": (GUIDANCE_CONTENT, "This creates an empty git commit", "\n\n"),
    "docs/ARCHITECTURE.md": (
        (_REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8"),
        "Two commands support the loop",
        "`pysmelly init` writes",
    ),
}


class TestScopeStatementsMatchTheRegistry:
    """Every statement of the marker's scope names exactly the checks it skips.

    The registry decides: a git-history check either carries
    notes_persisting_review (it narrows to post-review commits) or ignores the
    marker, and every check of the second kind must be named where the docs
    list the exceptions. Phase 31-6 found three later git-history checks
    missing from those lists.
    """

    @pytest.mark.parametrize("where", sorted(_SCOPE_STATEMENTS))
    def test_statement_names_every_check_that_skips_the_marker(self, where):
        text, start, end = _SCOPE_STATEMENTS[where]
        named = set(re.findall(r"`([a-z-]+)`", _scope_text(text, start, end)))
        history_checks = {
            name for name, category in CHECK_CATEGORIES.items() if category == GIT_HISTORY_CATEGORY
        }
        skipping = {name for name in history_checks if not _passes_review_note(name)}
        assert skipping - named == set()
        assert named & (history_checks - skipping) == set()

    def test_both_kinds_exist(self):
        history_checks = [
            name for name, category in CHECK_CATEGORIES.items() if category == GIT_HISTORY_CATEGORY
        ]
        assert any(_passes_review_note(name) for name in history_checks)
        assert not all(_passes_review_note(name) for name in history_checks)
