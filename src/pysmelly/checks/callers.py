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
                        f"but all {len(calls)} caller(s) always pass it"
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
    description="Public functions called exactly once (inline candidate)",
)
def check_single_call_site(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find public functions called exactly once (candidate for inlining)."""
    findings = []
    func_defs = build_function_index(all_trees)

    for func_name, defs in func_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        calls = find_calls_to_function(all_trees, func_name)
        if len(calls) != 1:
            continue

        if is_imported_elsewhere(func_name, def_file, all_trees):
            continue

        call = calls[0]
        findings.append(
            Finding(
                file=def_file,
                line=defs[0]["line"],
                check="single-call-site",
                message=(
                    f"{func_name}() has exactly 1 call site "
                    f"({call['file'].split('/')[-1]}:{call['line']}) — "
                    f"consider inlining"
                ),
                severity=Severity.LOW,
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
