"""Output formatting for findings."""

import json
from dataclasses import asdict

from pysmelly.registry import Finding, Severity


def format_text(findings: list[Finding], total_files: int) -> str:
    """Human-readable text output grouped by check."""
    lines = [f"Parsed {total_files} Python files", ""]

    by_check: dict[str, list[Finding]] = {}
    for f in findings:
        by_check.setdefault(f.check, []).append(f)

    for check_name, check_findings in by_check.items():
        lines.append(f"=== {check_name} ({len(check_findings)} finding(s)) ===")
        for f in check_findings:
            lines.append(f"  {f.file}:{f.line}: {f.message}")
        lines.append("")

    if findings:
        lines.append(f"Total: {len(findings)} finding(s)")
    else:
        lines.append("All checks passed.")

    return "\n".join(lines)


def format_json(
    findings: list[Finding],
    total_files: int,
    source_lines: dict[str, list[str]],
) -> str:
    """Machine-readable JSON output."""
    result_findings = []
    for f in findings:
        entry = {
            "file": f.file,
            "line": f.line,
            "check": f.check,
            "message": f.message,
            "severity": f.severity.value,
        }
        if source_lines and f.file in source_lines:
            lines = source_lines[f.file]
            idx = f.line - 1
            if 0 <= idx < len(lines):
                entry["source"] = lines[idx].rstrip()
        result_findings.append(entry)

    output = {
        "total_files": total_files,
        "total_findings": len(findings),
        "findings": result_findings,
    }
    return json.dumps(output, indent=2)
