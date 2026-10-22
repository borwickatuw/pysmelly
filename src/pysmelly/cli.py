"""CLI entry point for pysmelly."""

import ast
import contextlib
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click

# Import checks to trigger registration
import pysmelly.checks  # noqa: F401
from pysmelly import __version__
from pysmelly.catalog import EMPTY_CATALOG
from pysmelly.chains import chain_findings
from pysmelly.compare import (
    Analysis,
    Analyze,
    CompareError,
    format_comparison,
    run_comparison,
)
from pysmelly.config import CATALOG_KEY, EXPECTED_COUPLING_KEY, load_config
from pysmelly.context import DEFAULT_COMMIT_MESSAGES, AnalysisContext, GitSettings
from pysmelly.discovery import (
    GitDiffError,
    GitListError,
    GitNotFoundError,
    get_changed_lines,
    get_git_root,
    get_python_files,
    get_template_files,
    git_lists,
    parse_file,
    walk_note,
)
from pysmelly.git_history import GitHistoryQueryError, describe_window
from pysmelly.output import format_guidance, format_text, printed_findings
from pysmelly.project_entry_points import (
    DeclaredEntryPoint,
    ProjectEntryPointError,
    load_project_entry_points,
)
from pysmelly.provenance import ProvenanceReader, with_notes
from pysmelly.pytest_config import PytestConfig, PytestConfigError, load_pytest_config
from pysmelly.registry import (
    CHECK_CATEGORIES,
    CHECK_DESCRIPTIONS,
    CHECK_DISMISSALS,
    CHECK_SEVERITY,
    CHECKS,
    GIT_HISTORY_CATEGORY,
    UNPAIRED,
    Finding,
    Severity,
    Site,
    SiteRender,
    SitesError,
    may_anchor,
    run_check,
)
from pysmelly.templates import attribute_names
from pysmelly.trends import project_trends

_GIT_HISTORY_CHECKS = {
    name for name, cat in CHECK_CATEGORIES.items() if cat == GIT_HISTORY_CATEGORY
}


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
    """Check if a finding's anchor line is suppressed by an inline comment."""
    return _line_suppressed(finding.check, finding.file, finding.line, source_lines)


def _line_suppressed(
    check: str, file: str, lineno: int, source_lines: dict[str, list[str]]
) -> bool:
    """Whether an inline comment on ``lineno`` of ``file``, or the line above,
    suppresses ``check``."""
    lines = source_lines.get(file)
    if not lines:
        return False

    # Check the finding's line and the line above it
    for idx in (lineno - 1, lineno - 2):
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
            if check in check_names:
                return True
    return False


def _narrow_sites(
    finding: Finding, source_lines: dict[str, list[str]], ctx: AnalysisContext
) -> Finding | None:
    """A multi-site finding without the sites an inline ignore suppresses.

    Each ignore speaks for its own line only, so it removes its site from the
    group and leaves the rest reported. The message is re-rendered from the
    sites left, with a count of the ones removed. The anchor stays when its
    site is left (or when it is the subject's definition rather than a site),
    and otherwise moves to the first site left that the check may anchor on
    (``may_anchor``). None when no such site is left, or when the sites left
    no longer make a finding by the check's own rule (``render`` says so).
    """
    kept = tuple(
        index
        for index, site in enumerate(finding.sites)
        if not _line_suppressed(finding.check, site.file, site.line, source_lines)
    )
    if len(kept) == len(finding.sites):
        return finding
    if not kept:
        return None
    left = [finding.sites[index] for index in kept]
    anchor = Site(finding.file, finding.line)
    if anchor in finding.sites and anchor not in left:
        anchors = [site for site in left if may_anchor(finding.check, site.file, ctx)]
        if not anchors:
            return None
        anchor = anchors[0]
    if finding.render is None:  # Finding.__post_init__ pairs it with sites
        raise SitesError(finding.check, UNPAIRED)
    message = finding.render(kept)
    if message is None:
        return None
    removed = len(finding.sites) - len(kept)
    return Finding(
        file=anchor.file,
        line=anchor.line,
        check=finding.check,
        message=f"{message} ({removed} other site{'s' if removed != 1 else ''} ignored)",
        severity=finding.severity,
        sites=tuple(left),
        render=_reindexed(finding.render, kept),
        subjects=finding.subjects,
    )


def _reindexed(render: SiteRender, kept: tuple[int, ...]) -> SiteRender:
    """``render`` for a finding narrowed to the original sites ``kept``."""
    return lambda indices: render(tuple(kept[index] for index in indices))


# Checks that use cross-file caller analysis — findings are affected by file exclusions


# Text that stays inside one Markdown paragraph: any character, line breaks
# included, as long as it does not open a blank line.
_SAME_PARAGRAPH = r"(?:(?!\n[ \t]*\n)[\s\S])"

# The one marker that means "CLAUDE.md carries a guidance reference". Both the
# reader (_find_guidance_path) and the writer (_handle_init) key on it, so a
# CLAUDE.md that merely mentions the word "pysmelly" — a `make pysmelly` target,
# or pysmelly's own repo — still gets a reference written.
#
# The shape is `Read [..](PATH)`, then — in the same paragraph, across line
# breaks — `before running` and `pysmelly` (bare, backticked, or `make
# pysmelly`). That covers the sentence `pysmelly init` writes and the fleet's
# reworded, reflowed one ("Read [docs/PYSMELLY.md](docs/PYSMELLY.md) — findings
# register and review conventions — before running `make pysmelly`."). No other
# link may sit between PATH and `before running`, so PATH is the link the
# sentence is about; text between `before running` and `pysmelly` stays inside
# one sentence (no period).
GUIDANCE_REFERENCE_RE = re.compile(
    r"Read \[[^\]\n]*\]\(([^)\s]+)\)"
    rf"(?:(?!\]\(){_SAME_PARAGRAPH})*?"
    r"\bbefore\s+running\b"
    rf"(?:(?!\.){_SAME_PARAGRAPH})*?"
    r"\bpysmelly\b"
)

# Any Markdown link to a file named PYSMELLY.md — used only to tell "CLAUDE.md
# has no guidance reference" apart from "it has one pysmelly cannot parse".
_GUIDANCE_LINK_RE = re.compile(r"\]\(([^)\s]*PYSMELLY\.md)\)")


class UnparseableGuidanceReferenceError(Exception):
    """CLAUDE.md links a PYSMELLY.md but in no shape pysmelly reads as a reference."""

    def __init__(self, link: str) -> None:
        super().__init__(link)
        self.link = link


def _find_guidance_path() -> Path:
    """Find the guidance file path from CLAUDE.md's reference.

    Without a CLAUDE.md, or with one that never links a PYSMELLY.md, the
    guidance file is `pysmelly init`'s default, ./PYSMELLY.md.

    Raises:
        UnparseableGuidanceReferenceError: CLAUDE.md links a PYSMELLY.md but
            matches no reference shape. Guessing a location here is what made
            pysmelly tell repos with a guidance register to run `init` over it.
    """
    claude_md = Path("CLAUDE.md")
    if claude_md.exists():
        try:
            text = claude_md.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            m = GUIDANCE_REFERENCE_RE.search(text)
            if m:
                return Path(m.group(1))
            link = _GUIDANCE_LINK_RE.search(text)
            if link:
                raise UnparseableGuidanceReferenceError(link.group(1))

    return Path("PYSMELLY.md")


def _check_guidance_status() -> str | None:
    """Check if pysmelly guidance file exists and is current.

    Recognizes both full and short guidance variants.

    Returns:
        A suggestion string, or None only when the guidance file was read and
        is current. An unreadable guidance file is reported as a suggestion,
        never as "up to date".
    """
    try:
        guidance_path = _find_guidance_path()
    except UnparseableGuidanceReferenceError as err:
        return (
            f"CLAUDE.md links {err.link} but pysmelly cannot read the reference; "
            "use the sentence pysmelly init writes: "
            + GUIDANCE_REFERENCE_SENTENCE.format(path=err.link)
        )
    if not guidance_path.exists():
        return (
            "Run `pysmelly init` to set up AI review guidance for this project. "
            "This creates PYSMELLY.md with detailed instructions on how to "
            "act on findings."
        )
    try:
        content = guidance_path.read_text(encoding="utf-8")
    except OSError as err:
        return f"{guidance_path} could not be read ({err.strerror}) — run `pysmelly init`."
    valid_hashes = {
        hashlib.sha256(variant.encode()).hexdigest()[:12]
        for variant in (GUIDANCE_CONTENT, SHORT_GUIDANCE_CONTENT)
    }
    if not any(f"pysmelly-guidance {h}" in content for h in valid_hashes):
        try:
            _generated_block_span(content)
        except GuidanceMarkerError as err:
            return (
                f"{guidance_path} is outdated, but `pysmelly init` will refuse to "
                f"update it: {err}. Fix that and run `pysmelly init`; "
                "`pysmelly init --force` replaces the whole file."
            )
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
    """Print all registered checks with severity and description, each with its dismissal under it."""
    name_width = max(len(name) for name in CHECKS)
    for name in CHECKS:
        severity = CHECK_SEVERITY[name].value
        category = CHECK_CATEGORIES.get(name, "ast")
        tag = " [git] (use: pysmelly git-history)" if category == GIT_HISTORY_CATEGORY else ""
        description = CHECK_DESCRIPTIONS.get(name, "")
        click.echo(f"  {name:<{name_width}}  [{severity:<6}]{tag}  {description}")
        click.echo(f"  {'':<{name_width}}  {CHECK_DISMISSALS[name]}")


def _discover_and_parse(
    roots: list[Path], excludes: list[str]
) -> tuple[Path, dict[Path, ast.Module], frozenset[Path]]:
    """Discover Python files, parse them, and return (base, all_trees, shebang_files),
    the last being the files whose line 1 is a ``#!`` line.

    Validates that all roots are directories (exits on error). A root git
    cannot list is walked, with one stderr note saying so; one in a
    repository whose ``git ls-files`` fails exits 1.
    """
    for root in roots:
        if not root.is_dir():
            click.echo(f"Error: {root} is not a directory", err=True)
            sys.exit(1)

    base = roots[0] if len(roots) == 1 else Path(os.path.commonpath(roots))

    all_trees: dict[Path, ast.Module] = {}
    shebang_files: set[Path] = set()
    for root in roots:
        if not git_lists(root):
            click.echo(walk_note(root), err=True)
        for f in _listed(get_python_files, root):
            rel = f.relative_to(base)
            if _is_excluded(rel, excludes):
                continue
            parsed = parse_file(f)
            if parsed is None:
                continue
            all_trees[rel] = parsed.tree
            if parsed.shebang:
                shebang_files.add(rel)

    return base, all_trees, frozenset(shebang_files)


def _template_attributes(roots: list[Path], base: Path, excludes: list[str]) -> frozenset[str]:
    """Every name the templates under ``roots`` read after a dot.

    A file in a ``templates`` directory that is not UTF-8 text (an image, a
    font) is not a template, and is passed over.
    """
    names: set[str] = set()
    for root in roots:
        for path in _listed(get_template_files, root):
            if _is_excluded(path.relative_to(base), excludes):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            names |= attribute_names(text)
    return frozenset(names)


def _listed(lister: Callable[[Path], list[Path]], root: Path) -> list[Path]:
    """``lister(root)``; a repository git cannot list exits 1 with git's error."""
    try:
        return lister(root)
    except GitListError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)


def _config_dir(roots: list[Path]) -> Path:
    """The directory config is read from: the single root, or the current directory."""
    return roots[0] if len(roots) == 1 else Path.cwd()


def _load_pytest_config(roots: list[Path], base: Path) -> PytestConfig:
    """The pytest config beside pysmelly's own; a malformed one exits 1."""
    try:
        return load_pytest_config(_config_dir(roots), base)
    except PytestConfigError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)


def _load_declared_entry_points(roots: list[Path]) -> tuple[DeclaredEntryPoint, ...]:
    """The entry points pyproject.toml declares beside pysmelly's config; a malformed one exits 1."""
    try:
        return load_project_entry_points(_config_dir(roots))
    except ProjectEntryPointError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)


def _ast_context(
    roots: list[Path],
    base: Path,
    all_trees: dict[Path, ast.Module],
    shebang_files: frozenset[Path],
    verbose: bool,
    config: dict,
    excludes: list[str],
) -> AnalysisContext:
    """The AST pipeline's context: the trees, the pytest config beside
    pysmelly's own, and the project's ``catalog`` entries."""
    ctx = AnalysisContext(
        all_trees,
        verbose,
        pytest_config=_load_pytest_config(roots, base),
        user_catalog=config.get(CATALOG_KEY, EMPTY_CATALOG),
        declared_entry_points=_load_declared_entry_points(roots),
    )
    ctx.template_attributes = _template_attributes(roots, base, excludes)
    ctx.shebang_files = shebang_files
    return ctx


def _verbose_header(ctx: AnalysisContext) -> list[str]:
    """Under ``-v``, the test-role files and why each took the role, then the
    entry-point roots and why each is one; nothing otherwise."""
    if not ctx.verbose:
        return []
    files = ctx.roles.files
    source = ctx.pytest_config.source or "none (pytest defaults)"
    lines = [f"Test role: {len(files)} file(s); pytest config: {source}"]
    lines.extend(f"  {path}: {files[path]}" for path in sorted(files))
    lines.extend(ctx.entry_points.describe())
    return lines


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
    config = load_config(_config_dir(roots), set(CHECKS.keys()))

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
        try:
            changed = get_changed_lines(diff_ref, git_root)
        except GitDiffError as err:
            click.echo(f"Error: {err}", err=True)
            sys.exit(1)
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


def _apply_suppression(findings: list[Finding], base: Path, ctx: AnalysisContext) -> list[Finding]:
    """Remove findings suppressed by inline comments, and the suppressed
    sites of multi-site findings (``_narrow_sites``)."""
    source_lines: dict[str, list[str]] = {}
    files_with_findings = {f.file for f in findings} | {
        site.file for f in findings for site in f.sites
    }
    for file_rel in files_with_findings:
        with contextlib.suppress(OSError):
            source_lines[file_rel] = (base / file_rel).read_text(encoding="utf-8").splitlines()
    kept: list[Finding] = []
    for finding in findings:
        if Site(finding.file, finding.line) in finding.sites:
            # Anchored on a peer site: an ignore there removes only that site.
            narrowed = _narrow_sites(finding, source_lines, ctx)
        elif _is_suppressed(finding, source_lines):
            narrowed = None
        elif finding.sites:
            narrowed = _narrow_sites(finding, source_lines, ctx)
        else:
            narrowed = finding
        if narrowed is not None:
            kept.append(narrowed)
    return kept


def _run_checks_and_filter(
    ctx: AnalysisContext,
    checks_to_run: list[str],
    min_severity: str,
    diff_ref: str | None,
    base: Path,
) -> list[Finding]:
    """Run checks, apply inline suppressions, then severity/diff filters,
    then join the findings left into chains (``chains.chain_findings``).

    Suppression runs first because it can move a multi-site finding's
    anchor, and the diff filter reads the anchor. Chains come last, so a
    chain holds only findings that are reported.
    """
    all_findings: list[Finding] = []
    for name in checks_to_run:
        all_findings.extend(run_check(name, ctx))
    all_findings = _apply_suppression(all_findings, base, ctx)
    return chain_findings(ctx, _apply_filters(all_findings, min_severity, diff_ref, base))


def _output_and_exit(
    findings: list[Finding],
    file_count: int,
    context: list[str] | None,
    summary: bool,
    more_please: bool,
    *,
    header: list[str],
    provenance: ProvenanceReader,
) -> None:
    """Format output and exit with appropriate code.

    A git query behind a provenance line that fails is an error (exit 1),
    as one behind the trends block is, never a line left out.
    """
    max_findings = 0 if more_please else 10
    printed = printed_findings(findings, summary=summary, max_findings=max_findings)
    try:
        notes = provenance(printed)
    except GitHistoryQueryError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)
    click.echo(
        format_text(
            with_notes(findings, printed, notes),
            file_count,
            context=context,
            summary=summary,
            max_findings=max_findings,
            header=header,
        )
    )
    sys.exit(1 if findings else 0)


GUIDANCE_CONTENT = """\
# pysmelly — Code Smell Review Guide

> This block is generated by `pysmelly init` and can be safely regenerated:
> re-running it replaces only the block and keeps everything below the end
> marker (the closing pysmelly-guidance comment), such as a findings register.
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
uvx pysmelly --check dead-code --check middle-man   # repeat for several
uvx pysmelly --min-severity medium     # filter noise
uvx pysmelly --exclude 'vendor/' --exclude '*_pb2.py'   # one pattern per flag
uvx pysmelly --diff HEAD                # findings in uncommitted changes only
uvx pysmelly --diff main                # findings in changes since main
uvx pysmelly --compare main             # findings resolved and new since main
uvx pysmelly --list-checks              # every check, with its dismissal condition
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
markers. Most git-history checks will then only analyze commits **after** the
review date for that file, and `abandoned-code` treats the marker as a touch.
This effectively resets the analysis window — if the underlying
problem was fixed, the finding disappears. If the pattern persists in new
commits, the finding returns marked `reviewed YYYY-MM-DD, pattern persists`.
A pair check (change-coupling, fix-propagation) counts only commits after
the later of its two files' markers, and only when both files carry one, so
review both files of a pair to acknowledge it. The blame-age checks
(`frozen-feature-flag`, `fossil-temporary`, `dual-stack-dependency`,
`bypassed-helper-history`, `stalled-migration`) read line ages at HEAD and
ignore it too: acknowledging one of those means changing the line.

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

Write the directive on its own full line, directly above the line the finding
points at, with the rationale in the comment lines above it:

```python
# Registered by dotted-path string in settings.py, so the call graph
# cannot see the caller.
# pysmelly: ignore[dead-code]
def handle_event(payload):
    ...
```

A bare `# pysmelly: ignore` suppresses every check on the line below it; the
bracketed form suppresses only the checks named, comma-separated.

**The window is exactly two lines: the finding's own line and the single line
directly above it.** A rationale line between the directive and the finding
silently breaks the suppression, and on a decorated definition the directive
must sit between the last decorator and the `def`/`class`.

**Do not write the trailing form** (`x = 1  # pysmelly: ignore[dead-code]`).
pysmelly still honours it, but the next formatter run can rewrap a long line
and carry the comment off the finding's line, and the suppression then dies
while still looking present — the way 40 of one repo's 105 directives went
inert.

Ruff's `ERA001` (commented-out code) reads a bare bracketed directive as a
subscript expression. Where that rule is in effect for the file, add
`# noqa: ERA001` after the directive; where it is not, leave it off — a
`noqa` that suppresses nothing is itself a dead comment.

**Do not add suppression comments as a way to dismiss findings.** Suppression
is for a finding whose `Dismiss only` condition holds (framework calls this
via reflection, public API contract requires this signature), and its
rationale says how. If a finding reveals unfinished code
(TODO), finish it. If a finding reveals dead code, delete it. Adding
`# pysmelly: ignore` to avoid fixing the code is not suppression — it's
avoidance.

## Configuration

Create `.pysmelly.toml` or add `[tool.pysmelly]` to `pyproject.toml`:

```toml
exclude = ["vendor/", "*_pb2.py"]
skip = ["single-call-site"]
min-severity = "medium"
```

CLI arguments extend list values and override scalar values.

Do not exclude tests. pysmelly parses them as evidence: a call from a test
counts as a test caller, and no finding is reported inside a test file
(except by the checks about tests). Which files are tests comes from
pytest's own `testpaths` and `python_files` settings and from what the code
declares (a `unittest.TestCase` subclass, a `@pytest.fixture`), never from a
filename alone. `exclude` means "never look": vendored or generated code.

## Reading a finding

Every finding's message ends with two sentences, and they settle what to do
with it:

- **`Fix:`** names the edit. The message before it names the sites the edit
  touches: the callers, files and lines. Make that edit at every site named.
  Where a check cannot name its edit, the fix says what to look at; look,
  then edit.
- **`Dismiss only ...`** names the one condition under which the finding is
  not worth fixing. If that condition holds, suppress the finding and say in
  the rationale how it holds (name the outside caller, the hook, the
  standard). If it does not hold, fix. Nothing else dismisses a finding:
  "it works", "it is readable", "it might be needed later" and "the tests
  use it" are not the condition, so they are not arguments.
  `pysmelly --list-checks` prints each check's condition.

**Provenance.** A finding about something vestigial (a function nobody
calls, a constant nobody reads, a parameter the body ignores or every caller
fills in) may carry a line under it from git:

```text
provenance of timeout: added in a1b2c3d (feat: retry on timeout), last production reader removed in d4e5f6a (refactor: drop the retry loop)
```

It is the story of how the code became vestigial: the commit that added the
name, and the commit that removed its last production reader. The design
that needed the code is gone, so the code goes too. The line is printed
only when git proves it, so a finding without one is not evidence that the
code is in use.

**Finding chains.** One vestigial parameter surfaces as several findings
across the layers that thread it: an unused default in one function, a
pass-through in the next, a parameter no caller passes further up. Deleting
a dead function resolves the findings in everything its removal frees.
pysmelly prints such findings together, once, under `=== finding chains ===`,
each chain titled with its one edit ("delete X and the N definition(s) its
removal frees", "parameter chain over 3 function(s): ..."). **Work a chain
as one item**: make the one edit at every touchpoint it lists, in one
commit, then re-run. Do not fix one member and defend the rest. Every
member still counts in the totals and in `--summary`.

**Checking your work.** `pysmelly --compare main` (any ref works) runs the
same checks on that ref in a temporary git worktree and lists what was
resolved and what is new since then. Findings are matched on check, file and
enclosing definition, not line. It exits 1 when anything is new, so after a
refactoring it shows what you fixed and that you introduced nothing.

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

**If you do not fix a finding, it is because its `Dismiss only` condition
holds.** Say how it holds, in the suppression's rationale: the framework
that dictates the signature, the public API contract, the reflection or
dispatch that calls it. "The code is readable," "it might be useful later"
and "the abstraction makes sense in isolation" meet no check's condition.

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
and often require design-level thinking rather than quick fixes. They end
with `Fix:` and `Dismiss only` sentences too. When the dismissal condition
holds, acknowledge the file with a review marker (see "Git history
analysis" above), in a commit whose message says how it holds.

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

> Generated by `pysmelly init --short`. Regenerate any time with that command;
> everything below the end marker (the closing pysmelly-guidance comment) is kept.
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
- **Act on `Fix:`.** Every finding ends with the edit to make and the one
  condition that dismisses it (`Dismiss only ...`). Dismiss only when that
  condition holds, and say how in the ignore's rationale; "it works" is not
  the condition.
- **Work a finding chain as one item.** Findings one edit resolves print
  together under `=== finding chains ===`; make that edit at every
  touchpoint listed, in one commit.
- **A provenance line** (`added in <sha> (...), last production reader removed in <sha> (...)`)
  is git's record of how the code became vestigial: its design is gone.
- **Work incrementally**: fix top 1–2 highest severity, commit, re-run.
  `uvx pysmelly --compare main` lists what you resolved and anything new.

## Quick reference

```bash
uvx pysmelly                           # analyze current directory
uvx pysmelly --check dead-code         # single check
uvx pysmelly --diff main               # only findings in changes since main
uvx pysmelly --compare main            # findings resolved and new since main
uvx pysmelly git-history               # evolutionary/git history checks
```

Suppress a false positive with a full-line directive on the line *above* the
finding — never a trailing comment, which a formatter rewrap silently kills:

```python
# Why this one is a false positive.
# pysmelly: ignore[check-name]
x = 1
```

Configure via `.pysmelly.toml` or `[tool.pysmelly]` in `pyproject.toml`.
"""

GUIDANCE_REFERENCE_SENTENCE = (
    "Read [{path}]({path}) before running pysmelly code smell analysis on this project."
)

CLAUDE_MD_REFERENCE = f"""\

## pysmelly

{GUIDANCE_REFERENCE_SENTENCE}
"""


# The generated block of a guidance file runs from the start marker (which
# carries the template hash _check_guidance_status compares) to the end marker.
# Everything after the end marker belongs to the repo — PYSMELLY-REVIEW
# Practice 8a keeps the findings register there — and `init` never touches it.
# A marker counts only alone on its line: registers quote it in prose (s3-bagit:
# "Keep the `<!-- pysmelly-guidance ... -->` marker line").
#
# init before 21-6 wrote no end marker: its file was the start marker line, then
# the template whose hash that marker carries (mdformat has since put one blank
# line between them in many copies). For such a file the hash is the proof of
# where the generated block ends — see _hash_proven_block_end.
_GUIDANCE_START_RE = re.compile(r"^<!-- pysmelly-guidance ([^\s>]+) -->$", re.MULTILINE)
GUIDANCE_END_MARKER = "<!-- /pysmelly-guidance -->"
_GUIDANCE_END_RE = re.compile(rf"^{re.escape(GUIDANCE_END_MARKER)}$", re.MULTILINE)


def _guidance_block(content: str) -> str:
    """The generated block for one template: start marker, template, end marker."""
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"<!-- pysmelly-guidance {content_hash} -->\n\n{content}\n{GUIDANCE_END_MARKER}"


class GuidanceMarkerError(Exception):
    """A guidance file has no single intact start/end marker pair to splice into.

    The message completes "<path> exists and ...".
    """


@dataclass(frozen=True)
class GeneratedBlock:
    """Where the generated block sits in a guidance file.

    ``start`` is the start marker's offset. ``end`` is just past the end marker
    or, for a file written before the end marker existed (``end_proven_by_hash``),
    the offset of the newline ending the template text the start marker's hash
    proves — so ``text[end:]`` is what followed the block either way.
    """

    start: int
    end: int
    end_proven_by_hash: bool


def _hash_proven_block_end(text: str, start: re.Match[str]) -> int:
    """The end of a pre-21-6 generated block, proven by its start marker's hash.

    The block is the text after the start marker line, past at most one blank
    line, up to a line end where its sha256 prefix equals the marker's hash.
    Returns the offset of that line's newline.

    Raises:
        GuidanceMarkerError: no line end, or more than one, closes such a prefix.
    """
    marker_hash = start.group(1)
    body = start.end() + 1
    origins = [body, body + 1] if text.startswith("\n", body) else [body]
    proven: list[int] = []
    for origin in origins:
        digest = hashlib.sha256()
        line_start = origin
        while (newline := text.find("\n", line_start)) != -1:
            digest.update(text[line_start : newline + 1].encode())
            if digest.hexdigest()[:12] == marker_hash:
                proven.append(newline)
            line_start = newline + 1
    if not proven:
        raise GuidanceMarkerError(
            f"it has no {GUIDANCE_END_MARKER} end marker, and the text after its start "
            "marker no longer hashes to that marker: the generated block was edited "
            "since pysmelly init generated it, so where it ends cannot be verified; put "
            f"{GUIDANCE_END_MARKER}, on its own line, where the generated block ends and "
            "everything below it is kept"
        )
    if len(proven) > 1:
        raise GuidanceMarkerError(
            f"it has no {GUIDANCE_END_MARKER} end marker, and the text after its start "
            "marker hashes to that marker at more than one line, so where the generated "
            f"block ends is ambiguous; put {GUIDANCE_END_MARKER}, on its own line, where "
            "the generated block ends and everything below it is kept"
        )
    return proven[0]


def _generated_block_span(text: str) -> GeneratedBlock:
    """Locate the generated block in a guidance file, start marker to end marker inclusive.

    A file with a start marker and no end marker was written before the end
    marker existed; its block ends where the start marker's hash proves it does.

    Raises:
        GuidanceMarkerError: the markers are missing, repeated or out of order,
            or a block without an end marker no longer matches its hash.
    """
    starts = list(_GUIDANCE_START_RE.finditer(text))
    ends = [m.start() for m in _GUIDANCE_END_RE.finditer(text)]
    if not starts:
        raise GuidanceMarkerError(
            "it has no pysmelly-guidance start marker, so pysmelly init did not write it"
        )
    if len(starts) > 1 or len(ends) > 1:
        raise GuidanceMarkerError(
            "it has more than one pysmelly-guidance marker, so the generated block is ambiguous"
        )
    if not ends:
        return GeneratedBlock(
            starts[0].start(), _hash_proven_block_end(text, starts[0]), end_proven_by_hash=True
        )
    start, end = starts[0].start(), ends[0]
    if end < start:
        raise GuidanceMarkerError("its end marker comes before its start marker")
    return GeneratedBlock(start, end + len(GUIDANCE_END_MARKER), end_proven_by_hash=False)


def _render_guidance_file(path: Path, content: str, *, force: bool) -> tuple[str, bool]:
    """The text `init` writes to ``path``: a fresh file, or the old one with its block replaced.

    Returns:
        The text, and whether the end marker is new: the old block had none, and
        its end was proven by its start marker's hash.

    Raises:
        click.ClickException: ``path`` exists without one intact marker pair and
            ``force`` is off. Overwriting it would destroy whatever the repo
            keeps beyond the generated guidance (s3-bagit lost a 361-line
            register to exactly that).
    """
    block = _guidance_block(content)
    if force or not path.exists():
        return block + "\n", False
    existing = path.read_text(encoding="utf-8")
    try:
        span = _generated_block_span(existing)
    except GuidanceMarkerError as err:
        raise click.ClickException(
            f"{path} exists and {err}. Refusing to overwrite it; nothing was "
            "written. Re-run with --force to replace the whole file."
        ) from err
    return existing[: span.start] + block + existing[span.end :], span.end_proven_by_hash


def _handle_init(path_args: tuple[str, ...], *, short: bool = False, force: bool = False) -> None:
    """Handle `pysmelly init [PATH]` — write guidance file and reference in CLAUDE.md.

    An existing guidance file keeps everything outside its generated block; one
    whose block cannot be bounded — by a marker pair, or by the start marker's
    hash for a file written before the end marker — is refused (before anything
    is written, CLAUDE.md included) unless ``force``.
    """
    path = Path(path_args[0]) if path_args else Path("PYSMELLY.md")

    content = SHORT_GUIDANCE_CONTENT if short else GUIDANCE_CONTENT
    rendered, end_marker_added = _render_guidance_file(path, content, force=force)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    if end_marker_added:
        click.echo(
            f"{path} had no {GUIDANCE_END_MARKER} end marker; verified its generated "
            "block against the start marker's hash and added the end marker after it"
        )
    click.echo(f"Wrote {path}")

    # Add reference to CLAUDE.md (idempotent)
    claude_md = Path("CLAUDE.md")
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        found = GUIDANCE_REFERENCE_RE.search(existing)
        if found:
            click.echo(f"CLAUDE.md already references {found.group(1)}")
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
    name for name, cat in CHECK_CATEGORIES.items() if cat == GIT_HISTORY_CATEGORY
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

    def _find_param(self, opt_string: str) -> click.Option | None:
        """Look up an option by its option string (e.g., '--check').

        Only ``click.Option`` is matched — callers read ``is_flag``, which
        ``click.Argument`` does not define.
        """
        for param in self.params:
            if isinstance(param, click.Option) and (
                opt_string in param.opts or opt_string in param.secondary_opts
            ):
                return param
        return None


@click.group(cls=_GroupWithTargets, invoke_without_command=True)
@click.version_option(version=__version__, prog_name="pysmelly")
@click.option(
    "--check",
    "checks",
    type=click.Choice(_AST_CHECK_NAMES, case_sensitive=True),
    multiple=True,
    help="Run only this check; repeat the flag to run several (see --list-checks)",
)
@click.option("--skip", multiple=True, help="Skip this check (can be repeated)")
@click.option(
    "--exclude",
    multiple=True,
    help=(
        "Never analyze files matching pattern, such as vendored or generated code "
        "(repeatable; '*_pb2.py' for names, 'vendor/' for directories)"
    ),
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
    "--compare",
    "compare_ref",
    default=None,
    metavar="REF",
    help="Run the same checks at REF too and report what was resolved and what is new",
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
    checks,
    skip,
    exclude,
    verbose,
    min_severity,
    diff_ref,
    compare_ref,
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

    if compare_ref is not None and diff_ref is not None:
        click.echo(
            "Error: --compare and --diff cannot be combined: --compare reports every "
            "finding resolved or introduced since REF, --diff only those on changed lines",
            err=True,
        )
        sys.exit(1)

    # Targets come from ctx.args (positional args collected by _GroupWithTargets)
    targets = ctx.args or ["."]

    # Convert tuples to lists for merging
    exclude = list(exclude)
    skip = list(skip)

    # Load config file and merge with CLI args
    config, exclude, skip, min_severity = _load_and_merge_config(
        targets, exclude, skip, min_severity
    )
    selected = set(checks)
    if "check" in config and not selected:
        check_name = config["check"]
        if CHECK_CATEGORIES.get(check_name) == GIT_HISTORY_CATEGORY:
            click.echo(
                f"Error: {check_name} is a git-history check. "
                f"Use: pysmelly git-history --check {check_name}",
                err=True,
            )
            sys.exit(1)
        selected = {check_name}

    # Determine which checks to run (exclude git-history checks). An explicit
    # selection is the union of every --check given, and overrides the skip list.
    if selected:
        checks_to_run = [name for name in CHECKS if name in selected]
    else:
        checks_to_run = [
            name
            for name in CHECKS
            if name not in skip and CHECK_CATEGORIES.get(name, "ast") != GIT_HISTORY_CATEGORY
        ]

    def run(roots: list[Path]) -> tuple[Path, AnalysisContext, list[Finding]]:
        base, all_trees, shebang_files = _discover_and_parse(roots, exclude)
        analysis_ctx = _ast_context(roots, base, all_trees, shebang_files, verbose, config, exclude)
        findings = _run_checks_and_filter(analysis_ctx, checks_to_run, min_severity, diff_ref, base)
        return base, analysis_ctx, findings

    def analyze(roots: list[Path]) -> Analysis:
        _base, analysis_ctx, findings = run(roots)
        return Analysis(analysis_ctx.all_trees, findings)

    roots = [Path(t).resolve() for t in targets]

    # Build guidance preamble for LLM consumers (on by default)
    context: list[str] | None = None
    if not no_context:
        context = _build_guidance()

    if compare_ref is not None:
        _compare_and_exit(compare_ref, roots, analyze, context, summary)

    base, analysis_ctx, findings = run(roots)
    _output_and_exit(
        findings,
        len(analysis_ctx.all_trees),
        context,
        summary,
        more_please,
        header=_verbose_header(analysis_ctx),
        provenance=ProvenanceReader(base, analysis_ctx),
    )


def _compare_and_exit(
    ref: str,
    roots: list[Path],
    analyze: Analyze,
    context: list[str] | None,
    summary: bool,
) -> None:
    """Print the delta against REF; exit 1 when anything is new since REF, else 0."""
    try:
        comparison = run_comparison(ref, roots, analyze)
    except CompareError as err:
        click.echo(f"Error: {err}", err=True)
        sys.exit(1)
    lines = format_guidance(context) + format_comparison(comparison, summary=summary)
    click.echo("\n".join(lines))
    sys.exit(1 if comparison.new else 0)


@cli.command("init")
@click.option(
    "--short",
    is_flag=True,
    help="Generate a ~30-line summary instead of the full guidance file",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace the whole guidance file, discarding everything outside the "
    "generated block (without it, a file whose block cannot be bounded is refused)",
)
@click.argument("path", nargs=-1)
def init_cmd(short, force, path):
    """Write PYSMELLY.md guidance file for AI code review.

    Re-running replaces only the generated block, between the pysmelly-guidance
    start and end markers, and keeps everything else in the file byte for byte
    (a findings register below the end marker, for one). A file from before
    the end marker existed gets one where its start marker's hash proves the
    unedited generated block ends. Any other file without both markers is
    refused, and nothing is written, unless --force.
    """
    _handle_init(path, short=short, force=force)


@cli.group("git-history", cls=_GroupWithTargets, invoke_without_command=True)
@click.option(
    "--check",
    "checks",
    type=click.Choice(_GIT_HISTORY_CHECK_NAMES, case_sensitive=True),
    multiple=True,
    metavar="CHECK",
    help="Run only this check; repeat the flag to run several",
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
    checks,
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
    base, all_trees, shebang_files = _discover_and_parse(roots, exclude)
    try:
        git_root = get_git_root(base)
    except GitNotFoundError:
        click.echo("Error: pysmelly git-history requires a git repository", err=True)
        sys.exit(1)

    # Determine which checks to run
    git_history_checks = [
        name for name in CHECKS if CHECK_CATEGORIES.get(name) == GIT_HISTORY_CATEGORY
    ]
    selected = set(checks)
    if selected:
        checks_to_run = [name for name in git_history_checks if name in selected]
    else:
        checks_to_run = [name for name in git_history_checks if name not in skip]

    # Run checks
    git = GitSettings(
        root=git_root,
        window=window,
        commit_messages=commit_messages,
        expected_coupling=config.get(EXPECTED_COUPLING_KEY, []),
    )
    analysis_ctx = AnalysisContext(
        all_trees,
        verbose,
        git=git,
        pytest_config=_load_pytest_config(roots, base),
        user_catalog=config.get(CATALOG_KEY, EMPTY_CATALOG),
        declared_entry_points=_load_declared_entry_points(roots),
    )
    analysis_ctx.shebang_files = shebang_files
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

    _output_and_exit(
        all_findings,
        len(all_trees),
        context,
        summary,
        more_please,
        header=_git_history_header(analysis_ctx, window, summary),
        provenance=ProvenanceReader(base, analysis_ctx),
    )


def _git_history_header(ctx: AnalysisContext, window: str, summary: bool) -> list[str]:
    """The lines under the file count for a git-history run.

    The window is relative to today, so the count only means something next
    to the window and the date it was taken (plan item 19). Project trends
    are informational, never findings: the block reads the whole window, so
    the reviewed markers --ignore-reviewed clears have no bearing on it, and
    --summary leaves it out to stay exactly the per-check counts.
    """
    header = [
        f"Git history window: {describe_window(window, datetime.now().date())}",
        *_verbose_header(ctx),
    ]
    history = ctx.git_history
    if not summary and history is not None:
        try:
            trends = project_trends(history)
        except GitHistoryQueryError as err:
            click.echo(f"Error: {err}", err=True)
            sys.exit(1)
        if trends:
            header.extend(["", *trends])
    return header


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
