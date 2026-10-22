"""Parameters whose callers split: lone-dissenter and test-only-default.

Both read the caller-value index (``ctx.caller_values``, Phase 25-1) for the
functions dead-branch-by-callers and always-default-params judge
(``caller_decided._functions``: every caller visible to the index), and
both look for one side of the callers that differs from the rest.

**lone-dissenter.** A parameter with at least ``LONE_DISSENTER_MIN_CALLERS``
production callers, where all of them but one give it the same value: the
same constant, or the default by omitting it (a site passing the default's
constant explicitly agrees with one omitting it). The one caller that
differs had its special case pushed into shared code. The finding names that
caller's file:line and what it passes, and the majority with their sites.
A passthrough splat at any production site, or a majority that is not a
constant or the default, is unknown, and the parameter is not judged. A
dissenter passing the default's own expression (``floor=MIN_CHARS`` beside
``floor: int = MIN_CHARS``) does not dissent. A dissenter whose argument
is a parameter of its own enclosing function, never rebound there, only
forwards a value its callers choose: the message names that function
instead of calling the site a special case.

**test-only-default.** A defaulted parameter that every production caller
passes explicitly while some test-role caller omits it: the default exists
for the tests. It generalizes unused-defaults to any default, and keeps
that check's calibration (DECISIONS.md: tests should not introduce branches
production never takes), so the fix is the same: make the parameter
required and have the tests pass it. With no test-role files analyzed (a
project that excludes its tests), no caller can omit it from a test, so the
check cannot apply and reports nothing.

**Which check owns which case.** unused-defaults reports a ``None`` default
no caller omits, tests included; test-only-default needs a test omitting
it, so the two never meet. always-default-params needs every production
caller to omit the parameter, test-only-default every one to pass it. A
parameter deciding a branch dead-branch-by-callers reports belongs to that
finding, which already cites the tests that alone take the other arm, so
neither check here reports it. A parameter test-only-default reports is not
also a lone-dissenter finding. constant-args needs every caller to agree and
always-default-params every caller to omit, so neither meets a dissenter.
A parameter the body never reads is vestigial-params'.

**Methods.** The index binds a method's callers by name (Phase 25-1), so
``set.add(x)`` reaches a project method named ``add``. A method is judged
here only when no other analyzed class defines its name and every production
call's receiver ties it to the class, return-shape-unused's rule:
``Cls.m()``, or ``self.m()``/``cls.m()`` inside the class or a project
subclass. A test call that is not tied is left out. A single misattributed
call would otherwise be read as the dissenter, or as the omission only tests
make.

**Recursion.** A function calling itself with a value its outside callers
never pass (an accumulator such as ``seen``) is how recursion works, not a
special case, so a dissenter inside the function's own body is no finding.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pysmelly.checks.caller_decided import (
    _cite,
    _Function,
    _functions,
    _named_defaults,
    _sites,
)
from pysmelly.checks.callers import (
    CallerValues,
    ParamValues,
    PassedKind,
    PassedValue,
    param_subjects,
)
from pysmelly.checks.helpers import (
    ProjectClasses,
    enclosing_functions,
    get_param_names,
    walk_name_assignments,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

# A dissent needs a majority: with two callers neither one is the odd one out.
LONE_DISSENTER_MIN_CALLERS = 3

# The key of an omitted parameter whose default is not a constant.
_DEFAULT_EXPRESSION = ("default",)


@dataclass(frozen=True)
class _Subject:
    """A judged function and the test sites (indices into each ``ParamValues.tests``) it counts."""

    function: _Function
    tests: frozenset[int]

    @property
    def entry(self) -> CallerValues:
        return self.function.entry

    def test_values(self, param: ParamValues) -> list[PassedValue]:
        return [value for index, value in enumerate(param.tests) if index in self.tests]


def _subjects(ctx: AnalysisContext) -> Iterator[_Subject]:
    """The functions caller_decided judges; a method only by a unique name and tied calls."""
    methods = Counter(
        entry.name
        for entries in ctx.caller_values.by_file.values()
        for entry in entries
        if entry.is_method
    )
    classes = ProjectClasses(ctx)
    for function in _functions(ctx):
        entry = function.entry
        if entry.class_name is None:
            yield _Subject(function, frozenset(range(len(entry.test_callers))))
            continue
        if methods[function.name] != 1:
            continue
        owners = classes.by_name.get(entry.class_name, [])
        if not all(_tied(classes, call, owners) for call in entry.production_callers):
            continue
        tests = (i for i, call in enumerate(entry.test_callers) if _tied(classes, call, owners))
        yield _Subject(function, frozenset(tests))


def _tied(classes: ProjectClasses, call: dict, owners: list[ast.ClassDef]) -> bool:
    """Whether ``call`` is tied to any of ``owners``: the method's class is known by name."""
    return any(classes.tied(call, owner) for owner in owners)


def _agreement_key(param: ParamValues, value: PassedValue) -> tuple[str, ...] | None:
    """What a site gives the parameter, as a key sites agree on; None when not a constant."""
    if value.kind is PassedKind.OMITTED:
        default = param.default
        if isinstance(default, ast.Constant):
            return ("constant", repr(default.value))
        return _DEFAULT_EXPRESSION
    if value.kind in {PassedKind.LITERAL, PassedKind.NONE} and value.literal is not None:
        return ("constant", value.literal)
    return None


def _shown(param: ParamValues, value: PassedValue) -> str:
    """What a site passes, as a message shows it."""
    if value.kind is PassedKind.OMITTED:
        default = "" if param.default is None else f" {ast.unparse(param.default)}"
        return f"the default{default}"
    if value.argument is None:
        raise ValueError(f"{value.file}:{value.line} is a passthrough, which no finding cites")
    return value.literal if value.literal is not None else f"`{ast.unparse(value.argument)}`"


@dataclass(frozen=True)
class _Dissent:
    """One parameter on which every production caller but one agrees."""

    param: ParamValues
    dissenter: PassedValue
    majority: list[PassedValue]


def _dissent(param: ParamValues) -> _Dissent | None:
    production = param.production
    if param.default is None or len(production) < LONE_DISSENTER_MIN_CALLERS:
        return None
    if any(value.passthrough for value in production):
        return None
    keys = [_agreement_key(param, value) for value in production]
    counts = Counter(key for key in keys if key is not None)
    if not counts:
        return None
    majority_key, count = counts.most_common(1)[0]
    if count != len(production) - 1:
        return None
    majority = [value for value, key in zip(production, keys, strict=True) if key == majority_key]
    [dissenter] = [
        value for value, key in zip(production, keys, strict=True) if key != majority_key
    ]
    default = param.default
    if (
        dissenter.argument is not None
        and default is not None
        and ast.dump(dissenter.argument) == ast.dump(default)
    ):
        return None
    return _Dissent(param, dissenter, majority)


def _test_only_defaults(subject: _Subject, owned: set[str]) -> list[ParamValues]:
    """Defaulted parameters every production site passes and some test site omits."""
    function = subject.function
    return [
        param
        for param in subject.entry.params
        if param.default is not None
        and param.name in function.read
        and param.name not in owned
        and all(
            value.kind is not PassedKind.OMITTED and not value.passthrough
            for value in param.production
        )
        and any(value.kind is PassedKind.OMITTED for value in subject.test_values(param))
    ]


def _test_only_owned(subject: _Subject) -> tuple[list[ParamValues], set[str]]:
    """test-only-default's parameters, and every name another finding already owns."""
    owned = subject.function.dead_branch_params()
    params = _test_only_defaults(subject, owned)
    return params, owned | {param.name for param in params}


def _agreement(dissent: _Dissent) -> str:
    """``'floor' (the default MIN_CHARS at a.py:3, b.py:9)`` — one parameter's majority."""
    groups: dict[str, list[PassedValue]] = {}
    for value in dissent.majority:
        groups.setdefault(_shown(dissent.param, value), []).append(value)
    return f"'{dissent.param.name}' ({_cite(groups)})"


def _forwarding_function(
    ctx: AnalysisContext, dissents: list[_Dissent]
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[str]] | None:
    """The function whose own parameters the dissenting site passes on, with their names.

    Every dissenting argument must be a bare name that is a parameter of the
    function enclosing the call and that its body never rebinds; then the
    value is not that caller's choice but its callers'. None otherwise.
    """
    names = []
    function = None
    for dissent in dissents:
        argument = dissent.dissenter.argument
        if not isinstance(argument, ast.Name):
            return None
        parents = ctx.parent_map(ctx.all_trees[Path(dissent.dissenter.file)])
        node = next(enclosing_functions(argument, parents), None)
        if node is None:
            return None
        if argument.id not in get_param_names(node):
            return None
        if walk_name_assignments(node.body, argument.id)[1]:
            return None
        function = node
        names.append(argument.id)
    if function is None:
        return None
    return function, names


def _lone_dissenter_message(
    name: str,
    total: int,
    dissents: list[_Dissent],
    forwarding: tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[str]] | None,
) -> str:
    dissenter = dissents[0].dissenter
    agreed = " and ".join(_agreement(dissent) for dissent in dissents)
    passes = ", ".join(
        f"{dissent.param.name}={_shown(dissent.param, dissent.dissenter)}" for dissent in dissents
    )
    if forwarding is None:
        why = "That one caller's special case lives in shared code."
    else:
        function, params = forwarding
        own = (
            f"parameter '{params[0]}'"
            if len(params) == 1
            else f"parameters {', '.join(repr(p) for p in params)}"
        )
        why = (
            f"That caller forwards {function.name}()'s own {own}, so the value comes from "
            f"{function.name}()'s callers."
        )
    return (
        f"{name}(): {total - 1} of {total} production caller(s) agree on {agreed}; "
        f"only {dissenter.file}:{dissenter.line} differs, with {passes}. {why}"
    )


@check(
    "lone-dissenter",
    severity=Severity.LOW,
    description="A parameter all production callers but one give the same value",
    advice=Advice(
        fix=(
            "Let the dissenting caller handle its special case (or give it a function of its"
            " own) and drop the parameter."
        ),
        dismiss=(
            "Dismiss only if the parameter is a variation point the function genuinely offers,"
            " such as a public boundary that code outside the analyzed tree calls with other"
            " values."
        ),
    ),
)
def check_lone_dissenter(ctx: AnalysisContext) -> list[Finding]:
    """Find parameters one production caller alone varies (module docstring)."""
    findings = []
    for subject in _subjects(ctx):
        entry = subject.entry
        read = subject.function.read
        _params, owned = _test_only_owned(subject)
        by_site: dict[tuple[str, int], list[_Dissent]] = {}
        for param in entry.params:
            if param.name not in read or param.name in owned:
                continue
            dissent = _dissent(param)
            if dissent is None:
                continue
            site = (dissent.dissenter.file, dissent.dissenter.line)
            if not entry.encloses(*site):
                by_site.setdefault(site, []).append(dissent)
        total = len(entry.production_callers)
        for dissents in by_site.values():
            findings.append(
                Finding(
                    file=str(entry.file),
                    line=entry.line,
                    check="lone-dissenter",
                    message=_lone_dissenter_message(
                        entry.name, total, dissents, _forwarding_function(ctx, dissents)
                    ),
                    severity=Severity.LOW,
                    subjects=param_subjects(entry, [dissent.param.name for dissent in dissents]),
                )
            )
    return findings


@check(
    "test-only-default",
    severity=Severity.MEDIUM,
    description="A default only tests use: production always passes it (needs tests analyzed)",
    advice=Advice(
        fix=(
            "Make the parameter required and pass the value explicitly in the tests that omit it."
        ),
        dismiss=(
            "Dismiss only if the function is a genuine public boundary whose callers outside"
            " the analyzed tree rely on the default."
        ),
    ),
)
def check_test_only_default(ctx: AnalysisContext) -> list[Finding]:
    """Find defaults only test-role callers rely on (module docstring)."""
    if not ctx.roles.files:
        return []
    findings = []
    for subject in _subjects(ctx):
        params, _owned = _test_only_owned(subject)
        if not params:
            continue
        entry = subject.entry
        omitting = {
            (value.file, value.line): value
            for param in params
            for value in subject.test_values(param)
            if value.kind is PassedKind.OMITTED
        }
        findings.append(
            Finding(
                file=str(entry.file),
                line=entry.line,
                check="test-only-default",
                message=_test_only_message(entry.name, params, list(omitting.values())),
                severity=Severity.MEDIUM,
                subjects=param_subjects(entry, [param.name for param in params]),
            )
        )
    return findings


def _test_only_message(name: str, params: list[ParamValues], omitting: list[PassedValue]) -> str:
    passing = list(params[0].production)
    it = "it" if len(params) == 1 else "them"
    return (
        f"{name}() never uses the default of {_named_defaults(params)}: all {len(passing)} "
        f"production caller(s) pass {it} ({_sites(passing)}), and only tests omit {it} "
        f"({_sites(omitting)}), so the default is a path production never takes"
    )
