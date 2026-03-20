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
from pysmelly.config import load_config
from pysmelly.context import AnalysisContext
from pysmelly.discovery import get_changed_lines, get_git_root, get_python_files, parse_file
from pysmelly.output import format_text
from pysmelly.registry import (
    CHECK_CATEGORIES,
    CHECK_DESCRIPTIONS,
    CHECK_SEVERITY,
    CHECKS,
    Finding,
    Severity,
)

EPILOG = """\
pysmelly finds code smells that survive after design changes — vestigial
patterns that accumulate as code evolves. It performs cross-file call-graph
analysis to detect patterns that single-file linters miss.

Install:  uvx pysmelly (zero dependencies, no setup required)

Suppression:
  # pysmelly: ignore              Suppress all checks on this line
  # pysmelly: ignore[dead-code]   Suppress specific check(s)

Configuration:
  .pysmelly.toml or [tool.pysmelly] in pyproject.toml
  exclude = ["tests/", "test_*"]
  skip = ["single-call-site"]
  min-severity = "medium"

Acknowledging git history findings:
  pysmelly reviewed path/to/file.py  Create a commit acknowledging a file
  pysmelly reviewed a.py b.py        Acknowledge multiple files at once

Incremental analysis:
  pysmelly --diff              Findings in uncommitted changes only
  pysmelly --diff main         Findings in changes since main

Severity levels:
  high    Act on this or explicitly justify keeping it
  medium  Review each finding, fix what makes sense
  low     Informational — skim for surprises

Output pacing:
  By default, shows top 10 highest-confidence findings.
  pysmelly --more-please       Show all findings

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
    "dead-exceptions",
    "dead-dispatch-entries",
    "orphaned-test-helpers",
    "single-call-site",
    "internal-only",
    "return-none-instead-of-raise",
    "pass-through-params",
    "scattered-constants",
    "scattered-isinstance",
    "inconsistent-error-handling",
    "shared-mutable-module-state",
    "vestigial-params",
    "shotgun-surgery",
    "dict-as-dataclass",
    "repeated-string-parsing",
}


def _has_test_excludes(excludes: list[str]) -> bool:
    """Check if any --exclude patterns target test files."""
    return any("test" in pat.lower() for pat in excludes)


def _check_guidance_status() -> str | None:
    """Check if PYSMELLY.md exists and is current.

    Returns a suggestion string, or None if guidance is up to date.
    """
    import hashlib

    guidance_path = Path("PYSMELLY.md")
    if not guidance_path.exists():
        return (
            "Run `pysmelly init` to set up AI review guidance for this project. "
            "This creates PYSMELLY.md with detailed instructions on how to "
            "act on findings."
        )
    try:
        content = guidance_path.read_text()
    except OSError:
        return None
    expected_hash = hashlib.sha256(GUIDANCE_CONTENT.encode()).hexdigest()[:12]
    if f"pysmelly-guidance {expected_hash}" not in content:
        return (
            "PYSMELLY.md is outdated — run `pysmelly init` to update it with "
            "the latest guidance."
        )
    return None


def _build_guidance(excludes: list[str], checks_with_findings: set[str]) -> list[str]:
    """Build contextual guidance for LLM consumers."""
    guidance = [
        "pysmelly performs cross-file call-graph analysis to find vestigial "
        "code patterns — the kind of cruft that survives after design changes. "
        "Your default response to each finding should be to FIX it, not to "
        "explain why it's acceptable. Most fixes are small (delete dead code, "
        "remove an unused parameter, replace an elif chain with a dict). "
        "If you choose not to fix a finding, state the specific reason "
        "(framework requirement, public API contract) — not a general defense "
        "of the existing code.",
    ]

    init_hint = _check_guidance_status()
    if init_hint:
        guidance.append(init_hint)

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
        category = CHECK_CATEGORIES.get(name, "ast")
        tag = " [git]" if category == "git-history" else ""
        description = CHECK_DESCRIPTIONS.get(name, "")
        print(f"  {name:<{name_width}}  [{severity:<6}]{tag}  {description}")


GUIDANCE_CONTENT = """\
# pysmelly — Code Smell Review Guide

> This file is generated by `pysmelly init` and can be safely regenerated.
> It provides guidance for AI code review tools on how to use pysmelly.

## What pysmelly does

pysmelly is an AST-based Python code smell detector that acts as an
**investigation dispatcher** for AI code review. It performs **cross-file
call-graph analysis** to find vestigial code patterns — code that outlived the
design that created it — and reports them as **starting points for
investigation**, not mandates.

Each finding includes cross-file context (caller counts, blast radius) so you
can understand the scope of the issue before deciding what to do. These are
patterns that single-file linters (ruff, pylint) cannot detect because they
require seeing how code is used across the entire codebase.

## Running pysmelly

```bash
uvx pysmelly                           # analyze current directory (zero-install)
uvx pysmelly --summary                 # counts per check, no individual findings
uvx pysmelly --check dead-code         # run a single check
uvx pysmelly --min-severity medium     # filter noise
uvx pysmelly --exclude tests/ test_*   # exclude test files
uvx pysmelly --diff                    # findings in uncommitted changes only
uvx pysmelly --diff main              # findings in changes since main
uvx pysmelly --git-history             # enable git history checks
uvx pysmelly --list-checks             # see all available checks
```

## Suppressing findings

```python
x = 1  # pysmelly: ignore              — suppress all checks on this line
x = 1  # pysmelly: ignore[dead-code]   — suppress specific check(s)
```

**Do not add suppression comments as a way to dismiss findings.** Suppression
is for confirmed false positives (framework calls this via reflection, public
API contract requires this signature). If a finding reveals unfinished code
(TODO), finish it. If a finding reveals dead code, delete it. Adding
`# pysmelly: ignore` to avoid fixing the code is not suppression — it's
avoidance.

## Git history checks

pysmelly can analyze git history to detect evolutionary signals invisible to
static analysis. These checks require `--git-history`:

```bash
uvx pysmelly --git-history                  # run all checks including git history
uvx pysmelly --git-history --git-window 1y  # look back 1 year instead of 6 months
```

Git history findings (like `abandoned-code`) are persistent — the file is still
abandoned next time you run pysmelly. To acknowledge a finding after reviewing
the file, use the `reviewed` subcommand:

```bash
uvx pysmelly reviewed path/to/file.py        # acknowledge one file
uvx pysmelly reviewed a.py b.py               # acknowledge multiple files
```

This creates an empty git commit with `pysmelly: reviewed path/to/file.py`
markers. The finding disappears because the review commit resets the
last-modified clock. When the review commit ages out of the time window, the
finding returns — which is correct, because stale files should be re-evaluated
periodically.

You can also add the marker manually to any commit message:

```
Refactor auth module

pysmelly: reviewed utils/legacy_parser.py
pysmelly: reviewed utils/old_helpers.py
```

## Configuration

Create `.pysmelly.toml` or add `[tool.pysmelly]` to `pyproject.toml`:

```toml
exclude = ["tests/", "test_*", "conftest.py"]
skip = ["single-call-site"]
min-severity = "medium"
```

CLI arguments extend list values and override scalar values.

## How to act on findings

**Your default should be to fix each finding, not to explain why it's OK.**
Most fixes are small: delete dead code, remove an unused parameter, replace
an elif chain with a dict, inline a trivial wrapper. These take seconds. The
temptation is to write a paragraph defending the existing code — resist that.
If the fix is easy, just do it.

**Do not dismiss findings by defending the code in isolation.** Each finding
looks "fine" in isolation, because the code *is* fine in isolation. The smell
is in the cross-file picture: a function with no callers, a parameter every
caller passes the same value for, a constant nobody references. Looking at
the function and saying "it's well-structured" misses the point — pysmelly is
telling you how it's *used* (or not used) across the codebase.

**If you choose not to fix a finding, state the specific reason.** Valid
reasons: framework requires this signature, public API contract, the method
is called via reflection/dispatch. Invalid reasons: "the code is readable,"
"it might be useful later," "the abstraction makes sense in isolation."

**Use the cross-file context.** When pysmelly says a parameter has "18 callers
still passing it," trace those callers and update them. When it says a constant
is "never referenced anywhere," check whether the code that used it was removed
and delete the constant. The context in the finding message tells you the blast
radius of the fix.

**Severity guides priority, not whether to act:**
- **HIGH**: Fix these. Dead code, unused defaults, unreachable code.
- **MEDIUM**: Fix these unless there's a specific reason not to (framework
  convention, public API contract). State the reason if you skip one.
- **LOW**: Review and fix where it makes sense. These are investigation
  pointers — look at the code and decide, but don't ignore them.

**Work incrementally.** If there are many findings, fix the top 1-2 highest
severity issues, commit, then re-run pysmelly. Don't try to address everything
at once — focused changes are easier to review and less likely to introduce
regressions.
"""

CLAUDE_MD_REFERENCE = """\

## pysmelly

Read [{path}]({path}) before running pysmelly code smell analysis on this project.
"""


def _handle_init(args: list[str]) -> None:
    """Handle `pysmelly init [PATH]` — write guidance file and reference in CLAUDE.md."""
    path = Path(args[0]) if args else Path("PYSMELLY.md")

    # Create parent directories if needed
    path.parent.mkdir(parents=True, exist_ok=True)
    import hashlib

    content_hash = hashlib.sha256(GUIDANCE_CONTENT.encode()).hexdigest()[:12]
    versioned = f"<!-- pysmelly-guidance {content_hash} -->\n{GUIDANCE_CONTENT}"
    path.write_text(versioned)
    print(f"Wrote {path}")

    # Add reference to CLAUDE.md (idempotent)
    claude_md = Path("CLAUDE.md")
    marker = "pysmelly"
    if claude_md.exists():
        existing = claude_md.read_text()
        if marker in existing:
            print(f"CLAUDE.md already references pysmelly")
            return
        with claude_md.open("a") as f:
            f.write(CLAUDE_MD_REFERENCE.format(path=path))
    else:
        claude_md.write_text(CLAUDE_MD_REFERENCE.format(path=path).lstrip())
    print(f"Added pysmelly reference to CLAUDE.md")


def _handle_reviewed(args: list[str]) -> None:
    """Handle `pysmelly reviewed <file> [<file> ...]` — create a commit acknowledging files."""
    if not args:
        print("Error: pysmelly reviewed requires at least one file path", file=sys.stderr)
        sys.exit(1)

    # Verify we're in a git repo
    git_root = get_git_root(Path.cwd())
    if git_root is None:
        print("Error: pysmelly reviewed requires a git repository", file=sys.stderr)
        sys.exit(1)

    # Verify files exist
    for filepath in args:
        if not Path(filepath).exists():
            print(f"Error: {filepath} does not exist", file=sys.stderr)
            sys.exit(1)

    # Build commit message
    markers = "\n".join(f"pysmelly: reviewed {f}" for f in args)
    if len(args) == 1:
        subject = f"Acknowledge pysmelly finding for {args[0]}"
    else:
        subject = f"Acknowledge pysmelly findings for {len(args)} files"
    message = f"{subject}\n\n{markers}"

    try:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            check=True,
            cwd=git_root,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error: git commit failed (exit {e.returncode})", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    raw_args = argv if argv is not None else sys.argv[1:]
    if raw_args and raw_args[0] == "init":
        _handle_init(raw_args[1:])
        return
    if raw_args and raw_args[0] == "reviewed":
        _handle_reviewed(raw_args[1:])
        return

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
        "--summary",
        action="store_true",
        help="Show finding counts per check without individual findings",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List all available checks with descriptions and exit",
    )
    parser.add_argument(
        "--more-please",
        action="store_true",
        help="Show all findings (default: top 10 highest-confidence)",
    )
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="Enable git history checks (requires git repo with commit history)",
    )
    parser.add_argument(
        "--git-window",
        default="6m",
        metavar="PERIOD",
        help="Time window for git history analysis (default: 6m; e.g., 3m, 1y, 90d)",
    )
    parser.add_argument(
        "--commit-messages",
        choices=["auto", "structured", "unstructured"],
        default="auto",
        help="Commit message quality (default: auto-detect)",
    )
    args = parser.parse_args(argv)

    if args.list_checks:
        _print_check_list()
        return

    # Load config file and merge with CLI args
    roots = [Path(t).resolve() for t in args.targets]
    config_dir = roots[0] if len(roots) == 1 else Path.cwd()
    config = load_config(config_dir, set(CHECKS.keys()))

    # Config provides defaults; CLI args override.
    # For list args, config values come first, CLI values extend.
    if "exclude" in config:
        args.exclude = config["exclude"] + args.exclude
    if "skip" in config:
        args.skip = config["skip"] + args.skip
    # String args: CLI overrides config (argparse defaults are sentinel values)
    if "min-severity" in config and args.min_severity == "low":
        args.min_severity = config["min-severity"]
    if "check" in config and args.check is None:
        args.check = config["check"]
    if "git-history" in config and not args.git_history:
        args.git_history = config["git-history"]
    if "git-window" in config and args.git_window == "6m":
        args.git_window = config["git-window"]
    if "commit-messages" in config and args.commit_messages == "auto":
        args.commit_messages = config["commit-messages"]

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
        if CHECK_CATEGORIES.get(args.check) == "git-history" and not args.git_history:
            print(
                f"Error: --check {args.check} requires --git-history",
                file=sys.stderr,
            )
            sys.exit(1)
        checks_to_run = {args.check: CHECKS[args.check]}
    else:
        checks_to_run = {
            name: fn
            for name, fn in CHECKS.items()
            if name not in args.skip
            and (CHECK_CATEGORIES.get(name, "ast") != "git-history" or args.git_history)
        }

    # Resolve git root for history checks
    git_root = None
    if args.git_history:
        git_root = get_git_root(base)
        if git_root is None:
            print("Error: --git-history requires a git repository", file=sys.stderr)
            sys.exit(1)

    # Run checks
    ctx = AnalysisContext(
        all_trees,
        args.verbose,
        git_root=git_root,
        git_window=args.git_window,
        commit_messages=args.commit_messages,
    )
    all_findings: list[Finding] = []
    for name, check_fn in checks_to_run.items():
        all_findings.extend(check_fn(ctx))

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
    max_findings = 0 if args.more_please else 10
    print(
        format_text(
            all_findings,
            len(all_trees),
            context=context,
            summary=args.summary,
            max_findings=max_findings,
        )
    )

    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
