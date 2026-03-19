"""CLI entry point for pysmelly."""

import argparse
import sys
from pathlib import Path

# Import checks to trigger registration
import pysmelly.checks  # noqa: F401
from pysmelly.discovery import get_python_files, parse_file
from pysmelly.output import format_json, format_text
from pysmelly.registry import CHECK_DESCRIPTIONS, CHECK_SEVERITY, CHECKS, Finding, Severity

EPILOG = """\
pysmelly finds code smells that survive after design changes — vestigial
patterns that accumulate as code evolves. It performs cross-file call-graph
analysis to detect patterns that single-file linters miss.

Install:  uvx pysmelly (zero dependencies, no setup required)

Severity levels:
  high    Act on this or explicitly justify keeping it
  medium  Review each finding, fix what makes sense
  low     Informational — skim for surprises

Exit codes:
  0       No findings
  1       One or more findings reported

For LLM-assisted code review, use --format=json for structured output.
Each finding includes file, line, check name, message, and severity
for programmatic consumption. Use --list-checks to see available checks
with descriptions.

Complementary tools to run alongside pysmelly:
  vulture     Dead code detection (name-matching, no call graph)
  ruff        Fast single-file linting (style, bugs, complexity)
  pylint      Broad static analysis and code quality
  mypy        Static type checking
  bandit      Security-focused static analysis

pysmelly does NOT check formatting, types, or security — use the tools
above for those. pysmelly focuses on design smells and refactoring signals
that require cross-file analysis.
"""


def _print_check_list() -> None:
    """Print all registered checks with severity and description."""
    name_width = max(len(name) for name in CHECKS)
    for name in CHECKS:
        severity = CHECK_SEVERITY[name].value
        description = CHECK_DESCRIPTIONS.get(name, "")
        print(f"  {name:<{name_width}}  [{severity:<6}]  {description}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pysmelly",
        description="AST-based Python code smell detector",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Directory to analyze (default: current directory)",
    )
    parser.add_argument(
        "--check",
        choices=list(CHECKS.keys()),
        help="Run only this check",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help="Skip this check (can be repeated)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude files matching this pattern (can be repeated, e.g. 'test_*')",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show additional detail",
    )
    parser.add_argument(
        "--min-severity",
        choices=["low", "medium", "high"],
        default="low",
        help="Minimum severity to report (default: low)",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List all available checks with descriptions and exit",
    )
    args = parser.parse_args(argv)

    if args.list_checks:
        _print_check_list()
        return

    root = Path(args.target).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    files = get_python_files(root)
    all_trees = {}
    for f in files:
        rel = f.relative_to(root)
        if any(rel.match(pattern) for pattern in args.exclude):
            continue
        tree = parse_file(f)
        if tree:
            all_trees[rel] = tree

    # Determine which checks to run
    if args.check:
        checks_to_run = {args.check: CHECKS[args.check]}
    else:
        checks_to_run = {name: fn for name, fn in CHECKS.items() if name not in args.skip}

    # Run checks
    all_findings: list[Finding] = []
    for name, check_fn in checks_to_run.items():
        all_findings.extend(check_fn(all_trees, args.verbose))

    # Filter by minimum severity
    severity_order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
    min_level = severity_order[Severity(args.min_severity)]
    all_findings = [f for f in all_findings if severity_order[f.severity] >= min_level]

    # Output
    if args.format == "json":
        print(format_json(all_findings, len(all_trees)))
    else:
        print(format_text(all_findings, len(all_trees)))

    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
