"""Shared AST helpers used by multiple checks."""

import ast
from collections import defaultdict
from pathlib import Path


def find_calls_to_function(all_trees: dict[Path, ast.Module], func_name: str) -> list[dict]:
    """Find all call sites for a function name across the codebase."""
    calls = []
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match direct calls: func_name(...)
            if isinstance(node.func, ast.Name) and node.func.id == func_name:
                calls.append({"file": str(filepath), "line": node.lineno, "node": node})
            # Match attribute calls: obj.func_name(...)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
                calls.append({"file": str(filepath), "line": node.lineno, "node": node})
    return calls


def _find_main_entry_points(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Find function names called from ``if __name__ == "__main__":`` blocks."""
    entry_points: set[str] = set()
    for tree in all_trees.values():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.If):
                continue
            if not _is_main_guard(node.test):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    entry_points.add(child.func.id)
    return entry_points


def _is_main_guard(test: ast.expr) -> bool:
    """Check if an expression is ``__name__ == "__main__"``."""
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    # __name__ == "__main__"
    if isinstance(left, ast.Name) and left.id == "__name__":
        return isinstance(right, ast.Constant) and right.value == "__main__"
    # "__main__" == __name__
    if isinstance(right, ast.Name) and right.id == "__name__":
        return isinstance(left, ast.Constant) and left.value == "__main__"
    return False


def build_function_index(all_trees: dict[Path, ast.Module]) -> dict[str, list[dict]]:
    """Build map of public function definitions across the codebase.

    Returns only non-private, non-decorated, non-method, non-test, non-entry-point
    functions that are defined at the top level (not nested inside other functions).
    """
    entry_points = _find_main_entry_points(all_trees)
    func_defs: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        # Collect nested function ids to exclude them
        nested_funcs: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nested_funcs.add(id(child))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(node) in nested_funcs:
                continue
            # Skip private, dunder, test, and entry-point functions
            if node.name.startswith("_") or node.name.startswith("test"):
                continue
            if node.name in entry_points:
                continue
            # Skip decorated functions (registered via decorator, not direct calls)
            if node.decorator_list:
                continue
            # Skip methods (defined inside a class)
            is_method = False
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef):
                    for child in ast.iter_child_nodes(parent):
                        if child is node:
                            is_method = True
                            break
                if is_method:
                    break
            if not is_method:
                func_defs[node.name].append({"file": str(filepath), "line": node.lineno})
    return func_defs


def is_imported_elsewhere(func_name: str, def_file: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a function is imported in any other file."""
    for filepath, tree in all_trees.items():
        if str(filepath) == def_file:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == func_name:
                        return True
    return False


def is_referenced_as_dotted_string(func_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a function name appears as the final component of a dotted-path string.

    Frameworks like Django reference functions by dotted paths in settings:
    ``"myapp.context_processors.site_url"`` references ``site_url``.
    """
    suffix = f".{func_name}"
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "." in node.value
                and node.value.endswith(suffix)
            ):
                return True
    return False


def is_referenced_as_value(func_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a function name appears as a dict value, list element, or argument."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for val in node.values:
                    if isinstance(val, ast.Name) and val.id == func_name:
                        return True
            if isinstance(node, (ast.List, ast.Tuple)):
                for elt in node.elts:
                    if isinstance(elt, ast.Name) and elt.id == func_name:
                        return True
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == func_name:
                        return True
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Name) and kw.value.id == func_name:
                        return True
    return False
