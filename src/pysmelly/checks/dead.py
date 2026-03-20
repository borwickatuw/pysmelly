"""Dead code extension checks — exceptions, dispatch entries, test helpers."""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.helpers import (
    build_exception_index,
    build_test_function_index,
    is_caught_anywhere,
    is_imported_elsewhere,
    is_in_dunder_all,
    is_isinstance_target,
    is_raised_anywhere,
    is_referenced_as_dotted_string,
    is_referenced_as_value,
    is_subclassed,
    is_test_file,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


@check(
    "dead-exceptions",
    severity=Severity.HIGH,
    description="Custom exception classes never raised or caught anywhere",
)
def check_dead_exceptions(ctx: AnalysisContext) -> list[Finding]:
    """Find custom exception classes never raised, caught, imported, subclassed, or referenced."""
    findings = []
    exc_defs = build_exception_index(ctx.all_trees)

    for exc_name, defs in exc_defs.items():
        if len(defs) > 1:
            continue

        def_file = defs[0]["file"]
        def_tree = ctx.all_trees[Path(def_file)]

        if is_raised_anywhere(exc_name, ctx.all_trees):
            continue
        if is_caught_anywhere(exc_name, ctx.all_trees):
            continue
        if is_imported_elsewhere(exc_name, def_file, ctx):
            continue
        if is_subclassed(exc_name, ctx.all_trees):
            continue
        if is_isinstance_target(exc_name, ctx.all_trees):
            continue
        if is_referenced_as_value(exc_name, ctx):
            continue
        if is_referenced_as_dotted_string(exc_name, ctx):
            continue
        if is_in_dunder_all(exc_name, def_tree):
            continue

        findings.append(
            Finding(
                file=def_file,
                line=defs[0]["line"],
                check="dead-exceptions",
                message=f"{exc_name} (exception class) has no raise/except references",
                severity=Severity.HIGH,
            )
        )

    return findings


def _find_dispatch_dicts(
    all_trees: dict[Path, ast.Module],
) -> list[dict]:
    """Find dispatch dicts: top-level Assign -> Dict with all string keys and Name values, 3+ entries."""
    min_entries = 3
    results = []
    for filepath, tree in all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            d = node.value
            if not d.keys:
                continue
            if len(d.keys) < min_entries:
                continue
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in d.keys):
                continue
            if not all(isinstance(v, ast.Name) for v in d.values):
                continue
            # Skip when all values are UPPER_CASE — constants/config, not dispatch
            if all(v.id.isupper() for v in d.values):  # type: ignore[union-attr]
                continue

            var_name = None
            if isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id

            results.append(
                {
                    "file": str(filepath),
                    "filepath": filepath,
                    "line": node.lineno,
                    "var_name": var_name,
                    "dict_node": d,
                    "assign_node": node,
                }
            )
    return results


def _is_dict_passed_or_returned(var_name: str, tree: ast.Module, assign_node: ast.Assign) -> bool:
    """Check if the dict variable is passed to a function, returned, or assigned to another name."""
    for node in ast.walk(tree):
        if node is assign_node:
            continue
        # Passed as argument
        if isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == var_name:
                    return True
            for kw in node.keywords:
                if isinstance(kw.value, ast.Name) and kw.value.id == var_name:
                    return True
        # Returned
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Name) and node.value.id == var_name:
                return True
        # Assigned to another name
        if isinstance(node, ast.Assign) and node is not assign_node:
            if isinstance(node.value, ast.Name) and node.value.id == var_name:
                return True
    return False


def _count_string_occurrences(
    key_value: str,
    all_trees: dict[Path, ast.Module],
    exclude_file: Path,
    exclude_lines: set[int],
) -> int:
    """Count occurrences of an exact string constant across all files, excluding the dict definition."""
    count = 0
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            if node.value != key_value:
                continue
            # Exclude the dict definition key lines
            if filepath == exclude_file and node.lineno in exclude_lines:
                continue
            count += 1
    return count


@check(
    "dead-dispatch-entries",
    severity=Severity.MEDIUM,
    description="Dispatch dict entries whose key strings appear nowhere else",
)
def check_dead_dispatch_entries(ctx: AnalysisContext) -> list[Finding]:
    """Find entries in dispatch dicts whose key strings appear nowhere else in the codebase."""
    findings = []
    dispatch_dicts = _find_dispatch_dicts(ctx.all_trees)

    for dd in dispatch_dicts:
        var_name = dd["var_name"]
        filepath = dd["filepath"]
        tree = ctx.all_trees[filepath]

        # Suppress if the dict is passed to a function, returned, or reassigned
        if var_name and _is_dict_passed_or_returned(var_name, tree, dd["assign_node"]):
            continue

        d = dd["dict_node"]
        key_lines = {k.lineno for k in d.keys if hasattr(k, "lineno")}
        for key_node in d.keys:
            key_value = key_node.value  # type: ignore[union-attr]
            occurrences = _count_string_occurrences(key_value, ctx.all_trees, filepath, key_lines)
            if occurrences == 0:
                display_name = var_name or "dict"
                findings.append(
                    Finding(
                        file=dd["file"],
                        line=dd["line"],
                        check="dead-dispatch-entries",
                        message=(
                            f'{display_name}["{key_value}"] key never appears '
                            f"as a string elsewhere — dead entry?"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


def _collect_fixture_param_names(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect all parameter names from all functions in test files.

    This is the set of "requested" fixture names.
    """
    names: set[str] = set()
    for filepath, tree in all_trees.items():
        if not is_test_file(filepath):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for arg in node.args.args:
                if arg.arg not in ("self", "cls"):
                    names.add(arg.arg)
            for arg in node.args.kwonlyargs:
                names.add(arg.arg)
    return names


@check(
    "orphaned-test-helpers",
    severity=Severity.MEDIUM,
    description="Test helper functions and unused fixtures with zero callers",
)
def check_orphaned_test_helpers(ctx: AnalysisContext) -> list[Finding]:
    """Find helper functions and unused fixtures in test files with zero callers."""
    findings = []
    test_funcs = build_test_function_index(ctx.all_trees)
    fixture_params = _collect_fixture_param_names(ctx.all_trees)

    for func_info in test_funcs:
        name = func_info["name"]
        def_file = func_info["file"]
        is_fixture = func_info["is_fixture"]

        if is_fixture:
            # Fixtures are "called" by appearing as parameter names
            if name in fixture_params:
                continue
            findings.append(
                Finding(
                    file=def_file,
                    line=func_info["line"],
                    check="orphaned-test-helpers",
                    message=(
                        f"{name} fixture in {Path(def_file).name} "
                        f"is never requested — unused fixture?"
                    ),
                    severity=Severity.MEDIUM,
                )
            )
        else:
            # Non-fixture helpers: check calls, imports, value references
            calls = ctx.call_index.get(name, [])
            if calls:
                continue
            if is_imported_elsewhere(name, def_file, ctx):
                continue
            if is_referenced_as_value(name, ctx):
                continue
            findings.append(
                Finding(
                    file=def_file,
                    line=func_info["line"],
                    check="orphaned-test-helpers",
                    message=(
                        f"{name}() in {Path(def_file).name} has no callers "
                        f"— orphaned test helper?"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- dead-abstraction helpers ---


def _is_abstract_class(node: ast.ClassDef) -> bool:
    """Check if a class is abstract (inherits from ABC or has @abstractmethod methods)."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "ABC":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "ABC":
            return True
    for kw in node.keywords:
        if kw.arg == "metaclass":
            if isinstance(kw.value, ast.Name) and kw.value.id == "ABCMeta":
                return True
            if isinstance(kw.value, ast.Attribute) and kw.value.attr == "ABCMeta":
                return True
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in item.decorator_list:
                if isinstance(deco, ast.Name) and deco.id == "abstractmethod":
                    return True
                if isinstance(deco, ast.Attribute) and deco.attr == "abstractmethod":
                    return True
    return False


def _count_abstract_methods(node: ast.ClassDef) -> int:
    """Count the number of @abstractmethod decorated methods in a class."""
    count = 0
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in item.decorator_list:
            if isinstance(deco, ast.Name) and deco.id == "abstractmethod":
                count += 1
            elif isinstance(deco, ast.Attribute) and deco.attr == "abstractmethod":
                count += 1
    return count


@check(
    "dead-abstraction",
    severity=Severity.MEDIUM,
    description="Abstract base classes with no concrete implementations",
)
def check_dead_abstractions(ctx: AnalysisContext) -> list[Finding]:
    """Find ABCs that have no concrete subclasses anywhere in the codebase.

    ABCs are created 'for extensibility' that never materializes. The plugin
    system ships, nobody writes plugins, the ABC lives on as a monument to
    speculative generality.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_abstract_class(node):
                continue

            if is_subclassed(node.name, ctx.all_trees):
                continue
            if is_in_dunder_all(node.name, tree):
                continue
            if is_imported_elsewhere(node.name, str(filepath), ctx):
                continue
            if is_referenced_as_value(node.name, ctx):
                continue

            abstract_count = _count_abstract_methods(node)
            method_desc = f"with {abstract_count} abstract method(s)" if abstract_count else ""

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="dead-abstraction",
                    message=(
                        f"{node.name} (ABC {method_desc}) has no concrete "
                        f"implementations — speculative generality?"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- broken-backends helpers ---


def _body_is_raise_not_implemented(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a method body is just `raise NotImplementedError` (with optional docstring)."""
    stmts = method.body
    # Strip leading docstring
    body = stmts
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    if len(body) != 1:
        return False

    stmt = body[0]
    if not isinstance(stmt, ast.Raise):
        return False
    exc = stmt.exc
    if exc is None:
        return False
    # raise NotImplementedError
    if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
        return True
    # raise NotImplementedError(...)
    if (
        isinstance(exc, ast.Call)
        and isinstance(exc.func, ast.Name)
        and exc.func.id == "NotImplementedError"
    ):
        return True
    return False


@check(
    "broken-backends",
    severity=Severity.MEDIUM,
    description="Non-abstract classes where every method raises NotImplementedError",
)
def check_broken_backends(ctx: AnalysisContext) -> list[Finding]:
    """Find classes where all methods raise NotImplementedError but the class isn't abstract."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if _is_abstract_class(node):
                continue

            # Collect non-__init__ methods
            methods = [
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name != "__init__"
            ]

            if len(methods) < 2:
                continue

            if all(_body_is_raise_not_implemented(m) for m in methods):
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="broken-backends",
                        message=(
                            f"{node.name} has {len(methods)} methods all raising"
                            f" NotImplementedError but is not abstract"
                            f" — broken backend or missing ABC base?"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings
