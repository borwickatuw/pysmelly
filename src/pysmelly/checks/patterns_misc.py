"""Pattern checks — miscellaneous: env-fallbacks, monkey-patch, booleans, arrow-code, and more."""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import (
    enclosing_function,
    get_param_names,
    is_constant_reassigned,
    is_test_file,
    iter_uppercase_assigns,
    walk_name_assignments,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


# --- temp-accumulators helpers ---


def _has_append_to(node: ast.AST, var_name: str) -> bool:
    """Check if an AST node contains an append call to var_name."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == var_name
            and child.func.attr == "append"
        ):
            return True
    return False


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


def _find_consumer(siblings: list[ast.AST], var_name: str) -> tuple[str | None, int | None]:
    """Find where an accumulator is consumed (assigned to dict/attr, returned, passed).

    Returns (description, line) or (None, None) if no single consumer found.
    """
    consumers: list[tuple[str, int]] = []
    for stmt in siblings:
        for child in ast.walk(stmt):
            # bar["key"] = foo or bar.attr = foo
            if isinstance(child, ast.Assign) and (
                isinstance(child.value, ast.Name)
                and child.value.id == var_name
                and len(child.targets) == 1
            ):
                target = child.targets[0]
                if isinstance(target, ast.Subscript):
                    if isinstance(target.value, ast.Name):
                        if isinstance(target.slice, ast.Constant) and isinstance(
                            target.slice.value, str
                        ):
                            consumers.append(
                                (
                                    f"{target.value.id}[{target.slice.value!r}]",
                                    child.lineno,
                                )
                            )
                        else:
                            consumers.append((f"{target.value.id}[...]", child.lineno))
                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    consumers.append((f"{target.value.id}.{target.attr}", child.lineno))
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
                f"populate {consumer_desc} (line {consumer_line}) "
                f"— inline with a comprehension"
            )
        else:
            message = (
                f"'{var_name}' is a loop-and-append accumulator — replace with a comprehension"
            )
    elif conditional_appends > 0 and bare_appends == 0:
        severity = Severity.LOW
        if consumer_desc:
            message = (
                f"'{var_name}' is built from {conditional_appends} "
                f"independent conditions only to populate "
                f"{consumer_desc} (line {consumer_line}) "
                f"— accumulator may be appropriate here"
            )
        else:
            message = (
                f"'{var_name}' is built from {conditional_appends} "
                f"independent conditions then joined/checked "
                f"— accumulator may be appropriate here"
            )
    else:
        severity = Severity.MEDIUM
        if consumer_desc:
            message = (
                f"'{var_name}' is a temporary accumulator "
                f"({append_count} appends) only to populate "
                f"{consumer_desc} (line {consumer_line}) "
                f"— consider a comprehension or direct construction"
            )
        else:
            message = (
                f"'{var_name}' is a temporary accumulator "
                f"({append_count} appends then join/check) — "
                f"consider a comprehension or direct approach"
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


def _analyze_accumulator_usage(
    siblings: list[ast.AST], var_name: str
) -> tuple[int, int, bool, bool]:
    """Scan siblings for append/join/check patterns on var_name.

    Returns (append_count, other_uses, join_or_check, has_assignment_consumer).
    """
    append_count = 0
    other_uses = 0
    join_or_check = False
    has_assignment_consumer = False

    for subsequent in siblings:
        for child in ast.walk(subsequent):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == var_name
            ):
                if child.func.attr == "append":
                    append_count += 1
                elif child.func.attr == "join":
                    join_or_check = True
                else:
                    other_uses += 1

            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "join"
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == var_name
            ):
                join_or_check = True

            if isinstance(child, ast.If):
                if isinstance(child.test, ast.Name) and child.test.id == var_name:
                    join_or_check = True

            # Detect assignment consumers: bar["key"] = foo, bar.attr = foo
            if isinstance(child, ast.Assign) and (
                isinstance(child.value, ast.Name)
                and child.value.id == var_name
                and len(child.targets) == 1
            ):
                target = child.targets[0]
                if isinstance(target, (ast.Subscript, ast.Attribute)):
                    has_assignment_consumer = True
                    join_or_check = True

    return append_count, other_uses, join_or_check, has_assignment_consumer


@check(
    "temp-accumulators",
    severity=Severity.MEDIUM,
    description="Lists built by append then joined (use comprehension)",
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            if not isinstance(node.targets[0], ast.Name):
                continue
            if not isinstance(node.value, ast.List):
                continue
            if node.value.elts:
                continue

            var_name = node.targets[0].id
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
                    message=(
                        f"os.environ.get('{key_name}', {default}) has a fallback default — "
                        f"if this config is required, use os.environ['{key_name}'] and fail fast"
                    ),
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
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
            ):
                captured_attrs[_attr_chain(node.value)] = node.targets[0].id

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


def _function_shadows_toggle(func: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Check if a function locally shadows a module-level name."""
    if name in get_param_names(func):
        return True
    has_global, has_assign = walk_name_assignments(func.body, name)
    if has_global:
        return False
    return has_assign


def _evaluate_toggle_condition(test: ast.expr, name: str, const_value: object) -> bool | None:
    """Evaluate a conditional test given a constant's value.

    Returns the boolean result, or None if the pattern is not recognized.
    Handles: truthiness, negated truthiness, equality/inequality with literal.
    """
    # if FLAG:
    if isinstance(test, ast.Name) and test.id == name:
        return bool(const_value)

    # if not FLAG:
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id == name
    ):
        return not bool(const_value)

    # CONST == literal / CONST != literal / literal == CONST / literal != CONST
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        op = test.ops[0]
        left = test.left
        right = test.comparators[0]

        if isinstance(left, ast.Name) and left.id == name and isinstance(right, ast.Constant):
            other_value = right.value
        elif isinstance(right, ast.Name) and right.id == name and isinstance(left, ast.Constant):
            other_value = left.value
        else:
            return None

        if isinstance(op, ast.Eq):
            return const_value == other_value
        if isinstance(op, ast.NotEq):
            return const_value != other_value

    return None


def _find_toggle_conditional_uses(
    ctx: AnalysisContext,
    const_defs: dict[str, dict[Path, tuple[int, object]]],
) -> dict[tuple[Path, str], list[tuple[Path, int, bool, str]]]:
    """Find conditionals that reference non-reassigned constants.

    Returns {(def_filepath, name): [(use_filepath, lineno, result, keyword)]}.
    """
    uses: dict[tuple[Path, str], list[tuple[Path, int, bool, str]]] = {}

    for filepath, tree in ctx.all_trees.items():
        parents = ctx.parent_map(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test, keyword = node.test, "if"
            elif isinstance(node, ast.While):
                test, keyword = node.test, "while"
            elif isinstance(node, ast.IfExp):
                test, keyword = node.test, "ternary"
            else:
                continue

            # Extract candidate constant names from the test expression
            candidate_names: set[str] = set()
            if isinstance(test, ast.Name):
                candidate_names.add(test.id)
            elif (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
            ):
                candidate_names.add(test.operand.id)
            elif (
                isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1
            ):
                if isinstance(test.left, ast.Name):
                    candidate_names.add(test.left.id)
                if isinstance(test.comparators[0], ast.Name):
                    candidate_names.add(test.comparators[0].id)

            for const_name in candidate_names:
                if const_name not in const_defs:
                    continue
                definitions = const_defs[const_name]

                # Determine which definition this conditional references
                if filepath in definitions:
                    def_file = filepath
                elif len(definitions) == 1:
                    def_file = next(iter(definitions))
                else:
                    continue  # ambiguous cross-file

                value = definitions[def_file][1]
                result = _evaluate_toggle_condition(test, const_name, value)
                if result is None:
                    continue

                # Skip if inside a function that locally shadows the name
                enclosing = enclosing_function(node, parents)
                if enclosing and _function_shadows_toggle(enclosing, const_name):
                    continue

                uses.setdefault((def_file, const_name), []).append(
                    (filepath, node.lineno, result, keyword)
                )

    return uses


@check(
    "fossilized-toggles",
    severity=Severity.MEDIUM,
    description="Module-level constant makes conditional branches statically determinable",
)
def check_fossilized_toggles(ctx: AnalysisContext) -> list[Finding]:
    """Find UPPER_CASE module-level constants that gate conditionals.

    A constant like ENABLE_V2_API = False that is never reassigned makes
    every ``if ENABLE_V2_API:`` always-False — the guarded branch is
    permanently dead code.
    """
    findings = []

    # Collect non-reassigned constants: {name: {filepath: (lineno, value)}}
    const_defs: dict[str, dict[Path, tuple[int, object]]] = {}
    for filepath, tree in ctx.all_trees.items():
        for name, (lineno, value) in _collect_module_constants(tree).items():
            if not is_constant_reassigned(tree, name, lineno):
                const_defs.setdefault(name, {})[filepath] = (lineno, value)

    if not const_defs:
        return findings

    uses = _find_toggle_conditional_uses(ctx, const_defs)

    # Generate findings
    for (def_filepath, const_name), use_list in uses.items():
        def_lineno, const_value = const_defs[const_name][def_filepath]

        if len(use_list) == 1:
            _, use_line, always_val, kw = use_list[0]
            msg = (
                f"{const_name} = {const_value!r} is never reassigned — "
                f"`{kw}` at line {use_line} is always {always_val}"
            )
            if not always_val:
                msg += " (dead branch)"
        else:
            all_values = {r for _, _, r, _ in use_list}
            if len(all_values) == 1:
                always_str = f"always {next(iter(all_values))}"
            else:
                always_str = "statically determinable"
            msg = (
                f"{const_name} = {const_value!r} is never reassigned — "
                f"controls {len(use_list)} conditionals ({always_str})"
            )

        findings.append(
            Finding(
                file=str(def_filepath),
                line=def_lineno,
                check="fossilized-toggles",
                message=msg,
                severity=Severity.MEDIUM,
            )
        )

    return findings


# --- isinstance-chain ---


@check(
    "isinstance-chain",
    severity=Severity.MEDIUM,
    description="Function with many isinstance() checks suggesting missed polymorphism",
)
def check_isinstance_chain(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with 5+ isinstance() calls.

    Long isinstance chains often accumulate as code handles more types
    over time. They suggest a missed opportunity for polymorphism,
    a dispatch table, or functools.singledispatch.
    """
    findings = []
    min_count = 5

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            count = 0
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "isinstance"
                ):
                    count += 1
            if count >= min_count:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="isinstance-chain",
                        message=(
                            f"{node.name}() has {count} isinstance() checks "
                            f"— consider polymorphism or a dispatch table"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )
    return findings


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
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
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
                            f"parameters ({params_str}) — consider an options "
                            f"object or enum"
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


def _compute_max_nesting(stmts: list[ast.stmt], depth: int) -> int:
    """Recursively compute max nesting depth of control flow statements."""
    max_depth = depth
    for stmt in stmts:
        # Nested functions/classes start fresh — don't inherit parent depth
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        if isinstance(stmt, _NESTING_STMTS):
            child_depth = depth + 1
            for attr in ("body", "orelse"):
                children = getattr(stmt, attr, [])
                if children:
                    max_depth = max(max_depth, _compute_max_nesting(children, child_depth))
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
    description="Functions with deep nesting (5+ levels of if/for/while/try/with)",
)
def check_arrow_code(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with excessive nesting depth."""
    findings = []
    threshold = 5

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            max_depth = _compute_max_nesting(node.body, 0)
            if max_depth >= threshold:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="arrow-code",
                        message=(
                            f"{node.name}() has nesting depth {max_depth}"
                            f" — consider extracting inner blocks"
                        ),
                        severity=Severity.LOW,
                    )
                )

    return findings


# --- inconsistent-returns ---


def _infer_return_type(node: ast.Return) -> str | None:
    """Infer a type string from a return statement's value node."""
    if node.value is None:
        return "None"

    val = node.value

    if isinstance(val, ast.Constant):
        if val.value is None:
            return "None"
        return type(val.value).__name__

    if isinstance(val, ast.Dict):
        return "dict"
    if isinstance(val, ast.List):
        return "list"
    if isinstance(val, ast.Tuple):
        return "tuple"
    if isinstance(val, ast.Set):
        return "set"
    if isinstance(val, ast.ListComp):
        return "list"
    if isinstance(val, ast.DictComp):
        return "dict"
    if isinstance(val, ast.SetComp):
        return "set"
    if isinstance(val, ast.GeneratorExp):
        return "generator"
    if isinstance(val, ast.JoinedStr):
        return "str"
    if isinstance(val, ast.FormattedValue):
        return "str"

    if isinstance(val, ast.Call):
        # Constructor-like calls: int(x), str(x), MyClass() — use the name
        if isinstance(val.func, ast.Name):
            name = val.func.id
            # Builtins with known return types — normalize to the type name
            # so repr(x) and str(x) are both classified as "str"
            if name in {"repr", "str", "format", "chr", "ascii", "input"}:
                return "str"
            if name in {"int", "round", "hash", "len", "ord"}:
                return "int"
            if name in {"float", "abs"}:
                return "float"
            if name in {
                "bool",
            }:
                return "bool"
            return name
        # Method calls: obj.method() — can't infer return type from name
        # (e.g. result.strip() returns str, not "strip")
        return None

    if isinstance(val, ast.BoolOp):
        return None  # can't infer
    if isinstance(val, ast.IfExp):
        return None  # ternary — can't infer

    return None


def _has_overload_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @overload decorator."""
    for deco in func.decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "overload":
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
            if isinstance(deco.func, ast.Name) and deco.func.id == "wraps":
                return True
            if isinstance(deco.func, ast.Attribute) and deco.func.attr == "wraps":
                return True
    return False


def _is_test_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function is a test function by name."""
    return func.name.startswith("test_")


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
)
def check_inconsistent_returns(ctx: AnalysisContext) -> list[Finding]:
    """Find functions that return multiple distinct types."""
    findings = []
    min_types = 3

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_test_function(node):
                continue
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
                t = _infer_return_type(ret)
                if t is not None:
                    types.add(t)

            if len(types) >= min_types:
                sorted_types = sorted(types)
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="inconsistent-returns",
                        message=(
                            f"{node.name}() returns {len(types)} distinct types "
                            f"({', '.join(sorted_types)}) across {len(returns)} "
                            f"return paths — consider narrowing the return type"
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


def _chain_root(node: ast.Attribute) -> str | None:
    """Get the root variable name of an attribute chain."""
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def _chain_str(node: ast.Attribute) -> str:
    """Reconstruct the full dotted attribute chain as a string."""
    parts: list[str] = [node.attr]
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


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

_STDLIB_MODULES = frozenset(
    {
        "os",
        "sys",
        "ast",
        "re",
        "io",
        "json",
        "logging",
        "pathlib",
        "typing",
        "collections",
        "functools",
        "itertools",
        "datetime",
    }
)


def _should_report_chain(
    node: ast.Attribute,
    tree: ast.Module,
    ctx: AnalysisContext,
    threshold: int,
) -> tuple[int, str] | None:
    """Evaluate an attribute chain. Returns (depth, chain_str) if it should be reported."""
    if not isinstance(node.ctx, ast.Load):
        return None

    depth = _chain_length(node)
    if depth < threshold:
        return None

    root = _chain_root(node)
    if root is None:
        return None

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

    # Skip module-level attribute access (os.path.sep, etc.)
    if root[0].islower() and root in _STDLIB_MODULES:
        return None

    # Skip self.request.* chains — idiomatic in web framework views
    if root == "self" and "request" in chain_attrs:
        return None

    # Skip static namespace traversal (module.Class.InnerClass.CONST)
    # chain_attrs[0] is the leaf, chain_attrs[1:] are intermediates
    intermediate_attrs = chain_attrs[1:]
    if intermediate_attrs and all(a[0].isupper() for a in intermediate_attrs):
        return None

    return depth, _chain_str(node)


@check(
    "law-of-demeter",
    severity=Severity.LOW,
    description="Attribute chains 4+ deep (a.b.c.d) — reaching through object internals",
)
def check_law_of_demeter(ctx: AnalysisContext) -> list[Finding]:
    """Find deep attribute access chains suggesting Law of Demeter violations."""
    findings = []
    threshold = 4

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        # Migration files are auto-generated — fully-qualified paths are expected
        if is_migration_file(filepath):
            continue

        # Dedup: only report the deepest chain per line
        line_findings: dict[int, tuple[int, str]] = {}  # line -> (depth, chain_str)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            result = _should_report_chain(node, tree, ctx, threshold)
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
                    message=(
                        f"{chain} — chain depth {depth};"
                        f" consider asking the intermediate object instead"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings
