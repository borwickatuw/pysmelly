"""Caller-aware checks — cross-file call-graph analysis.

These checks build a picture of who calls what and flag functions
whose usage pattern suggests they should be refactored.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.helpers import (
    is_imported_elsewhere,
    is_in_dunder_all,
    is_referenced_as_dotted_string,
    is_referenced_as_value,
    is_test_file,
    is_used_as_decorator,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


def _find_function_defaults(tree: ast.Module, filepath: Path) -> list[dict]:
    """Find all functions with default parameter values of None."""
    # Collect methods in non-private classes — their defaults exist for
    # external callers we can't see via call-graph analysis.
    public_class_methods: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    public_class_methods.add(id(item))

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") and not node.name.startswith("__"):
            continue
        if id(node) in public_class_methods:
            continue

        args = node.args
        num_defaults = len(args.defaults)
        if num_defaults == 0:
            continue

        default_start = len(args.args) - num_defaults
        for i, default in enumerate(args.defaults):
            arg = args.args[default_start + i]
            if isinstance(default, ast.Constant) and default.value is None:
                results.append(
                    {
                        "file": str(filepath),
                        "line": node.lineno,
                        "func": node.name,
                        "param": arg.arg,
                    }
                )
    return results


@check(
    "unused-defaults",
    severity=Severity.HIGH,
    description="Params defaulting to None that every caller always passes",
)
def check_unused_defaults(ctx: AnalysisContext) -> list[Finding]:
    """Find Optional params where every caller always passes a value.

    If a parameter defaults to None but no caller ever relies on that
    default, the Optional is vestigial — make the parameter required.
    """
    findings = []

    all_defaults = []
    for filepath, tree in ctx.all_trees.items():
        all_defaults.extend(_find_function_defaults(tree, filepath))

    for func_info in all_defaults:
        func_name = func_info["func"]
        param_name = func_info["param"]

        calls = ctx.call_index.get(func_name, [])
        if not calls:
            continue

        all_callers_pass = True
        for call in calls:
            node = call["node"]
            param_index = None
            for tree2 in ctx.all_trees.values():
                for fnode in ast.walk(tree2):
                    if (
                        isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and fnode.name == func_name
                    ):
                        for idx, arg in enumerate(fnode.args.args):
                            if arg.arg == param_name:
                                param_index = idx
                                break
                        break
                if param_index is not None:
                    break

            if param_index is None:
                all_callers_pass = False
                break

            if len(node.args) > param_index:
                continue
            if any(kw.arg == param_name for kw in node.keywords):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue  # **kwargs — can't tell

            all_callers_pass = False
            break

        if all_callers_pass and len(calls) > 0:
            findings.append(
                Finding(
                    file=func_info["file"],
                    line=func_info["line"],
                    check="unused-defaults",
                    message=(
                        f"{func_name}() param '{param_name}' defaults to None "
                        f"but all {len(calls)} caller(s) always pass it — "
                        f"make it required"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


def _has_deprecation_warning(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function body contains warnings.warn(..., DeprecationWarning)."""
    _DEPRECATION_NAMES = {"DeprecationWarning", "PendingDeprecationWarning"}
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "warn"):
            continue
        # Check 2nd positional arg: warnings.warn("msg", DeprecationWarning)
        if len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Name) and arg.id in _DEPRECATION_NAMES:
                return True
        # Check category keyword: warnings.warn("msg", category=DeprecationWarning)
        for kw in node.keywords:
            if kw.arg == "category" and isinstance(kw.value, ast.Name):
                if kw.value.id in _DEPRECATION_NAMES:
                    return True
    return False


@check(
    "dead-code", severity=Severity.HIGH, description="Public functions with zero callers anywhere"
)
def check_dead_code(ctx: AnalysisContext) -> list[Finding]:
    """Find public functions with no callers at all.

    Checks direct calls, imports, dict/list references, callback passing,
    and dotted-path string references (e.g., Django settings).
    """
    findings = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = ctx.call_index.get(func_name, [])

        if (
            not calls
            and not is_imported_elsewhere(func_name, def_file, ctx)
            and not is_referenced_as_value(func_name, ctx)
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

            findings.append(
                Finding(
                    file=def_file,
                    line=defs[0]["line"],
                    check="dead-code",
                    message=f"{func_name}() has no callers (dead code?)",
                    severity=Severity.HIGH,
                )
            )

    return findings


@check(
    "single-call-site",
    severity=Severity.LOW,
    description="Short functions called exactly once (inline candidate)",
)
def check_single_call_site(ctx: AnalysisContext) -> list[Finding]:
    """Find short public functions called exactly once (candidate for inlining).

    Filters:
    - Functions with 5+ top-level statements are skipped
    - Functions spanning 10+ lines are skipped
    - Cross-directory calls are suppressed (public API boundaries)

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
        calls = ctx.call_index.get(func_name, [])
        if len(calls) != 1:
            continue

        if is_imported_elsewhere(func_name, def_file, ctx):
            continue

        func_node = defs[0].get("node")

        # Skip functions with 5+ statements — extracted for readability
        if func_node and len(func_node.body) > max_body_stmts:
            continue

        # Skip functions spanning 10+ lines — too large to inline
        if func_node and hasattr(func_node, "end_lineno") and func_node.end_lineno:
            if func_node.end_lineno - func_node.lineno + 1 > max_body_lines:
                continue

        call = calls[0]

        # Skip cross-directory calls — these are public API boundaries
        def_dir = str(Path(def_file).parent)
        call_dir = str(Path(call["file"]).parent)
        if def_dir != call_dir:
            continue

        call_node = call["node"]

        # Count non-self/cls params
        param_count = 0
        if func_node:
            param_count = len([a for a in func_node.args.args if a.arg not in ("self", "cls")])

        # Detect when all args come from a single object
        single_source = _args_from_single_object(call_node)

        # Bump severity when heuristics suggest a bad extraction
        severity = Severity.LOW
        if param_count >= 4 or single_source:
            severity = Severity.MEDIUM

        # Build message with context for triage
        parts = [f"{func_name}()"]
        if param_count > 0:
            parts.append(f"has {param_count} params and")
        parts.append(f"exactly 1 call site " f"({call['file'].split('/')[-1]}:{call['line']})")
        if single_source:
            parts.append(f"— all args from '{single_source}'")
        parts.append("— consider inlining")
        msg = " ".join(parts)

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


@check(
    "internal-only",
    severity=Severity.LOW,
    description="Public functions only called within their own file",
)
def check_internal_only(ctx: AnalysisContext) -> list[Finding]:
    """Find public functions only called within their own file (2+ calls).

    These are candidates for renaming to _private.
    """
    findings = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = ctx.call_index.get(func_name, [])
        if not calls:
            continue

        internal_calls = [c for c in calls if c["file"] == def_file]
        external_calls = [c for c in calls if c["file"] != def_file]

        if (
            not external_calls
            and len(internal_calls) >= 2
            and not is_imported_elsewhere(func_name, def_file, ctx)
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
                        f"{func_name}() is public but only called within same file "
                        f"({len(internal_calls)} internal call(s))"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings


@check(
    "constant-args",
    severity=Severity.MEDIUM,
    description="Param always receives the same literal value from every caller",
)
def check_constant_args(ctx: AnalysisContext) -> list[Finding]:
    """Find parameters where every caller passes the same literal value.

    If all callers pass the same constant, the value should be a default
    or a module-level constant rather than repeated at each call site.
    """
    findings = []
    func_defs = ctx.function_index

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        calls = ctx.call_index.get(func_name, [])
        if len(calls) < 2:
            continue

        # Find the actual function node to get parameter names
        func_node = defs[0].get("node")
        if func_node is None:
            continue

        params = [a.arg for a in func_node.args.args if a.arg not in ("self", "cls")]

        for param_idx, param_name in enumerate(params):
            values: list[str] = []
            all_constant = True

            for call in calls:
                node = call["node"]
                value = _get_arg_value(node, param_idx, param_name)
                if value is None:
                    all_constant = False
                    break
                values.append(value)

            if all_constant and values and len(set(values)) == 1:
                findings.append(
                    Finding(
                        file=defs[0]["file"],
                        line=defs[0]["line"],
                        check="constant-args",
                        message=(
                            f"{func_name}() param '{param_name}' always receives "
                            f"{values[0]} from all {len(calls)} caller(s)"
                        ),
                        severity=Severity.MEDIUM,
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


def _get_arg_value(call_node: ast.Call, param_idx: int, param_name: str) -> str | None:
    """Extract the constant value passed for a parameter, or None if not a constant."""
    # Check positional args first
    if param_idx < len(call_node.args):
        arg = call_node.args[param_idx]
        if isinstance(arg, ast.Constant):
            return repr(arg.value)
        return None

    # Check keyword args
    for kw in call_node.keywords:
        if kw.arg == param_name:
            if isinstance(kw.value, ast.Constant):
                return repr(kw.value.value)
            return None

    # **kwargs — can't determine
    if any(kw.arg is None for kw in call_node.keywords):
        return None

    # Parameter not passed at all (uses default) — not a constant-args case
    return None


@check(
    "return-none-instead-of-raise",
    severity=Severity.MEDIUM,
    description="Functions returning None on error where callers all guard against None",
)
def check_return_none_instead_of_raise(ctx: AnalysisContext) -> list[Finding]:
    """Find functions with mixed returns (None + non-None) where callers guard against None.

    If a function returns None on error paths and most callers check
    ``if result is None:``, the function should raise instead.
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

        guarded, unguarded = _count_none_guards(ctx.all_trees, func_name)
        if guarded >= 2:
            total = guarded + unguarded
            findings.append(
                Finding(
                    file=defs[0]["file"],
                    line=defs[0]["line"],
                    check="return-none-instead-of-raise",
                    message=(
                        f"{func_name}() returns None in some branches and "
                        f"{guarded} of {total} caller(s) guard against None "
                        f"— consider raising instead"
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
        elif isinstance(node.value, ast.Constant) and node.value.value is None:
            # return None
            none_returns += 1
        else:
            value_returns += 1

    return none_returns >= 1 and value_returns >= 1


def _is_none_guard(stmt: ast.stmt, var_name: str) -> bool:
    """Detect ``if x is None:`` / ``if x is not None:`` / ``if not x:`` / ``if x:`` patterns."""
    if not isinstance(stmt, ast.If):
        return False

    test = stmt.test

    # if x is None / if x is not None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        if isinstance(test.ops[0], (ast.Is, ast.IsNot)):
            left = test.left
            comp = test.comparators[0]
            if isinstance(left, ast.Name) and left.id == var_name:
                if isinstance(comp, ast.Constant) and comp.value is None:
                    return True
            if isinstance(comp, ast.Name) and comp.id == var_name:
                if isinstance(left, ast.Constant) and left.value is None:
                    return True

    # if not x (where x is the var_name)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        if isinstance(test.operand, ast.Name) and test.operand.id == var_name:
            return True

    # if x (truthiness check)
    if isinstance(test, ast.Name) and test.id == var_name:
        return True

    return False


def _count_none_guards(all_trees: dict[Path, ast.Module], func_name: str) -> tuple[int, int]:
    """Count callers that guard vs don't guard against None return.

    Returns (guarded_count, unguarded_count). Callers that discard the
    return value (bare call statements) are ignored.
    """
    guarded = 0
    unguarded = 0

    for tree in all_trees.values():
        for node in ast.walk(tree):
            # Look for: var = func(...)
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if isinstance(call.func, ast.Name) and call.func.id == func_name:
                pass
            elif isinstance(call.func, ast.Attribute) and call.func.attr == func_name:
                pass
            else:
                continue

            var_name = target.id

            # Find the containing block to check subsequent statements
            found_guard = _find_guard_in_tree(tree, node, var_name)
            if found_guard:
                guarded += 1
            else:
                unguarded += 1

    return guarded, unguarded


def _find_guard_in_tree(tree: ast.Module, assign_node: ast.Assign, var_name: str) -> bool:
    """Check if the assignment is followed by a None guard within 3 statements."""
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if not isinstance(body, list):
                continue
            for i, stmt in enumerate(body):
                if stmt is not assign_node:
                    continue
                # Check next 3 statements
                for j in range(i + 1, min(i + 4, len(body))):
                    if _is_none_guard(body[j], var_name):
                        return True
                return False
        if isinstance(parent, ast.ExceptHandler) and parent.body:
            for i, stmt in enumerate(parent.body):
                if stmt is not assign_node:
                    continue
                for j in range(i + 1, min(i + 4, len(parent.body))):
                    if _is_none_guard(parent.body[j], var_name):
                        return True
                return False
    return False


@check(
    "pass-through-params",
    severity=Severity.MEDIUM,
    description="Params received by a function and only forwarded to another function",
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
                        f"it to {targets_str} — consider passing directly or "
                        f"using a context object"
                    ),
                    severity=Severity.MEDIUM,
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
    param_names = {p for p in all_params if p not in ("self", "cls")}

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
            info = results[node.id]
            info["total_loads"] += 1

            parent = parent_map.get(id(node))
            if parent is None:
                info["non_forwarding_loads"] += 1
            else:
                call_node = None
                if isinstance(parent, ast.Call) and node in parent.args:
                    call_node = parent
                elif isinstance(parent, ast.keyword):
                    grandparent = parent_map.get(id(parent))
                    if isinstance(grandparent, ast.Call):
                        call_node = grandparent

                if call_node is not None:
                    target_name = _get_call_target_name(call_node)
                    if target_name and target_name in known_funcs:
                        info["call_targets"].add(target_name)
                    else:
                        info["non_forwarding_loads"] += 1
                else:
                    info["non_forwarding_loads"] += 1

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
    if isinstance(handler.type, ast.Name):
        return [handler.type.id]
    if isinstance(handler.type, ast.Attribute):
        return [handler.type.attr]
    if isinstance(handler.type, ast.Tuple):
        names = []
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.append(elt.attr)
        return names
    return []


_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})


def _classify_error_handling(
    call_node: ast.Call, tree: ast.Module, ctx: AnalysisContext
) -> tuple[str, list[str]]:
    """Classify the error handling context of a call node.

    Returns (category, exception_names) where category is one of:
    - "specific" — catches named exception types (not Exception/BaseException)
    - "broad" — catches Exception, BaseException, or bare except
    - "unhandled" — no enclosing try/except
    """
    parents = ctx.parent_map(tree)

    # Walk up to find the innermost enclosing Try
    current: ast.AST = call_node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Try):
            # Check if the call is in the try body (not in except/else/finally)
            call_line = call_node.lineno
            try_body_lines = set()
            for stmt in current.body:
                for node in ast.walk(stmt):
                    if hasattr(node, "lineno"):
                        try_body_lines.add(node.lineno)
            if call_line not in try_body_lines:
                continue

            # Classify the handlers
            all_names: list[str] = []
            has_specific = False
            has_broad = False
            for handler in current.handlers:
                names = _get_except_handler_names(handler)
                all_names.extend(names)
                for name in names:
                    if name in _BROAD_EXCEPTIONS or name == "bare except":
                        has_broad = True
                    else:
                        has_specific = True

            # If any handler catches specific types, classify as specific
            if has_specific:
                specific_names = [
                    n for n in all_names if n not in _BROAD_EXCEPTIONS and n != "bare except"
                ]
                return "specific", specific_names
            return "broad", all_names

    return "unhandled", []


@check(
    "inconsistent-error-handling",
    severity=Severity.MEDIUM,
    description="Same function called with divergent error handling across callers",
)
def check_inconsistent_error_handling(ctx: AnalysisContext) -> list[Finding]:
    """Find functions called from 3+ sites with divergent error handling.

    Flags when at least one caller catches specific exceptions (proving there's
    a known failure mode) while other callers catch broad Exception or don't
    handle errors at all.
    """
    findings = []
    func_index = ctx.function_index

    for func_name, defs in func_index.items():
        if len(defs) != 1:
            continue

        calls = ctx.call_index.get(func_name, [])
        # Filter out test file callers
        calls = [c for c in calls if not is_test_file(Path(c["file"]))]
        if len(calls) < 3:
            continue

        specific_callers: list[dict] = []
        broad_callers: list[dict] = []
        unhandled_callers: list[dict] = []
        all_specific_names: set[str] = set()

        for call in calls:
            tree = ctx.all_trees[Path(call["file"])]
            category, exc_names = _classify_error_handling(call["node"], tree, ctx)
            call_info = {"file": call["file"], "line": call["line"]}
            if category == "specific":
                specific_callers.append(call_info)
                all_specific_names.update(exc_names)
            elif category == "broad":
                broad_callers.append(call_info)
            else:
                unhandled_callers.append(call_info)

        # Only flag when at least one caller catches specific exceptions
        # AND at least one other caller is broad or unhandled
        if not specific_callers:
            continue
        if not broad_callers and not unhandled_callers:
            continue

        def_info = defs[0]
        parts = []
        if specific_callers:
            exc_str = ", ".join(sorted(all_specific_names))
            parts.append(f"{len(specific_callers)} catch specific ({exc_str})")
        if broad_callers:
            parts.append(f"{len(broad_callers)} catch broad Exception")
        if unhandled_callers:
            parts.append(f"{len(unhandled_callers)} unhandled")

        total = len(specific_callers) + len(broad_callers) + len(unhandled_callers)
        findings.append(
            Finding(
                file=def_info["file"],
                line=def_info["line"],
                check="inconsistent-error-handling",
                message=(
                    f"{func_name}() has {total} callers with inconsistent "
                    f"error handling: {', '.join(parts)} "
                    f"— error contract is unclear"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


# --- vestigial-params helpers ---


def _is_stub_body(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function body is a stub (pass, ..., bare return, raise NotImplementedError)."""
    body = func_node.body
    # Strip docstring and comment-only expressions
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        if stmt.value.value is ...:
            return True
    # bare return / return None — unimplemented function (often with # TODO)
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True
        if isinstance(stmt.value, ast.Constant) and stmt.value.value is None:
            return True
    if isinstance(stmt, ast.Raise) and stmt.exc:
        exc = stmt.exc
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            if exc.func.id == "NotImplementedError":
                return True
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
    return False


def _has_interface_decorator(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function is decorated with @abstractmethod or @override."""
    for deco in func_node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id in ("abstractmethod", "override"):
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in ("abstractmethod", "override"):
            return True
    return False


# Decorators that indicate framework dispatch — all params are
# required by the framework, not by the function's own logic.
_FRAMEWORK_DISPATCH_DECORATORS = frozenset(
    {
        "receiver",  # Django signals
        "task",  # Celery
        "shared_task",  # Celery
        "periodic_task",  # Celery
        "hookimpl",  # pluggy
    }
)


def _has_framework_dispatch_decorator(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Check if a function is decorated with a framework dispatch decorator."""
    for deco in func_node.decorator_list:
        name = None
        if isinstance(deco, ast.Name):
            name = deco.id
        elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
            name = deco.func.id
        elif isinstance(deco, ast.Attribute):
            name = deco.attr
        elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
            name = deco.func.attr
        if name in _FRAMEWORK_DISPATCH_DECORATORS:
            return True
    return False


def _find_unused_params(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Find parameter names never referenced in the function body."""
    param_names: list[str] = []
    first_real_param = True
    for arg in func_node.args.posonlyargs + func_node.args.args + func_node.args.kwonlyargs:
        if arg.arg in ("self", "cls"):
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

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Skip stubs, interface methods, and framework dispatch
            if _is_stub_body(node):
                continue
            if _has_interface_decorator(node):
                continue
            if _has_framework_dispatch_decorator(node):
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
                    msg = f"{param_name} is declared but never used in " f"{node.name}()"

                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="vestigial-params",
                        message=msg,
                        severity=Severity.MEDIUM,
                    )
                )

    return findings
