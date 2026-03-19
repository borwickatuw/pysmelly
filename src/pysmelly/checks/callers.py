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

    Checks direct calls, imports, dict/list references, and callback passing.
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

    Functions with 5+ statements are skipped — those were likely extracted
    for readability, not by accident.

    Severity is bumped to MEDIUM when the function has many parameters (4+)
    or when all arguments come from a single object (decomposing then
    recomposing a data structure).
    """
    findings = []
    func_defs = build_function_index(all_trees)
    max_body_stmts = 4

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = find_calls_to_function(all_trees, func_name)
        if len(calls) != 1:
            continue

        if is_imported_elsewhere(func_name, def_file, all_trees):
            continue

        # Skip functions with 5+ statements — extracted for readability
        func_node = _find_func_node(all_trees, func_name)
        if func_node and len(func_node.body) > max_body_stmts:
            continue

        call = calls[0]
        call_node = call["node"]

        # Count non-self/cls params
        param_count = 0
        if func_node:
            param_count = len(
                [a for a in func_node.args.args if a.arg not in ("self", "cls")]
            )

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
        parts.append(
            f"exactly 1 call site "
            f"({call['file'].split('/')[-1]}:{call['line']})"
        )
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
