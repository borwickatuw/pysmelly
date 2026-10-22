"""Every check carries a fix sentence and a dismissal sentence (Phase 29-1).

The fix is the edit a finding asks for; the dismissal is the one narrow
condition under which a finding is not worth fixing. Both close every
printed message, and ``--list-checks`` shows the dismissal.
"""

import pytest

from pysmelly.cli import main
from pysmelly.output import format_text
from pysmelly.registry import (
    CHECK_DISMISSALS,
    CHECK_FIXES,
    CHECKS,
    DISMISSAL_OPENING,
    Advice,
    CheckRegistrationError,
    Finding,
    Severity,
    check,
)

# Phrasings that let a session wave any finding away: a dismissal names a
# case, never the author's intent or a judgment call.
GENERIC_DISMISSALS = (
    "if intended",
    "if intentional",
    "if deliberate",
    "if appropriate",
    "if needed",
    "if necessary",
    "if desired",
    "if it makes sense",
    "if acceptable",
)


def _fixture(_ctx):
    return []


class TestEveryCheck:
    def test_every_registered_check_has_a_fix_sentence(self):
        missing = sorted(set(CHECKS) - set(CHECK_FIXES))
        assert not missing, f"checks without a fix sentence: {missing}"

    def test_every_registered_check_has_a_dismissal_sentence(self):
        missing = sorted(set(CHECKS) - set(CHECK_DISMISSALS))
        assert not missing, f"checks without a dismissal sentence: {missing}"

    @pytest.mark.parametrize("name", sorted(CHECKS))
    def test_the_dismissal_names_one_case(self, name):
        dismissal = CHECK_DISMISSALS[name]
        assert dismissal.startswith(DISMISSAL_OPENING)
        generic = [phrase for phrase in GENERIC_DISMISSALS if phrase in dismissal.lower()]
        assert not generic, f"{name}: generic dismissal {generic}: {dismissal!r}"

    @pytest.mark.parametrize("name", sorted(CHECKS))
    def test_the_fix_is_an_imperative_sentence(self, name):
        fix = CHECK_FIXES[name]
        assert fix[0].isupper() and fix.endswith(".")
        assert not fix.startswith(("Consider", "Maybe", "Perhaps", "Review")), fix


class TestRegistration:
    def test_a_check_without_advice_does_not_register(self):
        with pytest.raises(TypeError, match="advice"):
            check("no-advice")(_fixture)
        assert "no-advice" not in CHECKS

    def test_advice_without_a_fix_is_refused(self):
        with pytest.raises(TypeError, match="fix"):
            Advice(dismiss="Dismiss only if never.")

    def test_advice_without_a_dismissal_is_refused(self):
        with pytest.raises(TypeError, match="dismiss"):
            Advice(fix="Delete it.")

    @pytest.mark.parametrize(
        ("fix", "dismiss", "problem"),
        [
            ("Delete it", "Dismiss only if never.", "end with"),
            ("delete it.", "Dismiss only if never.", "capitalized"),
            ("Delete it.", "Dismiss if intended.", DISMISSAL_OPENING),
            ("Delete it.", " Dismiss only if never.", "stripped"),
        ],
    )
    def test_a_malformed_sentence_does_not_register(self, fix, dismiss, problem):
        with pytest.raises(CheckRegistrationError, match=problem):
            check("malformed", advice=Advice(fix=fix, dismiss=dismiss))(_fixture)
        assert "malformed" not in CHECKS


class TestPrinted:
    def test_a_message_ends_with_the_fix_then_the_dismissal(self):
        finding = Finding("a.py", 3, "dead-code", "f(): no production callers", Severity.HIGH)
        assert finding.text == (
            f"f(): no production callers. Fix: {CHECK_FIXES['dead-code']} "
            f"{CHECK_DISMISSALS['dead-code']}"
        )

    def test_a_message_that_ends_a_sentence_gets_no_second_stop(self):
        finding = Finding("a.py", 3, "dead-code", "One. Two.", Severity.HIGH)
        assert finding.text.startswith("One. Two. Fix: ")

    def test_format_text_prints_every_finding_with_its_fix_and_dismissal(self):
        findings = [
            Finding("a.py", 1, "dead-code", "f(): no production callers", Severity.HIGH),
            Finding("b.py", 2, "long-function", "g() spans 120 lines", Severity.LOW),
        ]
        out = format_text(findings, 2, None, summary=False, max_findings=0, header=[])
        for finding in findings:
            assert f"  {finding.file}:{finding.line}: {finding.text}" in out.splitlines()

    def test_list_checks_shows_every_dismissal(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            main(["--list-checks"])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        missing = [name for name, text in CHECK_DISMISSALS.items() if text not in out]
        assert not missing, f"--list-checks omits these dismissals: {missing}"
