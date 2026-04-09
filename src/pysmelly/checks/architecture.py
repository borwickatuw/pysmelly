"""Architectural checks — higher-level cross-file patterns."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.framework import FRAMEWORK_HOOK_METHODS, FRAMEWORK_PARAM_NAMES
from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
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
        if isinstance(node.func, ast.Name) and node.func.id in {
            "set",
            "dict",
            "list",
            "defaultdict",
            "OrderedDict",
        }:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "defaultdict",
            "OrderedDict",
        }:
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
        source = parent / f"{module.replace('.', '/')}.py" if module else parent / "__init__.py"
    else:
        # Absolute import
        source = Path(f"{module.replace('.', '/')}.py")

    # Find matching file in all_trees
    for filepath, tree in all_trees.items():
        if filepath == source or str(filepath).endswith(str(source)):
            # Collect top-level assignment names
            names = set()
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
            return filepath, names

    return None, set()


def _resolve_accessible_names(
    filepath: Path,
    tree: ast.Module,
    all_trees: dict[Path, ast.Module],
    mutable_vars: dict[str, list[tuple[Path, int]]],
) -> tuple[dict[str, Path], dict[str, Path], dict[str, str]]:
    """Resolve which mutable var names from other files are accessible here.

    Returns (imported_names, star_imported_names, imported_modules).
    """
    imported_names: dict[str, Path] = {}
    star_imported_names: dict[str, Path] = {}
    imported_modules: dict[str, str] = {}

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
                        for def_path, _ in mutable_vars[alias.name]:
                            if def_path != filepath:
                                imported_names[actual_name] = def_path
                                break
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    imported_modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    return imported_names, star_imported_names, imported_modules


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

        imported_names, star_imported_names, imported_modules = _resolve_accessible_names(
            filepath, tree, all_trees, mutable_vars
        )
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
                            if var_name in imported_names or var_name in star_imported_names:
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
                        if var_name in imported_names or var_name in star_imported_names:
                            mutations[var_name].append((filepath, stmt.lineno, "__setitem__"))

            # Pattern 4: VAR += [...] (augmented assignment)
            if isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id in accessible:
                    var_name = stmt.target.id
                    if var_name in imported_names or var_name in star_imported_names:
                        mutations[var_name].append((filepath, stmt.lineno, "__iadd__"))

    return mutations


@check(
    "shared-mutable-module-state",
    severity=Severity.MEDIUM,
    description="Module-level mutable variables mutated from other files at import time",
)
def check_shared_mutable_module_state(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level mutable variables mutated from other files at module scope."""
    findings = []

    mutable_vars = _collect_mutable_module_vars(ctx.all_trees)
    mutations = _collect_mutations(ctx.all_trees, mutable_vars)

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


# --- write-only-attributes helpers ---


def _collect_exported_names(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect all names listed in __all__ assignments across the codebase."""
    names: set[str] = set()
    for tree in all_trees.values():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                names.add(elt.value)
    return names


def _has_dataclass_decorator(node: ast.ClassDef) -> bool:
    """Check if a class has @dataclass or @dataclasses.dataclass decorator."""
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "dataclass":
            return True
        if (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Name)
            and deco.func.id == "dataclass"
        ):
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "dataclass":
            return True
        if (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "dataclass"
        ):
            return True
    return False


def _collect_dataclass_fields(
    all_trees: dict[Path, ast.Module],
) -> list[dict]:
    """Find @dataclass classes and their annotated fields."""
    fields: list[dict] = []
    for filepath, tree in all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _has_dataclass_decorator(node):
                continue
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id
                    if field_name.startswith("_"):
                        continue
                    fields.append(
                        {
                            "class_name": node.name,
                            "field_name": field_name,
                            "file": str(filepath),
                            "line": item.lineno,
                        }
                    )
    return fields


def _collect_all_attr_reads(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect all attribute names read (Load context) across the codebase."""
    reads: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                reads.add(node.attr)
    return reads


@check(
    "write-only-attributes",
    severity=Severity.MEDIUM,
    description="Dataclass fields that are never read anywhere in the codebase",
)
def check_write_only_attributes(ctx: AnalysisContext) -> list[Finding]:
    """Find @dataclass fields with no attribute reads across the entire codebase.

    Config classes accumulate fields as features iterate: each round adds
    parameters, but removal doesn't clean them up. Fields like
    async_max_connections or cache_compression persist long after the
    feature they configured was changed or dropped.

    Classes listed in __all__ are considered public API — their fields
    may be read by downstream consumers outside this codebase.
    """
    findings = []

    dc_fields = _collect_dataclass_fields(ctx.all_trees)
    if not dc_fields:
        return findings

    all_reads = _collect_all_attr_reads(ctx.all_trees)
    exported = _collect_exported_names(ctx.all_trees)

    for field in dc_fields:
        if field["class_name"] in exported:
            continue
        if field["field_name"] not in all_reads:
            findings.append(
                Finding(
                    file=field["file"],
                    line=field["line"],
                    check="write-only-attributes",
                    message=(
                        f"{field['class_name']}.{field['field_name']} is never "
                        f"read anywhere in the codebase — vestigial field?"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- temporal-coupling helpers ---


def _is_staticmethod_or_classmethod(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Check if a method has @staticmethod or @classmethod decorator."""
    for deco in method.decorator_list:
        if isinstance(deco, ast.Name) and deco.id in {"staticmethod", "classmethod"}:
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in {
            "staticmethod",
            "classmethod",
        }:
            return True
    return False


def _is_property(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a method has @property decorator."""
    for deco in method.decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "property":
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "property":
            return True
    return False


def _collect_self_attr_ops(
    class_node: ast.ClassDef,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Collect self.attr assignments and reads per method.

    Returns (writes, reads) where each is {method_name: {attr_name, ...}}.
    """
    writes: dict[str, set[str]] = defaultdict(set)
    reads: dict[str, set[str]] = defaultdict(set)

    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_staticmethod_or_classmethod(item):
            continue
        if _is_property(item):
            continue

        method_name = item.name
        for node in ast.walk(item):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            if node.value.id != "self":
                continue
            if isinstance(node.ctx, ast.Store):
                writes[method_name].add(node.attr)
            elif isinstance(node.ctx, ast.Load):
                reads[method_name].add(node.attr)

    return writes, reads


_TEST_CASE_BASES = frozenset(
    {"TestCase", "TransactionTestCase", "SimpleTestCase", "LiveServerTestCase"}
)


def _is_test_case_class(node: ast.ClassDef) -> bool:
    """Check if a class inherits from TestCase or similar test base classes."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _TEST_CASE_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _TEST_CASE_BASES:
            return True
    return False


@check(
    "temporal-coupling",
    severity=Severity.MEDIUM,
    description="Methods reading self.x only set by another non-__init__ method",
)
def check_temporal_coupling(ctx: AnalysisContext) -> list[Finding]:
    """Find attributes that create temporal coupling between methods."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if _has_dataclass_decorator(node):
                continue

            # Need at least 3 methods
            methods = [
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not _is_staticmethod_or_classmethod(item)
                and not _is_property(item)
            ]
            if len(methods) < 3:
                continue

            writes, reads = _collect_self_attr_ops(node)
            init_writes = writes.get("__init__", set())

            # TestCase subclasses: setUp/setUpClass are framework-guaranteed
            # initialization — treat like __init__
            if _is_test_case_class(node):
                init_writes |= writes.get("setUp", set())
                init_writes |= writes.get("setUpClass", set())

            for method_name, method_reads in reads.items():
                for attr in method_reads:
                    # Skip private attributes
                    if attr.startswith("_"):
                        continue
                    # Skip if set in __init__
                    if attr in init_writes:
                        continue
                    # Skip if set in same method
                    if attr in writes.get(method_name, set()):
                        continue

                    # Find which method(s) set this attr
                    setters = [
                        m
                        for m, w in writes.items()
                        if attr in w and m not in {"__init__", method_name}
                    ]
                    if setters:
                        setter_str = ", ".join(sorted(setters))
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=node.lineno,
                                check="temporal-coupling",
                                message=(
                                    f"{node.name}.{method_name}() reads self.{attr}"
                                    f" only set by {setter_str}() (not __init__)"
                                    f" — temporal coupling: {setter_str}() must be"
                                    f" called first"
                                ),
                                severity=Severity.MEDIUM,
                            )
                        )

    return findings


# --- feature-envy ---


def _is_dunder(name: str) -> bool:
    """Check if a name is a dunder method."""
    return name.startswith("__") and name.endswith("__")


@check(
    "feature-envy",
    severity=Severity.MEDIUM,
    description="Methods accessing 3+ attrs of another param, more than self",
)
def check_feature_envy(ctx: AnalysisContext) -> list[Finding]:
    """Find methods that use another object's attributes more than self."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            for item in class_node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _is_dunder(item.name):
                    continue
                if _is_staticmethod_or_classmethod(item):
                    continue

                # Skip known framework hooks where the signature is
                # dictated and accessing params more than self is expected
                if item.name in FRAMEWORK_HOOK_METHODS:
                    continue

                # Get parameter names (excluding self/cls and framework
                # objects that methods inherently operate on)
                param_names: set[str] = set()
                for arg in item.args.args:
                    if arg.arg in {"self", "cls"} | FRAMEWORK_PARAM_NAMES:
                        continue
                    param_names.add(arg.arg)

                if not param_names:
                    continue

                # Count attribute accesses per target
                attr_counts: dict[str, int] = defaultdict(int)  # target -> count
                for node in ast.walk(item):
                    if not isinstance(node, ast.Attribute):
                        continue
                    if not isinstance(node.ctx, ast.Load):
                        continue
                    if not isinstance(node.value, ast.Name):
                        continue
                    name = node.value.id
                    if name == "self" or name in param_names:
                        attr_counts[name] = attr_counts.get(name, 0) + 1

                self_count = attr_counts.get("self", 0)

                for param in param_names:
                    param_count = attr_counts.get(param, 0)
                    if param_count >= 3 and param_count > self_count:
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=item.lineno,
                                check="feature-envy",
                                message=(
                                    f"{class_node.name}.{item.name}() accesses"
                                    f" {param_count} attributes of '{param}' but"
                                    f" only {self_count} of 'self'"
                                    f" — consider moving this logic to"
                                    f" {param}'s class"
                                ),
                                severity=Severity.MEDIUM,
                            )
                        )

    return findings


# --- anemic-domain ---

_DATA_CLASS_BASES = frozenset({"BaseModel", "NamedTuple", "TypedDict"})

_DATA_CLASS_DECORATORS = frozenset({"dataclass", "attrs", "define", "attr.s", "attr.attrs"})


def _is_data_class_like(node: ast.ClassDef) -> bool:
    """Check if a class is a dataclass, NamedTuple, TypedDict, Pydantic BaseModel, or attrs."""
    if _has_dataclass_decorator(node):
        return True
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id in _DATA_CLASS_DECORATORS:
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in _DATA_CLASS_DECORATORS:
            return True
        if isinstance(deco, ast.Call):
            func = deco.func
            if isinstance(func, ast.Name) and func.id in _DATA_CLASS_DECORATORS:
                return True
            if isinstance(func, ast.Attribute) and func.attr in _DATA_CLASS_DECORATORS:
                return True
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _DATA_CLASS_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _DATA_CLASS_BASES:
            return True
    return False


def _count_init_attrs(class_node: ast.ClassDef) -> set[str]:
    """Get the set of attribute names assigned in __init__."""
    attrs: set[str] = set()
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name != "__init__":
            continue
        for node in ast.walk(item):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and isinstance(node.ctx, ast.Store)
            ):
                attrs.add(node.attr)
    return attrs


def _has_non_dunder_methods(class_node: ast.ClassDef) -> bool:
    """Check if a class has any non-dunder instance methods."""
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_dunder(item.name):
            continue
        if _is_staticmethod_or_classmethod(item):
            continue
        return True
    return False


def _base_has_methods(class_node: ast.ClassDef, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if any base class (within analyzed files) has non-dunder methods."""
    base_names: set[str] = set()
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            base_names.add(base.id)
        elif isinstance(base, ast.Attribute):
            base_names.add(base.attr)

    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in base_names:
                if _has_non_dunder_methods(node):
                    return True
    return False


@check(
    "anemic-domain",
    severity=Severity.MEDIUM,
    description="Classes with 5+ __init__ attrs but zero non-dunder methods",
)
def check_anemic_domain(ctx: AnalysisContext) -> list[Finding]:
    """Find classes that are data bags with no behavior."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if _is_data_class_like(node):
                continue

            init_attrs = _count_init_attrs(node)
            if len(init_attrs) < 5:
                continue

            if _has_non_dunder_methods(node):
                continue

            if _base_has_methods(node, ctx.all_trees):
                continue

            # Cross-file feature-envy evidence
            envy_files: set[str] = set()
            attr_names = init_attrs
            for other_path, other_tree in ctx.all_trees.items():
                if other_path == filepath:
                    continue
                if is_test_file(other_path):
                    continue
                # Count how many of this class's attrs are accessed
                accessed: set[str] = set()
                for n in ast.walk(other_tree):
                    if (
                        isinstance(n, ast.Attribute)
                        and isinstance(n.ctx, ast.Load)
                        and n.attr in attr_names
                    ):
                        accessed.add(n.attr)
                if len(accessed) >= 3:
                    envy_files.add(str(other_path))

            if envy_files:
                msg = (
                    f"{node.name} has {len(init_attrs)} attributes but no"
                    f" behavior — external functions in {len(envy_files)}"
                    f" file{'s' if len(envy_files) != 1 else ''} access 3+"
                    f" attributes — move behavior into the class"
                )
            else:
                msg = (
                    f"{node.name} has {len(init_attrs)} attributes but no"
                    f" behavior methods — consider adding methods or"
                    f" converting to a dataclass"
                )

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="anemic-domain",
                    message=msg,
                    severity=Severity.MEDIUM,
                )
            )

    return findings
