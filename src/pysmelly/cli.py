"""CLI entry point for pysmelly."""

import argparse  # pysmelly: ignore[stdlib-alternatives] — zero-dependency design
import fnmatch
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Import checks to trigger registration
import pysmelly.checks  # noqa: F401
from pysmelly.discovery import get_changed_lines, get_git_root, get_python_files, parse_file
from pysmelly.output import format_text
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

Output includes a guidance preamble to help LLM consumers interpret
findings in context. Use --no-context to suppress. Use --list-checks
to see available checks with descriptions.

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


def _get_version() -> str:
    """Get version from git describe (live), falling back to package metadata."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        return version("pysmelly")
    except PackageNotFoundError:
        return "unknown"


def _is_excluded(rel: Path, patterns: list[str]) -> bool:
    """Check if a relative path matches any exclude pattern.

    Patterns without / match filenames only (like .gitignore).
    Patterns with / match the full relative path.
    Trailing / means "everything under matching directories".
    """
    rel_str = str(rel)
    name = rel.name
    for pattern in patterns:
        if "/" in pattern:
            if pattern.endswith("/"):
                # Directory pattern: exclude everything under matching dirs
                dir_pattern = pattern.rstrip("/")
                for i in range(1, len(rel.parts)):
                    subpath = str(Path(*rel.parts[:i]))
                    if fnmatch.fnmatch(subpath, dir_pattern):
                        return True
            else:
                # Full path pattern
                if fnmatch.fnmatch(rel_str, pattern):
                    return True
        else:
            # Filename-only pattern
            if fnmatch.fnmatch(name, pattern):
                return True
    return False


def _is_suppressed(finding: Finding, source_lines: dict[str, list[str]]) -> bool:
    """Check if a finding is suppressed by an inline comment."""
    lines = source_lines.get(finding.file)
    if not lines:
        return False

    # Check the finding's line and the line above it
    for idx in (finding.line - 1, finding.line - 2):
        if not (0 <= idx < len(lines)):
            continue
        line = lines[idx]
        pos = line.find("pysmelly: ignore")
        if pos == -1:
            continue
        rest = line[pos + len("pysmelly: ignore") :]
        # Blanket ignore (no bracket follows)
        if not rest.startswith("["):
            return True
        # Specific check: pysmelly: ignore[check-name]
        end = rest.find("]")
        if end != -1:
            check_names = {c.strip() for c in rest[1:end].split(",")}
            if finding.check in check_names:
                return True
    return False


# Checks that use cross-file caller analysis — findings are affected by file exclusions
CALLER_AWARE_CHECKS = {
    "unused-defaults",
    "constant-args",
    "dead-code",
    "single-call-site",
    "internal-only",
    "return-none-instead-of-raise",
    "pass-through-params",
}


def _has_test_excludes(excludes: list[str]) -> bool:
    """Check if any --exclude patterns target test files."""
    return any("test" in pat.lower() for pat in excludes)


def _build_guidance(excludes: list[str], checks_with_findings: set[str]) -> list[str]:
    """Build contextual guidance for LLM consumers."""
    guidance = [
        "pysmelly performs cross-file call-graph analysis to find vestigial "
        "code patterns — the kind of cruft that survives after design changes. "
        "Findings are signals for review, not mandates — use judgment about "
        "whether each finding represents a real problem in context.",
    ]

    if _has_test_excludes(excludes):
        guidance.append(
            "Test files were excluded from this analysis (via --exclude). "
            "When evaluating findings, do not use test callers to justify "
            "keeping code as-is. If a parameter is always passed the same "
            "value in production, don't keep it general just because tests "
            "pass different values — simplify both the code and the tests. "
            "Tests should reflect actual usage, not speculative generality."
        )

    caller_findings = checks_with_findings & CALLER_AWARE_CHECKS
    if caller_findings:
        guidance.append(
            "Caller-aware checks ("
            + ", ".join(sorted(caller_findings))
            + ") analyze how functions are actually called across the codebase. "
            "A function that looks reasonable in isolation may reveal vestigial "
            "design when you see that every caller uses it the same way."
        )

    return guidance


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
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")
    parser.add_argument(
        "targets",
        nargs="*",
        default=["."],
        help="Directories to analyze (default: current directory)",
    )
    parser.add_argument(
        "--check",
        choices=list(CHECKS.keys()),
        metavar="CHECK",
        help="Run only this check (see --list-checks)",
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
        help="Exclude files matching pattern (repeatable; 'test_*' for names, 'path/to/dir/' for directories)",
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
        "--diff",
        nargs="?",
        const="HEAD",
        default=None,
        metavar="REF",
        help="Only report findings in lines changed since REF (default: HEAD)",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Suppress the guidance preamble (on by default for LLM consumers)",
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

    roots = [Path(t).resolve() for t in args.targets]
    for root in roots:
        if not root.is_dir():
            print(f"Error: {root} is not a directory", file=sys.stderr)
            sys.exit(1)

    # Common ancestor for relative paths
    if len(roots) == 1:
        base = roots[0]
    else:
        base = Path(os.path.commonpath(roots))

    all_trees = {}
    for root in roots:
        for f in get_python_files(root):
            rel = f.relative_to(base)
            if _is_excluded(rel, args.exclude):
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

    # Filter by diff (only findings in changed lines)
    if args.diff is not None:
        git_root = get_git_root(base)
        if git_root:
            changed = get_changed_lines(args.diff, git_root)
            try:
                offset = base.relative_to(git_root)
            except ValueError:
                offset = Path()
            all_findings = [
                f
                for f in all_findings
                if str(offset / f.file) in changed and f.line in changed[str(offset / f.file)]
            ]

    # Load source lines for suppression checks
    source_lines: dict[str, list[str]] = {}
    files_with_findings = {f.file for f in all_findings}
    for file_rel in files_with_findings:
        try:
            source_lines[file_rel] = (base / file_rel).read_text().splitlines()
        except OSError:
            pass

    # Filter suppressed findings (# pysmelly: ignore / # pysmelly: ignore[check-name])
    all_findings = [f for f in all_findings if not _is_suppressed(f, source_lines)]

    # Build guidance preamble for LLM consumers (on by default)
    context: list[str] | None = None
    if not args.no_context:
        checks_with_findings = {f.check for f in all_findings}
        context = _build_guidance(args.exclude, checks_with_findings)

    # Output
    print(format_text(all_findings, len(all_trees), context=context))

    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
