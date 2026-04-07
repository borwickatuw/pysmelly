"""Shared AST helpers used by multiple checks."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext


def build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Build a child→parent mapping for an AST."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


_EXCEPTION_BASES = frozenset(
    {
        "Exception",
        "BaseException",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "KeyError",
        "AttributeError",
        "IOError",
        "OSError",
        "LookupError",
        "IndexError",
        "NotImplementedError",
        "StopIteration",
        "ArithmeticError",
        "PermissionError",
        "FileNotFoundError",
        "ConnectionError",
        "TimeoutError",
    }
)


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
                func_defs[node.name].append(
                    {"file": str(filepath), "line": node.lineno, "node": node}
                )
    return func_defs


def build_call_index(all_trees: dict[Path, ast.Module]) -> dict[str, list[dict]]:
    """Build map of all function call sites across the codebase in a single pass.

    Returns {func_name: [{file, line, node}, ...]} for every called name.
    """
    calls: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                calls[node.func.id].append(
                    {"file": str(filepath), "line": node.lineno, "node": node}
                )
            elif isinstance(node.func, ast.Attribute):
                calls[node.func.attr].append(
                    {"file": str(filepath), "line": node.lineno, "node": node}
                )
    return calls


class ReferenceIndices(NamedTuple):
    """Indices built in a single pass over all AST trees."""

    import_index: dict[str, set[str]]
    value_references: set[str]
    dotted_string_suffixes: set[str]
    decorator_names: set[str]


def build_reference_indices(all_trees: dict[Path, ast.Module]) -> ReferenceIndices:
    """Single-pass builder for import, value-reference, dotted-string, and decorator indices."""
    import_index: dict[str, set[str]] = defaultdict(set)
    value_references: set[str] = set()
    dotted_string_suffixes: set[str] = set()
    decorator_names: set[str] = set()

    for filepath, tree in all_trees.items():
        file_str = str(filepath)
        for node in ast.walk(tree):
            # Import index
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    import_index[alias.name].add(file_str)

            # Value references (dict values, list/tuple elements, call args)
            elif isinstance(node, ast.Dict):
                for val in node.values:
                    if val is not None:
                        if isinstance(val, ast.Name):
                            value_references.add(val.id)
                        elif isinstance(val, ast.Attribute):
                            value_references.add(val.attr)
            elif isinstance(node, (ast.List, ast.Tuple)):
                for elt in node.elts:
                    if isinstance(elt, ast.Name):
                        value_references.add(elt.id)
                    elif isinstance(elt, ast.Attribute):
                        value_references.add(elt.attr)
            elif isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Name):
                        value_references.add(arg.id)
                    elif isinstance(arg, ast.Attribute):
                        value_references.add(arg.attr)
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Name):
                        value_references.add(kw.value.id)
                    elif isinstance(kw.value, ast.Attribute):
                        value_references.add(kw.value.attr)

            # Dotted string suffixes
            elif (
                isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value
            ):
                # Extract the final component after the last dot
                suffix = node.value.rsplit(".", 1)[-1]
                if suffix:
                    dotted_string_suffixes.add(suffix)

            # Decorator names
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Name):
                        decorator_names.add(deco.id)
                    elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
                        decorator_names.add(deco.func.id)
                    elif isinstance(deco, ast.Attribute):
                        decorator_names.add(deco.attr)
                    elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                        decorator_names.add(deco.func.attr)

    return ReferenceIndices(
        import_index=dict(import_index),
        value_references=value_references,
        dotted_string_suffixes=dotted_string_suffixes,
        decorator_names=decorator_names,
    )


def is_imported_elsewhere(func_name: str, def_file: str, ctx: "AnalysisContext") -> bool:
    """Check if a function is imported in any other file (O(1) via cached index)."""
    importing_files = ctx.import_index.get(func_name, set())
    return bool(importing_files - {def_file})


def is_referenced_as_dotted_string(func_name: str, ctx: "AnalysisContext") -> bool:
    """Check if a function name appears as the final component of a dotted-path string."""
    return func_name in ctx.dotted_string_suffixes


def is_used_as_decorator(func_name: str, ctx: "AnalysisContext") -> bool:
    """Check if a function name is used as a decorator anywhere."""
    return func_name in ctx.decorator_names


def _is_name_or_attr(node: ast.expr, name: str) -> bool:
    """Check if a node is a reference to ``name`` (bare or via attribute access).

    Matches ``func_name`` (ast.Name) and ``obj.func_name`` (ast.Attribute),
    e.g. ``views.home`` in Django URL confs.
    """
    if isinstance(node, ast.Name) and node.id == name:
        return True
    if isinstance(node, ast.Attribute) and node.attr == name:
        return True
    return False


def is_referenced_as_value(func_name: str, ctx: "AnalysisContext") -> bool:
    """Check if a function name appears as a dict value, list element, or argument.

    O(1) via cached index.
    """
    return func_name in ctx.value_references


def is_test_file(filepath: Path) -> bool:
    """Check if a file path looks like a test file."""
    name = filepath.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    return any(part in {"tests", "test"} for part in filepath.parts)


def is_in_dunder_all(name: str, tree: ast.Module) -> bool:
    """Check if a name is listed in the module's ``__all__``."""
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "__all__"):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and elt.value == name:
                        return True
    return False


def _is_exception_base(name: str) -> bool:
    """Check if a class name looks like an exception base class."""
    if name in _EXCEPTION_BASES:
        return True
    return name.endswith("Error") or name.endswith("Exception")


def build_exception_index(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[dict]]:
    """Build map of custom exception class definitions across the codebase.

    Returns classes whose bases include known exception types or names
    ending in Error/Exception.
    """
    exc_defs: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.bases:
                continue
            is_exc = False
            for base in node.bases:
                if isinstance(base, ast.Name) and _is_exception_base(base.id):
                    is_exc = True
                    break
                if isinstance(base, ast.Attribute) and _is_exception_base(base.attr):
                    is_exc = True
                    break
            if is_exc:
                exc_defs[node.name].append({"file": str(filepath), "line": node.lineno})
    return exc_defs


def is_raised_anywhere(exc_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if an exception class is raised anywhere."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            if node.exc is None:
                continue
            # raise ExcName(...) or raise ExcName
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name) and exc.id == exc_name:
                return True
            if isinstance(exc, ast.Attribute) and exc.attr == exc_name:
                return True
    return False


def is_caught_anywhere(exc_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if an exception class is caught in any except handler."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                continue
            # except ExcName:
            if isinstance(node.type, ast.Name) and node.type.id == exc_name:
                return True
            if isinstance(node.type, ast.Attribute) and node.type.attr == exc_name:
                return True
            # except (ExcA, ExcB):
            if isinstance(node.type, ast.Tuple):
                for elt in node.type.elts:
                    if isinstance(elt, ast.Name) and elt.id == exc_name:
                        return True
                    if isinstance(elt, ast.Attribute) and elt.attr == exc_name:
                        return True
    return False


def is_subclassed(class_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a class is used as a base class anywhere."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == class_name:
                    # Don't count the class itself
                    if node.name != class_name:
                        return True
                if isinstance(base, ast.Attribute) and base.attr == class_name:
                    return True
    return False


def is_isinstance_target(class_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a class is used as a target of isinstance() or issubclass()."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name) and node.func.id in {"isinstance", "issubclass"}
            ):
                continue
            if len(node.args) < 2:
                continue
            target = node.args[1]
            if isinstance(target, ast.Name) and target.id == class_name:
                return True
            if isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name) and elt.id == class_name:
                        return True
    return False


def build_test_function_index(
    all_trees: dict[Path, ast.Module],
) -> list[dict]:
    """Index non-test functions in test files, noting fixture status.

    Returns a list of dicts with keys: name, file, line, is_fixture.
    Skips test_ functions, methods, and decorated functions (except @pytest.fixture).
    """
    results: list[dict] = []
    for filepath, tree in all_trees.items():
        if not is_test_file(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Skip test functions — pytest runs them
            if node.name.startswith("test"):
                continue
            # Check for pytest.fixture decorator
            fixture = False
            other_decorator = False
            for deco in node.decorator_list:
                if _is_pytest_fixture(deco):
                    fixture = True
                else:
                    other_decorator = True
            # Skip decorated non-fixture functions (framework-registered)
            if other_decorator and not fixture:
                continue
            results.append(
                {
                    "name": node.name,
                    "file": str(filepath),
                    "line": node.lineno,
                    "is_fixture": fixture,
                }
            )
    return results


def _is_pytest_fixture(deco: ast.expr) -> bool:
    """Check if a decorator is @pytest.fixture or @pytest.fixture(...)."""
    # @pytest.fixture
    if (
        isinstance(deco, ast.Attribute)
        and deco.attr == "fixture"
        and isinstance(deco.value, ast.Name)
        and deco.value.id == "pytest"
    ):
        return True
    # @pytest.fixture(...)
    if (
        isinstance(deco, ast.Call)
        and isinstance(deco.func, ast.Attribute)
        and deco.func.attr == "fixture"
        and isinstance(deco.func.value, ast.Name)
        and deco.func.value.id == "pytest"
    ):
        return True
    return False
