"""Caller-aware checks — cross-file call-graph analysis.

These checks build a picture of who calls what and flag functions
whose usage pattern suggests they should be refactored.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from pysmelly.checks.cascade import removal_cascade
from pysmelly.checks.framework import (
    FRAMEWORK_HOOK_METHODS,
    framework_dispatch_decorators,
    has_dictated_signature,
)
from pysmelly.checks.helpers import (
    NESTED_SCOPES,
    ModuleImport,
    body_without_docstring,
    dotted_name,
    enclosing_functions,
    has_dataclass_decorator,
    is_bare_name,
    is_ellipsis,
    is_imported_in_production_elsewhere,
    is_in_dunder_all,
    is_referenced_as_dotted_string,
    is_referenced_as_value_in_production,
    is_used_as_decorator,
    iter_functions,
    iter_simple_assigns,
    raises_not_implemented,
    referenced_name,
    source_trees,
    type_expr_names,
)
from pysmelly.checks.inheritance import OverrideResolver
from pysmelly.checks.module_graph import ModuleGraph
from pysmelly.checks.test_surface import TESTS_LABEL, surface_of_units
from pysmelly.context import AnalysisContext
from pysmelly.registry import (
    Advice,
    Finding,
    Severity,
    Subject,
    check,
    function_scope,
    parameter_subject,
)

# How many call-site lines a caller clause cites per test file before
# summarising the rest as a count. Every test file is still named.
_CITED_LINES_PER_FILE = 3


@dataclass(frozen=True)
class _Callers:
    """A target's call sites split by the role of the file each sits in.

    ``test_files`` names the test-role files that reference the target
    without a call the call index sees (an import, a value reference), so
    a message citing the tests names every file counted as one.
    """

    production: list[dict]
    tests: list[dict]
    test_files: frozenset[str]

    @property
    def tested(self) -> bool:
        """Whether any test-role file calls or references the target."""
        return bool(self.tests or self.test_files)


def _split_callers(ctx: AnalysisContext, calls: list[dict]) -> _Callers:
    """Split ``calls`` into production and test call sites by the test role."""
    production = [c for c in calls if not ctx.roles.is_test(c["file"])]
    tests = [c for c in calls if ctx.roles.is_test(c["file"])]
    return _Callers(production, tests, frozenset())


def _cite_test_sites(callers: _Callers) -> str:
    """``tests/test_x.py:12, :40, tests/test_y.py`` — every test file, its call lines first.

    Files are sorted; each cites up to ``_CITED_LINES_PER_FILE`` call lines
    and counts the rest. A file that references without calling is named
    with no line.
    """
    return _cite_sites(callers.tests, callers.test_files)


def _cite_sites(calls: Iterable[dict], files: Iterable[str]) -> str:
    """``a.py:12, :40, b.py`` — the files of *calls* and *files*, sorted, each
    with its first call lines (``_cite_file``); a file in *files* with no call
    is named with no line.
    """
    lines: dict[str, set[int]] = {f: set() for f in files}
    for call in calls:
        lines.setdefault(call["file"], set()).add(call["line"])
    return ", ".join(_cite_file(filepath, sorted(lines[filepath])) for filepath in sorted(lines))


def _cite_file(filepath: str, numbers: list[int]) -> str:
    """``tests/test_x.py:12, :40 (+2 more)`` — one file and its first call lines."""
    shown = numbers[:_CITED_LINES_PER_FILE]
    text = filepath + ", ".join(f":{n}" for n in shown)
    if len(numbers) > len(shown):
        text += f" (+{len(numbers) - len(shown)} more)"
    return text


def _callers_clause(callers: _Callers) -> str:
    """The production and test caller counts every caller-counting check prints.

    One wording for dead-code, unused-defaults, pass-through-params,
    internal-only and single-call-site. Whenever a test is counted, the
    clause names the files counted as tests, so a misclassified file shows.
    """
    if not callers.production:
        if callers.tested:
            return f"no production callers; still referenced by {_cite_test_sites(callers)}"
        return "no production callers, no test callers"
    clause = f"{len(callers.production)} production caller(s), {len(callers.tests)} test caller(s)"
    if callers.tested:
        clause += f" in {_cite_test_sites(callers)}"
    return clause


def _defaults_seen_by_callers(entry: CallerValues) -> bool:
    """Whether the call graph can see every caller of ``entry``'s defaults.

    A private function's defaults are its own business, and a method of a
    public class has callers outside the analyzed code; neither is judged.
    """
    name = entry.node.name
    if name.startswith("_") and not name.startswith("__"):
        return False
    return entry.owner is None or entry.owner.name.startswith("_")


# The check whose findings root a finding chain's removal (``chains.py``).
DEAD_CODE = "dead-code"


@check(
    "unused-defaults",
    severity=Severity.HIGH,
    description="Params defaulting to None that every caller always passes",
    advice=Advice(
        fix=(
            "Make the parameter required: delete its `= None` default and the None handling"
            " in the body; every caller counted already passes it."
        ),
        dismiss=(
            "Dismiss only if the function is published API that code outside the analyzed"
            " tree calls without the parameter."
        ),
    ),
)
def check_unused_defaults(ctx: AnalysisContext) -> list[Finding]:
    """Find Optional params where every caller always passes a value.

    If a parameter defaults to None but no caller ever relies on that
    default, the Optional is vestigial — make the parameter required.
    Every caller counts, tests included. A passthrough (a ``*args`` or
    ``**kwargs`` splat covering the parameter) is unknown: it may rely on
    the default, so it does not prove the parameter always passed and there
    is no finding. Keyword-only parameters are not judged, nor is a method
    that may override a base outside the analyzed code, whose signature
    that base dictates.
    """
    findings = []
    for entries in ctx.caller_values.by_file.values():
        for entry in entries:
            if entry.overrides_unresolved_base or not _defaults_seen_by_callers(entry):
                continue
            callers = entry.callers
            count = len(entry.call_sites)
            if not count:
                continue
            for param in entry.params:
                if param.kind is ParamKind.KEYWORD_ONLY or param.default is None:
                    continue
                if not _is_none_literal(param.default):
                    continue
                passed = (*param.production, *param.tests)
                if any(value.kind is PassedKind.OMITTED or value.passthrough for value in passed):
                    continue
                findings.append(
                    Finding(
                        file=str(entry.file),
                        line=entry.node.lineno,
                        check="unused-defaults",
                        message=(
                            f"{entry.node.name}() param '{param.name}' defaults "
                            f"to None but all {count} caller(s) always pass it "
                            f"({_callers_clause(callers)})"
                        ),
                        severity=Severity.HIGH,
                        subjects=param_subjects(entry, [param.name]),
                    )
                )

    return findings


def _has_deprecation_warning(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function body contains warnings.warn(..., DeprecationWarning)."""
    _deprecation_names = {"DeprecationWarning", "PendingDeprecationWarning"}
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "warn"):
            continue
        # Check 2nd positional arg: warnings.warn("msg", DeprecationWarning)
        if len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Name) and arg.id in _deprecation_names:
                return True
        # Check category keyword: warnings.warn("msg", category=DeprecationWarning)
        for kw in node.keywords:
            if kw.arg == "category" and isinstance(kw.value, ast.Name):
                if kw.value.id in _deprecation_names:
                    return True
    return False


@check(
    DEAD_CODE,
    severity=Severity.HIGH,
    description="Public functions with no production callers, with what deleting each also frees",
    advice=Advice(
        fix=(
            "Delete the function, the definitions the message says it frees, and"
            " the tests it names."
        ),
        dismiss=(
            "Dismiss only if something outside the analyzed tree calls it (a plugin, a"
            " published API, an entry point pysmelly cannot see); name that caller in the"
            " ignore."
        ),
    ),
)
def check_dead_code(ctx: AnalysisContext) -> list[Finding]:
    """Find public functions nothing in production calls or references.

    Checks direct calls, imports, dict/list references, callback passing,
    and dotted-path string references (e.g., Django settings). Test code
    does not keep production code alive: calls, imports and value
    references from test-role files are reported, not counted. Dotted
    strings and decorators are indexed without their files, so either one
    anywhere keeps the function alive.

    Each finding ends with the cascade (``checks/cascade.py``): the
    definitions removing the function leaves with no production referrer,
    those that are dead-code findings themselves marked.
    """
    dead: list[tuple[str, dict, _Callers]] = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        importers = ctx.references.import_index.get(func_name, set()) - {def_file}
        refs = importers | ctx.references.value_references.get(func_name, set())
        callers = replace(
            _split_callers(ctx, ctx.call_index.get(func_name, [])),
            test_files=frozenset(f for f in refs if ctx.roles.is_test(f)),
        )

        if (
            not callers.production
            and not is_imported_in_production_elsewhere(func_name, def_file, ctx)
            and not is_referenced_as_value_in_production(func_name, ctx)
            and not is_referenced_as_dotted_string(func_name, ctx)
            and not is_used_as_decorator(func_name, ctx)
        ):
            # Functions with deprecation warnings are intentionally retained
            # public API — they were once used and are being phased out.
            func_node = defs[0].get("node")
            if func_node and _has_deprecation_warning(func_node):
                continue

            # Functions listed in __all__ are explicitly public API.
            def_tree = ctx.all_trees.get(Path(def_file))
            if def_tree and is_in_dunder_all(func_name, def_tree):
                continue

            dead.append((func_name, defs[0], callers))

    reported = frozenset((Path(definition["file"]), name) for name, definition, _ in dead)
    findings = []
    for func_name, definition, callers in dead:
        message = f"{func_name}(): {_callers_clause(callers)}"
        if callers.tested:
            message += " — test-only surface"
        cascade = removal_cascade(ctx, (Path(definition["file"]), func_name))
        surface = surface_of_units(
            ctx,
            [cascade.removed, *((member.file, member.name) for member in cascade.freed)],
            [*callers.test_files, *(call["file"] for call in callers.tests)],
        )
        tail = [
            cascade.describe(f"{func_name}()", reported),
            f"{TESTS_LABEL.format(it='it')}: {surface.describe()}" if surface else None,
        ]
        if any(tail):
            message += ". " + ". ".join(sentence for sentence in tail if sentence)
        findings.append(
            Finding(
                file=definition["file"],
                line=definition["line"],
                check=DEAD_CODE,
                message=message,
                severity=Severity.HIGH,
                subjects=(Subject(func_name, definition["file"], None, None, vestigial=True),),
            )
        )
    return findings


@check(
    "single-call-site",
    severity=Severity.LOW,
    description="Short functions called exactly once (inline candidate)",
    advice=Advice(
        fix=(
            "Inline the body at its one call site and delete the function; rewrite any test"
            " caller named to exercise that call site instead."
        ),
        dismiss=(
            "Dismiss only if a test patches the function by name as a seam, so inlining"
            " would remove the patch point."
        ),
    ),
)
def check_single_call_site(ctx: AnalysisContext) -> list[Finding]:
    """Find short public functions called exactly once (candidate for inlining).

    Filters:
    - Functions with 5+ top-level statements are skipped
    - Functions spanning 10+ lines are skipped
    - Cross-directory calls are suppressed (public API boundaries)
    - Functions referenced as values in production dispatch tables are
      skipped — the dispatch dict is the real fan-out

    Test code does not justify production code. Test-file imports, test-file
    direct calls, and test-file value references are excluded from the
    "called exactly once" judgement. If the production code has exactly one
    real call site, the finding stands — but the message will note any test
    callers so the inliner knows they'll have to be refactored too.

    Severity is bumped to MEDIUM when the function has many parameters (4+)
    or when all arguments come from a single object (decomposing then
    recomposing a data structure).
    """
    findings = []
    func_defs = ctx.function_index
    max_body_stmts = 4
    max_body_lines = 10

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        # The calls that resolve to this function (the Phase 21-3 rule): a
        # same-named method or library call (obj.f(), os.environ.get()) is
        # not one, and an aliased from-import is.
        callers = ctx.caller_values.of(defs[0]["node"]).callers
        if len(callers.production) != 1:
            continue

        if is_imported_in_production_elsewhere(func_name, def_file, ctx):
            continue

        # Referenced as a value in a production dispatch table — the dict
        # entry is a real call site we can't enumerate, so don't suggest
        # inlining what is effectively a callback target.
        if is_referenced_as_value_in_production(func_name, ctx):
            continue

        func_node = defs[0].get("node")

        # Skip functions with 5+ statements — extracted for readability
        if func_node and len(func_node.body) > max_body_stmts:
            continue

        # Skip functions spanning 10+ lines — too large to inline
        if func_node and func_node.end_lineno:
            if func_node.end_lineno - func_node.lineno + 1 > max_body_lines:
                continue

        call = callers.production[0]

        # Skip cross-directory calls — these are public API boundaries
        def_dir = str(Path(def_file).parent)
        call_dir = str(Path(call["file"]).parent)
        if def_dir != call_dir:
            continue

        msg, severity = _single_call_site_finding(func_name, func_node, callers)
        findings.append(
            Finding(
                file=def_file,
                line=defs[0]["line"],
                check="single-call-site",
                message=msg,
                severity=severity,
            )
        )

    return findings


def _single_call_site_finding(
    func_name: str,
    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None,
    callers: _Callers,
) -> tuple[str, Severity]:
    """Build the message and severity for a single-call-site finding."""
    call = callers.production[0]
    param_count = 0
    if func_node:
        param_count = len([a for a in func_node.args.args if a.arg not in {"self", "cls"}])
    single_source = _args_from_single_object(call["node"])

    severity = Severity.MEDIUM if (param_count >= 4 or single_source) else Severity.LOW

    parts = [f"{func_name}()"]
    if param_count > 0:
        parts.append(f"has {param_count} params and")
    parts.append(f"exactly 1 call site ({call['file'].split('/')[-1]}:{call['line']})")
    if single_source:
        parts.append(f"— all args from '{single_source}'")
    parts.append(f"({_callers_clause(callers)})")
    return " ".join(parts), severity


@check(
    "internal-only",
    severity=Severity.LOW,
    description="Public functions production only calls within their own file",
    advice=Advice(
        fix=(
            "Rename it with a leading underscore and update its callers in the file and"
            " the tests named."
        ),
        dismiss=(
            "Dismiss only if it is documented public API that code outside the"
            " analyzed tree imports."
        ),
    ),
)
def check_internal_only(ctx: AnalysisContext) -> list[Finding]:
    """Find public functions production only calls within their own file (2+ calls).

    These are candidates for renaming to _private. Test code does not make
    a function public: calls and imports from test-role files are reported,
    not counted, and the tests then reach for the private name.
    """
    findings = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        callers = _split_callers(ctx, ctx.call_index.get(func_name, []))
        internal_calls = [c for c in callers.production if c["file"] == def_file]

        if (
            len(internal_calls) == len(callers.production)
            and len(internal_calls) >= 2
            and not is_imported_in_production_elsewhere(func_name, def_file, ctx)
        ):
            # Functions listed in __all__ are explicitly public API.
            def_tree = ctx.all_trees.get(Path(def_file))
            if def_tree and is_in_dunder_all(func_name, def_tree):
                continue

            findings.append(
                Finding(
                    file=def_file,
                    line=defs[0]["line"],
                    check="internal-only",
                    message=(
                        f"{func_name}() is public but production only calls it within "
                        f"its own file ({_callers_clause(callers)})"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings


@check(
    "constant-args",
    severity=Severity.MEDIUM,
    description="Param always receives the same literal value from every caller",
    advice=Advice(
        fix=(
            "Remove the parameter, use the value inside the function (or as a named"
            " constant), and delete the argument from every caller."
        ),
        dismiss=(
            "Dismiss only if the function is published API whose callers outside the"
            " analyzed tree pass other values."
        ),
    ),
)
def check_constant_args(ctx: AnalysisContext) -> list[Finding]:
    """Find parameters where every caller passes the same literal value.

    If all callers pass the same constant, the value should be a default
    or a module-level constant rather than repeated at each call site.
    Only production callers count: a test passing one fixture literal
    every time says nothing about what the parameter should default to.
    Only ``function_index`` functions are judged, never a method, so
    ``CallerValues.overrides_unresolved_base`` is never set on one.
    """
    findings = []
    for func_name, defs in ctx.function_index.items():
        if len(defs) > 1:
            continue

        entry = ctx.caller_values.of(defs[0]["node"])
        calls = entry.production_callers
        if len(calls) < 2:
            continue

        for param in entry.params:
            # Parameters passed positionally or by name; positional-only and
            # keyword-only ones are not judged.
            if param.kind is not ParamKind.POSITIONAL_OR_KEYWORD:
                continue
            literals = {value.literal for value in param.production}
            if len(literals) != 1 or None in literals:
                continue
            findings.append(
                Finding(
                    file=defs[0]["file"],
                    line=defs[0]["line"],
                    check="constant-args",
                    message=(
                        f"{func_name}() param '{param.name}' always receives "
                        f"{literals.pop()} from all {len(calls)} caller(s)"
                    ),
                    severity=Severity.MEDIUM,
                    subjects=(
                        parameter_subject(str(entry.file), entry.node, param.name, vestigial=False),
                    ),
                )
            )

    return findings


def _count_meaningful_stmts(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count non-trivial top-level statements in a function body.

    Skips docstrings and pass statements.
    """
    count = 0
    for stmt in func_node.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        if isinstance(stmt, ast.Pass):
            continue
        count += 1
    return count


@check(
    "return-none-instead-of-raise",
    severity=Severity.MEDIUM,
    description="Functions returning None on error where callers all guard against None",
    advice=Advice(
        fix=(
            "Raise a specific exception where the function returns None, and replace each"
            " caller's None guard with handling of that exception (or none, to let it"
            " propagate)."
        ),
        dismiss=(
            "Dismiss only if None is an ordinary answer rather than a failure, such as a"
            " lookup whose callers treat a miss as a normal case."
        ),
    ),
)
def check_return_none_instead_of_raise(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with mixed returns (None + non-None) where callers guard against None.

    If a function returns None on error paths and most callers check
    ``if result is None:``, the function should raise instead.

    The printed "N of M callers guard" ratio credits every shape that sends
    each None down one branch: ``if``/conditional-expression tests on the
    assigned name (bare, negated, or as an operand of ``or``/``and`` whose
    polarity keeps None on one side), ``return x is [not] None``, and a
    direct ``func(...) is [not] None``. See DECISIONS.md, "Credit every guard
    shape in return-none's caller ratio". Its callers are only the calls that
    resolve to this function through the calling module's own bindings (see
    ``_CallMatcher`` and DECISIONS.md, "Return-none's callers are the call
    sites that resolve to the function"), and only production callers count:
    a test asserting ``f(x) is None`` pins the contract rather than guarding
    against it.
    """
    findings = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        func_node = defs[0].get("node")
        if func_node is None:
            continue

        if not _has_mixed_returns(func_node):
            continue

        target = _CallTarget.defined_in(ctx.module_graph, func_name, Path(defs[0]["file"]))
        guarded, unguarded = _count_none_guards(ctx, target)
        if guarded >= 2:
            total = guarded + unguarded
            findings.append(
                Finding(
                    file=defs[0]["file"],
                    line=defs[0]["line"],
                    check="return-none-instead-of-raise",
                    message=(
                        f"{func_name}() returns None in some branches and "
                        f"{guarded} of {total} caller(s) guard against None"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


def _walk_function_body(func_node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Walk the function body without descending into nested functions/classes."""
    for node in func_node.body:
        yield node
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield child


def _has_mixed_returns(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function has both None-returns and non-None-returns.

    Skips generators, void functions, and single-return functions.
    """
    # Skip generators
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return False

    none_returns = 0
    value_returns = 0

    for node in _walk_function_body(func_node):
        if not isinstance(node, ast.Return):
            continue
        if node.value is None:
            # bare return
            none_returns += 1
        elif _is_none_literal(node.value):
            # return None
            none_returns += 1
        else:
            value_returns += 1

    return none_returns >= 1 and value_returns >= 1


def _is_none_literal(node: ast.expr) -> bool:
    """Whether the expression is the literal ``None``."""
    return isinstance(node, ast.Constant) and node.value is None


def _identity_none_op(
    test: ast.expr, is_operand: Callable[[ast.expr], bool]
) -> type[ast.cmpop] | None:
    """``ast.Is`` / ``ast.IsNot`` when ``test`` compares an operand with ``None``.

    ``is_operand`` picks the operand (the guarded name, or a call to the
    flagged function); either operand order matches.
    """
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    op = type(test.ops[0])
    if op not in {ast.Is, ast.IsNot}:
        return None
    comparator = test.comparators[0]
    if (is_operand(test.left) and _is_none_literal(comparator)) or (
        is_operand(comparator) and _is_none_literal(test.left)
    ):
        return op
    return None


def _var_identity_op(test: ast.expr, var_name: str) -> type[ast.cmpop] | None:
    """``ast.Is`` / ``ast.IsNot`` when ``test`` is ``var_name is [not] None``."""
    return _identity_none_op(test, lambda node: is_bare_name(node, var_name))


def _true_whenever_none(test: ast.expr, var_name: str) -> bool:
    """Whether ``test`` is true every time ``var_name`` is None.

    ``x is None``, ``not x``, the negation of a test that is false whenever
    ``x`` is None, or an ``or`` with any such operand (``x is None or
    other``): its true branch is the only one ``None`` can reach.
    """
    if _var_identity_op(test, var_name) is ast.Is:
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _false_whenever_none(test.operand, var_name)
    return (
        isinstance(test, ast.BoolOp)
        and isinstance(test.op, ast.Or)
        and any(_true_whenever_none(value, var_name) for value in test.values)
    )


def _false_whenever_none(test: ast.expr, var_name: str) -> bool:
    """Whether ``test`` is false every time ``var_name`` is None.

    ``x is not None``, ``x``, the negation of a test that is true whenever
    ``x`` is None, or an ``and`` with any such operand (``x and other``):
    its true branch is reached only with a value.
    """
    if is_bare_name(test, var_name) or _var_identity_op(test, var_name) is ast.IsNot:
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _true_whenever_none(test.operand, var_name)
    return (
        isinstance(test, ast.BoolOp)
        and isinstance(test.op, ast.And)
        and any(_false_whenever_none(value, var_name) for value in test.values)
    )


def _is_none_guard_test(test: ast.expr, var_name: str) -> bool:
    """Whether branching on ``test`` sends every ``None`` down one branch.

    ``x is None and other`` is not a guard: when ``other`` is false, ``None``
    takes the same branch as a value.
    """
    return _true_whenever_none(test, var_name) or _false_whenever_none(test, var_name)


def _own_expression_nodes(stmt: ast.stmt):
    """Every expression node of ``stmt`` itself, not of the statements it contains."""
    for _field, value in ast.iter_fields(stmt):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, ast.expr):
                yield from ast.walk(item)


def _is_none_guard(stmt: ast.stmt, var_name: str) -> bool:
    """Whether ``stmt`` branches on ``var_name`` being None.

    An ``if`` whose test is a guard test (compound tests included, see
    ``_is_none_guard_test``), ``return x is [not] None``, or a conditional
    expression anywhere in the statement's own expressions whose test is a
    guard test (``head.get(...) if head else None``).
    """
    if isinstance(stmt, ast.If) and _is_none_guard_test(stmt.test, var_name):
        return True
    if isinstance(stmt, ast.Return) and stmt.value is not None:
        if _var_identity_op(stmt.value, var_name) is not None:
            return True
    return any(
        isinstance(node, ast.IfExp) and _is_none_guard_test(node.test, var_name)
        for node in _own_expression_nodes(stmt)
    )


def _reaches_module(imported: str, defining: str) -> bool:
    """Whether importing the dotted path ``imported`` reaches module ``defining``.

    Equal paths match, and so does a package above ``defining``: ``from
    deployer.utils import select_index`` reaches ``deployer.utils.cli``
    through the package's re-export. A directory on ``sys.path`` names the
    same file by a shorter path, so ``imported`` may also sit after a
    leading run of ``defining``'s names: ``from s3store import x`` reaches
    ``shared/s3store.py``, and a Django project's ``from works.mime import
    f`` reaches ``myapp/works/mime/detect.py`` through ``works/mime``'s
    re-export. A longer ``imported`` that ends with ``defining`` matches
    too.
    """
    return f".{imported}." in f".{defining}." or imported.endswith("." + defining)


@dataclass(frozen=True)
class _CallTarget:
    """The flagged function's name and the dotted path of the module defining it."""

    name: str
    file: Path
    module: str

    @classmethod
    def defined_in(cls, graph: ModuleGraph, name: str, file: Path) -> _CallTarget:
        """The target ``name`` defined in the analyzed ``file``, its module read from the graph."""
        return cls(name=name, file=file, module=graph.nodes[file].module)


class _CallMatcher:
    """A predicate for the calls in one module that resolve to ``target``.

    ``name(...)`` resolves where ``name`` is bound to the function in this
    module: the defining module itself, or a ``from M import name [as alias]``
    / ``from M import *`` whose ``M`` reaches the defining module. ``mod.name(...)``
    resolves where the import binding ``mod``'s root, extended by the rest
    of the chain, reaches the defining module. Anything else with the same
    bare name is a different function: a same-named method (``obj.name()``),
    a library's (``json.loads()`` for a project ``loads``), or one this module
    defines or imports from elsewhere. Text never resolves, since only call
    nodes are read.

    The bindings come from the module's ``ctx.module_graph`` node (its
    ``bindings``, the ``ctx.module_imports`` entry), replayed in walk order
    so a later binding of a name wins, as it did when the tree itself was
    walked.
    """

    @classmethod
    def in_module(cls, graph: ModuleGraph, path: Path, target: _CallTarget) -> _CallMatcher:
        """The matcher for the analyzed module at ``path``, from its graph node's bindings."""
        return cls(path, target, graph.nodes[path].bindings)

    def __init__(self, path: Path, target: _CallTarget, imports: list[ModuleImport]) -> None:
        self.target = target
        names: set[str] = {target.name} if path == target.file else set()
        modules: dict[str, str] = {}
        for imp in imports:
            # Only a from-import (``source`` set) can import the function by name.
            if imp.source is not None and imp.name in {target.name, "*"}:
                if _reaches_module(imp.source, target.module):
                    names.add(target.name if imp.name == "*" else imp.bound)
            else:
                modules[imp.bound] = imp.dotted
        self.names = frozenset(names)
        self._modules = modules

    def __call__(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in self.names
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == self.target.name):
            return False
        chain = dotted_name(node.func.value)
        if chain is None:
            return False
        root, _, rest = chain.partition(".")
        if root not in self._modules:
            return False
        module = f"{self._modules[root]}.{rest}" if rest else self._modules[root]
        return _reaches_module(module, self.target.module)


class _CallResolver:
    """Finds the calls that resolve to a target, for one target or many.

    The resolution rule is ``_CallMatcher``'s and nothing else. This only
    decides where to look: a resolving call is ``target.name(...)``,
    ``mod.target.name(...)`` or ``alias(...)`` for a name some module binds
    to the function by a from-import, so the call sites are looked up in
    ``ctx.call_index`` under those names instead of walking every tree, and
    a module's matcher is built only when it holds such a call. The
    from-import aliases are collected once, so resolving every function
    costs one pass over the imports plus the candidate calls.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self._graph = ctx.module_graph
        self._paths = {str(path): path for path in self._graph.nodes}
        aliases: dict[str, set[str]] = {}
        for node in self._graph.nodes.values():
            for imp in node.bindings:
                if imp.source is not None and imp.name is not None and imp.bound != imp.name:
                    aliases.setdefault(imp.name, set()).add(imp.bound)
        self._aliases = aliases

    def calls(self, target: _CallTarget) -> Iterator[tuple[Path, _CallMatcher, ast.Call]]:
        """Every call resolving to ``target``, by callee name, then file and walk order."""
        matchers: dict[str, _CallMatcher] = {}
        for callee in sorted({target.name, *self._aliases.get(target.name, ())}):
            for entry in self.ctx.call_index.get(callee, []):
                filename = entry["file"]
                path = self._paths[filename]
                if filename not in matchers:
                    matchers[filename] = _CallMatcher.in_module(self._graph, path, target)
                if matchers[filename](entry["node"]):
                    yield path, matchers[filename], entry["node"]


def _resolving_calls(
    ctx: AnalysisContext, target: _CallTarget
) -> Iterator[tuple[Path, _CallMatcher, ast.Call]]:
    """Every call in the analyzed code that resolves to ``target`` (see ``_CallMatcher``).

    Each call comes with its module and that module's matcher.
    """
    return _CallResolver(ctx).calls(target)


class PassedKind(Enum):
    """What a call site passes for one parameter."""

    LITERAL = "literal"
    NONE = "None"
    OMITTED = "omitted"
    OTHER = "other"


class ParamKind(Enum):
    """How a call site can bind a parameter."""

    POSITIONAL_ONLY = "positional-only"
    POSITIONAL_OR_KEYWORD = "positional-or-keyword"
    KEYWORD_ONLY = "keyword-only"


@dataclass(frozen=True)
class PassedValue:
    """What one call site passes for one parameter, and where the call is.

    ``literal`` is the ``repr`` of the constant passed, for ``LITERAL`` and
    ``NONE`` (``"None"``); it is None for ``OMITTED`` and ``OTHER``.

    ``passthrough`` marks an ``OTHER`` that no argument names: a ``*args``
    or ``**kwargs`` splat covers the parameter, so the site may fill it or
    leave the default, and nothing is known about either. Any other
    ``OTHER`` is an explicit argument whose value is not a constant: the
    parameter is passed. A check that asks whether a site passes the
    parameter must treat a passthrough as unknown, never as evidence.

    ``argument`` is the expression the site passes, None for ``OMITTED``
    and for a passthrough.
    """

    kind: PassedKind
    literal: str | None
    passthrough: bool
    file: str
    line: int
    argument: ast.expr | None

    @property
    def name(self) -> str | None:
        """The variable the site passes, when its argument is a bare name."""
        return self.argument.id if isinstance(self.argument, ast.Name) else None


# What a call passes for one parameter: kind, literal, passthrough, argument
# (see ``PassedValue``).
_Passed = tuple[PassedKind, str | None, bool, ast.expr | None]


@dataclass(frozen=True)
class ParamValues:
    """One parameter and what each call site passes for it, split by role.

    ``default`` is the parameter's default expression, None when it has
    none. ``production`` and ``tests`` hold one ``PassedValue`` per call site
    in the matching ``CallerValues.callers`` list, in the same order.
    """

    name: str
    kind: ParamKind
    default: ast.expr | None
    production: tuple[PassedValue, ...]
    tests: tuple[PassedValue, ...]


@dataclass(frozen=True)
class CallerValues:
    """One function or method, its call sites, and what each passes per parameter.

    ``owner`` is the class whose body defines a method, None for any other
    function. ``params`` follows the signature, leaving out a method's
    receiver and the ``*args``/``**kwargs`` collectors.
    ``overrides_unresolved_base`` is set on a method that may override a
    name defined on a base outside the analyzed code
    (``inheritance.OverrideResolver``): its signature is that base's, so
    every caller-value check skips it.
    """

    file: Path
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: ast.ClassDef | None
    callers: _Callers
    params: tuple[ParamValues, ...]
    overrides_unresolved_base: bool

    @property
    def is_method(self) -> bool:
        """Whether a class body defines it, so ``owner`` is that class."""
        return self.owner is not None

    @property
    def name(self) -> str:
        """The function's own name, as its definition spells it."""
        return self.node.name

    @property
    def line(self) -> int:
        """The line of the ``def``."""
        return self.node.lineno

    @property
    def class_name(self) -> str | None:
        """The name of the class whose body defines the method; None for a function."""
        return None if self.owner is None else self.owner.name

    def names_passed(self, param: str) -> Iterator[tuple[dict, str]]:
        """Each production call site that passes a bare name for ``param``, with the name.

        Nothing for a name that is not one of ``params`` (a method's receiver).
        """
        for values in (each.production for each in self.params if each.name == param):
            for call, value in zip(self.production_callers, values, strict=True):
                if value.name is not None:
                    yield call, value.name

    def encloses(self, filepath: str, line: int) -> bool:
        """Whether ``filepath:line`` sits inside this definition (a recursive call)."""
        last = self.node.end_lineno or self.node.lineno
        return filepath == str(self.file) and self.node.lineno <= line <= last

    @property
    def test_callers(self) -> list[dict]:
        """The call sites in test-role files, in the order of each ``ParamValues.tests``."""
        return self.callers.tests

    @property
    def production_callers(self) -> list[dict]:
        """The call sites in production-role files."""
        return self.callers.production

    @property
    def call_sites(self) -> list[dict]:
        """Every call site the index sees, production first, then tests."""
        return [*self.callers.production, *self.callers.tests]

    @property
    def callers_clause(self) -> str:
        """The production and test caller counts, as every caller-counting check prints them."""
        return _callers_clause(self.callers)


def param_subjects(entry: CallerValues, names: Iterable[str]) -> tuple[Subject, ...]:
    """Parameters of ``entry``'s function a finding calls vestigial, as its subjects.

    Each is scoped to the function's lines in its file (``Subject.scope``).
    """
    return tuple(
        parameter_subject(str(entry.file), entry.node, name, vestigial=True) for name in names
    )


def literal_subjects(entry: CallerValues, tokens: Iterable[str]) -> tuple[Subject, ...]:
    """Literals written in ``entry``'s function, as a finding's vestigial subjects."""
    scope = function_scope(entry.node)
    return tuple(Subject(token, str(entry.file), scope, None, vestigial=True) for token in tokens)


class CallerValueIndex:
    """``ctx.caller_values``: every analyzed function's ``CallerValues``.

    ``by_file`` lists each file's functions in ``ast.walk`` order; ``of``
    looks one up by its definition node.
    """

    def __init__(self, by_file: dict[Path, list[CallerValues]]) -> None:
        self.by_file = by_file
        self._by_node = {id(entry.node): entry for entries in by_file.values() for entry in entries}

    def of(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> CallerValues:
        """The entry for the function defined by ``node``; a KeyError for any other node."""
        return self._by_node[id(node)]

    def function_calls(
        self,
    ) -> Iterator[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, dict]]:
        """Each call site of a function outside a class body, with the function.

        Yields ``(file, definition, call)`` per call site, production and
        tests alike, in ``by_file`` order and each entry's ``call_sites``
        order. Methods are left out: their callers are matched by name only.
        """
        for path, entries in self.by_file.items():
            for entry in entries:
                if entry.is_method:
                    continue
                for call in entry.call_sites:
                    yield path, entry.node, call


def _has_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    return any(is_bare_name(dec, name) for dec in func.decorator_list)


class _Signature:
    """A function's parameters as call sites bind them.

    ``slots`` holds each parameter a call site supplies, in signature
    order, with its kind and its index in the definition's positional list
    (None for a keyword-only one). A method's receiver is left out of
    ``slots`` but keeps index 0, so a call-site position is that index less
    the receiver shift.
    """

    def __init__(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, owner: ast.ClassDef | None
    ) -> None:
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        defaults = dict(
            zip(
                (arg.arg for arg in positional[len(positional) - len(args.defaults) :]),
                args.defaults,
                strict=True,
            )
        )
        defaults.update(
            (arg.arg, default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
            if default is not None
        )
        self.defaults: dict[str, ast.expr] = defaults
        self.owner = owner
        # Every function in a class body takes the receiver as its first
        # positional parameter, whatever it is named, unless it is static.
        self.receiver = (
            owner is not None and bool(positional) and not _has_decorator(node, "staticmethod")
        )
        self.is_classmethod = _has_decorator(node, "classmethod")
        slots: list[tuple[ast.arg, ParamKind, int | None]] = []
        for index, arg in enumerate(positional):
            if index == 0 and self.receiver:
                continue
            kind = (
                ParamKind.POSITIONAL_ONLY
                if index < len(args.posonlyargs)
                else ParamKind.POSITIONAL_OR_KEYWORD
            )
            slots.append((arg, kind, index))
        slots.extend((arg, ParamKind.KEYWORD_ONLY, None) for arg in args.kwonlyargs)
        self.slots = tuple(slots)

    def receiver_shift(self, call: ast.Call) -> int:
        """How many leading positional parameters the call fills without passing them.

        A bound call (``obj.m(x)``, ``self.m(x)``, ``Cls.m(x)`` for a
        classmethod) fills the receiver itself. An unbound call through the
        defining class (``Cls.m(obj, x)``, ``mod.Cls.m(obj, x)``) passes it
        as its first argument.
        """
        if self.owner is None or not self.receiver:
            return 0
        if (
            not self.is_classmethod
            and isinstance(call.func, ast.Attribute)
            and referenced_name(call.func.value) == self.owner.name
        ):
            return 0
        return 1

    def bind(self, call: ast.Call) -> list[_Passed]:
        """What ``call`` passes for each slot, in slot order (see ``_CallArgs.passed``)."""
        shift = self.receiver_shift(call)
        args = _CallArgs.of(call)
        return [
            args.passed(
                None if index is None else index - shift,
                None if kind is ParamKind.POSITIONAL_ONLY else arg.arg,
            )
            for arg, kind, index in self.slots
        ]


@dataclass(frozen=True)
class _CallArgs:
    """A call's arguments as parameters bind them.

    ``star`` is the index of the first ``*args`` in ``positional``, or its
    length when there is none. ``double_star`` says a ``**kwargs`` is passed.
    """

    positional: list[ast.expr]
    star: int
    keywords: dict[str, ast.expr]
    double_star: bool

    @classmethod
    def of(cls, call: ast.Call) -> _CallArgs:
        starred = (i for i, arg in enumerate(call.args) if isinstance(arg, ast.Starred))
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
        return cls(
            positional=call.args,
            star=next(starred, len(call.args)),
            keywords=keywords,
            double_star=len(keywords) < len(call.keywords),
        )

    def passed(self, position: int | None, keyword: str | None) -> _Passed:
        """What the call passes for a parameter at ``position`` or named ``keyword``.

        Either is None when the parameter cannot be bound that way. Returns
        the kind, the literal, whether the parameter is a passthrough and the
        argument expression (None when nothing names the parameter). An
        argument before the first ``*args``, or one passed by name, is
        explicit. Otherwise ``*args`` at or before the position, or
        ``**kwargs``, may or may not fill the parameter: a passthrough,
        recorded as ``OTHER``, since the analysis cannot see what they hold.
        A name passed alongside ``*args`` wins, since a call that also filled
        it positionally would raise ``TypeError``.
        """
        if position is not None and position < self.star:
            return _passed(self.positional[position])
        if keyword is not None and keyword in self.keywords:
            return _passed(self.keywords[keyword])
        if position is not None and self.star < len(self.positional):
            return PassedKind.OTHER, None, True, None
        if self.double_star:
            return PassedKind.OTHER, None, True, None
        return PassedKind.OMITTED, None, False, None


def _passed(expr: ast.expr) -> _Passed:
    """A value passed explicitly: a constant is a literal (None its own kind); else other."""
    if not isinstance(expr, ast.Constant):
        return PassedKind.OTHER, None, False, expr
    kind = PassedKind.NONE if expr.value is None else PassedKind.LITERAL
    return kind, repr(expr.value), False, expr


def _method_owners(tree: ast.Module) -> dict[int, ast.ClassDef]:
    """Each function defined directly in a class body, by node id, mapped to that class."""
    return {
        id(item): node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def build_caller_values(ctx: AnalysisContext) -> CallerValueIndex:
    """Every analyzed function, its call sites, and what each passes per parameter.

    A function outside a class body is called by the calls that resolve to
    it (``_CallResolver``, the Phase 21-3 rule). A method is called by the
    attribute calls of its name (``obj.m()``, ``self.m()``, ``Cls.m()``)
    that do not resolve to a function, since the resolution rule covers
    functions bound by module and import, not by receiver. Each call site
    counts once per function it reaches and goes to production or tests by
    the role of its file.
    """
    resolver = _CallResolver(ctx)
    defined: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]] = []
    for path, tree in ctx.all_trees.items():
        owners = _method_owners(tree)
        defined.extend((path, node, owners.get(id(node))) for node in iter_functions(tree))

    calls_of: dict[int, list[dict]] = {}
    resolved: set[int] = set()
    for path, node, owner in defined:
        if owner is not None:
            continue
        target = _CallTarget.defined_in(ctx.module_graph, node.name, path)
        calls = [
            {"file": str(call_path), "line": call.lineno, "node": call}
            for call_path, _matcher, call in resolver.calls(target)
        ]
        resolved.update(id(call["node"]) for call in calls)
        calls_of[id(node)] = calls
    for _path, node, owner in defined:
        if owner is None:
            continue
        calls_of[id(node)] = [
            entry
            for entry in ctx.call_index.get(node.name, [])
            if isinstance(entry["node"].func, ast.Attribute) and id(entry["node"]) not in resolved
        ]

    overrides = OverrideResolver(ctx)
    by_file: dict[Path, list[CallerValues]] = {path: [] for path in ctx.all_trees}
    for path, node, owner in defined:
        callers = _split_callers(ctx, calls_of[id(node)])
        by_file[path].append(
            CallerValues(
                path,
                node,
                owner,
                callers,
                _param_values(node, owner, callers),
                owner is not None and overrides.overrides_unresolved_base(path, owner, node.name),
            )
        )
    return CallerValueIndex(by_file)


def _param_values(
    node: ast.FunctionDef | ast.AsyncFunctionDef, owner: ast.ClassDef | None, callers: _Callers
) -> tuple[ParamValues, ...]:
    """Each parameter of ``node`` with what every production and test call passes for it."""
    signature = _Signature(node, owner)

    def passed(calls: list[dict]) -> list[list[PassedValue]]:
        """One list per call, one value per slot."""
        return [
            [
                PassedValue(kind, literal, passthrough, call["file"], call["line"], argument)
                for kind, literal, passthrough, argument in signature.bind(call["node"])
            ]
            for call in calls
        ]

    production = passed(callers.production)
    tests = passed(callers.tests)
    return tuple(
        ParamValues(
            name=arg.arg,
            kind=kind,
            default=signature.defaults.get(arg.arg),
            production=tuple(row[slot] for row in production),
            tests=tuple(row[slot] for row in tests),
        )
        for slot, (arg, kind, _index) in enumerate(signature.slots)
    )


def _count_none_guards(ctx: AnalysisContext, target: _CallTarget) -> tuple[int, int]:
    """Count callers that guard vs don't guard against None return.

    Returns (guarded_count, unguarded_count). A caller is a call that
    resolves to this function (see ``_CallMatcher``), either assigned
    ``var = func(...)``, guarded when a following statement branches on
    ``var`` being None, or compared directly ``func(...) is [not] None``,
    which is a guard by construction. Callers that discard the return value
    (bare call statements) are ignored, and so are callers in test-role
    files, whose ``is None`` comparisons are assertions about the contract.
    """
    guarded = 0
    unguarded = 0

    for path, is_call, call in _resolving_calls(ctx, target):
        if ctx.roles.is_test(path):
            continue
        parents = ctx.parent_map(ctx.all_trees[path])
        parent = parents[call]
        if isinstance(parent, ast.Compare):
            if _identity_none_op(parent, is_call):
                guarded += 1
            continue
        # Look for: var = func(...)
        for stmt, target_name, value in iter_simple_assigns([parent]):
            if value is not call:
                continue
            if _find_guard(parents, stmt, target_name.id):
                guarded += 1
            else:
                unguarded += 1

    return guarded, unguarded


def _find_guard(parents: dict[ast.AST, ast.AST], assign_node: ast.Assign, var_name: str) -> bool:
    """Check if the assignment is followed by a None guard within 3 statements.

    The statements that follow are those after it in the block that holds it
    (a ``body``, ``orelse`` or ``finalbody``), found through the parent map.
    """
    parent = parents[assign_node]
    for attr in ("body", "orelse", "finalbody"):
        body = getattr(parent, attr, None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if stmt is assign_node:
                return any(_is_none_guard(after, var_name) for after in body[i + 1 : i + 4])
    return False


@check(
    "pass-through-params",
    severity=Severity.MEDIUM,
    description="Params received by a function and only forwarded to another function",
    advice=Advice(
        fix=(
            "Remove the parameter from this function and have the callers counted give the"
            " value to the function it is forwarded to, or bundle the forwarded values into"
            " one object."
        ),
        dismiss=(
            "Dismiss only if this function is the public boundary that external callers"
            " pass the value through, and the callee is private to it."
        ),
    ),
)
def check_pass_through_params(ctx: AnalysisContext) -> list[Finding]:
    """Find parameters that a function receives but only passes through to other functions.

    If a parameter is never used by the intermediary function — only
    forwarded to known functions in the codebase — the caller should
    pass directly to the consumer, or a context/config object should be used.
    """
    findings = []
    func_defs = ctx.function_index
    known_funcs = set(func_defs.keys())

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        func_node = defs[0].get("node")
        if func_node is None:
            continue

        # Skip orphan functions — dead-code handles those
        calls = ctx.call_index.get(func_name, [])
        if not calls:
            continue

        # Skip functions with substantial bodies — forwarding is incidental
        if _count_meaningful_stmts(func_node) > 2:
            continue

        classifications = _classify_param_uses(func_node, known_funcs)
        callers = _split_callers(ctx, calls)

        for param_name, info in classifications.items():
            if info["total_loads"] == 0:
                continue  # unused param, different smell
            if info["non_forwarding_loads"] > 0:
                continue  # used for more than forwarding
            if not info["call_targets"]:
                continue  # no known targets

            targets = sorted(info["call_targets"])
            targets_str = ", ".join(f"{t}()" for t in targets)

            findings.append(
                Finding(
                    file=defs[0]["file"],
                    line=defs[0]["line"],
                    check="pass-through-params",
                    message=(
                        f"{func_name}() receives '{param_name}' but only forwards "
                        f"it to {targets_str} ({_callers_clause(callers)})"
                    ),
                    severity=Severity.MEDIUM,
                    subjects=(
                        parameter_subject(defs[0]["file"], func_node, param_name, vestigial=False),
                    ),
                )
            )

    return findings


def _build_body_parent_map(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[int, ast.AST]:
    """Build child→parent map for function body, excluding nested functions/classes."""
    parent_map: dict[int, ast.AST] = {}
    worklist: list[ast.AST] = []
    for stmt in func_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        worklist.append(stmt)

    while worklist:
        node = worklist.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            parent_map[id(child)] = node
            worklist.append(child)

    return parent_map


def _get_call_target_name(call_node: ast.Call) -> str | None:
    """Extract the function name from a Call node, or None if complex."""
    if isinstance(call_node.func, ast.Name):
        return call_node.func.id
    if isinstance(call_node.func, ast.Attribute):
        return call_node.func.attr
    return None


def _enclosing_call_arg(node: ast.Name, parent_map: dict[int, ast.AST]) -> ast.Call | None:
    """The call this name is an argument to — positionally or by keyword."""
    parent = parent_map.get(id(node))
    if isinstance(parent, ast.Call) and node in parent.args:
        return parent
    if isinstance(parent, ast.keyword):
        grandparent = parent_map.get(id(parent))
        if isinstance(grandparent, ast.Call):
            return grandparent
    return None


def _record_param_load(
    info: dict,
    node: ast.Name,
    parent_map: dict[int, ast.AST],
    known_funcs: set[str],
) -> None:
    """Attribute one parameter reference to either forwarding or real use."""
    info["total_loads"] += 1

    call_node = _enclosing_call_arg(node, parent_map)
    target_name = _get_call_target_name(call_node) if call_node is not None else None
    if target_name and target_name in known_funcs:
        info["call_targets"].add(target_name)
    else:
        info["non_forwarding_loads"] += 1


def _classify_param_uses(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    known_funcs: set[str],
) -> dict[str, dict]:
    """Classify each parameter's uses as forwarding or non-forwarding.

    Returns a dict mapping param name to:
      total_loads: number of Name(Load) references
      non_forwarding_loads: uses outside call-arg position or to unknown targets
      call_targets: set of known function names the param is forwarded to
    """
    all_params = (
        [a.arg for a in func_node.args.posonlyargs]
        + [a.arg for a in func_node.args.args]
        + [a.arg for a in func_node.args.kwonlyargs]
    )
    param_names = {p for p in all_params if p not in {"self", "cls"}}

    if not param_names:
        return {}

    parent_map = _build_body_parent_map(func_node)

    results: dict[str, dict] = {}
    for p in param_names:
        results[p] = {
            "total_loads": 0,
            "non_forwarding_loads": 0,
            "call_targets": set(),
        }

    # Walk function body (excluding nested functions/classes)
    worklist: list[ast.AST] = []
    for stmt in func_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        worklist.append(stmt)

    while worklist:
        node = worklist.pop()

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in results:
            _record_param_load(results[node.id], node, parent_map, known_funcs)

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            worklist.append(child)

    return results


def _args_from_single_object(call_node: ast.Call) -> str | None:
    """Check if all call arguments are attributes of a single object.

    Returns the object name (e.g. 'svc') when all args are like
    svc.field1, svc.field2 — a sign that the function is just
    decomposing a data structure.  Returns None otherwise.
    """
    sources: list[str] = []
    for arg in call_node.args:
        if isinstance(arg, ast.Starred):
            return None
        if isinstance(arg, ast.Attribute):
            sources.append(ast.dump(arg.value))
        else:
            return None
    for kw in call_node.keywords:
        if kw.arg is None:  # **kwargs
            return None
        if isinstance(kw.value, ast.Attribute):
            sources.append(ast.dump(kw.value.value))
        else:
            return None

    if len(sources) < 2 or len(set(sources)) != 1:
        return None

    # Extract a readable name for the message
    first = call_node.args[0] if call_node.args else call_node.keywords[0].value
    if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name):
        return first.value.id
    return "one object"


def _get_except_handler_names(handler: ast.ExceptHandler) -> list[str]:
    """Extract exception type names from an except handler."""
    if handler.type is None:
        return ["bare except"]
    return [name for name in type_expr_names(handler.type) if name != "?"]


_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException", "bare except"})
_CONTEXTMANAGER_DECORATORS = frozenset({"contextmanager", "asynccontextmanager"})
_EXIT_METHODS = frozenset({"__exit__", "__aexit__"})
_SUPPRESS = _CallTarget(name="suppress", file=Path("<stdlib>/contextlib.py"), module="contextlib")

# A with-item whose context manager pysmelly cannot see into (``open()``, a
# library's, a lock held in a variable): neither handled nor unhandled.
_UNRESOLVED_WITH = ("with", [])


def _classify_caught(names: list[str]) -> tuple[str, list[str]]:
    """Classify the exception names one handler site catches.

    Any specific name makes the site "specific" (and only the specific
    names are reported); otherwise it is "broad".
    """
    specific = [n for n in names if n not in _BROAD_EXCEPTIONS]
    if specific:
        return "specific", specific
    return "broad", names


# The loops a ``break``/``continue`` stays inside. A nested scope
# (``NESTED_SCOPES``) a handler defines does not run in the handler's flow.
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _is_sys_exit(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and dotted_name(node.func) == "sys.exit"


def _reraises_caught(stmt: ast.Raise, caught: str | None) -> bool:
    """Whether ``stmt`` re-raises the exception its ``except`` clause caught.

    A bare ``raise`` does, and so does ``raise e`` or ``raise e from x`` when
    ``e`` is the clause's ``as`` name (``caught``; None when the clause has
    none or rebinds it). Raising anything else converts the exception.
    """
    if stmt.exc is None:
        return True
    return caught is not None and isinstance(stmt.exc, ast.Name) and stmt.exc.id == caught


def _may_leave(node: ast.AST, *, in_loop: bool) -> bool:
    """Whether a compound statement in a handler may leave the handler itself.

    It may when it holds a ``return``, a ``raise``, a ``sys.exit()``, or a
    ``break``/``continue`` that is not inside one of its own loops. Nested
    function and class bodies are not part of the handler's flow.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, NESTED_SCOPES):
            continue
        if isinstance(child, (ast.Return, ast.Raise)) or _is_sys_exit(child):
            return True
        if isinstance(child, (ast.Break, ast.Continue)) and not in_loop:
            return True
        if _may_leave(child, in_loop=in_loop or isinstance(child, _LOOPS)):
            return True
    return False


def _always_reraises(stmts: list[ast.stmt], caught: str | None) -> bool:
    """Whether every path through ``stmts`` ends by re-raising the caught exception.

    A path ends at the first ``raise``, ``return``, ``break``, ``continue`` or
    ``sys.exit()`` statement it reaches, or by falling off the end of the
    handler, which swallows the exception. An ``if`` splits the path: each
    branch, followed by the statements after the ``if``, must re-raise. Any
    other compound statement (a loop, ``with``, ``try``, ``match``) is one
    step the path falls through, unless it may leave the handler itself
    (``_may_leave``); then the path is not known to re-raise.
    """
    for index, stmt in enumerate(stmts):
        if isinstance(stmt, ast.Raise):
            return _reraises_caught(stmt, caught)
        if isinstance(stmt, (ast.Return, ast.Break, ast.Continue)) or (
            isinstance(stmt, ast.Expr) and _is_sys_exit(stmt.value)
        ):
            return False
        if isinstance(stmt, ast.If):
            rest = stmts[index + 1 :]
            return _always_reraises([*stmt.body, *rest], caught) and _always_reraises(
                [*stmt.orelse, *rest], caught
            )
        if not isinstance(stmt, NESTED_SCOPES) and _may_leave(
            stmt, in_loop=isinstance(stmt, _LOOPS)
        ):
            return False
    return False


def _passes_through(handler: ast.ExceptHandler) -> bool:
    """Whether an ``except`` clause cleans up rather than handles.

    It does when every path through its body ends by re-raising the
    exception it caught (``_always_reraises``): logging, rolling back or
    deleting first changes nothing. A clause that raises a different
    exception converts it, and a clause with any path that returns, falls
    off the end or exits handles it. See DECISIONS.md, "Catch-and-reraise
    is pass-through".
    """
    caught = handler.name
    if caught is not None and any(
        isinstance(node, ast.Name) and node.id == caught and not isinstance(node.ctx, ast.Load)
        for node in ast.walk(handler)
    ):
        caught = None
    return _always_reraises(handler.body, caught)


def _handling_clauses(handlers: list[ast.ExceptHandler]) -> list[ast.ExceptHandler]:
    """The ``except`` clauses that handle what they catch (not pass-through)."""
    return [handler for handler in handlers if not _passes_through(handler)]


@dataclass(frozen=True)
class _ContextManagerDef:
    """A context manager the analyzed code defines, by the function that handles.

    ``function`` is a class's own ``__exit__``/``__aexit__`` when
    ``is_exit_method``, else the ``@contextmanager`` generator itself.
    """

    name: str
    is_exit_method: bool
    function: ast.FunctionDef | ast.AsyncFunctionDef


def _context_manager_def(node: ast.stmt) -> _ContextManagerDef | None:
    """The context manager a top-level statement defines, or None.

    A class with its own ``__exit__``/``__aexit__`` (an inherited one is not
    seen), or a function decorated ``@contextmanager``/``@asynccontextmanager``
    (bare or ``contextlib.``).
    """
    if isinstance(node, ast.ClassDef):
        method = _exit_method(node)
        return (
            None
            if method is None
            else _ContextManagerDef(node.name, is_exit_method=True, function=method)
        )
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        (name := dotted_name(decorator)) is not None
        and name.rpartition(".")[2] in _CONTEXTMANAGER_DECORATORS
        for decorator in node.decorator_list
    ):
        return _ContextManagerDef(node.name, is_exit_method=False, function=node)
    return None


def _exit_method(node: ast.ClassDef) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The class's own ``__exit__``/``__aexit__``, or None."""
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in _EXIT_METHODS:
            return stmt
    return None


def _bind_arguments(
    func: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call
) -> dict[str, list[ast.expr]] | None:
    """Map each of ``func``'s parameters to the expressions ``call`` passes it.

    A ``*args`` parameter maps to every surplus positional argument; an
    unpassed parameter maps to its default. Returns None when the call
    unpacks (``*xs``/``**kw``), since what it binds is then unknowable.
    """
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        kw.arg is None for kw in call.keywords
    ):
        return None
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    bound: dict[str, list[ast.expr]] = {}
    defaulted = positional[len(positional) - len(args.defaults) :]
    for param, default in zip(defaulted, args.defaults, strict=True):
        bound[param.arg] = [default]
    for param, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if kw_default is not None:
            bound[param.arg] = [kw_default]
    # A call may pass fewer positionals (defaults fill in) or more (*args).
    for param, value in zip(positional, call.args, strict=False):
        bound[param.arg] = [value]
    if args.vararg is not None:
        bound[args.vararg.arg] = call.args[len(positional) :]
    for kw in call.keywords:
        if kw.arg is not None:
            bound[kw.arg] = [kw.value]
    return bound


def _caught_names(
    type_expr: ast.expr | None,
    bound: dict[str, list[ast.expr]],
    params: set[str],
) -> list[str] | None:
    """The exception names one ``except`` clause catches, through parameters.

    A name that is a parameter of the enclosing context manager stands for
    whatever the ``with`` passed it (``except excs`` in ``exit_on(*excs)``).
    Returns None when a type cannot be named: an unbound parameter or an
    expression ``type_expr_names`` cannot read.
    """
    if type_expr is None:
        return ["bare except"]
    elements = type_expr.elts if isinstance(type_expr, ast.Tuple) else [type_expr]
    names: list[str] = []
    for element in elements:
        if isinstance(element, ast.Name) and element.id in params:
            if element.id not in bound:
                return None
            for value in bound[element.id]:
                value_names = _caught_names(value, {}, set())
                if value_names is None:
                    return None
                names.extend(value_names)
            continue
        element_names = type_expr_names(element)
        if "?" in element_names:
            return None
        names.extend(element_names)
    return names


def _generator_handling(
    func: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call
) -> tuple[str, list[str]] | None:
    """How a ``@contextmanager`` generator handles what its ``with`` body raises.

    The body's exceptions are thrown in at the ``yield``, so they are caught
    by the ``except`` clauses of every ``try`` whose body holds that yield.
    Returns None when no such clause handles (the exception passes through):
    there is none, or each one only cleans up and re-raises.
    """
    handlers = [
        handler
        for node in ast.walk(func)
        if isinstance(node, ast.Try)
        and any(isinstance(inner, ast.Yield) for stmt in node.body for inner in ast.walk(stmt))
        for handler in _handling_clauses(node.handlers)
    ]
    if not handlers:
        return None
    bound = _bind_arguments(func, call)
    if bound is None:
        return _UNRESOLVED_WITH
    params = {a.arg for a in ast.walk(func.args) if isinstance(a, ast.arg)}
    names: list[str] = []
    for handler in handlers:
        handler_names = _caught_names(handler.type, bound, params)
        if handler_names is None:
            return _UNRESOLVED_WITH
        names.extend(handler_names)
    return _classify_caught(names)


def _exit_suppresses_or_converts(node: ast.AST) -> bool:
    """Whether one node of an ``__exit__`` ends the exception's propagation.

    ``return True`` or ``return isinstance(...)``/``issubclass(...)``
    suppresses it; ``raise X`` and ``sys.exit()`` replace it, which is what
    an ``except`` clause that converts or exits does too.
    """
    if isinstance(node, ast.Return):
        value = node.value
        if isinstance(value, ast.Constant):
            return value.value is True
        return isinstance(value, ast.Call) and dotted_name(value.func) in {
            "isinstance",
            "issubclass",
        }
    if isinstance(node, ast.Raise):
        return node.exc is not None
    return _is_sys_exit(node)


def _exit_tested_names(node: ast.AST, params: set[str]) -> list[str]:
    """Exception names one node of an ``__exit__`` tests a parameter against.

    ``issubclass(exc_type, X)``, ``isinstance(exc, X)`` and ``exc_type is X``
    (or ``==``) all name ``X``.
    """
    if (
        isinstance(node, ast.Call)
        and dotted_name(node.func) in {"isinstance", "issubclass"}
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in params
    ):
        return [n for n in type_expr_names(node.args[1]) if n != "?"]
    if (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id in params
        and all(isinstance(op, (ast.Is, ast.Eq)) for op in node.ops)
    ):
        return [
            n
            for comparator in node.comparators
            if not isinstance(comparator, ast.Constant)
            for n in type_expr_names(comparator)
            if n != "?"
        ]
    return []


def _exit_handling(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, list[str]] | None:
    """How a class's ``__exit__``/``__aexit__`` handles what its ``with`` body raises.

    It handles when it suppresses or converts (see
    ``_exit_suppresses_or_converts``); what it handles is every exception
    type it tests its parameters against, or everything ("broad") when it
    tests none. Returns None when it neither suppresses nor converts.
    """
    if not any(_exit_suppresses_or_converts(node) for node in ast.walk(method)):
        return None
    params = {a.arg for a in [*method.args.posonlyargs, *method.args.args][1:]}
    names = [n for node in ast.walk(method) for n in _exit_tested_names(node, params)]
    if not names:
        return "broad", []
    return _classify_caught(names)


def _suppress_handling(call: ast.Call) -> tuple[str, list[str]]:
    """``contextlib.suppress(...)`` catches exactly the types it is passed."""
    names = _caught_names(ast.Tuple(elts=list(call.args)), {}, set())
    if not names:
        return _UNRESOLVED_WITH
    return _classify_caught(names)


class _ContextManagers:
    """The context managers the analyzed code defines, resolved per ``with`` item.

    Indexes every top-level ``@contextmanager``/``@asynccontextmanager``
    function and every top-level class with its own ``__exit__``/``__aexit__``.
    A ``with`` item resolves to one of them only through the module's own
    bindings (``_CallMatcher``), the same rule return-none's callers use.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self._ctx = ctx
        self._defs: dict[str, list[tuple[_CallTarget, _ContextManagerDef]]] = {}
        for path, tree in ctx.all_trees.items():
            for node in tree.body:
                cm = _context_manager_def(node)
                if cm is not None:
                    target = _CallTarget.defined_in(ctx.module_graph, cm.name, path)
                    self._defs.setdefault(cm.name, []).append((target, cm))
        self._matchers: dict[tuple[Path, _CallTarget], _CallMatcher] = {}

    def _resolves(self, path: Path, target: _CallTarget, expr: ast.expr) -> bool:
        key = (path, target)
        if key not in self._matchers:
            self._matchers[key] = _CallMatcher.in_module(self._ctx.module_graph, path, target)
        return self._matchers[key](expr)

    def handling(self, expr: ast.expr, path: Path) -> tuple[str, list[str]] | None:
        """How the context manager ``expr`` builds handles its body's exceptions.

        Returns ("specific"/"broad", names) as a ``try`` would be classified,
        None when it is known to let them through, or the unresolved marker
        ("with", []) when pysmelly cannot see into it.
        """
        if not isinstance(expr, ast.Call):
            return _UNRESOLVED_WITH
        if self._resolves(path, _SUPPRESS, expr):
            return _suppress_handling(expr)
        matches = [
            cm
            for target, cm in self._defs.get(referenced_name(expr.func) or "", [])
            if self._resolves(path, target, expr)
        ]
        if len(matches) != 1:
            return _UNRESOLVED_WITH
        (cm,) = matches
        if cm.is_exit_method:
            return _exit_handling(cm.function)
        return _generator_handling(cm.function, expr)


def _classify_error_handling(
    call: dict, ctx: AnalysisContext, cms: _ContextManagers
) -> tuple[str, list[str]]:
    """Classify the error handling around one call-index entry.

    Walks out from the call to the innermost construct that handles what it
    raises: a ``try`` whose body holds the call and which has an ``except``
    clause that does more than clean up and re-raise (``_passes_through``),
    or a ``with``/``async with`` whose body holds it and whose context
    manager catches (see ``_ContextManagers.handling``). A ``with`` known to
    let exceptions through is passed over, and so is one pysmelly cannot see
    into, but the latter is remembered.

    Returns (category, exception_names) where category is one of:
    - "specific" — catches named exception types (not Exception/BaseException)
    - "broad" — catches Exception, BaseException, or bare except
    - "with" — no handler found, but the call sits inside a ``with`` whose
      context manager is not in the analyzed code (``open()``, a library's),
      so it may be handled there: counted neither handled nor unhandled
    - "unhandled" — none of the above
    """
    path = Path(call["file"])
    parents = ctx.parent_map(ctx.all_trees[path])
    inside_unresolved_with = False
    child: ast.AST = call["node"]
    while child in parents:
        current = parents[child]
        # Only the body runs under the handler: a call in an except/else/
        # finally clause or a with-item expression is outside it.
        # A try with no except clause (try/finally), or whose every clause
        # cleans up and re-raises, lets the exception through.
        if isinstance(current, ast.Try) and child in current.body:
            handlers = _handling_clauses(current.handlers)
            if handlers:
                return _classify_caught(
                    [n for handler in handlers for n in _get_except_handler_names(handler)]
                )
        if isinstance(current, (ast.With, ast.AsyncWith)) and child in current.body:
            # The last item is the innermost context manager.
            for item in reversed(current.items):
                handling = cms.handling(item.context_expr, path)
                if handling == _UNRESOLVED_WITH:
                    inside_unresolved_with = True
                elif handling is not None:
                    return handling
        child = current
    return _UNRESOLVED_WITH if inside_unresolved_with else ("unhandled", [])


def _enclosing_function(
    call: dict, ctx: AnalysisContext
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The innermost function whose body holds the call-index entry ``call``."""
    parents = ctx.parent_map(ctx.all_trees[Path(call["file"])])
    return next(enclosing_functions(call["node"], parents), None)


def _any_caller_catches(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    exc_names: set[str],
    ctx: AnalysisContext,
    cms: _ContextManagers,
) -> bool:
    """Check if any production caller of ``function`` catches one of the given exceptions.

    Follows exception propagation through wrappers: if a caller doesn't
    handle the exception, checks whether that caller's own callers do.
    The callers are the caller-value index's (``ctx.caller_values``): for a
    function outside a class body, the calls that resolve to it (the Phase
    21-3 rule), so a same-named method or library call (``cur.execute()``
    for a module-level ``execute``) credits nothing.

    The walk visits each function once: whether a function's callers catch
    does not depend on the path that reached it, so a breadth-first search
    with one visited set answers the question without re-walking a call
    graph whose paths multiply.
    """
    visited = {id(function)}
    queue = deque([function])
    while queue:
        for call in ctx.caller_values.of(queue.popleft()).production_callers:
            category, caught = _classify_error_handling(call, ctx, cms)
            if category == "specific" and exc_names.intersection(caught):
                return True
            if category == "broad":
                return True
            # An unresolved with most likely lets the exception through too.
            if category in {"unhandled", "with"}:
                wrapper = _enclosing_function(call, ctx)
                if wrapper is not None and id(wrapper) not in visited:
                    visited.add(id(wrapper))
                    queue.append(wrapper)
    return False


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# The argparse method that names a subcommand's handler: ``sub.set_defaults(func=handler)``.
_ARGPARSE_DISPATCH = "set_defaults"


class _EntryPointScopes:
    """The definitions whose bodies are an entry point's own code (Phase 33-2).

    They are the function and class roots of ``ctx.entry_points`` (a click
    command is one, and so is a Django view or management command), plus the
    argparse handlers a parser dispatches to: each module-level function a
    ``.set_defaults(...)`` keyword names, resolved by the Phase 21-3 rule
    (``_CallMatcher``). A call sits at an entry point when one of these
    definitions encloses it, at any depth.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self._ctx = ctx
        self._scopes: dict[Path, set[ast.AST]] = {}
        for root in ctx.entry_points.roots:
            if root.file is not None and isinstance(root.node, _SCOPE_NODES):
                self._scopes.setdefault(root.file, set()).add(root.node)
        for path, handler in self._argparse_handlers():
            self._scopes.setdefault(path, set()).add(handler)

    def _argparse_handlers(self) -> Iterator[tuple[Path, ast.AST]]:
        """Every module-level function a production ``.set_defaults(...)`` keyword names."""
        ctx = self._ctx
        defs: dict[str, list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
        for path, tree in ctx.all_trees.items():
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.setdefault(node.name, []).append((path, node))
        for path, tree in ctx.all_trees.items():
            if ctx.roles.is_test(path):
                continue
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == _ARGPARSE_DISPATCH
                ):
                    continue
                for keyword in node.keywords:
                    # The 21-3 matcher reads a call, so the reference is read as one.
                    as_call = ast.Call(func=keyword.value, args=[], keywords=[])
                    for def_path, handler in defs.get(referenced_name(keyword.value) or "", []):
                        graph = ctx.module_graph
                        target = _CallTarget.defined_in(graph, handler.name, def_path)
                        if _CallMatcher.in_module(graph, path, target)(as_call):
                            yield def_path, handler

    def encloses(self, call: dict) -> bool:
        """Whether an entry point's definition encloses the call-index entry ``call``."""
        path = Path(call["file"])
        scopes = self._scopes.get(path)
        if not scopes:
            return False
        parents = self._ctx.parent_map(self._ctx.all_trees[path])
        node: ast.AST = call["node"]
        while node in parents:
            node = parents[node]
            if node in scopes:
                return True
        return False


@check(
    "inconsistent-error-handling",
    severity=Severity.MEDIUM,
    description="Callers handle one function's errors divergently (try, or a catching with)",
    advice=Advice(
        fix=(
            "Decide the function's error contract and make every caller follow it: catch"
            " the specific exceptions everywhere, or nowhere and let them propagate to the"
            " entry point."
        ),
        dismiss=(
            "Dismiss only if the unhandled callers sit where an enclosing layer already"
            " handles the same exceptions."
        ),
    ),
)
def check_inconsistent_error_handling(ctx: AnalysisContext) -> list[Finding]:
    """Find functions called from 3+ sites with divergent error handling.

    Flags when at least one caller catches specific exceptions (proving there's
    a known failure mode) while other callers catch broad Exception or don't
    handle errors at all.

    A ``with`` block is handling when its context manager, defined in the
    analyzed code, catches: a ``@contextmanager`` generator's ``except``
    around its ``yield``, or an ``__exit__`` that suppresses or converts. It
    is classified like the equivalent ``try``. A call inside any other
    ``with`` (``open()``, a library's context manager) counts neither as
    handled nor as unhandled, and the message says how many sat there. See
    DECISIONS.md, "inconsistent-error-handling sees ``with`` blocks".

    An ``except`` clause (in a ``try`` or around a generator's ``yield``)
    whose every path re-raises what it caught is cleanup, not handling: the
    walk continues outward past it. See DECISIONS.md, "Catch-and-reraise is
    pass-through".

    A site inside an entry point (``_EntryPointScopes``) that catches is
    deliberate layering, not evidence of an unclear contract: it is set
    aside, and the message reports it separately ("N entry-point sites
    catch"). Its catch still credits the library sites that propagate to it.
    See DECISIONS.md, "Entry-point catches and library keywords (Phase 33-2)".

    The callers, here and up the propagation walk, are the calls that
    resolve to the function (``ctx.caller_values``, the Phase 21-3 rule). A
    site credited through propagation is reported as propagating ("N
    propagate to callers that catch"), not among the sites that catch. See
    DECISIONS.md, "36-3 (calls): callers are the calls that resolve".
    """
    findings = []
    func_index = ctx.function_index
    cms = _ContextManagers(ctx)
    entry_scopes = _EntryPointScopes(ctx)

    for func_name, defs in func_index.items():
        if len(defs) != 1:
            continue

        # The production calls that resolve to the function (the Phase 21-3
        # rule): a same-named method or library call (os.environ.get() for a
        # project get) is not a caller, and an aliased from-import is.
        calls = ctx.caller_values.of(defs[0]["node"]).production_callers
        if len(calls) < 3:
            continue

        sites, specific_names, entry_names = _classify_callers(calls, ctx, cms, entry_scopes)

        # Only flag when at least one caller catches specific exceptions
        # AND at least one other caller is broad or unhandled
        if not sites["specific"]:
            continue
        if not sites["broad"] and not sites["unhandled"]:
            continue
        _credit_propagation(sites, specific_names | entry_names, ctx, cms)
        if not sites["broad"] and not sites["unhandled"]:
            continue

        findings.append(
            Finding(
                file=defs[0]["file"],
                line=defs[0]["line"],
                check="inconsistent-error-handling",
                message=_error_handling_message(func_name, sites, specific_names),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _classify_callers(
    calls: list[dict],
    ctx: AnalysisContext,
    cms: _ContextManagers,
    entry_scopes: _EntryPointScopes,
) -> tuple[dict[str, list[dict]], set[str], set[str]]:
    """Group call sites by category, with the specific names caught.

    A site that catches (specifically or broadly) inside an entry point goes
    to "entry" instead. Returns the sites, the names the counted specific
    sites catch, and the names the entry-point sites catch.
    """
    sites: dict[str, list[dict]] = {
        "specific": [],
        "propagated": [],
        "broad": [],
        "unhandled": [],
        "with": [],
        "entry": [],
    }
    specific_names: set[str] = set()
    entry_names: set[str] = set()
    for call in calls:
        category, exc_names = _classify_error_handling(call, ctx, cms)
        if category in {"specific", "broad"} and entry_scopes.encloses(call):
            sites["entry"].append(call)
            entry_names.update(exc_names)
            continue
        sites[category].append(call)
        if category == "specific":
            specific_names.update(exc_names)
    return sites, specific_names, entry_names


def _credit_propagation(
    sites: dict[str, list[dict]],
    specific_names: set[str],
    ctx: AnalysisContext,
    cms: _ContextManagers,
) -> None:
    """Move deliberately propagating sites to "propagated", in place.

    An unhandled (or unresolved-with) site inside a function whose own
    callers catch the specific exceptions is propagating them on purpose.
    It counts as handled, but apart from the sites that catch: it catches
    nothing itself, so the message does not label it with their names.
    """
    for category in ("unhandled", "with"):
        still_open: list[dict] = []
        for call in sites[category]:
            wrapper = _enclosing_function(call, ctx)
            if wrapper is not None and _any_caller_catches(wrapper, specific_names, ctx, cms):
                sites["propagated"].append(call)
            else:
                still_open.append(call)
        sites[category] = still_open


_CATEGORY_LABELS = {
    "broad": "catch broad Exception",
    "unhandled": "unhandled",
    "with": "inside an unresolved `with`",
}


def _error_handling_message(
    func_name: str, sites: dict[str, list[dict]], specific_names: set[str]
) -> str:
    """One sentence naming how many sites fall in each non-empty category."""
    labels = {"specific": f"catch specific ({', '.join(sorted(specific_names))})"}
    if propagated := len(sites["propagated"]):
        labels["propagated"] = (
            "propagate to callers that catch"
            if propagated != 1
            else "propagates to a caller that catches"
        )
    labels.update(_CATEGORY_LABELS)
    parts = [
        f"{len(sites[category])} {label}" for category, label in labels.items() if sites[category]
    ]
    if entry := len(sites["entry"]):
        parts.append(f"{entry} entry-point site{'s catch' if entry != 1 else ' catches'}")
    total = sum(len(category_sites) for category_sites in sites.values())
    return (
        f"{func_name}() has {total} callers with inconsistent error handling: "
        f"{', '.join(parts)} — error contract is unclear"
    )


# --- vestigial-params helpers ---


def _is_stub_body(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function body is a stub (pass, ..., bare return, raise NotImplementedError)."""
    body = body_without_docstring(func_node.body)
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and is_ellipsis(stmt.value):
        return True
    # bare return / return None — unimplemented function (often with # TODO)
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True
        if _is_none_literal(stmt.value):
            return True
    return raises_not_implemented(stmt)


def _has_interface_decorator(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function is decorated with @abstractmethod or @override."""
    for deco in func_node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id in {"abstractmethod", "override"}:
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in {
            "abstractmethod",
            "override",
        }:
            return True
    return False


def _find_unused_params(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Find parameter names never referenced in the function body."""
    param_names: list[str] = []
    first_real_param = True
    for arg in func_node.args.posonlyargs + func_node.args.args + func_node.args.kwonlyargs:
        if arg.arg in {"self", "cls"}:
            continue
        if arg.arg.startswith("_"):
            continue
        # Skip 'request' as first non-self param (web framework dispatch)
        if first_real_param and arg.arg == "request":
            first_real_param = False
            continue
        first_real_param = False
        param_names.append(arg.arg)

    if not param_names:
        return []

    # Collect all Name references in the body
    used_names: set[str] = set()
    for stmt in func_node.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name):
                used_names.add(node.id)

    return [p for p in param_names if p not in used_names]


@check(
    "vestigial-params",
    severity=Severity.MEDIUM,
    description="Function parameters declared but never referenced in the body",
    advice=Advice(
        fix=("Delete the parameter and the argument every caller passes for it."),
        dismiss=(
            "Dismiss only if the signature is dictated from outside, a callback or hook"
            " whose caller passes that parameter; prefix it with an underscore then."
        ),
    ),
)
def check_vestigial_params(ctx: AnalysisContext) -> list[Finding]:
    """Find parameters that exist in a function signature but are never used.

    Parameters accumulate as features iterate: format_type is added for
    multi-format support, the format handling is later removed, but the
    parameter remains in the signature and all callers still pass it.

    The cross-file caller count shows blast radius: removing the vestigial
    parameter means updating every call site.
    """
    findings = []

    for filepath, tree in source_trees(ctx):
        for node in iter_functions(tree):
            # Skip stubs, interface methods, and framework hook methods
            if _is_stub_body(node):
                continue
            if _has_interface_decorator(node):
                continue
            if framework_dispatch_decorators(node):
                continue
            if node.name in FRAMEWORK_HOOK_METHODS:
                continue
            if has_dictated_signature(node):
                continue

            unused = _find_unused_params(node)
            if not unused:
                continue

            # Count callers for cross-file context
            callers = ctx.call_index.get(node.name, [])
            caller_count = len(callers)

            for param_name in sorted(unused):
                if caller_count > 0:
                    msg = (
                        f"{param_name} is declared but never used in "
                        f"{node.name}() — {caller_count} caller(s) still pass it"
                    )
                else:
                    msg = f"{param_name} is declared but never used in {node.name}()"

                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="vestigial-params",
                        message=msg,
                        severity=Severity.MEDIUM,
                        subjects=(
                            parameter_subject(str(filepath), node, param_name, vestigial=True),
                        ),
                    )
                )

    return findings


# --- dict-as-dataclass ---


def _collect_dict_returning_functions(ctx: AnalysisContext) -> dict[str, list[dict]]:
    """Find functions that return dict literals with 4+ string keys.

    Returns ``{func_name: [{"file", "line", "keys", "node", "enclosing_class"}, ...]}``.
    """
    results: dict[str, list[dict]] = {}
    for filepath, tree in source_trees(ctx):
        parent_map = ctx.parent_map(tree)
        for node in iter_functions(tree):
            dict_keys: set[str] = set()
            for child in ast.walk(node):
                if child is node:
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not isinstance(child, ast.Return):
                    continue
                if child.value is None:
                    continue
                val = child.value
                if not isinstance(val, ast.Dict):
                    continue
                keys: set[str] = set()
                for k in val.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
                if len(keys) >= 4:
                    dict_keys |= keys

            if len(dict_keys) >= 4:
                results.setdefault(node.name, []).append(
                    {
                        "file": str(filepath),
                        "line": node.lineno,
                        "keys": dict_keys,
                        "node": node,
                        "enclosing_class": _enclosing_class(node, parent_map),
                    }
                )

    return results


def _enclosing_class(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    """Return the innermost ClassDef enclosing ``node``, or None."""
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, ast.ClassDef):
            return cur
        cur = parent_map.get(cur)
    return None


def _build_dataclass_like_class_names(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Names of classes that act as typed-record containers across the codebase:
    ``@dataclass``-decorated classes plus subclasses of ``BaseModel`` (Pydantic)
    or ``NamedTuple`` (typing). Used to recognize wire-format serializers that
    project a typed record into a dict.
    """
    result: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if has_dataclass_decorator(node):
                result.add(node.name)
                continue
            for base in node.bases:
                base_name: str | None = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name in {"BaseModel", "NamedTuple"}:
                    result.add(node.name)
                    break
    return result


def _iter_returns_no_nested(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
):
    """Yield ``Return`` statements in ``func``, without descending into nested
    functions or classes."""
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            yield node
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _expr_references_name(expr: ast.expr, name: str) -> bool:
    """True if ``expr`` references the local name ``name`` anywhere."""
    return any(is_bare_name(node, name) for node in ast.walk(expr))


def _projects_dataclass_source(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    enclosing_class: ast.ClassDef | None,
    dataclass_classes: set[str],
) -> str | None:
    """If the function returns a dict literal that is essentially a projection
    of a dataclass-typed source, return the source class name; else None.

    Candidate sources:
      - ``self``/``cls`` when the enclosing class is dataclass-like
      - any parameter annotated with a known dataclass-like class name

    A return-dict counts as a projection when at most one of its values does
    NOT reference the source — i.e. the dict is essentially a re-shaping of
    that record (allowing one computed/constant entry alongside direct field
    accesses).
    """
    sources: dict[str, str] = {}
    args = func_node.args

    if (
        enclosing_class is not None
        and enclosing_class.name in dataclass_classes
        and args.args
        and args.args[0].arg in {"self", "cls"}
    ):
        sources[args.args[0].arg] = enclosing_class.name

    for arg in args.args:
        if arg.annotation is None:
            continue
        type_name: str | None = None
        if isinstance(arg.annotation, ast.Name):
            type_name = arg.annotation.id
        elif isinstance(arg.annotation, ast.Attribute):
            type_name = arg.annotation.attr
        if type_name in dataclass_classes:
            sources[arg.arg] = type_name

    if not sources:
        return None

    for ret in _iter_returns_no_nested(func_node):
        if not isinstance(ret.value, ast.Dict):
            continue
        values = [v for v in ret.value.values if v is not None]
        if len(values) < 4:
            continue
        for source_name, source_class in sources.items():
            referenced = sum(1 for v in values if _expr_references_name(v, source_name))
            if referenced >= len(values) - 1:
                return source_class

    return None


@check(
    "dict-as-dataclass",
    severity=Severity.MEDIUM,
    description="Functions returning dict literals with 4+ keys — consider a dataclass",
    advice=Advice(
        fix=(
            "Return a dataclass or NamedTuple with these fields, and change the files that"
            " read the keys to read attributes."
        ),
        dismiss=(
            "Dismiss only if the dict leaves the program as is (a JSON response, a template"
            " context, a serialized payload) and nothing in the code reads its keys."
        ),
    ),
)
def check_dict_as_dataclass(ctx: AnalysisContext) -> list[Finding]:
    """Find functions returning large dict literals that should be dataclasses.

    Suppresses functions that project an existing dataclass/BaseModel/NamedTuple
    into a dict — that's wire-format serialization, not a missing dataclass.
    """
    findings = []

    dict_funcs = _collect_dict_returning_functions(ctx)
    if not dict_funcs:
        return findings

    dataclass_classes = _build_dataclass_like_class_names(ctx.all_trees)

    for func_name, defs in dict_funcs.items():
        for func_def in defs:
            if _projects_dataclass_source(
                func_def["node"], func_def["enclosing_class"], dataclass_classes
            ):
                continue
            keys = func_def["keys"]
            # Files that read any of these keys by subscript (``x["key"]``),
            # whatever ``x`` is.
            access_files = [
                filepath
                for filepath, _tree in source_trees(ctx)
                if not keys.isdisjoint(ctx.subscript_string_keys[filepath])
            ]

            sorted_keys = sorted(keys)
            key_str = ", ".join(sorted_keys[:6])
            if len(sorted_keys) > 6:
                key_str += ", ..."

            if access_files:
                msg = (
                    f"{func_name}() returns a dict with {len(keys)} string"
                    f" keys ({key_str}) accessed from {len(access_files)}"
                    f" file{'s' if len(access_files) != 1 else ''}"
                )
            else:
                msg = f"{func_name}() returns a dict with {len(keys)} string keys ({key_str})"

            findings.append(
                Finding(
                    file=func_def["file"],
                    line=func_def["line"],
                    check="dict-as-dataclass",
                    message=msg,
                    severity=Severity.MEDIUM,
                )
            )

    return findings
