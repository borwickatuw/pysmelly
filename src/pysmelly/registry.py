"""Check registry and finding dataclass."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pysmelly.chains import Chain
    from pysmelly.context import AnalysisContext


class Severity(Enum):
    """How likely this finding represents a real problem."""

    HIGH = "high"  # Act on this or explicitly justify keeping it
    MEDIUM = "medium"  # Review each finding, fix what makes sense
    LOW = "low"  # Informational — skim for surprises


@dataclass(frozen=True)
class Site:
    """One place a pattern occurs, in a finding that reports several."""

    file: str
    line: int


@dataclass(frozen=True)
class Subject:
    """A name a finding is about: a definition, a parameter or a literal.

    ``name`` is the name (the text ``git log -S`` looks for). ``file`` is the
    analyzed file defining it, relative to the analyzed root. ``scope`` is
    None for a module-level definition (a function, a constant), whose
    readers may sit in any file. For a parameter or a literal it is the
    enclosing function's first and last lines, and history is read for
    ``file`` alone, so the name must appear in it only inside those lines.
    ``parameter_of`` is the name of the function a parameter belongs to, and
    None for a definition or a literal: a parameter is the one subject
    finding chains (``chains.py``) join on.

    ``vestigial`` is whether the finding says the subject is vestigial. Only
    those get a provenance line (``provenance.py``): a parameter that is
    only forwarded, or always receives one value, is still read.
    """

    name: str
    file: str
    scope: tuple[int, int] | None
    parameter_of: str | None
    vestigial: bool


def function_scope(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """A function's first and last lines, decorators included, as a ``Subject.scope``."""
    first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    last = node.end_lineno if node.end_lineno is not None else node.lineno
    return first, last


def parameter_subject(
    file: str, node: ast.FunctionDef | ast.AsyncFunctionDef, name: str, *, vestigial: bool
) -> Subject:
    """Parameter ``name`` of the function ``node`` defined in ``file``, as a ``Subject``."""
    return Subject(name, file, function_scope(node), node.name, vestigial)


# Rebuilds a multi-site finding's message from the indices (into its
# ``sites``) of the sites still reported, in their original order. None when
# those sites no longer make a finding by the check's own rule (one copy of a
# block left is not a duplicate; two files do not reach a three-file floor).
SiteRender = Callable[[tuple[int, ...]], str | None]

UNPAIRED = "a finding carries sites and render together, or neither"
UNRENDERABLE = "its render refuses the full set of sites the check found"


class SitesError(Exception):
    """A multi-site finding that is malformed: ``problem`` says how."""

    def __init__(self, check: str, problem: str) -> None:
        super().__init__(f"{check}: {problem}")


@dataclass
class Finding:
    """A single code smell finding.

    ``chain`` is the work item the finding was collapsed into, set after
    filtering (``chains.chain_findings``); it is None for most findings, and
    takes no part in comparison.

    Most findings speak for one line. A finding that reports one pattern
    across several sites (the same import in many files, the same block in
    many functions, one attribute read from many modules) also lists them in
    ``sites`` and carries ``render`` to rebuild its message from any subset.
    It anchors either on one of its sites (peers, none more the subject than
    another) or on the subject's own definition. An inline ignore speaks for
    its own line and nothing else: on a site's line it removes that site, so
    the suppression pass (``cli._apply_suppression``) re-renders the finding
    from the sites left, re-anchors it when its anchor was the site removed,
    and drops it when none are left or ``render`` says the sites left no
    longer make a finding; on a definition anchor it suppresses the finding,
    as on any single-line finding.

    ``subjects`` names what a finding is about: the definitions, parameters
    or literals it judges (``Subject``). The output traces each vestigial
    one through git for a provenance line (``provenance.py``), and finding
    chains join on the parameters. Most findings name none. ``notes`` are
    lines the output prints under the message (the provenance lines), added
    only to findings that will be printed.
    """

    file: str
    line: int
    check: str
    message: str
    severity: Severity
    chain: Chain | None = field(default=None, compare=False, repr=False)
    sites: tuple[Site, ...] = field(default=(), compare=False, repr=False)
    render: SiteRender | None = field(default=None, compare=False, repr=False)
    subjects: tuple[Subject, ...] = field(default=(), compare=False, repr=False)
    notes: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if (self.render is None) != (not self.sites):
            raise SitesError(self.check, UNPAIRED)

    @property
    def places(self) -> tuple[Site, ...]:
        """Every place the finding speaks for: its sites, or its one line."""
        return self.sites or (Site(self.file, self.line),)

    @property
    def text(self) -> str:
        """The message as printed: what the check found, then its fix and its dismissal."""
        stop = "" if self.message.endswith((".", "?", "!")) else "."
        return f"{self.message}{stop} Fix: {CHECK_FIXES[self.check]} {CHECK_DISMISSALS[self.check]}"


def multi_site_finding(
    check: str,
    severity: Severity,
    sites: list[Site],
    render: SiteRender,
    anchor: Site | None = None,
) -> Finding:
    """A finding for one pattern at several sites; its message is ``render``
    over every site. It anchors on the first site unless ``anchor`` names
    another site or the subject's definition."""
    at = sites[0] if anchor is None else anchor
    message = render(tuple(range(len(sites))))
    if message is None:
        raise SitesError(check, UNRENDERABLE)
    return Finding(
        file=at.file,
        line=at.line,
        check=check,
        message=message,
        severity=severity,
        sites=tuple(sites),
        render=render,
    )


# Maximum width for truncating display values in finding messages
MAX_DISPLAY_WIDTH = 40

# Type for check functions
CheckFn = Callable[["AnalysisContext"], list[Finding]]

# Global registry
CHECKS: dict[str, CheckFn] = {}
CHECK_SEVERITY: dict[str, Severity] = {}
CHECK_DESCRIPTIONS: dict[str, str] = {}
CHECK_CATEGORIES: dict[str, str] = {}
# What to change for a finding, and the one condition under which it is not
# worth changing (Phase 29-1). Every printed message ends with both
# (``Finding.text``), and ``--list-checks`` shows the dismissal.
CHECK_FIXES: dict[str, str] = {}
CHECK_DISMISSALS: dict[str, str] = {}
# How every dismissal sentence opens: it names the one case, not a judgment call.
DISMISSAL_OPENING = "Dismiss only "
# The category that puts a check in the `pysmelly git-history` pipeline.
GIT_HISTORY_CATEGORY = "git-history"
# Checks whose subject is test code: their findings may sit in test-role files.
TEST_SUBJECT_CHECKS: set[str] = set()


@dataclass(frozen=True)
class Advice:
    """What a check tells the reader to do with a finding (Phase 29-1).

    ``fix`` is the edit a finding asks for, an imperative sentence; the
    message names the sites. ``dismiss`` is the one narrow condition under
    which a finding is not worth fixing, opening with ``DISMISSAL_OPENING``.
    """

    fix: str
    dismiss: str


class CheckRegistrationError(Exception):
    """A ``@check`` registration whose fix or dismissal sentence is malformed."""

    def __init__(self, check: str, problem: str) -> None:
        super().__init__(f"{check}: {problem}")


def _sentences(name: str, advice: Advice) -> None:
    """Refuse a fix or dismissal that is not one finished sentence of the expected shape."""
    for label, text in (("fix", advice.fix), ("dismissal", advice.dismiss)):
        if text != text.strip() or not text.endswith("."):
            raise CheckRegistrationError(
                name, f"the {label} sentence must be stripped and end with '.'"
            )
    if not advice.fix[0].isupper():
        raise CheckRegistrationError(name, "the fix sentence must open with a capitalized verb")
    if not advice.dismiss.startswith(DISMISSAL_OPENING):
        raise CheckRegistrationError(
            name, f"the dismissal sentence must open with {DISMISSAL_OPENING!r}"
        )


def check(
    name: str,
    severity: Severity = Severity.MEDIUM,
    description: str = "",
    category: str = "ast",
    *,
    advice: Advice,
    about_tests: bool = False,
):
    """Register a lint check function by name, severity, description, and category.

    ``advice`` carries the fix and the dismissal every printed message of the
    check closes with. It is required, and a malformed sentence is refused
    (``CheckRegistrationError``), so a check without both does not register.

    ``about_tests`` marks a check whose subject is test code, so its
    findings are kept in test-role files (see ``run_check``).
    """
    _sentences(name, advice)

    def decorator(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        CHECK_SEVERITY[name] = severity
        CHECK_DESCRIPTIONS[name] = description
        CHECK_CATEGORIES[name] = category
        CHECK_FIXES[name] = advice.fix
        CHECK_DISMISSALS[name] = advice.dismiss
        if about_tests:
            TEST_SUBJECT_CHECKS.add(name)
        return fn

    return decorator


def run_check(name: str, ctx: AnalysisContext) -> list[Finding]:
    """Run one registered check, keeping only findings on code it may report on.

    Test-role files are evidence, never the subject of a finding: a finding
    anchored in one is dropped, unless the check is about tests
    (``TEST_SUBJECT_CHECKS``).
    """
    findings = CHECKS[name](ctx)
    return [finding for finding in findings if may_anchor(name, finding.file, ctx)]


def may_anchor(name: str, file: str, ctx: AnalysisContext) -> bool:
    """Whether check ``name`` may report a finding anchored in ``file``."""
    return name in TEST_SUBJECT_CHECKS or not ctx.roles.is_test(file)
