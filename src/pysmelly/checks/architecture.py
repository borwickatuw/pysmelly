"""Architectural checks — higher-level cross-file patterns."""

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.helpers import is_test_file
from pysmelly.registry import Finding, Severity, check

# Methods that mutate containers
_MUTATION_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "update",
        "add",
        "setdefault",
        "pop",
        "remove",
        "clear",
        "discard",
    }
)

# Registry methods — intentional patterns, not a smell
_REGISTRY_METHODS = frozenset({"register", "register_type", "add_handler", "connect"})


def _iter_module_scope(tree: ast.Module):
    """Yield all statements at module scope, including inside if/for/while/with/try.

    Stops at function and class boundaries — those are runtime, not import-time.
    """
    worklist = list(tree.body)
    while worklist:
        node = worklist.pop()
        yield node
        # Don't descend into function/class bodies
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Descend into control flow (runs at import time)
        for attr in ("body", "orelse", "finalbody"):
            children = getattr(node, attr, None)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, ast.stmt):
                        worklist.append(child)
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                for child in handler.body:
                    if isinstance(child, ast.stmt):
                        worklist.append(child)


def _collect_mutable_module_vars(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[tuple[Path, int]]]:
    """Find module-level variables assigned to mutable containers.

    Returns {var_name: [(file, line), ...]}.
    """
    mutables: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not _is_mutable_value(node.value):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mutables[target.id].append((filepath, node.lineno))
    return mutables


def _is_mutable_value(node: ast.expr) -> bool:
    """Check if an expression creates a mutable container."""
    # [] or [...]
    if isinstance(node, ast.List):
        return True
    # {} or {...}
    if isinstance(node, ast.Dict):
        return True
    # set() or {1, 2, 3} (Set literal)
    if isinstance(node, ast.Set):
        return True
    # set(), defaultdict(), OrderedDict(), etc.
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in (
            "set",
            "dict",
            "list",
            "defaultdict",
            "OrderedDict",
        ):
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "defaultdict",
            "OrderedDict",
        ):
            return True
    return False


def _resolve_star_import_names(
    import_node: ast.ImportFrom,
    importing_file: Path,
    all_trees: dict[Path, ast.Module],
) -> tuple[Path | None, set[str]]:
    """Resolve `from X import *` to the set of top-level names defined in X.

    Returns (source_path, {name1, name2, ...}) or (None, set()).
    """
    module = import_node.module or ""
    level = import_node.level

    # Resolve relative import
    if level > 0:
        # Go up 'level' directories from importing file
        parent = importing_file.parent
        for _ in range(level - 1):
            parent = parent.parent
        if module:
            source = parent / f"{module.replace('.', '/')}.py"
        else:
            source = parent / "__init__.py"
    else:
        # Absolute import
        source = Path(f"{module.replace('.', '/')}.py")

    # Find matching file in all_trees
    for filepath in all_trees:
        if filepath == source or str(filepath).endswith(str(source)):
            # Collect top-level assignment names
            names = set()
            tree = all_trees[filepath]
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    names.add(node.name)
            return filepath, names

    return None, set()


def _collect_mutations(
    all_trees: dict[Path, ast.Module],
    mutable_vars: dict[str, list[tuple[Path, int]]],
) -> dict[str, list[tuple[Path, int, str]]]:
    """Find module-scope mutations of mutable vars from other files.

    Returns {var_name: [(mutating_file, line, method), ...]}.
    """
    mutations: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)

    for filepath, tree in all_trees.items():
        if is_test_file(filepath):
            continue

        # What names from other files are accessible here?
        # Track direct imports: from X import VAR
        imported_names: dict[str, Path] = {}  # name -> source file
        star_imported_names: dict[str, Path] = {}  # name -> source file

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    source_path, names = _resolve_star_import_names(node, filepath, all_trees)
                    if source_path:
                        for name in names:
                            if name in mutable_vars:
                                star_imported_names[name] = source_path
                else:
                    for alias in node.names:
                        actual_name = alias.asname or alias.name
                        if alias.name in mutable_vars:
                            # Find which file defines it
                            for def_path, _ in mutable_vars[alias.name]:
                                if def_path != filepath:
                                    imported_names[actual_name] = def_path
                                    break

        # Also track module imports for attribute-style mutations
        imported_modules: dict[str, str] = {}  # alias -> module_name
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name != "*":
                        # from package import module -> module is accessible
                        imported_modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        accessible = set(imported_names.keys()) | set(star_imported_names.keys())
        if not accessible and not imported_modules:
            continue

        # Walk module-scope statements looking for mutations
        for stmt in _iter_module_scope(tree):
            # Pattern 1: VAR.method(...) where VAR is imported
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                    var_name = call.func.value.id
                    method = call.func.attr
                    if var_name in accessible and method in _MUTATION_METHODS:
                        if method not in _REGISTRY_METHODS:
                            orig_name = var_name  # might be aliased
                            if var_name in imported_names:
                                mutations[orig_name].append((filepath, stmt.lineno, method))
                            elif var_name in star_imported_names:
                                mutations[orig_name].append((filepath, stmt.lineno, method))

            # Pattern 2: module.VAR.method(...) — attribute mutation via module
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Attribute) and isinstance(
                    call.func.value, ast.Attribute
                ):
                    inner = call.func.value
                    if isinstance(inner.value, ast.Name):
                        mod_alias = inner.value.id
                        var_attr = inner.attr
                        method = call.func.attr
                        if (
                            mod_alias in imported_modules
                            and var_attr in mutable_vars
                            and method in _MUTATION_METHODS
                            and method not in _REGISTRY_METHODS
                        ):
                            mutations[var_attr].append((filepath, stmt.lineno, method))

            # Pattern 3: VAR[key] = value (subscript assignment)
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in accessible
                    ):
                        var_name = target.value.id
                        if var_name in imported_names:
                            mutations[var_name].append((filepath, stmt.lineno, "__setitem__"))
                        elif var_name in star_imported_names:
                            mutations[var_name].append((filepath, stmt.lineno, "__setitem__"))

            # Pattern 4: VAR += [...] (augmented assignment)
            if isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id in accessible:
                    var_name = stmt.target.id
                    if var_name in imported_names:
                        mutations[var_name].append((filepath, stmt.lineno, "__iadd__"))
                    elif var_name in star_imported_names:
                        mutations[var_name].append((filepath, stmt.lineno, "__iadd__"))

    return mutations


@check(
    "shared-mutable-module-state",
    severity=Severity.MEDIUM,
    description="Module-level mutable variables mutated from other files at import time",
)
def check_shared_mutable_module_state(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find module-level mutable variables mutated from other files at module scope."""
    findings = []

    mutable_vars = _collect_mutable_module_vars(all_trees)
    mutations = _collect_mutations(all_trees, mutable_vars)

    for var_name, mutation_list in sorted(mutations.items()):
        if not mutation_list:
            continue

        # Find the definition site
        defs = mutable_vars.get(var_name, [])
        if not defs:
            continue

        # Group mutations by file
        mutation_files: dict[Path, list[tuple[int, str]]] = defaultdict(list)
        for mpath, mline, mmethod in mutation_list:
            mutation_files[mpath].append((mline, mmethod))

        # Filter: must have mutations from files other than the definition
        def_files = {d[0] for d in defs}
        external_files = {f for f in mutation_files if f not in def_files}
        if not external_files:
            continue

        # Format mutation locations
        loc_parts = [
            f"{mpath}:{mline}"
            for mpath in sorted(external_files, key=str)
            for mline, _ in mutation_files[mpath]
        ]

        # Anchor at first definition
        def_path, def_line = defs[0]

        findings.append(
            Finding(
                file=str(def_path),
                line=def_line,
                check="shared-mutable-module-state",
                message=(
                    f"{var_name} (defined in {def_path}:{def_line}) is mutated at "
                    f"module scope from {len(external_files)} other "
                    f"file{'s' if len(external_files) != 1 else ''} "
                    f"({', '.join(loc_parts)}) "
                    f"— consider consolidating or using an immutable pattern"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
