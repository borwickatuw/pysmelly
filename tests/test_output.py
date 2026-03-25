"""Tests for output formatting."""

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
        output = format_text(findings, total_files=2, context=None)
        assert "=== convergence hotspots ===" in output
        assert "app.py: flagged by 3 checks" in output

    def test_convergence_appears_before_checks(self):
        """Convergence hotspots appear before individual check sections."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
            _finding("app.py", "no-refactoring"),
        ]
        output = format_text(findings, total_files=1, context=None)
        hotspot_pos = output.index("convergence hotspots")
        check_pos = output.index("=== bug-magnet")
        assert hotspot_pos < check_pos

    def test_no_convergence_below_threshold(self):
        """2 checks on same file -> no convergence section."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
        ]
        output = format_text(findings, total_files=1, context=None)
        assert "convergence hotspots" not in output

    def test_convergence_in_summary_mode(self):
        """Hotspots appear in summary mode too."""
        findings = [
            _finding("app.py", "bug-magnet"),
            _finding("app.py", "blast-radius"),
            _finding("app.py", "no-refactoring"),
        ]
        output = format_text(findings, total_files=1, context=None, summary=True)
        assert "convergence hotspots" in output
        assert "app.py: flagged by 3 checks" in output

    def test_no_findings_no_convergence(self):
        """No findings -> no convergence section."""
        output = format_text([], total_files=1, context=None)
        assert "convergence hotspots" not in output
