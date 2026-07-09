"""Architectural checks — higher-level cross-file patterns."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.framework import FRAMEWORK_HOOK_METHODS, FRAMEWORK_PARAM_NAMES
from pysmelly.checks.helpers import has_dataclass_decorator, is_test_file
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
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.add(elt.value)
    return names


_SERIALIZE_ALL_FIELDS = frozenset({"asdict", "astuple", "vars"})


def _class_serializes_self(node: ast.ClassDef) -> bool:
    """True when the class calls asdict/astuple/vars on self — every
    field is then read via serialization, not attribute access."""
    for child in ast.walk(node):
        if not (isinstance(child, ast.Call) and child.args):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
        else:
            continue
        if func_name not in _SERIALIZE_ALL_FIELDS:
            continue
        arg = child.args[0]
        if isinstance(arg, ast.Name) and arg.id == "self":
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
            if not has_dataclass_decorator(node):
                continue
            if _class_serializes_self(node):
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

    Classes listed in __all__ are public API — their fields may be read
    by downstream consumers outside this codebase, so those findings are
    downgraded to LOW rather than suppressed: a field nothing in the
    defining repo reads is still worth an investigation, especially when
    a whole config class accretes them.
    """
    findings = []

    dc_fields = _collect_dataclass_fields(ctx.all_trees)
    if not dc_fields:
        return findings

    all_reads = _collect_all_attr_reads(ctx.all_trees)
    exported = _collect_exported_names(ctx.all_trees)

    for field in dc_fields:
        if field["field_name"] not in all_reads:
            is_exported = field["class_name"] in exported
            export_note = (
                f" ({field['class_name']} is in __all__ — external readers possible)"
                if is_exported
                else ""
            )
            findings.append(
                Finding(
                    file=field["file"],
                    line=field["line"],
                    check="write-only-attributes",
                    message=(
                        f"{field['class_name']}.{field['field_name']} is never "
                        f"read anywhere in the codebase — vestigial field?"
                        f"{export_note}"
                    ),
                    severity=Severity.LOW if is_exported else Severity.MEDIUM,
                )
            )

    return findings


# --- write-only-globals ---

# Method calls that mutate a container without yielding a value worth
# reading. pop/setdefault return values, so they count as reads.
_CONTAINER_MUTATORS = frozenset(
    {"append", "extend", "insert", "add", "update", "clear", "remove", "discard"}
)


@check(
    "write-only-globals",
    severity=Severity.MEDIUM,
    description="Module-level containers that are mutated but never read",
)
def check_write_only_globals(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level mutable containers nothing ever reads.

    An events list that functions append to (and maybe clear) but no
    code iterates, returns, or inspects is dead bookkeeping — it costs
    memory forever and misleads readers into thinking something consumes
    it. The module analog of write-only-attributes.
    """
    findings = []

    exported = _collect_exported_names(ctx.all_trees)

    # Candidate containers per (file, name), skipping test files
    candidates: dict[str, tuple[Path, int]] = {}
    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not _is_mutable_value(node.value):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and not _is_dunder(target.id)
                    and target.id not in exported
                ):
                    candidates[target.id] = (filepath, node.lineno)

    if not candidates:
        return findings

    # Scan the entire codebase (tests included — a test reading the
    # container is still a read) classifying every use by name.
    func_mutations: dict[str, int] = defaultdict(int)
    reads: set[str] = set()
    for filepath, tree in ctx.all_trees.items():
        parent_map = ctx.parent_map(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Name) and node.id in candidates):
                continue
            if isinstance(node.ctx, ast.Store):
                continue  # (re)assignment — neither read nor container op
            parent = parent_map.get(node)
            # X.append(...) — mutating method call
            is_mutation = (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr in _CONTAINER_MUTATORS
                and isinstance(parent_map.get(parent), ast.Call)
            )
            # X[k] = v / del X[k] — subscript store/delete
            is_mutation = is_mutation or (
                isinstance(parent, ast.Subscript)
                and parent.value is node
                and isinstance(parent.ctx, (ast.Store, ast.Del))
            )
            if not is_mutation:
                reads.add(node.id)
                continue
            # Only function-scope mutations count: module-scope population
            # of a never-read container is dead-constants territory.
            current = parent_map.get(node)
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_mutations[node.id] += 1
                    break
                current = parent_map.get(current)

    for name, count in sorted(func_mutations.items()):
        if name in reads or count < 1:
            continue
        filepath, line = candidates[name]
        findings.append(
            Finding(
                file=str(filepath),
                line=line,
                check="write-only-globals",
                message=(
                    f"{name} is mutated in {count} place(s) but never read"
                    f" anywhere in the codebase — dead bookkeeping;"
                    f" delete it or add the missing consumer"
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


def _collect_init_none_attrs(class_node: ast.ClassDef) -> set[str]:
    """Attributes assigned exactly None in __init__."""
    none_attrs: set[str] = set()
    for item in class_node.body:
        if not (isinstance(item, ast.FunctionDef) and item.name == "__init__"):
            continue
        for node in ast.walk(item):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value is None
            ):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        none_attrs.add(target.attr)
    return none_attrs


def _method_guards_attr(method: ast.FunctionDef | ast.AsyncFunctionDef, attr: str) -> bool:
    """True when the method tests self.attr before using it — a None
    comparison, or self.attr appearing bare (possibly negated) in an
    if/while/assert test."""

    def is_self_attr(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    for node in ast.walk(method):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(is_self_attr(o) for o in operands) and any(
                isinstance(o, ast.Constant) and o.value is None for o in operands
            ):
                return True
        elif isinstance(node, (ast.If, ast.While, ast.Assert)):
            test = node.test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                test = test.operand
            if is_self_attr(test):
                return True
            if isinstance(test, ast.BoolOp) and any(
                is_self_attr(v)
                or (
                    isinstance(v, ast.UnaryOp)
                    and isinstance(v.op, ast.Not)
                    and is_self_attr(v.operand)
                )
                for v in test.values
            ):
                return True
    return False


def _is_dereference(node: ast.AST, parent_map: dict) -> bool:
    """True when the node's value is immediately used — attribute access,
    subscript, or call — so a None value would raise."""
    parent = parent_map.get(node)
    if isinstance(parent, ast.Attribute) and parent.value is node:
        return True
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return True
    return isinstance(parent, ast.Call) and parent.func is node


def _none_init_deref_findings(
    filepath: Path,
    class_node: ast.ClassDef,
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    writes: dict[str, set[str]],
    parent_map: dict,
) -> list[Finding]:
    """None-initialized attribute dereferenced unguarded in a method that
    never sets it, while another method sets the real value.

    ``self._x = None`` in __init__ passes the "set in __init__" test, but
    a method doing ``self._x.items`` before the setter runs crashes —
    the None assignment is a placeholder, not initialization. Private
    attrs are NOT skipped here: the crash risk is identical and the
    dereference evidence is strong.
    """
    findings = []
    none_attrs = _collect_init_none_attrs(class_node)
    if not none_attrs:
        return findings

    reported: set[tuple[str, str]] = set()
    for method in methods:
        if method.name == "__init__":
            continue
        method_writes = writes.get(method.name, set())
        for node in ast.walk(method):
            if not (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in none_attrs
            ):
                continue
            attr = node.attr
            if (method.name, attr) in reported:
                continue
            if attr in method_writes:
                continue
            if not _is_dereference(node, parent_map):
                continue
            if _method_guards_attr(method, attr):
                continue
            setters = sorted(
                m for m, w in writes.items() if attr in w and m not in {"__init__", method.name}
            )
            if not setters:
                continue
            reported.add((method.name, attr))
            setter_str = ", ".join(f"{s}()" for s in setters)
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="temporal-coupling",
                    message=(
                        f"{class_node.name}.{method.name}() dereferences"
                        f" self.{attr}, which __init__ only sets to None —"
                        f" crashes unless {setter_str} runs first"
                    ),
                    severity=Severity.MEDIUM,
                )
            )
    return findings


def _func_none_guarded_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef, candidates: set[str]
) -> set[str]:
    """Candidate names the function tests before use (None comparison or
    bare truthiness in an if/while/assert test)."""
    guarded: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            names = {o.id for o in operands if isinstance(o, ast.Name)}
            if names & candidates and any(
                isinstance(o, ast.Constant) and o.value is None for o in operands
            ):
                guarded |= names & candidates
        elif isinstance(node, (ast.If, ast.While, ast.Assert)):
            test = node.test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                test = test.operand
            if isinstance(test, ast.Name) and test.id in candidates:
                guarded.add(test.id)
    return guarded


def _module_temporal_coupling_findings(filepath: Path, tree: ast.Module) -> list[Finding]:
    """Module analog of temporal coupling: a None-initialized module
    global reassigned via ``global`` in some function and read unguarded
    in others — behavior depends on which function ran first."""
    none_globals: dict[str, int] = {}
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
        ):
            none_globals[node.targets[0].id] = node.lineno
    if not none_globals:
        return []

    candidates = set(none_globals)
    assigners: dict[str, list[str]] = defaultdict(list)
    readers: dict[str, list[str]] = defaultdict(list)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Global):
                declared.update(node.names)
        assigned: set[str] = set()
        read: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and node.id in candidates:
                if isinstance(node.ctx, ast.Store) and node.id in declared:
                    assigned.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    read.add(node.id)
        guarded = _func_none_guarded_names(func, candidates)
        for name in assigned:
            assigners[name].append(func.name)
        for name in read - assigned - guarded:
            readers[name].append(func.name)

    findings = []
    for name, line in sorted(none_globals.items()):
        if not assigners.get(name) or not readers.get(name):
            continue
        assigner_str = ", ".join(f"{m}()" for m in sorted(set(assigners[name]))[:3])
        reader_list = sorted(set(readers[name]))
        reader_str = ", ".join(f"{m}()" for m in reader_list[:4])
        if len(reader_list) > 4:
            reader_str += ", ..."
        findings.append(
            Finding(
                file=str(filepath),
                line=line,
                check="temporal-coupling",
                message=(
                    f"module global {name} (initialized None) is set via"
                    f" `global` in {assigner_str} and read unguarded in"
                    f" {reader_str} — behavior depends on call order;"
                    f" pass the value explicitly or encapsulate it"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings


@check(
    "temporal-coupling",
    severity=Severity.MEDIUM,
    description="Methods reading self.x only set by another non-__init__ method",
)
def check_temporal_coupling(ctx: AnalysisContext) -> list[Finding]:
    """Find attributes that create temporal coupling between methods.

    Three variants: methods reading a self attribute only ever set by
    another non-__init__ method; methods dereferencing a None-initialized
    attribute without a guard; and the module-scope analog — a
    None-initialized module global set via ``global`` in one function
    and read unguarded in others.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        findings.extend(_module_temporal_coupling_findings(filepath, tree))

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if has_dataclass_decorator(node):
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

            findings.extend(
                _none_init_deref_findings(filepath, node, methods, writes, ctx.parent_map(tree))
            )

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
    if has_dataclass_decorator(node):
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
