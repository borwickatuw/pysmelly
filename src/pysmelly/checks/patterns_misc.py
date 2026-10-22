"""Pattern checks — miscellaneous: env-fallbacks, monkey-patch, booleans, arrow-code, and more."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TypeGuard

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import (
    collect_imported_names,
    enclosing_functions,
    get_param_names,
    is_bare_name,
    is_constant_reassigned,
    iter_functions,
    iter_simple_assigns,
    iter_uppercase_assigns,
    name_chain,
    source_trees,
    walk_name_assignments,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

# --- temp-accumulators helpers ---


def _method_call_on_name(node: ast.AST, var_name: str) -> str | None:
    """Return the method name for `var_name.method(...)`, or None otherwise."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == var_name
    ):
        return node.func.attr
    return None


def _joins_name(node: ast.AST, var_name: str) -> bool:
    """True for `<sep>.join(var_name)` — a join consuming var_name."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == var_name
    )


def _has_append_to(node: ast.AST, var_name: str) -> bool:
    """Check if an AST node contains an append call to var_name."""
    return any(_method_call_on_name(child, var_name) == "append" for child in ast.walk(node))


def _has_batch_flush_in_loop(siblings: list[ast.AST], var_name: str) -> bool:
    """Check if any loop body both appends to and resets var_name (batch-flush pattern)."""
    for sibling in siblings:
        if not isinstance(sibling, (ast.For, ast.AsyncFor)):
            continue
        if not _has_append_to(sibling, var_name):
            continue
        for child in ast.walk(sibling):
            # var_name = []
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
                and child.targets[0].id == var_name
                and isinstance(child.value, ast.List)
                and not child.value.elts
            ):
                return True
            # var_name.clear()
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == var_name
                and child.func.attr == "clear"
            ):
                return True
    return False


def _classify_appends(siblings: list[ast.AST], var_name: str) -> tuple[int, int, int]:
    """Classify appends by context: (loop_appends, conditional_appends, bare_appends).

    A sibling For/AsyncFor containing appends → loop_appends.
    A sibling If containing appends → conditional_appends.
    A bare Expr with an append → bare_appends.
    """
    loop = 0
    conditional = 0
    bare = 0
    for sibling in siblings:
        if not _has_append_to(sibling, var_name):
            continue
        if isinstance(sibling, (ast.For, ast.AsyncFor)):
            loop += 1
        elif isinstance(sibling, ast.If):
            conditional += 1
        else:
            bare += 1
    return loop, conditional, bare


def _assigns_bare_name(node: ast.AST, var_name: str) -> TypeGuard[ast.Assign]:
    """True when node is `<something> = var_name` with exactly one target."""
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.value, ast.Name)
        and node.value.id == var_name
    )


def _subscript_consumer_label(target: ast.Subscript) -> str | None:
    """Label a `bar[...] = foo` consumer, or None when the container isn't a name."""
    if not isinstance(target.value, ast.Name):
        return None
    key = target.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return f"{target.value.id}[{key.value!r}]"
    return f"{target.value.id}[...]"


def _describe_consumer_target(target: ast.expr) -> str | None:
    """Name the container an accumulator is assigned into, if the target is one.

    Recognizes `bar["key"] = foo`, `bar[expr] = foo` and `bar.attr = foo`.
    """
    if isinstance(target, ast.Subscript):
        return _subscript_consumer_label(target)
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"
    return None


def _find_consumer(siblings: list[ast.AST], var_name: str) -> tuple[str | None, int | None]:
    """Find where an accumulator is consumed (assigned to dict/attr, returned, passed).

    Returns (description, line) or (None, None) if no single consumer found.
    """
    consumers: list[tuple[str, int]] = []
    for stmt in siblings:
        for child in ast.walk(stmt):
            if not _assigns_bare_name(child, var_name):
                continue
            description = _describe_consumer_target(child.targets[0])
            if description is not None:
                consumers.append((description, child.lineno))
    if len(consumers) == 1:
        return consumers[0]
    return None, None


def _build_accumulator_message(
    var_name: str,
    append_count: int,
    loop_appends: int,
    conditional_appends: int,
    bare_appends: int,
    consumer_desc: str | None,
    consumer_line: int | None,
) -> tuple[Severity, str]:
    """Build severity and message for a temp-accumulator finding."""
    if loop_appends > 0:
        severity = Severity.MEDIUM
        if consumer_desc:
            message = (
                f"'{var_name}' is built by loop-and-append only to "
                f"populate {consumer_desc} (line {consumer_line})"
            )
        else:
            message = f"'{var_name}' is a loop-and-append accumulator"
    elif conditional_appends > 0 and bare_appends == 0:
        severity = Severity.LOW
        if consumer_desc:
            message = (
                f"'{var_name}' is built from {conditional_appends} "
                f"independent conditions only to populate "
                f"{consumer_desc} (line {consumer_line})"
            )
        else:
            message = (
                f"'{var_name}' is built from {conditional_appends} "
                f"independent conditions then joined/checked"
            )
    else:
        severity = Severity.MEDIUM
        if consumer_desc:
            message = (
                f"'{var_name}' is a temporary accumulator "
                f"({append_count} appends) only to populate "
                f"{consumer_desc} (line {consumer_line})"
            )
        else:
            message = (
                f"'{var_name}' is a temporary accumulator ({append_count} appends then join/check)"
            )
    return severity, message


def _find_siblings_after(tree: ast.Module, target: ast.AST) -> list[ast.AST]:
    """Find statements that come after target in the same block."""
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "handlers", "finalbody"):
            block = getattr(parent, attr, None)
            if not isinstance(block, list):
                continue
            for i, child in enumerate(block):
                if child is target:
                    return block[i + 1 :]
    return []


def _tally_method_calls(siblings: list[ast.AST], var_name: str) -> Counter[str]:
    """Count the methods invoked on `var_name` across siblings, by method name."""
    return Counter(
        method
        for stmt in siblings
        for child in ast.walk(stmt)
        if (method := _method_call_on_name(child, var_name)) is not None
    )


def _tests_name_truthiness(node: ast.AST, var_name: str) -> bool:
    """True for a bare `if var_name:` statement."""
    return isinstance(node, ast.If) and is_bare_name(node.test, var_name)


def _assigns_name_into_container(node: ast.AST, var_name: str) -> bool:
    """True for `bar["key"] = var_name` or `bar.attr = var_name`."""
    return _assigns_bare_name(node, var_name) and isinstance(
        node.targets[0], (ast.Subscript, ast.Attribute)
    )


def _analyze_accumulator_usage(
    siblings: list[ast.AST], var_name: str
) -> tuple[int, int, bool, bool]:
    """Scan siblings for append/join/check patterns on var_name.

    Returns (append_count, other_uses, join_or_check, has_assignment_consumer).
    """
    methods = _tally_method_calls(siblings, var_name)
    append_count = methods["append"]
    other_uses = sum(count for name, count in methods.items() if name not in {"append", "join"})

    has_assignment_consumer = False
    consumed = methods["join"] > 0

    for subsequent in siblings:
        for child in ast.walk(subsequent):
            if _joins_name(child, var_name) or _tests_name_truthiness(child, var_name):
                consumed = True
            if _assigns_name_into_container(child, var_name):
                has_assignment_consumer = True
                consumed = True

    return append_count, other_uses, consumed, has_assignment_consumer


@check(
    "temp-accumulators",
    severity=Severity.MEDIUM,
    description="Lists built by append then joined (use comprehension)",
    advice=Advice(
        fix=(
            "Replace the list and its appends with a comprehension, a literal, or a"
            " generator passed straight to the consumer."
        ),
        dismiss=(
            "Dismiss only if each append sits under its own independent condition, so no"
            " single comprehension or literal can express the list."
        ),
    ),
)
def check_temp_accumulators(ctx: AnalysisContext) -> list[Finding]:
    """Find temporary lists used only to accumulate and join/check.

    Pattern: name = [], then appends, then join() or 'if name:'.

    Distinguishes sub-patterns:
    - Loop appending a transform → high confidence, use a comprehension (MEDIUM)
    - Multiple independent conditional appends → low confidence, accumulator
      is often the right choice for heterogeneous conditions (LOW)
    - Single consumer via assignment (bar["key"] = foo) → name the target (MEDIUM)
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        for node, target, value in iter_simple_assigns(ast.walk(tree)):
            if not isinstance(value, ast.List):
                continue
            if value.elts:
                continue

            var_name = target.id
            assign_line = node.lineno

            siblings = _find_siblings_after(tree, node)
            if not siblings:
                continue

            append_count, other_uses, join_or_check, has_assignment_consumer = (
                _analyze_accumulator_usage(siblings, var_name)
            )

            loop_appends, conditional_appends, bare_appends = _classify_appends(siblings, var_name)

            # Suppress batch-flush pattern: append + reset within same loop
            if _has_batch_flush_in_loop(siblings, var_name):
                continue

            # A loop body runs N times, so 1 append in a loop is sufficient
            min_appends = 1 if loop_appends > 0 else 2

            if append_count >= min_appends and join_or_check and other_uses == 0:
                consumer_desc, consumer_line = None, None
                if has_assignment_consumer:
                    consumer_desc, consumer_line = _find_consumer(siblings, var_name)

                severity, message = _build_accumulator_message(
                    var_name,
                    append_count,
                    loop_appends,
                    conditional_appends,
                    bare_appends,
                    consumer_desc,
                    consumer_line,
                )
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=assign_line,
                        check="temp-accumulators",
                        message=message,
                        severity=severity,
                    )
                )

    return findings


# --- env-fallbacks ---


def _get_env_call_key(node: ast.Call) -> str | None:
    """Return the env var name if this is an os.environ.get() or os.getenv() call."""
    # os.environ.get("KEY", ...)
    if (
        (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
        )
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return str(node.args[0].value)
    # os.getenv("KEY", ...)
    if (
        (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        )
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return str(node.args[0].value)
    return None


def _get_env_default(node: ast.Call) -> str | None:
    """Return repr of the default value if it's non-None, or None otherwise."""
    # Second positional arg
    if len(node.args) >= 2:
        default = node.args[1]
        if isinstance(default, ast.Constant) and default.value is None:
            return None
        if isinstance(default, ast.Constant):
            return repr(default.value)
        # Non-constant default (variable, call, etc.) — still suspicious
        if isinstance(default, ast.Name):
            return default.id
        return "..."
    # default= keyword arg
    for kw in node.keywords:
        if kw.arg == "default":
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return None
            if isinstance(kw.value, ast.Constant):
                return repr(kw.value.value)
            return "..."
    return None


@check(
    "env-fallbacks",
    severity=Severity.MEDIUM,
    description="os.environ.get() or os.getenv() with non-None defaults",
    advice=Advice(
        fix=(
            "Read the variable with os.environ[...] so a missing value fails fast, and"
            " delete the default."
        ),
        dismiss=(
            "Dismiss only if the setting is optional and the default is its documented"
            " value (a log level, a timeout), never a credential, host or path."
        ),
    ),
)
def check_env_fallbacks(ctx: AnalysisContext) -> list[Finding]:
    """Find environment variable lookups with non-None fallback defaults.

    If the config is required, it should fail fast on missing values rather
    than silently falling back to a default that masks misconfiguration.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            key_name = _get_env_call_key(node)
            if key_name is None:
                continue

            # Check for non-None default
            default = _get_env_default(node)
            if default is None:
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="env-fallbacks",
                    message=(f"os.environ.get('{key_name}', {default}) has a fallback default"),
                    severity=Severity.HIGH,
                )
            )

    return findings


# --- runtime-monkey-patch ---


def _attr_chain(node: ast.expr) -> str:
    """Build a dotted string from nested Attribute nodes."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_chain(node.value)}.{node.attr}"
    return "?"


@check(
    "runtime-monkey-patch",
    severity=Severity.MEDIUM,
    description="Function assigned to attribute of external object at module scope",
    advice=Advice(
        fix=(
            "Call the replacement where it is needed, or wrap or subclass the object,"
            " instead of patching it at import."
        ),
        dismiss=(
            "Dismiss only if the patch works around a bug in a pinned third-party version,"
            " with the upstream issue named beside it."
        ),
    ),
)
def check_runtime_monkey_patch(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level monkey-patches: obj.attr = local_function.

    Monkey-patching replaces behavior at runtime, making code harder to
    trace and debug. Consider subclassing, decoration, or dependency
    injection instead.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        # Collect locally-defined function names at module level
        local_funcs: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_funcs.add(node.name)

        if not local_funcs:
            continue

        # Collect captured originals: name = obj.attr at module level
        captured_attrs: dict[str, str] = {}
        for _stmt, target, value in iter_simple_assigns(ast.iter_child_nodes(tree)):
            if isinstance(value, ast.Attribute):
                captured_attrs[_attr_chain(value)] = target.id

        # Find module-level: obj.attr = local_func
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            if node.value.id not in local_funcs:
                continue

            target_str = _attr_chain(target)
            func_name = node.value.id

            capture_name = captured_attrs.get(target_str)
            if capture_name:
                message = (
                    f"{target_str} = {func_name} — "
                    f"monkey-patch at module scope "
                    f"(original captured as '{capture_name}')"
                )
            else:
                message = f"{target_str} = {func_name} — monkey-patch at module scope"

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="runtime-monkey-patch",
                    message=message,
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- fossilized-toggles helpers ---


def _collect_module_constants(tree: ast.Module) -> dict[str, tuple[int, object]]:
    """Find UPPER_CASE module-level names assigned literal values."""
    constants: dict[str, tuple[int, object]] = {}
    for name, lineno, value_node in iter_uppercase_assigns(tree):
        if isinstance(value_node, ast.Constant):
            constants[name] = (lineno, value_node.value)
    return constants


def _locally_shadowed(node: ast.AST, parents: dict[ast.AST, ast.AST], name: str) -> bool:
    """Whether ``name`` at ``node`` resolves to a function local, not the module constant.

    Python's lookup for a free name: the innermost enclosing function that
    binds it (a parameter or an assignment) owns it, and a ``global``
    declaration on the way out sends it to the module. A closure reading an
    outer function's local is therefore shadowed too.
    """
    for func in enclosing_functions(node, parents):
        if name in get_param_names(func):
            return True
        has_global, has_assign = walk_name_assignments(func.body, name)
        if has_global:
            return False
        if has_assign:
            return True
    return False


@dataclass(frozen=True)
class _ToggleTest:
    """A conditional test reduced to the toggle shapes this check can evaluate.

    ``names`` lists every module-constant name the test could be reading, so a
    caller can look them up before committing to an evaluation. ``evaluate``
    then resolves the test for one of those names given the constant's value.
    """

    names: tuple[str, ...]
    negated: bool = False
    # Set only for comparison shapes: the operator, plus the literal each name
    # is compared against (absent when the other operand isn't a literal).
    op: ast.cmpop | None = None
    literals: dict[str, object] = field(default_factory=dict)

    def decidable_names(self) -> tuple[str, ...]:
        """The names whose constant value alone could decide this test.

        ``evaluate`` answers None for every other name, whatever its value.
        """
        if self.op is None:
            return self.names
        if not isinstance(self.op, (ast.Eq, ast.NotEq)):
            return ()
        return tuple(name for name in self.names if name in self.literals)

    def evaluate(self, name: str, const_value: object) -> bool | None:
        """Resolve the test for `name` holding `const_value`, or None if unknown."""
        if name not in self.names:
            return None
        if self.op is None:
            # if FLAG: / if not FLAG:
            return not bool(const_value) if self.negated else bool(const_value)
        if name not in self.literals:
            return None
        other_value = self.literals[name]
        if isinstance(self.op, ast.Eq):
            return const_value == other_value
        if isinstance(self.op, ast.NotEq):
            return const_value != other_value
        return None


def _negated_name(test: ast.expr) -> str | None:
    """Return the name in `not NAME`, or None when the test isn't that shape."""
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
    ):
        return test.operand.id
    return None


def _compare_toggle_test(test: ast.Compare) -> _ToggleTest | None:
    """Reduce `NAME <op> literal` (in either operand order) to a toggle test."""
    left, right = test.left, test.comparators[0]
    names: list[str] = []
    literals: dict[str, object] = {}
    for name_side, other_side in ((left, right), (right, left)):
        if not isinstance(name_side, ast.Name):
            continue
        if name_side.id not in names:
            names.append(name_side.id)
        if isinstance(other_side, ast.Constant):
            literals[name_side.id] = other_side.value
    if not names:
        return None
    return _ToggleTest(names=tuple(names), op=test.ops[0], literals=literals)


def _toggle_condition_shape(test: ast.expr) -> _ToggleTest | None:
    """Reduce a conditional test to its toggle shape, or None if unrecognized.

    Recognized: `NAME`, `not NAME`, and a single-operator comparison with a
    name on either side.
    """
    if isinstance(test, ast.Name):
        return _ToggleTest(names=(test.id,))
    negated = _negated_name(test)
    if negated is not None:
        return _ToggleTest(names=(negated,), negated=True)
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        return _compare_toggle_test(test)
    return None


_CONDITIONAL_KEYWORDS: dict[type[ast.AST], str] = {
    ast.If: "if",
    ast.While: "while",
    ast.IfExp: "ternary",
}


@dataclass(frozen=True)
class _ToggleSite:
    """An if / while / ternary whose test has a toggle shape."""

    file: Path
    node: ast.If | ast.While | ast.IfExp
    shape: _ToggleTest
    keyword: str


def _toggle_sites(ctx: AnalysisContext) -> list[_ToggleSite]:
    """Every conditional in the analyzed files whose test has a toggle shape."""
    sites = []
    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.While, ast.IfExp)):
                continue
            shape = _toggle_condition_shape(node.test)
            if shape is not None:
                sites.append(_ToggleSite(filepath, node, shape, _CONDITIONAL_KEYWORDS[type(node)]))
    return sites


def _toggle_definition_file(
    use_file: Path, definitions: dict[Path, tuple[int, object]]
) -> Path | None:
    """Pick the definition a conditional in `use_file` references.

    A same-file definition always wins; otherwise a single cross-file
    definition is unambiguous. Returns None when several files define the name.
    """
    if use_file in definitions:
        return use_file
    if len(definitions) == 1:
        return next(iter(definitions))
    return None


@dataclass(frozen=True)
class ToggleUse:
    """One conditional a fossilized toggle decides.

    ``two_sided`` is True when the conditional carries code on both sides
    (an ``if`` with an ``else``/``elif``, or a ternary), so the source still
    holds the path the constant never takes next to the one it always does.
    """

    line: int
    result: bool
    keyword: str
    two_sided: bool


def _has_two_sides(node: ast.If | ast.While | ast.IfExp) -> bool:
    if isinstance(node, ast.IfExp):
        return True
    return isinstance(node, ast.If) and bool(node.orelse)


def _find_toggle_conditional_uses(
    ctx: AnalysisContext,
    sites: list[_ToggleSite],
    const_defs: dict[str, dict[Path, tuple[int, object]]],
) -> dict[tuple[Path, str], list[ToggleUse]]:
    """Find conditionals that reference non-reassigned constants.

    Returns {(def_filepath, name): [use, ...]}.
    """
    uses: dict[tuple[Path, str], list[ToggleUse]] = {}

    for site in sites:
        for const_name in site.shape.names:
            if const_name not in const_defs:
                continue
            definitions = const_defs[const_name]

            def_file = _toggle_definition_file(site.file, definitions)
            if def_file is None:
                continue

            value = definitions[def_file][1]
            result = site.shape.evaluate(const_name, value)
            if result is None:
                continue

            # Skip if inside a function that locally shadows the name. The
            # parent map is built only for a file that reaches this point.
            parents = ctx.parent_map(ctx.all_trees[site.file])
            if _locally_shadowed(site.node, parents, const_name):
                continue

            uses.setdefault((def_file, const_name), []).append(
                ToggleUse(site.node.lineno, result, site.keyword, _has_two_sides(site.node))
            )

    return uses


@dataclass(frozen=True)
class FossilizedToggle:
    """A never-reassigned module constant that decides the conditionals reading it."""

    file: Path
    name: str
    line: int
    value: object
    uses: tuple[ToggleUse, ...]

    def describe(self) -> str:
        """The fossilized-toggles message: the value and what it decides."""
        if len(self.uses) == 1:
            use = self.uses[0]
            msg = (
                f"{self.name} = {self.value!r} is never reassigned — "
                f"`{use.keyword}` at line {use.line} is always {use.result}"
            )
            if not use.result:
                msg += " (dead branch)"
            return msg
        all_values = {use.result for use in self.uses}
        if len(all_values) == 1:
            always_str = f"always {next(iter(all_values))}"
        else:
            always_str = "statically determinable"
        return (
            f"{self.name} = {self.value!r} is never reassigned — "
            f"controls {len(self.uses)} conditionals ({always_str})"
        )


def find_fossilized_toggles(ctx: AnalysisContext) -> list[FossilizedToggle]:
    """Every UPPER_CASE module constant, never reassigned, that a conditional reads.

    Shared by fossilized-toggles and the git-history check frozen-feature-flag,
    which raises the same toggles when blame shows them frozen.
    """
    sites = _toggle_sites(ctx)
    tested_names = {name for site in sites for name in site.shape.decidable_names()}

    # Collect non-reassigned constants that some toggle-shaped test names:
    # {name: {filepath: (lineno, value)}}. A constant no test names can decide
    # nothing, so its file's rebindings are never asked for.
    const_defs: dict[str, dict[Path, tuple[int, object]]] = {}
    for filepath, tree in ctx.all_trees.items():
        constants = {
            name: definition
            for name, definition in _collect_module_constants(tree).items()
            if name in tested_names
        }
        if not constants:
            continue
        rebindings = ctx.module_rebindings[filepath]
        for name, (lineno, value) in constants.items():
            if not is_constant_reassigned(rebindings, name, lineno):
                const_defs.setdefault(name, {})[filepath] = (lineno, value)

    if not const_defs:
        return []

    toggles = []
    for (def_filepath, const_name), use_list in _find_toggle_conditional_uses(
        ctx, sites, const_defs
    ).items():
        def_lineno, const_value = const_defs[const_name][def_filepath]
        toggles.append(
            FossilizedToggle(def_filepath, const_name, def_lineno, const_value, tuple(use_list))
        )
    return toggles


@check(
    "fossilized-toggles",
    severity=Severity.MEDIUM,
    description="Module-level constant makes conditional branches statically determinable",
    advice=Advice(
        fix=(
            "Delete the constant and the branch it never takes, keeping the live branch"
            " unconditionally."
        ),
        dismiss=(
            "Dismiss only if the constant is a kill switch kept to be flipped during an incident."
        ),
    ),
)
def check_fossilized_toggles(ctx: AnalysisContext) -> list[Finding]:
    """Find UPPER_CASE module-level constants that gate conditionals.

    A constant like ENABLE_V2_API = False that is never reassigned makes
    every ``if ENABLE_V2_API:`` always-False — the guarded branch is
    permanently dead code.
    """
    return [
        Finding(
            file=str(toggle.file),
            line=toggle.line,
            check="fossilized-toggles",
            message=toggle.describe(),
            severity=Severity.MEDIUM,
        )
        for toggle in find_fossilized_toggles(ctx)
    ]


# --- isinstance-chain ---

# Builtin type names can't gain new members, so the "every new type means
# editing every ladder" failure mode doesn't apply to ladders over them.
_BUILTIN_TYPE_NAMES = frozenset(
    {
        "str",
        "bytes",
        "bytearray",
        "int",
        "float",
        "complex",
        "bool",
        "list",
        "tuple",
        "dict",
        "set",
        "frozenset",
        "type",
        "object",
        "NoneType",
    }
)


def _is_type_name_expr(expr: ast.expr) -> bool:
    """True for ``type(x).__name__`` or ``x.__class__.__name__``."""
    if not (isinstance(expr, ast.Attribute) and expr.attr == "__name__"):
        return False
    inner = expr.value
    if isinstance(inner, ast.Call) and is_bare_name(inner.func, "type"):
        return True
    return isinstance(inner, ast.Attribute) and inner.attr == "__class__"


def _type_name_bound_vars(node: ast.AST) -> set[str]:
    """Local names bound from ``type(x).__name__`` / ``x.__class__.__name__``."""
    bound: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and _is_type_name_expr(child.value):
            bound.update(t.id for t in child.targets if isinstance(t, ast.Name))
    return bound


def _isinstance_target_names(call: ast.Call) -> set[str]:
    """Bare-name type targets of ``isinstance(x, T)`` / ``isinstance(x, (A, B))``."""
    if not (is_bare_name(call.func, "isinstance") and len(call.args) == 2):
        return set()
    type_arg = call.args[1]
    elts = type_arg.elts if isinstance(type_arg, ast.Tuple) else [type_arg]
    return {elt.id for elt in elts if isinstance(elt, ast.Name)}


def _type_name_comparison_literals(compare: ast.Compare, type_name_vars: set[str]) -> set[str]:
    """String literals an ==/!= compares against a type-name expression."""
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], (ast.Eq, ast.NotEq)):
        return set()
    found: set[str] = set()
    for expr, other in (
        (compare.left, compare.comparators[0]),
        (compare.comparators[0], compare.left),
    ):
        if not (isinstance(other, ast.Constant) and isinstance(other.value, str)):
            continue
        if _is_type_name_expr(expr) or (isinstance(expr, ast.Name) and expr.id in type_name_vars):
            found.add(other.value)
    return found


def function_dispatch_type_set(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    """Type names a function dispatches on, in either spelling.

    Counts bare-name isinstance() targets (``isinstance(x, Circle)``,
    including tuple targets) and string comparisons against
    ``type(x).__name__`` / ``x.__class__.__name__`` — including via a
    variable bound from those expressions. Dotted targets like
    ``ast.Call`` are deliberately excluded: ladders over another
    library's node types are how visitors are written, not a missed
    polymorphism opportunity in this codebase.
    """
    type_name_vars = _type_name_bound_vars(node)

    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            names |= _isinstance_target_names(child)
        elif isinstance(child, ast.Compare):
            names |= _type_name_comparison_literals(child, type_name_vars)
    return frozenset(names)


# A function with this many isinstance() calls is an isinstance-chain finding.
ISINSTANCE_MIN_CALLS = 5


def isinstance_call_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """The ``isinstance()`` calls in a function, nested functions included."""
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "isinstance"
    )


def can_recur(type_set: frozenset[str]) -> bool:
    """Whether a function's dispatch type set can make a recurring ladder.

    Two names or more, not all of them builtins.
    """
    return len(type_set) >= 2 and not type_set <= _BUILTIN_TYPE_NAMES


@check(
    "isinstance-chain",
    severity=Severity.MEDIUM,
    description="Function with many isinstance() checks suggesting missed polymorphism",
    advice=Advice(
        fix=(
            "Move the per-type behavior into methods on the types, or into one"
            " functools.singledispatch function, and delete the ladders."
        ),
        dismiss=(
            "Dismiss only if the ladder decodes untyped external data (JSON values) and is"
            " the one place that dispatches on those types."
        ),
    ),
)
def check_isinstance_chain(ctx: AnalysisContext) -> list[Finding]:
    """Find isinstance ladders: long ones, and recurring ones.

    Two forms, both suggesting a missed opportunity for polymorphism,
    a dispatch table, or functools.singledispatch:

    - A single function with 5+ isinstance() calls.
    - The same type-set ladder repeated across 3+ functions (each below
      the per-function threshold, but recurring — the "every new type
      means editing every ladder" failure mode). Counts both
      isinstance() ladders and string dispatch on
      ``type(x).__name__`` / ``x.__class__.__name__``.
    """
    findings = []
    min_recurrence = 3

    # frozenset of type names -> list of (file, line, func_name)
    ladder_sites: dict[frozenset[str], list[tuple[str, int, str]]] = {}

    for filepath, tree in ctx.all_trees.items():
        for node in iter_functions(tree):
            count = isinstance_call_count(node)
            if count >= ISINSTANCE_MIN_CALLS:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="isinstance-chain",
                        message=(f"{node.name}() has {count} isinstance() checks"),
                        severity=Severity.MEDIUM,
                    )
                )

            type_set = function_dispatch_type_set(node)
            if can_recur(type_set):
                ladder_sites.setdefault(type_set, []).append(
                    (str(filepath), node.lineno, node.name)
                )

    # Recurring ladders: same type-set dispatched on in 3+ functions
    for type_set, ladders in sorted(ladder_sites.items(), key=lambda kv: sorted(kv[0])):
        if len(ladders) < min_recurrence:
            continue
        findings.append(
            multi_site_finding(
                "isinstance-chain",
                Severity.MEDIUM,
                [Site(file, line) for file, line, _name in ladders],
                partial(
                    _recurring_ladder_message,
                    ", ".join(sorted(type_set)),
                    min_recurrence,
                    ladders,
                ),
            )
        )
    return findings


def _recurring_ladder_message(
    type_list: str,
    min_recurrence: int,
    ladders: list[tuple[str, int, str]],
    indices: tuple[int, ...],
) -> str | None:
    """isinstance-chain's recurring-ladder message over the ladders at
    ``indices``; None once fewer than ``min_recurrence`` are left."""
    shown = [ladders[index] for index in indices]
    if len(shown) < min_recurrence:
        return None
    site_strs = [f"{name}() {file}:{line}" for file, line, name in shown[:6]]
    if len(shown) > 6:
        site_strs.append("...")
    return (
        f"type dispatch on ({type_list}) repeated in"
        f" {len(shown)} functions ({', '.join(site_strs)})"
        f" — adding a type means editing every ladder"
    )


# --- boolean-param-explosion ---


def _get_boolean_params(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Return names of parameters with boolean defaults (True/False)."""
    bool_params = []
    args = func_node.args

    # Positional args with defaults (right-aligned)
    offset = len(args.args) - len(args.defaults)
    for i, default in enumerate(args.defaults):
        if isinstance(default, ast.Constant) and isinstance(default.value, bool):
            bool_params.append(args.args[i + offset].arg)

    # Keyword-only args
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if (
            default is not None
            and isinstance(default, ast.Constant)
            and isinstance(default.value, bool)
        ):
            bool_params.append(arg.arg)

    return bool_params


@check(
    "boolean-param-explosion",
    severity=Severity.MEDIUM,
    description="Function with 4+ boolean parameters suggesting accumulated flags",
    advice=Advice(
        fix=(
            "Group the flags into an options dataclass or an enum, or split the function"
            " along the combinations callers actually use."
        ),
        dismiss=(
            "Dismiss only if the flags are independent keyword options of a published API,"
            " such as a command's switches."
        ),
    ),
)
def check_boolean_param_explosion(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with 4+ boolean-defaulted parameters.

    Boolean flags accumulate over time as quick fixes: dry_run, verbose,
    use_cache, strict, parallel. Call sites become unreadable walls of
    True/False. Consider an options object, enum, or decomposition.
    """
    findings = []
    min_bool_params = 4

    for filepath, tree in ctx.all_trees.items():
        for node in iter_functions(tree):
            bool_params = _get_boolean_params(node)
            if len(bool_params) >= min_bool_params:
                params_str = ", ".join(bool_params)
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="boolean-param-explosion",
                        message=(
                            f"{node.name}() has {len(bool_params)} boolean "
                            f"parameters ({params_str})"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )
    return findings


# --- arrow-code ---

_NESTING_STMTS = (
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.AsyncFor,
    ast.AsyncWith,
)


def _is_elif(stmt: ast.stmt) -> bool:
    """True when ``stmt`` is an ``if`` whose ``orelse`` is an ``elif`` arm.

    The AST spells ``elif`` and ``else:`` followed by an indented ``if`` the
    same way — an ``orelse`` holding one ``If``. Only the source position tells
    them apart: an ``elif`` keyword starts at its chain's column, while the
    indented ``if`` sits one block further in. ``ast.unparse`` draws the same
    line.
    """
    if not isinstance(stmt, ast.If) or len(stmt.orelse) != 1:
        return False
    arm = stmt.orelse[0]
    return isinstance(arm, ast.If) and arm.col_offset == stmt.col_offset


def _compute_max_nesting(stmts: list[ast.stmt], depth: int) -> int:
    """Recursively compute max nesting depth of control flow statements.

    The arms of an ``if``/``elif``/``else`` chain are siblings: every arm's
    body sits one level in from the chain, however many arms precede it.
    """
    max_depth = depth
    for stmt in stmts:
        # Nested functions/classes start fresh — don't inherit parent depth
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        if isinstance(stmt, _NESTING_STMTS):
            max_depth = max(max_depth, _compute_max_nesting(stmt.body, depth + 1))
            orelse = getattr(stmt, "orelse", [])
            # An elif arm re-enters at this statement's depth; it adds its own level.
            orelse_depth = depth if _is_elif(stmt) else depth + 1
            if orelse:
                max_depth = max(max_depth, _compute_max_nesting(orelse, orelse_depth))
        elif isinstance(stmt, ast.Try):
            # try body adds one level
            max_depth = max(max_depth, _compute_max_nesting(stmt.body, depth + 1))
            # each handler adds one level
            for handler in stmt.handlers:
                max_depth = max(max_depth, _compute_max_nesting(handler.body, depth + 1))
            if stmt.orelse:
                max_depth = max(max_depth, _compute_max_nesting(stmt.orelse, depth + 1))
            if stmt.finalbody:
                max_depth = max(max_depth, _compute_max_nesting(stmt.finalbody, depth + 1))
        else:
            # Recurse into compound statements (e.g. match/case)
            for attr in ("body", "orelse"):
                children = getattr(stmt, attr, [])
                if children:
                    max_depth = max(max_depth, _compute_max_nesting(children, depth))
    return max_depth


@check(
    "arrow-code",
    severity=Severity.LOW,
    description=(
        "Functions with deep nesting (5+ levels of if/for/while/try/with;"
        " an elif arm is a sibling, not a level)"
    ),
    advice=Advice(
        fix=(
            "Extract the inner blocks into functions, or turn conditions into early"
            " returns and continues, until the depth is under 5."
        ),
        dismiss=(
            "Dismiss only if every level is a `with` or `try` that must enclose the levels"
            " inside it, so no block can move without splitting a resource's scope."
        ),
    ),
)
def check_arrow_code(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with excessive nesting depth."""
    findings = []
    threshold = 5

    for filepath, tree in source_trees(ctx):
        for node in iter_functions(tree):
            max_depth = _compute_max_nesting(node.body, 0)
            if max_depth >= threshold:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="arrow-code",
                        message=(f"{node.name}() has nesting depth {max_depth}"),
                        severity=Severity.LOW,
                    )
                )

    return findings


# --- inconsistent-returns ---


# Node types whose literal syntax names the returned type outright.
_LITERAL_RETURN_TYPES: dict[type[ast.expr], str] = {
    ast.Dict: "dict",
    ast.DictComp: "dict",
    ast.List: "list",
    ast.ListComp: "list",
    ast.Set: "set",
    ast.SetComp: "set",
    ast.Tuple: "tuple",
    ast.GeneratorExp: "generator",
    ast.JoinedStr: "str",
    ast.FormattedValue: "str",
}

# Builtins with known return types — normalized to the type name so that
# repr(x) and str(x) are both classified as "str".
_BUILTIN_RETURN_TYPES: dict[str, str] = {
    "ascii": "str",
    "chr": "str",
    "format": "str",
    "input": "str",
    "repr": "str",
    "str": "str",
    "hash": "int",
    "int": "int",
    "len": "int",
    "ord": "int",
    "round": "int",
    "abs": "float",
    "float": "float",
    "bool": "bool",
    "bytearray": "bytearray",
    "bytes": "bytes",
    "complex": "complex",
    "dict": "dict",
    "frozenset": "frozenset",
    "list": "list",
    "set": "set",
    "tuple": "tuple",
}


def _infer_call_return_type(call: ast.Call, ctx: AnalysisContext) -> str | None:
    """Infer the type string a call expression returns, or None if unknown."""
    if not isinstance(call.func, ast.Name):
        # Method calls: obj.method() — can't infer return type from name
        # (e.g. result.strip() returns str, not "strip")
        return None

    name = call.func.id
    builtin = _BUILTIN_RETURN_TYPES.get(name)
    if builtin is not None:
        return builtin

    # Follow the call to the function's declared return type when
    # we can find it in the project. Without this, `return get_user()`
    # and `return load_user()` would look like two distinct types
    # ("get_user", "load_user") even when both declare `-> User`.
    defs = ctx.function_index.get(name)
    if defs:
        annotation = defs[0]["node"].returns
        # A function with no return annotation tells us nothing.
        return ast.unparse(annotation) if annotation is not None else None

    # Capitalized name we can't resolve — likely a class constructor
    # from an import (e.g. `return Response(...)`), so the name is
    # a reasonable type proxy.
    if name and name[0].isupper():
        return name

    # Lowercase function call with no resolvable annotation —
    # we don't know the return type, so don't guess.
    return None


def _infer_return_type(node: ast.Return, ctx: AnalysisContext) -> str | None:
    """Infer a type string from a return statement's value node."""
    val = node.value
    if val is None:
        return "None"

    if isinstance(val, ast.Constant):
        return "None" if val.value is None else type(val.value).__name__

    literal_type = _LITERAL_RETURN_TYPES.get(type(val))
    if literal_type is not None:
        return literal_type

    if isinstance(val, ast.Call):
        return _infer_call_return_type(val, ctx)

    # Everything else — BoolOp, IfExp, bare names, attribute access — carries
    # no type readable off the syntax.
    return None


def _has_overload_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @overload decorator."""
    for deco in func.decorator_list:
        if is_bare_name(deco, "overload"):
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "overload":
            return True
    return False


def _is_wrapper_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function is a decorator/wrapper (uses @wraps or named like one).

    Decorators and middleware legitimately return different types (e.g. a
    Django permission decorator may return a redirect, a 403, or the
    wrapped view's response).
    """
    for deco in func.decorator_list:
        # @wraps(...) or @functools.wraps(...)
        if isinstance(deco, ast.Call):
            if is_bare_name(deco.func, "wraps"):
                return True
            if isinstance(deco.func, ast.Attribute) and deco.func.attr == "wraps":
                return True
    return False


def _unpack_union_annotation(annotation: ast.expr) -> set[str]:
    """Expand a return-type annotation into the set of allowed type strings.

    Handles `X | Y` (PEP 604), `Union[X, Y, ...]`, and `Optional[X]` (which
    becomes `{X, "None"}`). Returns a single-element set for a non-union
    annotation. Aliases like `ValidationResult = A | B | C` can't be
    resolved without symbol-table analysis and stay opaque.
    """
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _unpack_union_annotation(annotation.left) | _unpack_union_annotation(
            annotation.right
        )

    if isinstance(annotation, ast.Subscript):
        outer = annotation.value
        outer_name: str | None = None
        if isinstance(outer, ast.Name):
            outer_name = outer.id
        elif isinstance(outer, ast.Attribute):
            outer_name = outer.attr

        if outer_name == "Union":
            slice_node = annotation.slice
            if isinstance(slice_node, ast.Tuple):
                result: set[str] = set()
                for el in slice_node.elts:
                    result |= _unpack_union_annotation(el)
                return result
            return _unpack_union_annotation(slice_node)

        if outer_name == "Optional":
            return _unpack_union_annotation(annotation.slice) | {"None"}

    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return {"None"}

    return {ast.unparse(annotation)}


def _collect_returns(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Return]:
    """Collect all return statements in a function, excluding nested functions/classes."""
    returns: list[ast.Return] = []

    def _visit(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.Return):
                returns.append(stmt)
            for attr in ("body", "orelse", "finalbody"):
                children = getattr(stmt, attr, None)
                if isinstance(children, list):
                    _visit(children)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    _visit(handler.body)

    _visit(func.body)
    return returns


@check(
    "inconsistent-returns",
    severity=Severity.MEDIUM,
    description="Functions returning 3+ distinct types across return paths",
    advice=Advice(
        fix=(
            "Narrow the function to one return type: raise instead of returning a"
            " sentinel, and convert the other paths to the same type."
        ),
        dismiss=(
            "Dismiss only if the function decodes external data into a union its return"
            " annotation declares."
        ),
    ),
)
def check_inconsistent_returns(ctx: AnalysisContext) -> list[Finding]:
    """Find functions that return multiple distinct types."""
    findings = []
    min_types = 3

    for filepath, tree in source_trees(ctx):
        for node in iter_functions(tree):
            if _has_overload_decorator(node):
                continue
            if _is_wrapper_function(node):
                continue

            # Skip short private functions (<15 lines)
            if node.name.startswith("_") and node.end_lineno is not None:
                if node.end_lineno - node.lineno + 1 < 15:
                    continue

            returns = _collect_returns(node)
            if len(returns) < min_types:
                continue

            types: set[str] = set()
            for ret in returns:
                t = _infer_return_type(ret, ctx)
                if t is not None:
                    types.add(t)

            if len(types) >= min_types:
                # Honor declared union annotations: if the function declares
                # `-> A | B | C` and every actual return is one of A/B/C, it
                # is fulfilling its contract, not violating one.
                if node.returns is not None:
                    declared = _unpack_union_annotation(node.returns)
                    if len(declared) >= 2 and types <= declared:
                        continue

                sorted_types = sorted(types)
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="inconsistent-returns",
                        message=(
                            f"{node.name}() returns {len(types)} distinct types "
                            f"({', '.join(sorted_types)}) across {len(returns)} "
                            f"return paths"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


# --- law-of-demeter ---


def _chain_length(node: ast.Attribute) -> int:
    """Count the depth of a chained attribute access (a.b.c.d = 4)."""
    depth = 1
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        depth += 1
        current = current.value
    if isinstance(current, ast.Name):
        depth += 1  # count the root name
    return depth


# Attribute names that indicate AST/IR node navigation, not domain object access
_AST_NAV_ATTRS = frozenset(
    {
        "func",
        "value",
        "args",
        "body",
        "orelse",
        "targets",
        "target",
        "elts",
        "slice",
        "ctx",
        "op",
        "ops",
        "operand",
        "left",
        "right",
        "comparators",
        "handlers",
        "finalbody",
        "keywords",
        "decorator_list",
        "bases",
        "returns",
        "annotation",
        "exc",
        "cause",
        "test",
        "iter",
        "ifs",
        "generators",
        "keys",
        "values",
        "vararg",
        "kwarg",
        "posonlyargs",
        "kwonlyargs",
        "kw_defaults",
        "defaults",
        "names",
        "module",
        "asname",
        "arg",
        "id",
        "attr",
        "lineno",
        "col_offset",
        "end_lineno",
        "end_col_offset",
    }
)


def _should_report_chain(
    node: ast.Attribute,
    tree: ast.Module,
    ctx: AnalysisContext,
    threshold: int,
    imported_names: set[str],
) -> tuple[int, str] | None:
    """Evaluate an attribute chain. Returns (depth, chain_str) if it should be reported."""
    if not isinstance(node.ctx, ast.Load):
        return None

    depth = _chain_length(node)
    if depth < threshold:
        return None

    chain = name_chain(node)
    if not chain:
        return None
    root = chain[0]

    # Skip method calls in the chain — fluent APIs / builder pattern
    is_fluent = False
    current: ast.expr = node
    parents = ctx.parent_map(tree)
    while isinstance(current, ast.Attribute):
        parent = parents.get(current)
        if isinstance(parent, ast.Call) and parent.func is current:
            is_fluent = True
            break
        current = current.value
    if is_fluent:
        return None

    # Skip AST/IR node navigation chains (node.func.value.id etc.)
    chain_attrs: list[str] = []
    nav = node
    while isinstance(nav, ast.Attribute):
        chain_attrs.append(nav.attr)
        nav = nav.value
    if sum(1 for a in chain_attrs if a in _AST_NAV_ATTRS) >= 2:
        return None

    # Skip chains rooted in an imported module/name — reaching into a library
    # namespace (globus_sdk.AuthClient.scopes.all, os.path.sep) is namespace
    # traversal, not reaching through an object handed to this code.
    if root in imported_names:
        return None

    # Skip self.request.* chains — idiomatic in web framework views
    if root == "self" and "request" in chain_attrs:
        return None

    # Skip static namespace traversal (module.Class.InnerClass.CONST)
    # chain_attrs[0] is the leaf, chain_attrs[1:] are intermediates
    intermediate_attrs = chain_attrs[1:]
    if intermediate_attrs and all(a[0].isupper() for a in intermediate_attrs):
        return None

    return depth, ".".join(chain)


@check(
    "law-of-demeter",
    severity=Severity.LOW,
    description="Attribute chains 4+ deep (a.b.c.d) — reaching through object internals",
    advice=Advice(
        fix=(
            "Add a method or property on the first object that returns what the chain"
            " reaches for, and call that."
        ),
        dismiss=(
            "Dismiss only if the chain walks a library's public namespace (a module path,"
            " a client's exceptions attribute) rather than objects the project defines."
        ),
    ),
)
def check_law_of_demeter(ctx: AnalysisContext) -> list[Finding]:
    """Find deep attribute access chains suggesting Law of Demeter violations."""
    findings = []
    threshold = 4

    for filepath, tree in source_trees(ctx):
        # Migration files are auto-generated — fully-qualified paths are expected
        if is_migration_file(filepath):
            continue

        # Dedup: only report the deepest chain per line
        line_findings: dict[int, tuple[int, str]] = {}  # line -> (depth, chain_str)
        imported_names = collect_imported_names(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            result = _should_report_chain(node, tree, ctx, threshold, imported_names)
            if result is None:
                continue

            depth, chain = result
            line = node.lineno
            if line not in line_findings or depth > line_findings[line][0]:
                line_findings[line] = (depth, chain)

        for line, (depth, chain) in sorted(line_findings.items()):
            findings.append(
                Finding(
                    file=str(filepath),
                    line=line,
                    check="law-of-demeter",
                    message=(f"{chain} — chain depth {depth}"),
                    severity=Severity.LOW,
                )
            )

    return findings
