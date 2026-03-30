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
from pysmelly.discovery import (
    GitNotFoundError,
    get_changed_lines,
    get_git_root,
    get_python_files,
    parse_file,
)
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

Git history analysis:
  pysmelly git-history             Analyze git history for structural smells
  pysmelly git-history --window 1y Look back 1 year instead of 6 months
  pysmelly git-history reviewed    Acknowledge git history findings

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

_GIT_HISTORY_CHECKS = {name for name, cat in CHECK_CATEGORIES.items() if cat == "git-history"}


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
        if category == "git-history":
            tag = " [git] (use: pysmelly git-history)"
        else:
            tag = ""
        description = CHECK_DESCRIPTIONS.get(name, "")
        print(f"  {name:<{name_width}}  [{severity:<6}]{tag}  {description}")


def _discover_and_parse(roots: list[Path], excludes: list[str]) -> tuple[Path, dict[Path, object]]:
    """Discover Python files, parse them, and return (base, all_trees).

    Validates that all roots are directories (exits on error).
    """
    for root in roots:
        if not root.is_dir():
            print(f"Error: {root} is not a directory", file=sys.stderr)
            sys.exit(1)

    if len(roots) == 1:
        base = roots[0]
    else:
        base = Path(os.path.commonpath(roots))

    all_trees = {}
    for root in roots:
        for f in get_python_files(root):
            rel = f.relative_to(base)
            if _is_excluded(rel, excludes):
                continue
            tree = parse_file(f)
            if tree:
                all_trees[rel] = tree

    return base, all_trees


def _apply_filters(
    findings: list[Finding],
    min_severity: str,
    diff_ref: str | None,
    base: Path,
) -> list[Finding]:
    """Apply severity and diff filters to findings."""
    severity_order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
    min_level = severity_order[Severity(min_severity)]
    findings = [f for f in findings if severity_order[f.severity] >= min_level]

    if diff_ref is not None:
        try:
            git_root = get_git_root(base)
        except GitNotFoundError:
            return findings
        changed = get_changed_lines(diff_ref, git_root)
        try:
            offset = base.relative_to(git_root)
        except ValueError:
            offset = Path()
        findings = [
            f
            for f in findings
            if str(offset / f.file) in changed and f.line in changed[str(offset / f.file)]
        ]

    return findings


def _apply_suppression(findings: list[Finding], base: Path) -> list[Finding]:
    """Remove findings suppressed by inline comments."""
    source_lines: dict[str, list[str]] = {}
    files_with_findings = {f.file for f in findings}
    for file_rel in files_with_findings:
        try:
            source_lines[file_rel] = (base / file_rel).read_text().splitlines()
        except OSError:
            pass
    return [f for f in findings if not _is_suppressed(f, source_lines)]


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
uvx pysmelly --list-checks             # see all available checks
```

## Git history analysis

pysmelly can analyze git history to detect evolutionary signals invisible to
static analysis. These are run via the `git-history` subcommand:

```bash
uvx pysmelly git-history                    # run all git history checks
uvx pysmelly git-history --window 1y        # look back 1 year instead of 6 months
uvx pysmelly git-history --check blast-radius  # run a single git check
```

Some git history findings (notably `abandoned-code`) are persistent — the file
is still abandoned next time you run pysmelly. To acknowledge a finding after
reviewing the file, use the `reviewed` subcommand:

```bash
uvx pysmelly git-history reviewed path/to/file.py   # acknowledge one file
uvx pysmelly git-history reviewed a.py b.py          # acknowledge multiple files
```

This creates an empty git commit with `pysmelly: reviewed path/to/file.py`
markers. All git-history checks will then only analyze commits **after** the
review date for that file. This effectively resets the analysis window — if the
underlying problem was fixed, the finding disappears. If the pattern re-emerges
from new commits, the finding returns.

You can also add the marker manually to any commit message:

```
Refactor auth module

pysmelly: reviewed utils/legacy_parser.py
pysmelly: reviewed utils/old_helpers.py
```

When the review commit ages out of the time window, the full history is analyzed
again — which is correct, because stale acknowledgments shouldn't suppress
findings forever.

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

## Configuration

Create `.pysmelly.toml` or add `[tool.pysmelly]` to `pyproject.toml`:

```toml
exclude = ["tests/", "test_*", "conftest.py"]
skip = ["single-call-site"]
min-severity = "medium"
```

CLI arguments extend list values and override scalar values.

## How to act on AST findings

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

## How to act on git-history findings

Git history findings are different from AST findings. AST findings point to
specific code patterns you can fix in a single commit. Git history findings
reveal **evolutionary patterns** — how your codebase is changing over time —
and often require design-level thinking rather than quick fixes.

**Start with convergence hotspots.** When pysmelly reports a file flagged by
3+ different checks, that's your highest-confidence signal. A file that is
simultaneously a bug-magnet, growing fast, and producing fix-after-feature
sequences has a structural problem — not a series of unrelated issues. Read
the file, understand why multiple checks converge on it, and consider whether
it needs to be decomposed or redesigned.

**Cluster blast-radius and change-coupling findings.** If 20 files all show
high blast-radius, they are not 20 independent problems — they are one
tightly-coupled subsystem. Group the findings by directory or package and ask:
what shared abstraction (base class, data model, configuration) is forcing all
these files to change together? Stabilizing that shared interface will resolve
the entire cluster at once.

**yo-yo-code and fix-follows-feature point to unstable abstractions.** When
code is being rewritten repeatedly (yo-yo) or features reliably produce bugs
(fix-follows-feature), the fix is usually a redesign, not a quick patch. Read
the git log for that file to understand *what* keeps changing and *why*, then
address the root cause. These findings often go away once the right abstraction
is found.

**bug-magnet files need structural attention.** A file where the majority of
commits are fixes will keep attracting fixes until the underlying design
changes. Look at the pattern of bugs — are they all in one area of the file?
Is the file doing too many things? Would splitting it help isolate the
fragile part?

**growth-trajectory is a leading indicator.** A file that doubled in size in
6 months will probably keep growing. Act before it becomes unmanageable —
extract responsibilities while the file is still comprehensible.

**knowledge-silo and abandoned-code are team-health signals.** These don't
require code changes — they require human attention. Knowledge-silo means one
person owns all the context for a file; pair programming or code review can
spread that knowledge. Abandoned-code means a file may be dead weight; decide
whether to delete it, update it, or acknowledge it with `pysmelly git-history
reviewed`.

**Not every finding demands a code change.** Some git-history findings are
informational. If blast-radius is high because your project genuinely has
tightly-integrated components, that may be acceptable — but you should be
aware of it. The findings that most reliably demand action are convergence
hotspots, bug-magnets, and fix-follows-feature patterns.
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
    """Handle `pysmelly git-history reviewed <file|dir> [...]` — create acknowledgment commit."""
    if not args:
        print(
            "Error: pysmelly git-history reviewed requires at least one file or directory path",
            file=sys.stderr,
        )
        sys.exit(1)

    # Verify we're in a git repo
    try:
        git_root = get_git_root(Path.cwd())
    except GitNotFoundError:
        print("Error: pysmelly git-history reviewed requires a git repository", file=sys.stderr)
        sys.exit(1)

    # Expand directories to .py files, verify paths exist
    filepaths: list[str] = []
    for arg in args:
        p = Path(arg)
        if not p.exists():
            print(f"Error: {arg} does not exist", file=sys.stderr)
            sys.exit(1)
        if p.is_dir():
            py_files = sorted(str(f) for f in p.rglob("*.py"))
            if not py_files:
                print(f"Warning: no .py files found in {arg}", file=sys.stderr)
            filepaths.extend(py_files)
        else:
            filepaths.append(arg)

    if not filepaths:
        print("Error: no files to review", file=sys.stderr)
        sys.exit(1)

    # Build commit message
    markers = "\n".join(f"pysmelly: reviewed {f}" for f in filepaths)
    if len(filepaths) == 1:
        subject = f"Acknowledge pysmelly finding for {filepaths[0]}"
    elif len(args) == 1 and Path(args[0]).is_dir():
        subject = f"Acknowledge pysmelly findings for {args[0]}/ ({len(filepaths)} files)"
    else:
        subject = f"Acknowledge pysmelly findings for {len(filepaths)} files"
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


def _handle_git_history(argv: list[str]) -> None:
    """Handle `pysmelly git-history [reviewed|TARGETS...]` subcommand."""
    if argv and argv[0] == "reviewed":
        _handle_reviewed(argv[1:])
        return

    git_history_checks = {
        name: fn for name, fn in CHECKS.items() if CHECK_CATEGORIES.get(name) == "git-history"
    }

    parser = argparse.ArgumentParser(
        prog="pysmelly git-history",
        description="Analyze git history for structural and evolutionary code smells",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["."],
        help="Directories to analyze (default: current directory)",
    )
    parser.add_argument(
        "--check",
        choices=list(git_history_checks.keys()),
        metavar="CHECK",
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
        help="Exclude files matching pattern (repeatable)",
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
        "--no-context",
        action="store_true",
        help="Suppress the guidance preamble",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show finding counts per check without individual findings",
    )
    parser.add_argument(
        "--more-please",
        "--all",
        action="store_true",
        help="Show all findings (default: top 10 highest-confidence)",
    )
    parser.add_argument(
        "--window",
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
    parser.add_argument(
        "--ignore-reviewed",
        action="store_true",
        help="Analyze full history, ignoring any 'pysmelly: reviewed' markers",
    )
    args = parser.parse_args(argv)

    # Load config and merge
    roots = [Path(t).resolve() for t in args.targets]
    config_dir = roots[0] if len(roots) == 1 else Path.cwd()
    config = load_config(config_dir, set(CHECKS.keys()))

    if "exclude" in config:
        args.exclude = config["exclude"] + args.exclude
    if "skip" in config:
        args.skip = config["skip"] + args.skip
    if "min-severity" in config and args.min_severity == "low":
        args.min_severity = config["min-severity"]
    if "git-window" in config and args.window == "6m":
        args.window = config["git-window"]
    if "commit-messages" in config and args.commit_messages == "auto":
        args.commit_messages = config["commit-messages"]

    # Must be in a git repo
    base, all_trees = _discover_and_parse(roots, args.exclude)
    try:
        git_root = get_git_root(base)
    except GitNotFoundError:
        print("Error: pysmelly git-history requires a git repository", file=sys.stderr)
        sys.exit(1)

    # Determine which checks to run
    if args.check:
        checks_to_run = {args.check: git_history_checks[args.check]}
    else:
        checks_to_run = {
            name: fn for name, fn in git_history_checks.items() if name not in args.skip
        }

    # Run checks
    expected_coupling = config.get("expected-coupling", [])
    ctx = AnalysisContext(
        all_trees,
        args.verbose,
        git_root=git_root,
        git_window=args.window,
        commit_messages=args.commit_messages,
        expected_coupling=expected_coupling,
    )
    if args.ignore_reviewed and ctx.git_history is not None:
        ctx.git_history.reviewed_at = {}
    all_findings: list[Finding] = []
    for name, check_fn in checks_to_run.items():
        all_findings.extend(check_fn(ctx))

    # Filter and suppress
    all_findings = _apply_filters(all_findings, args.min_severity, None, base)
    all_findings = _apply_suppression(all_findings, base)

    # Guidance
    context: list[str] | None = None
    if not args.no_context:
        context = [
            "pysmelly git-history analyzes version control history to find "
            "structural and evolutionary signals invisible to static analysis. "
            "These findings reveal files with poor encapsulation, hidden coupling, "
            "or rapid growth that suggest design problems."
        ]

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


def main(argv: list[str] | None = None) -> None:
    raw_args = argv if argv is not None else sys.argv[1:]
    if raw_args and raw_args[0] == "init":
        _handle_init(raw_args[1:])
        return
    if raw_args and raw_args[0] == "git-history":
        _handle_git_history(raw_args[1:])
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
        choices=[n for n in CHECKS if n not in _GIT_HISTORY_CHECKS],
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
        "--all",
        action="store_true",
        help="Show all findings (default: top 10 highest-confidence)",
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
        check_name = config["check"]
        if CHECK_CATEGORIES.get(check_name) == "git-history":
            print(
                f"Error: {check_name} is a git-history check. "
                f"Use: pysmelly git-history --check {check_name}",
                file=sys.stderr,
            )
            sys.exit(1)
        args.check = check_name

    base, all_trees = _discover_and_parse(roots, args.exclude)

    # Determine which checks to run (exclude git-history checks)
    if args.check:
        checks_to_run = {args.check: CHECKS[args.check]}
    else:
        checks_to_run = {
            name: fn
            for name, fn in CHECKS.items()
            if name not in args.skip and CHECK_CATEGORIES.get(name, "ast") != "git-history"
        }

    # Run checks
    ctx = AnalysisContext(all_trees, args.verbose)
    all_findings: list[Finding] = []
    for name, check_fn in checks_to_run.items():
        all_findings.extend(check_fn(ctx))

    # Filter
    all_findings = _apply_filters(all_findings, args.min_severity, args.diff, base)
    all_findings = _apply_suppression(all_findings, base)

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
