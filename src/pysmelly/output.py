"""Output formatting for findings."""

from pysmelly.registry import Finding


def format_text(
    findings: list[Finding],
    total_files: int,
    context: list[str] | None = None,
) -> str:
    """Text output grouped by check, with optional guidance preamble."""
    lines = []

    if context:
        lines.append("--- Guidance ---")
        for item in context:
            lines.append(item)
        lines.append("----------------")
        lines.append("")

    lines.extend([f"Parsed {total_files} Python files", ""])

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
