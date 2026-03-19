"""Caller-aware checks — cross-file call-graph analysis.

These checks build a picture of who calls what and flag functions
whose usage pattern suggests they should be refactored.
"""

import ast
from pathlib import Path

from pysmelly.checks.helpers import (
    build_function_index,
    find_calls_to_function,
    is_imported_elsewhere,
    is_referenced_as_dotted_string,
    is_referenced_as_value,
)
from pysmelly.registry import Finding, Severity, check


def _find_function_defaults(tree: ast.Module, filepath: Path) -> list[dict]:
    """Find all functions with default parameter values of None."""
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") and not node.name.startswith("__"):
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
def check_unused_defaults(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find Optional params where every caller always passes a value.

    If a parameter defaults to None but no caller ever relies on that
    default, the Optional is vestigial — make the parameter required.
    """
    findings = []

    all_defaults = []
    for filepath, tree in all_trees.items():
        all_defaults.extend(_find_function_defaults(tree, filepath))

    for func_info in all_defaults:
        func_name = func_info["func"]
        param_name = func_info["param"]

        calls = find_calls_to_function(all_trees, func_name)
        if not calls:
            continue

        all_callers_pass = True
        for call in calls:
            node = call["node"]
            param_index = None
            for tree2 in all_trees.values():
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


@check(
    "dead-code", severity=Severity.HIGH, description="Public functions with zero callers anywhere"
)
def check_dead_code(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find public functions with no callers at all.

    Checks direct calls, imports, dict/list references, callback passing,
    and dotted-path string references (e.g., Django settings).
    """
    findings = []
    func_defs = build_function_index(all_trees)

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = find_calls_to_function(all_trees, func_name)

        if (
            not calls
            and not is_imported_elsewhere(func_name, def_file, all_trees)
            and not is_referenced_as_value(func_name, all_trees)
            and not is_referenced_as_dotted_string(func_name, all_trees)
        ):
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
def check_single_call_site(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find short public functions called exactly once (candidate for inlining).

    Filters:
    - Functions with 5+ top-level statements are skipped
    - Functions spanning 30+ lines are skipped
    - Cross-directory calls are suppressed (public API boundaries)

    Severity is bumped to MEDIUM when the function has many parameters (4+)
    or when all arguments come from a single object (decomposing then
    recomposing a data structure).
    """
    findings = []
    func_defs = build_function_index(all_trees)
    max_body_stmts = 4
    max_body_lines = 30

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = find_calls_to_function(all_trees, func_name)
        if len(calls) != 1:
            continue

        if is_imported_elsewhere(func_name, def_file, all_trees):
            continue

        func_node = _find_func_node(all_trees, func_name)

        # Skip functions with 5+ statements — extracted for readability
        if func_node and len(func_node.body) > max_body_stmts:
            continue

        # Skip functions spanning 30+ lines — too large to inline
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
def check_internal_only(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find public functions only called within their own file (2+ calls).

    These are candidates for renaming to _private.
    """
    findings = []
    func_defs = build_function_index(all_trees)

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = find_calls_to_function(all_trees, func_name)
        if not calls:
            continue

        internal_calls = [c for c in calls if c["file"] == def_file]
        external_calls = [c for c in calls if c["file"] != def_file]

        if (
            not external_calls
            and len(internal_calls) >= 2
            and not is_imported_elsewhere(func_name, def_file, all_trees)
        ):
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
def check_constant_args(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find parameters where every caller passes the same literal value.

    If all callers pass the same constant, the value should be a default
    or a module-level constant rather than repeated at each call site.
    """
    findings = []
    func_defs = build_function_index(all_trees)

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        calls = find_calls_to_function(all_trees, func_name)
        if len(calls) < 2:
            continue

        # Find the actual function node to get parameter names
        func_node = _find_func_node(all_trees, func_name)
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


def _find_func_node(
    all_trees: dict[Path, ast.Module], func_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the first function definition node matching func_name."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                return node
    return None


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
def check_return_none_instead_of_raise(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find functions with mixed returns (None + non-None) where callers guard against None.

    If a function returns None on error paths and most callers check
    ``if result is None:``, the function should raise instead.
    """
    findings = []
    func_defs = build_function_index(all_trees)

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        func_node = _find_func_node(all_trees, func_name)
        if func_node is None:
            continue

        if not _has_mixed_returns(func_node):
            continue

        guarded, unguarded = _count_none_guards(all_trees, func_name)
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
def check_pass_through_params(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find parameters that a function receives but only passes through to other functions.

    If a parameter is never used by the intermediary function — only
    forwarded to known functions in the codebase — the caller should
    pass directly to the consumer, or a context/config object should be used.
    """
    findings = []
    func_defs = build_function_index(all_trees)
    known_funcs = set(func_defs.keys())

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        func_node = _find_func_node(all_trees, func_name)
        if func_node is None:
            continue

        # Skip orphan functions — dead-code handles those
        calls = find_calls_to_function(all_trees, func_name)
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
