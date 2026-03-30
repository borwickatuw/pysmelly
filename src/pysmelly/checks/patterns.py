"""Pattern-based checks — detect specific code idioms that suggest refactoring."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.framework import is_migration_file, is_settings_file
from pysmelly.checks.helpers import is_in_dunder_all, is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Walk up the parent chain to find the enclosing function."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _get_param_names(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get all parameter names from a function definition."""
    names = {a.arg for a in func_node.args.args}
    names |= {a.arg for a in func_node.args.posonlyargs}
    names |= {a.arg for a in func_node.args.kwonlyargs}
    if func_node.args.vararg:
        names.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        names.add(func_node.args.kwarg.arg)
    return names


def _count_name_loads(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, int]:
    """Count Load occurrences of each name in a function body."""
    counts: dict[str, int] = {}
    for child in ast.walk(func_node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            counts[child.id] = counts.get(child.id, 0) + 1
    return counts


@check(
    "foo-equals-foo",
    severity=Severity.MEDIUM,
    description="Single-use locals gathered into an object — inline or build directly",
)
def check_foo_equals_foo(ctx: AnalysisContext) -> list[Finding]:
    """Find calls where many kwargs match local variable names (name=name).

    Distinguishes three cases:
    - Single-use locals (x = compute(); g(x=x) where x isn't used again) — the
      real smell, these intermediates can be inlined.
    - Forwarded parameters (def f(x): g(x=x)) — just passing through, not a smell.
    - Multi-use locals — used elsewhere too, less clear-cut.

    Pure parameter forwarding is suppressed. Single-use locals are MEDIUM severity.
    """
    findings = []
    threshold = 4

    for filepath, tree in ctx.all_trees.items():
        parents = ctx.parent_map(tree)
        func_cache: dict[int, tuple[set[str], dict[str, int]]] = {}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not node.keywords:
                continue

            foo_foo_names = []
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
                    foo_foo_names.append(kw.arg)

            if len(foo_foo_names) < threshold:
                continue

            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = "?"

            # Classify each foo=foo name
            enclosing = _enclosing_function(node, parents)
            if enclosing:
                fid = id(enclosing)
                if fid not in func_cache:
                    func_cache[fid] = (
                        _get_param_names(enclosing),
                        _count_name_loads(enclosing),
                    )
                param_names, load_counts = func_cache[fid]

                single_use = [
                    n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) == 1
                ]
                forwarded = [n for n in foo_foo_names if n in param_names]
                multi_use = [
                    n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) > 1
                ]

                # Only report when there are single-use locals to inline
                if not single_use:
                    continue

                names_str = ", ".join(single_use[:5])
                if len(single_use) > 5:
                    names_str += "..."
                message = (
                    f"{call_name}() has {len(foo_foo_names)} foo=foo args, "
                    f"{len(single_use)} are single-use locals "
                    f"({names_str}) that could be inlined"
                )
                severity = Severity.MEDIUM
            else:
                # Module-level call — no function context for classification
                message = (
                    f"{call_name}() has {len(foo_foo_names)} foo=foo args "
                    f"— consider building the object directly"
                )
                severity = Severity.MEDIUM

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="foo-equals-foo",
                    message=message,
                    severity=severity,
                )
            )

    return findings


@check(
    "suspicious-fallbacks",
    severity=Severity.HIGH,
    description="dict.get()/setdefault() with non-trivial defaults on constant dicts",
)
def check_suspicious_fallbacks(ctx: AnalysisContext) -> list[Finding]:
    """Find .get()/.setdefault() on module-level constant dicts with non-trivial defaults.

    A default of None/0/False/"" is normal. A non-trivial default suggests
    the caller expects a miss — which may mean the constant dict is incomplete
    or the fallback masks a bug. If the key should always exist, use [] indexing.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        constant_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        constant_names.add(target.id)

        if not constant_names:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("get", "setdefault"):
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id not in constant_names:
                continue
            if len(node.args) < 2:
                continue

            default = node.args[1]
            if isinstance(default, ast.Constant) and default.value in (None, 0, False, ""):
                continue

            method = node.func.attr
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="suspicious-fallbacks",
                    message=(
                        f"{node.func.value.id}.{method}() has a non-trivial fallback default — "
                        f"if the key should always exist, use [] indexing and fail fast"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


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
            if isinstance(child, ast.Assign):
                if (
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
                    if isinstance(child, ast.Assign):
                        if (
                            isinstance(child.value, ast.Name)
                            and child.value.id == var_name
                            and len(child.targets) == 1
                        ):
                            target = child.targets[0]
                            if isinstance(target, (ast.Subscript, ast.Attribute)):
                                has_assignment_consumer = True
                                join_or_check = True

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
                            f"'{var_name}' is a loop-and-append accumulator "
                            f"— replace with a comprehension"
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


@check(
    "constant-dispatch-dicts",
    severity=Severity.MEDIUM,
    description="Module-level string-to-function dispatch tables",
)
def check_constant_dispatch_dicts(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level dicts where all values are bare name references.

    These dispatch/registration tables can get out of sync with the functions
    they reference. Consider a decorator pattern that colocates the name
    with the definition.
    """
    findings = []
    min_entries = 3

    for filepath, tree in ctx.all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            if not node.value.keys:
                continue

            d = node.value
            if not all(isinstance(k, ast.Constant) for k in d.keys):
                continue
            if not all(isinstance(v, ast.Name) for v in d.values):
                continue
            if len(d.keys) < min_entries:
                continue

            # Skip when all values are UPPER_CASE — constants/config, not dispatch
            if all(v.id.isupper() for v in d.values):  # type: ignore[union-attr]
                continue

            if isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id
            else:
                var_name = "?"

            names = [v.id for v in d.values]  # type: ignore[union-attr]
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="constant-dispatch-dicts",
                    message=(
                        f"{var_name} is a {len(d.keys)}-entry dispatch dict "
                        f"mapping strings to functions ({', '.join(names[:3])}...) — "
                        f"consider decorator registration"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


def _is_subclass_method(func_node: ast.AST, subclass_methods: set[int]) -> bool:
    """Check if a function node is a method in a class with base classes."""
    return id(func_node) in subclass_methods


def _is_self_method_chain(value: ast.expr) -> bool:
    """Check if the return is self.method(...) — part of a deliberate API chain."""
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "self"
    )


def _is_pure_forwarding_call(
    call_node: ast.Call, func_node: ast.FunctionDef | ast.AsyncFunctionDef
) -> bool:
    """Check if a call only forwards the wrapper's own parameters.

    Returns False when the call adds any extra arguments (constants,
    expressions, etc.) beyond what the wrapper receives — the wrapper
    is adding configuration, not just forwarding.
    """
    param_names = {a.arg for a in func_node.args.args if a.arg not in ("self", "cls")}
    param_names |= {a.arg for a in func_node.args.posonlyargs}
    param_names |= {a.arg for a in func_node.args.kwonlyargs}

    for arg in call_node.args:
        if isinstance(arg, ast.Starred):
            continue  # *args pass-through
        if not (isinstance(arg, ast.Name) and arg.id in param_names):
            return False
    for kw in call_node.keywords:
        if kw.arg is None:
            continue  # **kwargs pass-through
        if not (isinstance(kw.value, ast.Name) and kw.value.id in param_names):
            return False
    return True


def _collect_subclass_methods(tree: ast.Module) -> set[int]:
    """Collect ids of methods defined in classes that have base classes."""
    methods: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.bases:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(id(item))
    return methods


@check(
    "trivial-wrappers",
    severity=Severity.LOW,
    description="Functions whose body is a single return (inline candidate)",
)
def check_trivial_wrappers(ctx: AnalysisContext) -> list[Finding]:
    """Find functions whose only real statement is a return.

    Functions that just return a dict lookup, attribute access, or single
    function call are candidates for inlining at call sites.

    Suppresses:
    - Abstract method implementations (constant returns in subclass methods)
    - Self-method chains (return self.other_method())
    - Calls with complex args (from_dict doing data.get() mapping)
    - Multi-caller wrappers (3+ callers = intentional abstraction point)
    """
    findings = []
    multi_caller_threshold = 3

    for filepath, tree in ctx.all_trees.items():
        subclass_methods = _collect_subclass_methods(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            if node.decorator_list:
                continue

            # Strip docstring from body
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]

            if len(body) != 1:
                continue
            stmt = body[0]
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue

            ret_value = stmt.value

            # Suppress: subclass methods (protocol implementations can't be inlined)
            if _is_subclass_method(node, subclass_methods):
                continue

            # Suppress: self-method chains (return self.to_dict() etc.)
            if _is_self_method_chain(ret_value):
                continue

            # Suppress: calls that add arguments beyond parameter forwarding
            if isinstance(ret_value, ast.Call) and not _is_pure_forwarding_call(ret_value, node):
                continue

            desc = _describe_trivial_return(ret_value)
            if desc is None:
                continue

            # Suppress: multi-caller wrappers (central point for change)
            callers = ctx.call_index.get(node.name, [])
            if len(callers) >= multi_caller_threshold:
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="trivial-wrappers",
                    message=(
                        f"{node.name}() just returns {desc} — consider inlining at call sites"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings


def _describe_trivial_return(value: ast.expr) -> str | None:
    """Describe a trivial return value, or None if it's not trivial."""
    # dict[key] or dict.get(key)
    if isinstance(value, ast.Subscript):
        if isinstance(value.value, ast.Name):
            return f"{value.value.id}[...]"
    # obj.attr
    if isinstance(value, ast.Attribute):
        if isinstance(value.value, ast.Name):
            return f"{value.value.id}.{value.attr}"
    # single function call: func(...)
    if isinstance(value, ast.Call):
        if isinstance(value.func, ast.Name):
            return f"{value.func.id}(...)"
        if isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
            return f"{value.func.value.id}.{value.func.attr}(...)"
    # constant
    if isinstance(value, ast.Constant):
        return repr(value.value)
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


def _get_env_call_key(node: ast.Call) -> str | None:
    """Return the env var name if this is an os.environ.get() or os.getenv() call."""
    # os.environ.get("KEY", ...)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
    ):
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    # os.getenv("KEY", ...)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ):
        if node.args and isinstance(node.args[0], ast.Constant):
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


# --- fossilized-toggles helpers ---


def _collect_module_constants(tree: ast.Module) -> dict[str, tuple[int, object]]:
    """Find UPPER_CASE module-level names assigned literal values."""
    constants: dict[str, tuple[int, object]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        if not name.isupper() or name.startswith("_"):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        constants[name] = (node.lineno, node.value.value)
    return constants


def _is_constant_reassigned(tree: ast.Module, name: str, def_lineno: int) -> bool:
    """Check if a module-level constant is reassigned in the same file.

    Checks module-level Assign/AugAssign/Delete and global+assign inside functions.
    Class body assignments are not considered reassignments (iter_child_nodes
    only sees top-level statements).
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and node.lineno != def_lineno:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return True
        if isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        if isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return True

    # global NAME + assignment inside any function (scope-aware: skip nested scopes)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_global = False
        has_assign = False
        todo = list(node.body)
        while todo:
            child = todo.pop()
            if isinstance(child, ast.Global) and name in child.names:
                has_global = True
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        has_assign = True
            if isinstance(child, ast.AugAssign):
                if isinstance(child.target, ast.Name) and child.target.id == name:
                    has_assign = True
            for sub in ast.iter_child_nodes(child):
                if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    todo.append(sub)
        if has_global and has_assign:
            return True

    return False


def _function_shadows_toggle(func: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Check if a function locally shadows a module-level name."""
    if name in _get_param_names(func):
        return True
    has_global = False
    has_assign = False
    todo = list(func.body)
    while todo:
        child = todo.pop()
        if isinstance(child, ast.Global) and name in child.names:
            has_global = True
        if isinstance(child, ast.Assign):
            for t in child.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    has_assign = True
        if isinstance(child, ast.AugAssign):
            if isinstance(child.target, ast.Name) and child.target.id == name:
                has_assign = True
        for sub in ast.iter_child_nodes(child):
            if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                todo.append(sub)
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
            if not _is_constant_reassigned(tree, name, lineno):
                const_defs.setdefault(name, {})[filepath] = (lineno, value)

    if not const_defs:
        return findings

    # Find conditional uses: {(def_filepath, name): [(use_filepath, lineno, result, kw)]}
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
                enclosing = _enclosing_function(node, parents)
                if enclosing and _function_shadows_toggle(enclosing, const_name):
                    continue

                uses.setdefault((def_file, const_name), []).append(
                    (filepath, node.lineno, result, keyword)
                )

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


# --- dead-constants helpers ---


def _collect_all_name_and_attr_loads(
    all_trees: dict[Path, ast.Module],
) -> set[str]:
    """Collect all names used in Load context (bare names and attribute accesses)."""
    names: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
    return names


def _collect_module_level_names(tree: ast.Module) -> dict[str, tuple[int, str]]:
    """Find all UPPER_CASE module-level names (including non-literal assignments).

    Returns {name: (lineno, description)} where description is a short
    representation of the assigned value for the finding message.
    """
    names: dict[str, tuple[int, str]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        if not name.isupper() or name.startswith("_"):
            continue

        # Build a short description of the value
        val = node.value
        if isinstance(val, ast.Constant):
            desc = repr(val.value)
        elif isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
            desc = f"{val.func.id}(...)"
        elif isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute):
            desc = f"{val.func.attr}(...)"
        elif isinstance(val, ast.Dict):
            desc = "{...}"
        elif isinstance(val, ast.List):
            desc = "[...]"
        elif isinstance(val, ast.Set):
            desc = "{...}"
        elif isinstance(val, ast.Tuple):
            desc = "(...)"
        else:
            desc = "..."

        if len(desc) > 40:
            desc = desc[:37] + "..."
        names[name] = (node.lineno, desc)
    return names


@check(
    "dead-constants",
    severity=Severity.MEDIUM,
    description="UPPER_CASE module-level constants never referenced anywhere",
)
def check_dead_constants(ctx: AnalysisContext) -> list[Finding]:
    """Find UPPER_CASE module-level names that are defined but never used.

    Covers both literal constants (strings, ints) and non-literal assignments
    (frozenset, dict, list constructors). Event name constants, skip lists,
    configuration keys — these accumulate as code evolves and become dead
    weight when the consuming code is changed or removed.
    """
    findings = []

    # Collect all UPPER_CASE module-level names: {name: [(filepath, lineno, desc)]}
    all_constants: dict[str, list[tuple[Path, int, str]]] = {}
    for filepath, tree in ctx.all_trees.items():
        # Settings files contain UPPER_CASE constants read by frameworks
        # via getattr() — they're not dead, just invisible to static analysis
        if is_settings_file(filepath):
            continue
        for name, (lineno, desc) in _collect_module_level_names(tree).items():
            if not _is_constant_reassigned(tree, name, lineno):
                all_constants.setdefault(name, []).append((filepath, lineno, desc))

    if not all_constants:
        return findings

    # Build set of all referenced names (Name.Load and Attribute.attr in Load)
    referenced_names = _collect_all_name_and_attr_loads(ctx.all_trees)

    for const_name, defs in all_constants.items():
        # Skip if referenced anywhere as a name or attribute
        if const_name in referenced_names:
            continue
        # Skip if imported elsewhere
        if ctx.import_index.get(const_name):
            continue

        for filepath, lineno, desc in defs:
            tree = ctx.all_trees[filepath]
            if is_in_dunder_all(const_name, tree):
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=lineno,
                    check="dead-constants",
                    message=(
                        f"{const_name} = {desc} is never referenced " f"anywhere in the codebase"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- unreachable-after-return helpers ---


def _is_terminating_block(stmts: list[ast.stmt]) -> bool:
    """Check if a block of statements always terminates (return/raise in all paths)."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _is_terminating_block(last.body) and _is_terminating_block(last.orelse)
    return False


def _find_unreachable_in_body(
    stmts: list[ast.stmt], filepath: str, func_name: str
) -> list[Finding]:
    """Check a function body for unreachable tail code after terminators."""
    for i, stmt in enumerate(stmts):
        remaining = len(stmts) - i - 1
        if remaining == 0:
            continue

        if isinstance(stmt, (ast.Return, ast.Raise)):
            kind = "return" if isinstance(stmt, ast.Return) else "raise"
            return [
                Finding(
                    file=filepath,
                    line=stmts[i + 1].lineno,
                    check="unreachable-after-return",
                    message=(
                        f"{remaining} statement(s) in {func_name}() after "
                        f"{kind} at line {stmt.lineno} can never execute"
                    ),
                    severity=Severity.HIGH,
                )
            ]

        if isinstance(stmt, ast.If) and stmt.orelse:
            if _is_terminating_block(stmt.body) and _is_terminating_block(stmt.orelse):
                return [
                    Finding(
                        file=filepath,
                        line=stmts[i + 1].lineno,
                        check="unreachable-after-return",
                        message=(
                            f"{remaining} statement(s) in {func_name}() after "
                            f"exhaustive if/else at line {stmt.lineno} "
                            f"can never execute"
                        ),
                        severity=Severity.HIGH,
                    )
                ]

    return []


@check(
    "unreachable-after-return",
    severity=Severity.HIGH,
    description="Code after return/raise that can never execute",
)
def check_unreachable_after_return(ctx: AnalysisContext) -> list[Finding]:
    """Find code after unconditional return/raise or exhaustive if/else branches.

    When someone refactors a function to return early in each branch, the
    original code at the bottom becomes unreachable. This dead tail code
    accumulates silently because each branch individually looks correct.
    """
    findings = []
    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            findings.extend(_find_unreachable_in_body(node.body, str(filepath), node.name))
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


# --- boolean-param-explosion helpers ---


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
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
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


# --- exception-flow-control helpers ---


def _inherits_from_exception(class_node: ast.ClassDef) -> bool:
    """Check if a class inherits from an exception type."""
    for base in class_node.bases:
        name = None
        if isinstance(base, ast.Name):
            name = base.id
        elif isinstance(base, ast.Attribute):
            name = base.attr
        if name is not None:
            if name.endswith(("Error", "Exception")) or name == "BaseException":
                return True
    return False


def _collect_exception_classes_in_scope(tree: ast.AST) -> set[str]:
    """Collect exception class names defined anywhere in the given AST scope."""
    exceptions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _inherits_from_exception(node):
            exceptions.add(node.name)
    return exceptions


def _collect_raised_in_try_body(body: list[ast.stmt]) -> set[str]:
    """Collect exception names raised within a try body, skipping nested scopes."""
    raised: set[str] = set()
    todo: list[ast.AST] = list(body)
    while todo:
        child = todo.pop()
        if isinstance(child, ast.Raise) and child.exc:
            exc = child.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                raised.add(exc.func.id)
            elif isinstance(exc, ast.Name):
                raised.add(exc.id)
        for sub in ast.iter_child_nodes(child):
            if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                todo.append(sub)
    return raised


def _collect_caught_names(handlers: list[ast.ExceptHandler]) -> set[str]:
    """Collect exception names caught by except handlers."""
    caught: set[str] = set()
    for handler in handlers:
        if handler.type is None:
            continue
        if isinstance(handler.type, ast.Name):
            caught.add(handler.type.id)
        elif isinstance(handler.type, ast.Tuple):
            for elt in handler.type.elts:
                if isinstance(elt, ast.Name):
                    caught.add(elt.id)
    return caught


@check(
    "exception-flow-control",
    severity=Severity.MEDIUM,
    description="Custom exceptions raised and caught in the same function for flow control",
)
def check_exception_flow_control(ctx: AnalysisContext) -> list[Finding]:
    """Find custom exceptions used as goto/control flow within a single function.

    When a locally-defined exception is raised in a try body and caught in
    the same try's except handler, it's being used as a goto — the exception
    represents a jump, not an error. Consider return/break/flag instead.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        local_exceptions = _collect_exception_classes_in_scope(tree)
        if not local_exceptions:
            continue

        for func_node in ast.walk(tree):
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Also collect exceptions defined inside this function
            func_exceptions: set[str] = set()
            for node in ast.walk(func_node):
                if isinstance(node, ast.ClassDef) and _inherits_from_exception(node):
                    func_exceptions.add(node.name)
            all_exceptions = local_exceptions | func_exceptions

            for node in ast.walk(func_node):
                if not isinstance(node, ast.Try):
                    continue

                raised = _collect_raised_in_try_body(node.body)
                caught = _collect_caught_names(node.handlers)
                flow_control = raised & caught & all_exceptions

                for exc_name in sorted(flow_control):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="exception-flow-control",
                            message=(
                                f"{exc_name} is raised and caught within "
                                f"{func_node.name}() — consider using "
                                f"return/break/flag instead"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

    return findings


# --- arrow-code helpers ---

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


# --- hungarian-notation ---

# Apps Hungarian (type-as-prefix): strName, intCount, lstItems
# Systems Hungarian (storage-as-prefix): szName, lpBuffer, dwFlags, fnCallback
_HUNGARIAN_RE = re.compile(
    r"^(str|int|bool|lst|dict|arr|obj|flt|tpl|set"  # Apps Hungarian
    r"|sz|lp|dw|fn|cb|rg|pi|pf|pp|lpsz|pfn)[A-Z]"  # Systems Hungarian
)


def _to_snake_case(name: str) -> str:
    """Convert camelCase hungarian name to snake_case suggestion."""
    # Split on uppercase letter boundaries
    parts: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch.isupper() and current:
            parts.append("".join(current).lower())
            current = [ch]
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).lower())
    return "_".join(parts)


@check(
    "hungarian-notation",
    severity=Severity.LOW,
    description="Variables using Hungarian notation (strName, intCount, lstItems)",
)
def check_hungarian_notation(ctx: AnalysisContext) -> list[Finding]:
    """Find variables using Hungarian notation prefixes."""
    findings = []
    seen: set[tuple[str, int]] = set()  # (file, line) dedup

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            names_to_check: list[tuple[str, int]] = []

            # Assignment targets
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names_to_check.append((target.id, node.lineno))

            # Annotated assignment targets
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names_to_check.append((node.target.id, node.lineno))

            # Function parameters
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    names_to_check.append((arg.arg, node.lineno))

            # For-loop targets
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                names_to_check.append((node.target.id, node.lineno))

            for name, line in names_to_check:
                # Skip UPPER_CASE names
                if name.isupper() or name.upper() == name:
                    continue
                m = _HUNGARIAN_RE.match(name)
                if m:
                    key = (str(filepath), line)
                    if key not in seen:
                        seen.add(key)
                        snake = _to_snake_case(name)
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=line,
                                check="hungarian-notation",
                                message=(
                                    f"{name} uses Hungarian notation"
                                    f" — consider snake_case: {snake}"
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
            return val.func.id
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


# --- plaintext-passwords ---

_PASSWORD_NAMES = frozenset({"password", "passwd", "secret", "token", "api_key", "apikey"})


def _has_password_name(node: ast.expr) -> bool:
    """Check if an expression refers to a password-related variable."""
    name = None
    if isinstance(node, ast.Name):
        name = node.id.lower()
    elif isinstance(node, ast.Attribute):
        name = node.attr.lower()
    if name is None:
        return False
    return any(pw in name for pw in _PASSWORD_NAMES)


@check(
    "plaintext-passwords",
    severity=Severity.HIGH,
    description="Equality comparison on password/secret/token variables",
)
def check_plaintext_passwords(ctx: AnalysisContext) -> list[Finding]:
    """Find plaintext password comparisons using == or !=."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            # Check == and != comparisons
            if isinstance(node, ast.Compare):
                for op in node.ops:
                    if not isinstance(op, (ast.Eq, ast.NotEq)):
                        continue
                    # Check left side and all comparators
                    all_operands = [node.left] + node.comparators
                    for operand in all_operands:
                        if _has_password_name(operand):
                            op_str = "==" if isinstance(op, ast.Eq) else "!="
                            if isinstance(operand, ast.Name):
                                var_name = operand.id
                            else:
                                var_name = operand.attr  # type: ignore[union-attr]
                            findings.append(
                                Finding(
                                    file=str(filepath),
                                    line=node.lineno,
                                    check="plaintext-passwords",
                                    message=(
                                        f"{var_name} compared with {op_str}"
                                        f" — possible plaintext comparison; use"
                                        f" hmac.compare_digest() or hash comparison"
                                    ),
                                    severity=Severity.HIGH,
                                )
                            )
                            break  # one finding per Compare node
                    else:
                        continue
                    break

    return findings


# --- getattr-strings ---


@check(
    "getattr-strings",
    severity=Severity.MEDIUM,
    description="getattr(obj, 'literal') without default or hasattr(obj, 'literal')",
)
def check_getattr_strings(ctx: AnalysisContext) -> list[Finding]:
    """Find stringly-typed getattr/hasattr calls with literal strings."""
    findings = []
    # For cross-file aggregation
    cross_file: dict[str, list[tuple[str, int]]] = {}

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue

            func_name = node.func.id

            if func_name == "getattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # Skip if 3rd arg or default= keyword provided
                has_default = len(node.args) >= 3 or any(
                    kw.arg == "default" for kw in node.keywords
                )
                attr_name = attr_arg.value
                # Track for cross-file
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if has_default:
                    continue
                if not is_test_file(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(
                                f"getattr(obj, '{attr_name}') without default"
                                f" — use obj.{attr_name} directly"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

            elif func_name == "hasattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # hasattr(self, ...) is legitimate introspection (e.g. Django
                # reverse OneToOneField checks), not stringly-typed access
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Name) and first_arg.id == "self":
                    continue
                attr_name = attr_arg.value
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if not is_test_file(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(
                                f"hasattr(obj, '{attr_name}')"
                                f" — stringly-typed attribute check;"
                                f" consider a Protocol or isinstance()"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

    # Cross-file shotgun surgery check
    for attr_name, locs in cross_file.items():
        files = {loc[0] for loc in locs}
        if len(locs) >= 3 and len(files) >= 3:
            loc_strs = [f"{f}:{line}" for f, line in sorted(locs)[:5]]
            findings.append(
                Finding(
                    file=locs[0][0],
                    line=locs[0][1],
                    check="getattr-strings",
                    message=(
                        f"'{attr_name}' used in getattr/hasattr across"
                        f" {len(locs)} locations in {len(files)} files"
                        f" ({', '.join(loc_strs)}) — shotgun surgery risk"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- late-binding-closures ---

_LOOP_STMTS = (ast.For, ast.AsyncFor)


def _get_loop_var_names(loop: ast.For | ast.AsyncFor) -> set[str]:
    """Extract variable names from a for-loop target."""
    names: set[str] = set()
    target = loop.target
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Tuple):
        for elt in target.elts:
            if isinstance(elt, ast.Name):
                names.add(elt.id)
    return names


def _find_free_names_in_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """Find names read (Load) inside a function/lambda that aren't its own params or locals."""
    # Collect parameter names
    if isinstance(func, ast.Lambda):
        args = func.args
        body_nodes: list[ast.AST] = [func.body]
    else:
        args = func.args
        body_nodes = func.body  # type: ignore[assignment]

    param_names: set[str] = set()
    for a in args.args + args.posonlyargs + args.kwonlyargs:
        param_names.add(a.arg)
    if args.vararg:
        param_names.add(args.vararg.arg)
    if args.kwarg:
        param_names.add(args.kwarg.arg)
    # Default args with same name as param are captures (x=x pattern)
    captured_via_default: set[str] = set()
    all_defaults = args.defaults + args.kw_defaults
    for d in all_defaults:
        if isinstance(d, ast.Name):
            captured_via_default.add(d.id)

    # Collect local assignments
    local_names: set[str] = set()
    for node in ast.walk(func if isinstance(func, ast.Lambda) else ast.Module(body=func.body)):  # type: ignore[arg-type]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            local_names.add(node.target.id)

    # Collect all Name loads
    read_names: set[str] = set()
    for start_node in body_nodes:
        for node in ast.walk(start_node):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                read_names.add(node.id)

    # Free names = reads that aren't params, locals, or captured via defaults
    return read_names - param_names - local_names - captured_via_default


@check(
    "late-binding-closures",
    severity=Severity.HIGH,
    description="Lambda/closure in loop captures loop variable by reference, not value",
)
def check_late_binding_closures(ctx: AnalysisContext) -> list[Finding]:
    """Find closures in loops that capture the loop variable by late binding."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, _LOOP_STMTS):
                continue

            loop_vars = _get_loop_var_names(node)
            if not loop_vars:
                continue

            # Walk the loop body looking for lambdas and nested function defs
            for child in ast.walk(node):
                if child is node:
                    continue

                if isinstance(child, ast.Lambda):
                    free = _find_free_names_in_func(child)
                    captured = loop_vars & free
                    if captured:
                        var_str = ", ".join(sorted(captured))
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=child.lineno,
                                check="late-binding-closures",
                                message=(
                                    f"lambda captures loop variable {var_str}"
                                    f" by reference — all closures will see"
                                    f" the final value; use default arg"
                                    f" ({var_str}={var_str}) to capture"
                                ),
                                severity=Severity.HIGH,
                            )
                        )

                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    free = _find_free_names_in_func(child)
                    captured = loop_vars & free
                    if captured:
                        var_str = ", ".join(sorted(captured))
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=child.lineno,
                                check="late-binding-closures",
                                message=(
                                    f"{child.name}() captures loop variable"
                                    f" {var_str} by reference — all closures"
                                    f" will see the final value"
                                ),
                                severity=Severity.HIGH,
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
            if not isinstance(node.ctx, ast.Load):
                continue

            depth = _chain_length(node)
            if depth < threshold:
                continue

            root = _chain_root(node)
            if root is None:
                continue

            # Skip method calls in the chain — fluent APIs / builder pattern
            # Check if any intermediate node is the func of a Call
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
                continue

            # Skip AST/IR node navigation chains (node.func.value.id etc.)
            chain_attrs: list[str] = []
            nav = node
            while isinstance(nav, ast.Attribute):
                chain_attrs.append(nav.attr)
                nav = nav.value
            if sum(1 for a in chain_attrs if a in _AST_NAV_ATTRS) >= 2:
                continue

            # Skip module-level attribute access (os.path.sep, etc.)
            if root[0].islower() and root in {
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
            }:
                continue

            # Skip self.request.* chains — idiomatic in web framework views
            if root == "self" and "request" in chain_attrs:
                continue

            # Skip static namespace traversal (module.Class.InnerClass.CONST)
            # If all intermediate attrs start uppercase, it's PEP 8 class/enum
            # namespace resolution, not runtime object coupling
            # chain_attrs[0] is the leaf, chain_attrs[1:] are intermediates
            intermediate_attrs = chain_attrs[1:]
            if intermediate_attrs and all(a[0].isupper() for a in intermediate_attrs):
                continue

            chain = _chain_str(node)
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
