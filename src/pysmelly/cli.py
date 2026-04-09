"""CLI entry point for pysmelly."""

import fnmatch
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

# Import checks to trigger registration
import pysmelly.checks  # noqa: F401
from pysmelly.config import load_config
from pysmelly.context import DEFAULT_COMMIT_MESSAGES, AnalysisContext
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
import contextlib

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
            # Full path pattern
            elif fnmatch.fnmatch(rel_str, pattern):
                return True
        # Filename-only pattern
        elif fnmatch.fnmatch(name, pattern):
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

def _find_guidance_path() -> Path | None:
    """Find the guidance file path from CLAUDE.md, falling back to PYSMELLY.md."""
    import re

    claude_md = Path("CLAUDE.md")
    if claude_md.exists():
        try:
            text = claude_md.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            # Match the link target from the reference: Read [path](path)
            m = re.search(r"Read \[.*?\]\((.*?)\) before running pysmelly", text)
            if m:
                return Path(m.group(1))

    # Fall back to default location
    return Path("PYSMELLY.md")


def _check_guidance_status() -> str | None:
    """Check if pysmelly guidance file exists and is current.

    Returns a suggestion string, or None if guidance is up to date.
    Recognizes both full and short guidance variants.
    """
    import hashlib

    guidance_path = _find_guidance_path()
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
    valid_hashes = {
        hashlib.sha256(variant.encode()).hexdigest()[:12]
        for variant in (GUIDANCE_CONTENT, SHORT_GUIDANCE_CONTENT)
    }
    if not any(f"pysmelly-guidance {h}" in content for h in valid_hashes):
        return (
            f"{guidance_path} is outdated — run `pysmelly init` to update it with "
            "the latest guidance."
        )
    return None


def _build_guidance() -> list[str]:
    """Build guidance preamble — just the PYSMELLY.md staleness/init hint.

    Run-specific guidance (test exclusions, caller-aware context) now lives
    in PYSMELLY.md (generated by ``pysmelly init``).
    """
    guidance: list[str] = []

    init_hint = _check_guidance_status()
    if init_hint:
        guidance.append(init_hint)

    return guidance


def _print_check_list() -> None:
    """Print all registered checks with severity and description."""
    name_width = max(len(name) for name in CHECKS)
    for name in CHECKS:
        severity = CHECK_SEVERITY[name].value
        category = CHECK_CATEGORIES.get(name, "ast")
        tag = " [git] (use: pysmelly git-history)" if category == "git-history" else ""
        description = CHECK_DESCRIPTIONS.get(name, "")
        click.echo(f"  {name:<{name_width}}  [{severity:<6}]{tag}  {description}")


def _discover_and_parse(roots: list[Path], excludes: list[str]) -> tuple[Path, dict[Path, object]]:
    """Discover Python files, parse them, and return (base, all_trees).

    Validates that all roots are directories (exits on error).
    """
    for root in roots:
        if not root.is_dir():
            click.echo(f"Error: {root} is not a directory", err=True)
            sys.exit(1)

    base = roots[0] if len(roots) == 1 else Path(os.path.commonpath(roots))

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


def _load_and_merge_config(
    targets: list[str],
    exclude: list[str],
    skip: list[str],
    min_severity: str,
) -> tuple[dict, list[str], list[str], str]:
    """Load config file and merge common settings.

    Returns (raw_config, merged_exclude, merged_skip, merged_min_severity).
    """
    roots = [Path(t).resolve() for t in targets]
    config_dir = roots[0] if len(roots) == 1 else Path.cwd()
    config = load_config(config_dir, set(CHECKS.keys()))

    if "exclude" in config:
        exclude = config["exclude"] + exclude
    if "skip" in config:
        skip = config["skip"] + skip
    if "min-severity" in config and min_severity == "low":
        min_severity = config["min-severity"]

    return config, exclude, skip, min_severity


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
        with contextlib.suppress(OSError):
            source_lines[file_rel] = (base / file_rel).read_text().splitlines()
    return [f for f in findings if not _is_suppressed(f, source_lines)]


def _run_checks_and_filter(
    ctx: AnalysisContext,
    checks_to_run: dict,
    min_severity: str,
    diff_ref: str | None,
    base: Path,
) -> list[Finding]:
    """Run checks, apply severity/diff filters and inline suppressions."""
    all_findings: list[Finding] = []
    for _name, check_fn in checks_to_run.items():
        all_findings.extend(check_fn(ctx))
    all_findings = _apply_filters(all_findings, min_severity, diff_ref, base)
    return _apply_suppression(all_findings, base)


def _output_and_exit(
    findings: list[Finding],
    file_count: int,
    context: list[str] | None,
    summary: bool,
    more_please: bool,
) -> None:
    """Format output and exit with appropriate code."""
    max_findings = 0 if more_please else 10
    click.echo(
        format_text(
            findings,
            file_count,
            context=context,
            summary=summary,
            max_findings=max_findings,
        )
    )
    sys.exit(1 if findings else 0)


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

**Tests should not dictate function signatures.** If the only callers using a
default value are tests, that default is not part of the production API — it's
a testing convenience. The fix is to make tests pass the argument explicitly,
not to suppress the finding because "tests use it." (Note: if the default is a
deliberate public API convenience for *future* callers, that's a valid reason
to keep it — but "tests rely on it" alone is not.)

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

SHORT_GUIDANCE_CONTENT = """\
# pysmelly — Code Smell Review Guide (Short)

> Generated by `pysmelly init --short`. Regenerate any time with that command.
> Full guidance: https://github.com/borwickatuw/pysmelly#readme

pysmelly performs **cross-file call-graph analysis** to find vestigial code
patterns that single-file linters (ruff, pylint) cannot detect.

## Severity

- **HIGH**: Fix these. Dead code, unused defaults, unreachable code.
- **MEDIUM**: Fix unless there's a specific reason not to (framework convention,
  public API). State the reason if you skip.
- **LOW**: Review and fix where it makes sense.

## Key principles

- **Default is to fix**, not to explain why the code is acceptable.
- **Do not dismiss findings by defending code in isolation** — the smell is in
  the cross-file picture (caller counts, blast radius), not the function itself.
- **Work incrementally**: fix top 1–2 highest severity, commit, re-run.

## Quick reference

```bash
uvx pysmelly                           # analyze current directory
uvx pysmelly --check dead-code         # single check
uvx pysmelly --diff main              # only findings in changes since main
uvx pysmelly git-history               # evolutionary/git history checks
```

Suppress a false positive: `x = 1  # pysmelly: ignore[check-name]`

Configure via `.pysmelly.toml` or `[tool.pysmelly]` in `pyproject.toml`.
"""

CLAUDE_MD_REFERENCE = """\

## pysmelly

Read [{path}]({path}) before running pysmelly code smell analysis on this project.
"""


def _handle_init(path_args: tuple[str, ...], *, short: bool = False) -> None:
    """Handle `pysmelly init [PATH]` — write guidance file and reference in CLAUDE.md."""
    path = Path(path_args[0]) if path_args else Path("PYSMELLY.md")

    # Create parent directories if needed
    path.parent.mkdir(parents=True, exist_ok=True)
    import hashlib

    content = SHORT_GUIDANCE_CONTENT if short else GUIDANCE_CONTENT
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    versioned = f"<!-- pysmelly-guidance {content_hash} -->\n{content}"
    path.write_text(versioned)
    click.echo(f"Wrote {path}")

    # Add reference to CLAUDE.md (idempotent)
    claude_md = Path("CLAUDE.md")
    marker = "pysmelly"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if marker in existing:
            click.echo("CLAUDE.md already references pysmelly")
            return
        with claude_md.open("a", encoding="utf-8") as f:
            f.write(CLAUDE_MD_REFERENCE.format(path=path))
    else:
        claude_md.write_text(CLAUDE_MD_REFERENCE.format(path=path).lstrip(), encoding="utf-8")
    click.echo("Added pysmelly reference to CLAUDE.md")


def _handle_reviewed(files: tuple[str, ...]) -> None:
    """Handle `pysmelly git-history reviewed <file|dir> [...]` — create acknowledgment commit."""
    if not files:
        click.echo(
            "Error: pysmelly git-history reviewed requires at least one file or directory path",
            err=True,
        )
        sys.exit(1)

    # Verify we're in a git repo
    try:
        git_root = get_git_root(Path.cwd())
    except GitNotFoundError:
        click.echo("Error: pysmelly git-history reviewed requires a git repository", err=True)
        sys.exit(1)

    # Expand directories to .py files, verify paths exist
    filepaths: list[str] = []
    for arg in files:
        p = Path(arg)
        if not p.exists():
            click.echo(f"Error: {arg} does not exist", err=True)
            sys.exit(1)
        if p.is_dir():
            py_files = sorted(str(f) for f in p.rglob("*.py"))
            if not py_files:
                click.echo(f"Warning: no .py files found in {arg}", err=True)
            filepaths.extend(py_files)
        else:
            filepaths.append(arg)

    if not filepaths:
        click.echo("Error: no files to review", err=True)
        sys.exit(1)

    # Build commit message
    markers = "\n".join(f"pysmelly: reviewed {f}" for f in filepaths)
    if len(filepaths) == 1:
        subject = f"Acknowledge pysmelly finding for {filepaths[0]}"
    elif len(files) == 1 and Path(files[0]).is_dir():
        subject = f"Acknowledge pysmelly findings for {files[0]}/ ({len(filepaths)} files)"
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
        click.echo(f"Error: git commit failed (exit {e.returncode})", err=True)
        sys.exit(1)


# AST check names (exclude git-history checks)
_AST_CHECK_NAMES = [n for n in CHECKS if n not in _GIT_HISTORY_CHECKS]
_GIT_HISTORY_CHECK_NAMES = list(
    name for name, cat in CHECK_CATEGORIES.items() if cat == "git-history"
)


class _GroupWithTargets(click.Group):
    """Click Group that accepts positional target arguments alongside subcommands.

    Standard Click groups treat the first positional arg as a subcommand name
    and fail with "No such command" for directory paths. This subclass
    intercepts parsing: when the first positional arg is not a known subcommand,
    it collects all positional args in ctx.args as directory targets and invokes
    the group callback (invoke_without_command behavior).

    Group callbacks should read targets from ``ctx.args`` instead of declaring
    a ``click.argument``.
    """

    def parse_args(self, ctx, args):
        # Find the first positional (non-option) token.
        # If it is a known subcommand, let Click route normally.
        # If not, stash everything in ctx.args and skip subcommand resolution.
        first_pos_idx = None
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                break
            if arg.startswith("-"):
                # Check if this option expects a value (skip next arg if so).
                # We detect this by checking the group's declared options.
                param = self._find_param(arg)
                if param is not None and not param.is_flag:
                    i += 1  # skip the option's value
                i += 1
                continue
            first_pos_idx = i
            break

        if first_pos_idx is not None and args[first_pos_idx] in self.commands:
            # It's a real subcommand — let Click handle it normally
            return super().parse_args(ctx, args)

        # No subcommand found. Parse only the options (everything before and
        # after the first positional), and put positional args in ctx.args.
        # Split args into options and positional targets.
        opts = []
        targets = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                targets.extend(args[i + 1 :])
                break
            if arg.startswith("-"):
                opts.append(arg)
                # If the option takes a value and it's not joined with =,
                # consume the next token as the value.
                opt_key = arg.split("=", 1)[0] if "=" in arg else arg
                param = self._find_param(opt_key)
                if param is not None and not param.is_flag and "=" not in arg and i + 1 < len(args):
                    i += 1
                    opts.append(args[i])
            else:
                targets.append(arg)
            i += 1

        # Let Click parse just the options
        super().parse_args(ctx, opts)
        # Stash targets for the callback to read
        ctx.args = targets
        return args

    def _find_param(self, opt_string: str) -> click.Parameter | None:
        """Look up a parameter by its option string (e.g., '--check')."""
        for param in self.params:
            if isinstance(param, click.Option) and opt_string in param.opts:
                return param
            if isinstance(param, click.Option) and opt_string in param.secondary_opts:
                return param
        return None


@click.group(cls=_GroupWithTargets, invoke_without_command=True)
@click.version_option(version=_get_version(), prog_name="pysmelly")
@click.option(
    "--check",
    type=click.Choice(_AST_CHECK_NAMES, case_sensitive=True),
    default=None,
    help="Run only this check (see --list-checks)",
)
@click.option("--skip", multiple=True, help="Skip this check (can be repeated)")
@click.option(
    "--exclude",
    multiple=True,
    help="Exclude files matching pattern (repeatable; 'test_*' for names, 'path/to/dir/' for directories)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show additional detail")
@click.option(
    "--min-severity",
    type=click.Choice(["low", "medium", "high"]),
    default="low",
    help="Minimum severity to report (default: low)",
)
@click.option(
    "--diff",
    "diff_ref",
    default=None,
    metavar="REF",
    help="Only report findings in lines changed since REF (e.g., --diff HEAD)",
)
@click.option(
    "--no-context",
    is_flag=True,
    help="Suppress the guidance preamble (on by default for LLM consumers)",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Show finding counts per check without individual findings",
)
@click.option(
    "--list-checks",
    is_flag=True,
    help="List all available checks with descriptions and exit",
)
@click.option(
    "--more-please/--no-more-please",
    "--all",
    is_flag=True,
    default=False,
    help="Show all findings (default: top 10 highest-confidence)",
)
@click.pass_context
def cli(
    ctx,
    check,
    skip,
    exclude,
    verbose,
    min_severity,
    diff_ref,
    no_context,
    summary,
    list_checks,
    more_please,
):
    """AST-based Python code smell detector."""
    # If a subcommand is being invoked, do nothing here
    if ctx.invoked_subcommand is not None:
        return

    if list_checks:
        _print_check_list()
        return

    # Targets come from ctx.args (positional args collected by _GroupWithTargets)
    targets = ctx.args or ["."]

    # Convert tuples to lists for merging
    exclude = list(exclude)
    skip = list(skip)

    # Load config file and merge with CLI args
    config, exclude, skip, min_severity = _load_and_merge_config(
        targets, exclude, skip, min_severity
    )
    if "check" in config and check is None:
        check_name = config["check"]
        if CHECK_CATEGORIES.get(check_name) == "git-history":
            click.echo(
                f"Error: {check_name} is a git-history check. "
                f"Use: pysmelly git-history --check {check_name}",
                err=True,
            )
            sys.exit(1)
        check = check_name

    roots = [Path(t).resolve() for t in targets]
    base, all_trees = _discover_and_parse(roots, exclude)

    # Determine which checks to run (exclude git-history checks)
    if check:
        checks_to_run = {check: CHECKS[check]}
    else:
        checks_to_run = {
            name: fn
            for name, fn in CHECKS.items()
            if name not in skip and CHECK_CATEGORIES.get(name, "ast") != "git-history"
        }

    # Run checks
    analysis_ctx = AnalysisContext(all_trees, verbose)
    all_findings = _run_checks_and_filter(analysis_ctx, checks_to_run, min_severity, diff_ref, base)

    # Build guidance preamble for LLM consumers (on by default)
    context: list[str] | None = None
    if not no_context:
        context = _build_guidance()

    _output_and_exit(all_findings, len(all_trees), context, summary, more_please)


@cli.command("init")
@click.option(
    "--short",
    is_flag=True,
    help="Generate a ~30-line summary instead of the full guidance file",
)
@click.argument("path", nargs=-1)
def init_cmd(short, path):
    """Write PYSMELLY.md guidance file for AI code review."""
    _handle_init(path, short=short)


@cli.group("git-history", cls=_GroupWithTargets, invoke_without_command=True)
@click.option(
    "--check",
    type=click.Choice(_GIT_HISTORY_CHECK_NAMES, case_sensitive=True),
    default=None,
    metavar="CHECK",
    help="Run only this check",
)
@click.option("--skip", multiple=True, help="Skip this check (can be repeated)")
@click.option(
    "--exclude",
    multiple=True,
    help="Exclude files matching pattern (repeatable)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show additional detail")
@click.option(
    "--min-severity",
    type=click.Choice(["low", "medium", "high"]),
    default="low",
    help="Minimum severity to report (default: low)",
)
@click.option(
    "--no-context",
    is_flag=True,
    help="Suppress the guidance preamble",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Show finding counts per check without individual findings",
)
@click.option(
    "--more-please/--no-more-please",
    "--all",
    is_flag=True,
    default=False,
    help="Show all findings (default: top 10 highest-confidence)",
)
@click.option(
    "--window",
    default="6m",
    metavar="PERIOD",
    help="Time window for git history analysis (default: 6m; e.g., 3m, 1y, 90d)",
)
@click.option(
    "--commit-messages",
    type=click.Choice([DEFAULT_COMMIT_MESSAGES, "structured", "unstructured"]),
    default=DEFAULT_COMMIT_MESSAGES,
    help="Commit message quality (default: auto-detect)",
)
@click.option(
    "--ignore-reviewed",
    is_flag=True,
    help="Analyze full history, ignoring any 'pysmelly: reviewed' markers",
)
@click.pass_context
def git_history_group(
    ctx,
    check,
    skip,
    exclude,
    verbose,
    min_severity,
    no_context,
    summary,
    more_please,
    window,
    commit_messages,
    ignore_reviewed,
):
    """Analyze git history for structural and evolutionary code smells."""
    # If a subcommand (like 'reviewed') is being invoked, do nothing here
    if ctx.invoked_subcommand is not None:
        return

    # Targets come from ctx.args (positional args collected by _GroupWithTargets)
    targets = ctx.args or ["."]

    # Convert tuples to lists for merging
    exclude = list(exclude)
    skip = list(skip)

    # Load config and merge
    config, exclude, skip, min_severity = _load_and_merge_config(
        targets, exclude, skip, min_severity
    )
    if "git-window" in config and window == "6m":
        window = config["git-window"]
    if "commit-messages" in config and commit_messages == DEFAULT_COMMIT_MESSAGES:
        commit_messages = config["commit-messages"]

    # Must be in a git repo
    roots = [Path(t).resolve() for t in targets]
    base, all_trees = _discover_and_parse(roots, exclude)
    try:
        git_root = get_git_root(base)
    except GitNotFoundError:
        click.echo("Error: pysmelly git-history requires a git repository", err=True)
        sys.exit(1)

    # Determine which checks to run
    git_history_checks = {
        name: fn for name, fn in CHECKS.items() if CHECK_CATEGORIES.get(name) == "git-history"
    }
    if check:
        checks_to_run = {check: git_history_checks[check]}
    else:
        checks_to_run = {name: fn for name, fn in git_history_checks.items() if name not in skip}

    # Run checks
    expected_coupling = config.get("expected-coupling", [])
    analysis_ctx = AnalysisContext(
        all_trees,
        verbose,
        git_root=git_root,
        git_window=window,
        commit_messages=commit_messages,
        expected_coupling=expected_coupling,
    )
    if ignore_reviewed and analysis_ctx.git_history is not None:
        analysis_ctx.git_history.reviewed_at = {}
    all_findings = _run_checks_and_filter(analysis_ctx, checks_to_run, min_severity, None, base)

    # Guidance — run-specific hints only; generic guidance lives in PYSMELLY.md
    context: list[str] | None = None
    if not no_context:
        context = []
        init_hint = _check_guidance_status()
        if init_hint:
            context.append(init_hint)

    _output_and_exit(all_findings, len(all_trees), context, summary, more_please)


@git_history_group.command("reviewed")
@click.argument("files", nargs=-1)
def reviewed_cmd(files):
    """Acknowledge git history findings by creating a review marker commit."""
    _handle_reviewed(files)


def main(argv: list[str] | None = None) -> None:
    """Entry point wrapper for backward compatibility with tests."""
    cli(argv, standalone_mode=True)


if __name__ == "__main__":
    main()
