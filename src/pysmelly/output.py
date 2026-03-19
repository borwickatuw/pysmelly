"""Output formatting for findings."""

import random

from pysmelly.registry import Finding, Severity

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}

_TAGLINES = [
    "Whoever smelt it, committed it.",
    "I love the smell of refactoring in the morning.",
    "I smell dead code.",
    "Something is rotten in the state of your codebase.",
    "Houston, we have a code smell.",
    "You can't handle the smell!",
    "This code doesn't pass the smell test.",
    "Follow your nose -- it always knows the code that smells.",
]


def format_text(
    findings: list[Finding],
    total_files: int,
    context: list[str] | None,
    summary: bool = False,
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

    if summary:
        sorted_checks = sorted(
            by_check.items(),
            key=lambda item: (_SEVERITY_ORDER[item[1][0].severity], -len(item[1])),
        )
        for check_name, check_findings in sorted_checks:
            severity = check_findings[0].severity.value
            lines.append(f"  {check_name:<30} [{severity:<6}]  {len(check_findings)}")
        lines.append("")
    else:
        for check_name, check_findings in by_check.items():
            lines.append(f"=== {check_name} ({len(check_findings)} finding(s)) ===")
            for f in check_findings:
                lines.append(f"  {f.file}:{f.line}: {f.message}")
            lines.append("")

    if findings:
        lines.append(f"Total: {len(findings)} finding(s)")
    else:
        lines.append("All checks passed.")

    lines.append(f"\n  -- {random.choice(_TAGLINES)}")

    return "\n".join(lines)
