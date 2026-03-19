"""Shared AST helpers used by multiple checks."""

import ast
from collections import defaultdict
from pathlib import Path


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


def is_used_as_decorator(func_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a function name is used as a decorator anywhere.

    Handles both @func_name and @func_name(...) forms,
    as well as @module.func_name variants.
    """
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for deco in node.decorator_list:
                # @func_name
                if isinstance(deco, ast.Name) and deco.id == func_name:
                    return True
                # @func_name(...)
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Name)
                    and deco.func.id == func_name
                ):
                    return True
                # @module.func_name
                if isinstance(deco, ast.Attribute) and deco.attr == func_name:
                    return True
                # @module.func_name(...)
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr == func_name
                ):
                    return True
    return False


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


def is_referenced_as_value(func_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a function name appears as a dict value, list element, or argument."""
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for val in node.values:
                    if val is not None and _is_name_or_attr(val, func_name):
                        return True
            if isinstance(node, (ast.List, ast.Tuple)):
                for elt in node.elts:
                    if _is_name_or_attr(elt, func_name):
                        return True
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if _is_name_or_attr(arg, func_name):
                        return True
                for kw in node.keywords:
                    if _is_name_or_attr(kw.value, func_name):
                        return True
    return False


def is_test_file(filepath: Path) -> bool:
    """Check if a file path looks like a test file."""
    name = filepath.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    for part in filepath.parts:
        if part in ("tests", "test"):
            return True
    return False


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
                isinstance(node.func, ast.Name) and node.func.id in ("isinstance", "issubclass")
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
