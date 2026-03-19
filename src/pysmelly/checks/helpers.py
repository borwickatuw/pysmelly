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


def build_function_index(all_trees: dict[Path, ast.Module]) -> dict[str, list[dict]]:
    """Build map of public function definitions across the codebase.

    Returns only non-private, non-decorated, non-method, non-test functions
    that are defined at the top level (not nested inside other functions).
    """
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
            # Skip private, dunder, and test functions
            if node.name.startswith("_") or node.name.startswith("test"):
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


def is_imported_elsewhere(
    func_name: str, def_file: str, all_trees: dict[Path, ast.Module]
) -> bool:
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
