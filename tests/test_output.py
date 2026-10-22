"""Tests for output formatting."""

import random

from pysmelly.output import format_text
from pysmelly.registry import Finding, Severity


def _finding(file: str, check: str, severity: Severity = Severity.MEDIUM) -> Finding:
    return Finding(
        file=file,
        line=1,
        check=check,
        message=f"{file}: {check} finding",
        severity=severity,
    )


class TestConvergenceHotspots:
    def test_convergence_hotspots_shown(self):
        """3+ checks on same file -> convergence section appears."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
            _finding("app.py", "no-refactoring"),
            _finding("other.py", "bug-magnet"),
        ]
        output = format_text(
            findings, total_files=2, context=None, summary=False, max_findings=0, header=[]
        )
        assert "=== convergence hotspots (1 file(s)) ===" in output
        assert "app.py: flagged by 3 checks" in output

    def test_convergence_appears_before_checks(self):
        """Convergence hotspots appear before individual check sections."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
            _finding("app.py", "no-refactoring"),
        ]
        output = format_text(
            findings, total_files=1, context=None, summary=False, max_findings=0, header=[]
        )
        hotspot_pos = output.index("convergence hotspots")
        check_pos = output.index("=== bug-magnet")
        assert hotspot_pos < check_pos

    def test_no_convergence_below_threshold(self):
        """2 checks on same file -> no convergence section."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
        ]
        output = format_text(
            findings, total_files=1, context=None, summary=False, max_findings=0, header=[]
        )
        assert "convergence hotspots" not in output

    def test_convergence_in_summary_mode(self):
        """Hotspots appear in summary mode too."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
            _finding("app.py", "no-refactoring"),
        ]
        output = format_text(
            findings, total_files=1, context=None, summary=True, max_findings=0, header=[]
        )
        assert "convergence hotspots" in output
        assert "app.py: flagged by 3 checks" in output

    @staticmethod
    def _hot_and_capped() -> list[Finding]:
        """hot.py flagged by 3 checks, all LOW; 3 HIGH findings fill a cap of 3."""
        return [
            _finding("hot.py", "dead-code", Severity.LOW),
            _finding("hot.py", "middle-man", Severity.LOW),
            _finding("hot.py", "arrow-code", Severity.LOW),
            *(_finding(f"o{n}.py", "env-fallbacks", Severity.HIGH) for n in range(3)),
        ]

    def test_hotspots_count_findings_the_cap_hides(self):
        """A hotspot is a file 3+ checks flag, not 3+ checks among those shown."""
        output = format_text(
            self._hot_and_capped(),
            total_files=4,
            context=None,
            summary=False,
            max_findings=3,
            header=[],
        )
        assert "=== convergence hotspots (1 file(s)) ===" in output
        assert "hot.py: flagged by 3 checks" in output

    def test_summary_hotspots_count_every_finding(self):
        output = format_text(
            self._hot_and_capped(),
            total_files=4,
            context=None,
            summary=True,
            max_findings=3,
            header=[],
        )
        assert "hot.py: flagged by 3 checks" in output

    @staticmethod
    def _many_hotspots() -> list[Finding]:
        """f0.py flagged by 5 checks, f1.py by 4, f2.py and f3.py by 3."""
        checks = ["arrow-code", "blast-radius", "bug-magnet", "dead-code", "middle-man"]
        return [
            _finding(f"f{n}.py", check)
            for n, width in enumerate((5, 4, 3, 3))
            for check in checks[:width]
        ]

    def test_capped_view_lists_the_top_hotspots_by_check_count_then_path(self):
        for summary in (False, True):
            output = format_text(
                self._many_hotspots(),
                total_files=4,
                context=None,
                summary=summary,
                max_findings=3,
                header=[],
            )
            section = output.split("=== convergence hotspots ")[1].split("\n\n")[0].splitlines()
            assert section == [
                "(4 file(s), 3 shown) ===",
                "  f0.py: flagged by 5 checks (arrow-code, blast-radius, bug-magnet, dead-code, "
                "middle-man)",
                "  f1.py: flagged by 4 checks (arrow-code, blast-radius, bug-magnet, dead-code)",
                "  f2.py: flagged by 3 checks (arrow-code, blast-radius, bug-magnet)",
            ]

    def test_uncapped_view_lists_every_hotspot(self):
        output = format_text(
            self._many_hotspots(),
            total_files=4,
            context=None,
            summary=False,
            max_findings=0,
            header=[],
        )
        assert "=== convergence hotspots (4 file(s)) ===" in output
        assert "f3.py: flagged by 3 checks" in output

    def test_no_findings_no_convergence(self):
        """No findings -> no convergence section."""
        output = format_text(
            [], total_files=1, context=None, summary=False, max_findings=0, header=[]
        )
        assert "convergence hotspots" not in output


def _body(text: str) -> str:
    """The output without its closing tagline, which is deliberately random."""
    return text.split("\n\n  -- ", maxsplit=1)[0]


class TestDeterministicOrder:
    """Two runs over the same findings must print the same bytes.

    Checks build findings by walking dicts and sets, whose iteration order
    varies between interpreter runs, so the output boundary is where order
    has to be pinned -- otherwise a before/after diff of a remediation shows
    transposed lines that are not changes.
    """

    def _findings(self) -> list[Finding]:
        return [
            _finding("z.py", "dead-code", Severity.HIGH),
            _finding("a.py", "dead-code", Severity.HIGH),
            _finding("m.py", "arrow-code", Severity.LOW),
            _finding("a.py", "trivial-wrappers"),
            _finding("a.py", "middle-man"),
            _finding("b.py", "dead-code", Severity.HIGH),
            _finding("a.py", "arrow-code", Severity.LOW),
        ]

    def test_shuffled_input_gives_identical_output(self):
        """Any input permutation formats to the same text."""
        findings = self._findings()
        expected = _body(
            format_text(
                findings, total_files=3, context=None, summary=False, max_findings=0, header=[]
            )
        )
        for seed in range(25):
            shuffled = list(findings)
            random.Random(seed).shuffle(shuffled)
            assert (
                _body(
                    format_text(
                        shuffled,
                        total_files=3,
                        context=None,
                        summary=False,
                        max_findings=0,
                        header=[],
                    )
                )
                == expected
            )

    def test_shuffled_input_gives_identical_summary(self):
        """Summary mode orders its per-check rows the same way every time."""
        findings = self._findings()
        expected = _body(
            format_text(
                findings, total_files=3, context=None, summary=True, max_findings=0, header=[]
            )
        )
        for seed in range(25):
            shuffled = list(findings)
            random.Random(seed).shuffle(shuffled)
            got = format_text(
                shuffled, total_files=3, context=None, summary=True, max_findings=0, header=[]
            )
            assert _body(got) == expected

    def test_truncation_picks_the_same_findings(self):
        """The top-N cut is not decided by input order either."""
        findings = self._findings()
        expected = _body(
            format_text(
                findings, total_files=3, context=None, summary=False, max_findings=3, header=[]
            )
        )
        for seed in range(25):
            shuffled = list(findings)
            random.Random(seed).shuffle(shuffled)
            got = format_text(
                shuffled, total_files=3, context=None, summary=False, max_findings=3, header=[]
            )
            assert _body(got) == expected

    def test_hotspot_rows_break_ties_by_path(self):
        """Files tied on check count are listed in path order, not dict order."""
        findings = [
            _finding(name, check)
            for name in ("b.py", "a.py")
            for check in ("dead-code", "middle-man", "arrow-code")
        ]
        output = format_text(
            findings, total_files=2, context=None, summary=False, max_findings=0, header=[]
        )
        assert output.index("a.py: flagged by 3 checks") < output.index("b.py: flagged by 3 checks")

    def test_findings_within_a_check_are_path_ordered(self):
        """Within one section, findings read in path order."""
        findings = [
            _finding("z.py", "dead-code", Severity.HIGH),
            _finding("a.py", "dead-code", Severity.HIGH),
            _finding("m.py", "dead-code", Severity.HIGH),
        ]
        output = format_text(
            findings, total_files=3, context=None, summary=False, max_findings=0, header=[]
        )
        assert output.index("a.py:1") < output.index("m.py:1") < output.index("z.py:1")

    def test_high_severity_sections_come_first(self):
        """Section order follows severity, so the fix list reads top-down."""
        findings = [
            _finding("a.py", "arrow-code", Severity.LOW),
            _finding("a.py", "dead-code", Severity.HIGH),
        ]
        output = format_text(
            findings, total_files=1, context=None, summary=False, max_findings=0, header=[]
        )
        assert output.index("=== dead-code") < output.index("=== arrow-code")


class TestHeader:
    def test_header_sits_under_the_file_count(self):
        output = format_text(
            [],
            total_files=1,
            context=None,
            summary=False,
            max_findings=0,
            header=["Git history window: 6m"],
        )
        lines = output.splitlines()
        assert lines[lines.index("Parsed 1 Python files") + 1] == "Git history window: 6m"

    def test_none_header_prints_nothing(self):
        assert "window" not in format_text(
            [], total_files=1, context=None, summary=False, max_findings=0, header=[]
        )

    def test_header_lines_print_in_order_before_the_findings(self):
        """The git-history header carries the trends block; it is not a finding."""
        header = [
            "Git history window: 6m",
            "",
            "Project trends (...):",
            "  commits per week: 1.0 -> 2.0 (+100%)",
        ]
        findings = [_finding("a.py", "dead-code", Severity.HIGH)]
        lines = format_text(
            findings, total_files=1, context=None, summary=False, max_findings=0, header=header
        ).splitlines()
        start = lines.index("Parsed 1 Python files") + 1
        assert lines[start : start + 5] == [*header, ""]
        assert lines.index("Project trends (...):") < lines.index(
            "=== dead-code (1 finding(s)) ==="
        )
        assert "Total: 1 finding(s)" in lines


class TestCappedDisplay:
    """The default view's cap hides findings; the text around it must not under-report."""

    @staticmethod
    def _findings() -> list[Finding]:
        # One HIGH check that fired once, one MEDIUM check that fired three times:
        # a cap of 2 shows the HIGH finding and one of the three.
        return [
            _finding("a.py", "dead-code", Severity.HIGH),
            *(_finding(f"{name}.py", "middle-man") for name in ("b", "c", "d")),
        ]

    def test_capped_section_header_gives_the_check_total_and_the_shown_count(self):
        output = format_text(
            self._findings(), total_files=4, context=None, summary=False, max_findings=2, header=[]
        )
        assert "=== middle-man (3 finding(s), 1 shown) ===" in output.splitlines()
        assert "=== dead-code (1 finding(s)) ===" in output.splitlines()

    def test_uncapped_section_header_gives_the_total_alone(self):
        output = format_text(
            self._findings(), total_files=4, context=None, summary=False, max_findings=0, header=[]
        )
        assert "=== middle-man (3 finding(s)) ===" in output.splitlines()

    def test_capped_run_names_the_cap(self):
        output = format_text(
            self._findings(), total_files=4, context=None, summary=False, max_findings=2, header=[]
        )
        assert "Showing top 2. Run with --more-please for all 4." in output

    def test_summary_names_no_cap(self):
        """A summary already counts every finding, so it has nothing more to show."""
        output = format_text(
            self._findings(), total_files=4, context=None, summary=True, max_findings=2, header=[]
        )
        assert "Total: 4 finding(s)" in output
        assert "Showing top" not in output
        assert "--more-please" not in output
