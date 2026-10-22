"""Architectural checks — higher-level cross-file patterns."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from pysmelly.checks.framework import FRAMEWORK_HOOK_METHODS, FRAMEWORK_PARAM_NAMES
from pysmelly.checks.helpers import (
    INIT_FILE,
    bare_target,
    has_dataclass_decorator,
    has_decorator_named,
    is_bare_name,
    iter_module_classes,
    iter_simple_assigns,
    source_trees,
)
from pysmelly.checks.records import RecordField, RecordIndex, RecordKind, collect_record_uses
from pysmelly.checks.test_surface import TestSurface, surface_of_sites
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

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
            for name in _assigned_names(node):
                mutables[name].append((filepath, node.lineno))
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


def _assigned_names(node: ast.AST) -> set[str]:
    """The bare names an assignment binds; empty for any other statement."""
    if not isinstance(node, ast.Assign):
        return set()
    return {target.id for target in node.targets if isinstance(target, ast.Name)}


def _toplevel_defined_names(tree: ast.Module) -> set[str]:
    """Top-level names a module binds by assignment, def or class."""
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        else:
            names |= _assigned_names(node)
    return names


def _star_import_source(import_node: ast.ImportFrom, importing_file: Path) -> Path:
    """The file path `from X import *` names, resolving relative imports."""
    module = import_node.module or ""
    if import_node.level == 0:
        return Path(f"{module.replace('.', '/')}.py")
    parent = importing_file.parent
    for _ in range(import_node.level - 1):
        parent = parent.parent
    if not module:
        return parent / INIT_FILE
    return parent / f"{module.replace('.', '/')}.py"


def _resolve_star_import_names(
    import_node: ast.ImportFrom,
    importing_file: Path,
    all_trees: dict[Path, ast.Module],
) -> tuple[Path | None, set[str]]:
    """Resolve `from X import *` to the set of top-level names defined in X.

    Returns (source_path, {name1, name2, ...}) or (None, set()).
    """
    source = _star_import_source(import_node, importing_file)
    for filepath, tree in all_trees.items():
        if filepath == source or str(filepath).endswith(str(source)):
            return filepath, _toplevel_defined_names(tree)
    return None, set()


@dataclass
class _MutationScope:
    """What one file can reach of *other* modules' mutable module-level state.

    ``mutable_vars`` is the codebase-wide registry of module-level mutable
    containers ({name: [(defining file, line), ...]}). ``imported_names`` maps
    each locally visible name to the module-level var it refers to — the two
    differ under ``from base import VAR as ALIAS``, and findings are reported
    against the defining name. ``imported_modules`` maps a module alias to its
    dotted name so that ``mod.VAR.append(...)`` resolves too.
    """

    mutable_vars: dict[str, list[tuple[Path, int]]]
    imported_names: dict[str, str] = field(default_factory=dict)
    imported_modules: dict[str, str] = field(default_factory=dict)

    def reaches_other_modules(self) -> bool:
        """Whether this file can name another module's mutable state at all."""
        return bool(self.imported_names or self.imported_modules)


def _record_star_imports(
    node: ast.ImportFrom,
    filepath: Path,
    all_trees: dict[Path, ast.Module],
    scope: _MutationScope,
) -> None:
    """Record mutable vars reaching this file through `from X import *`."""
    source_path, names = _resolve_star_import_names(node, filepath, all_trees)
    if source_path is None or source_path == filepath:
        return
    for name in names & set(scope.mutable_vars):
        scope.imported_names[name] = name


def _record_named_imports(
    node: ast.ImportFrom,
    filepath: Path,
    scope: _MutationScope,
) -> None:
    """Record mutable vars this file imports by name from another file."""
    for alias in node.names:
        defined_elsewhere = any(
            def_path != filepath for def_path, _ in scope.mutable_vars.get(alias.name, [])
        )
        if defined_elsewhere:
            scope.imported_names[alias.asname or alias.name] = alias.name


def _record_module_aliases(
    aliases: list[ast.alias],
    prefix: str,
    scope: _MutationScope,
) -> None:
    """Record local alias -> dotted module name for each imported module."""
    for alias in aliases:
        if alias.name != "*":
            scope.imported_modules[alias.asname or alias.name] = f"{prefix}{alias.name}"


def _resolve_mutation_scope(
    filepath: Path,
    tree: ast.Module,
    all_trees: dict[Path, ast.Module],
    mutable_vars: dict[str, list[tuple[Path, int]]],
) -> _MutationScope:
    """Build the scope of other modules' mutable state this file can reach."""
    scope = _MutationScope(mutable_vars=mutable_vars)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            _record_module_aliases(node.names, "", scope)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                _record_star_imports(node, filepath, all_trees, scope)
            else:
                _record_named_imports(node, filepath, scope)
            if node.module:
                _record_module_aliases(node.names, f"{node.module}.", scope)
    return scope


def _mutating_call(stmt: ast.Expr) -> tuple[ast.expr, str] | None:
    """The (receiver, method) of a container-mutating call statement."""
    if not isinstance(stmt.value, ast.Call):
        return None
    func = stmt.value.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _MUTATION_METHODS or func.attr in _REGISTRY_METHODS:
        return None
    return func.value, func.attr


def _call_stmt_mutations(stmt: ast.Expr, scope: _MutationScope) -> list[tuple[str, str]]:
    """`VAR.method(...)` on an imported name, or `mod.VAR.method(...)`."""
    call = _mutating_call(stmt)
    if call is None:
        return []
    receiver, method = call
    if isinstance(receiver, ast.Name):
        origin = scope.imported_names.get(receiver.id)
        return [(origin, method)] if origin else []
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id in scope.imported_modules
        and receiver.attr in scope.mutable_vars
    ):
        return [(receiver.attr, method)]
    return []


def _subscript_assign_mutations(stmt: ast.Assign, scope: _MutationScope) -> list[tuple[str, str]]:
    """`VAR[key] = value` on an imported name."""
    mutations = []
    for target in stmt.targets:
        if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)):
            continue
        origin = scope.imported_names.get(target.value.id)
        if origin:
            mutations.append((origin, "__setitem__"))
    return mutations


def _aug_assign_mutations(stmt: ast.AugAssign, scope: _MutationScope) -> list[tuple[str, str]]:
    """`VAR += [...]` on an imported name."""
    target = bare_target(stmt)
    if target is None:
        return []
    origin = scope.imported_names.get(target.id)
    return [(origin, "__iadd__")] if origin else []


# Module-scope statement type -> the mutations it records. A dispatch table
# rather than an isinstance ladder: a new mutation shape is a new entry.
_MUTATION_HANDLERS: dict[type[ast.stmt], Callable[[Any, _MutationScope], list[tuple[str, str]]]] = {
    ast.Expr: _call_stmt_mutations,
    ast.Assign: _subscript_assign_mutations,
    ast.AugAssign: _aug_assign_mutations,
}


def _collect_mutations(
    ctx: AnalysisContext,
    mutable_vars: dict[str, list[tuple[Path, int]]],
) -> dict[str, list[tuple[Path, int, str]]]:
    """Find module-scope mutations of mutable vars from other files.

    Returns {var_name: [(mutating_file, line, method), ...]}.
    """
    mutations: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)

    for filepath, tree in source_trees(ctx):
        scope = _resolve_mutation_scope(filepath, tree, ctx.all_trees, mutable_vars)
        if not scope.reaches_other_modules():
            continue

        for stmt in _iter_module_scope(tree):
            handler = _MUTATION_HANDLERS.get(type(stmt))
            if handler is None:
                continue
            for var_name, method in handler(stmt, scope):
                mutations[var_name].append((filepath, stmt.lineno, method))

    return mutations


@check(
    "shared-mutable-module-state",
    severity=Severity.MEDIUM,
    description="Module-level mutable variables mutated from other files at import time",
    advice=Advice(
        fix=(
            "Build the value completely in its defining module (or behind a function that"
            " returns it), and delete the mutations at the sites listed."
        ),
        dismiss=(
            "Dismiss only if the mutating files are configuration layers a framework loads"
            " in a fixed order, such as Django settings modules extending a base."
        ),
    ),
)
def check_shared_mutable_module_state(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level mutable variables mutated from other files at module scope."""
    findings = []

    mutable_vars = _collect_mutable_module_vars(ctx.all_trees)
    mutations = _collect_mutations(ctx, mutable_vars)

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

        # The mutation sites, by file
        sites = [
            Site(str(mpath), mline)
            for mpath in sorted(external_files, key=str)
            for mline, _ in mutation_files[mpath]
        ]

        # Anchor at first definition
        def_path, def_line = defs[0]

        findings.append(
            multi_site_finding(
                "shared-mutable-module-state",
                Severity.MEDIUM,
                sites,
                partial(
                    _mutated_state_message, f"{var_name} (defined in {def_path}:{def_line})", sites
                ),
                anchor=Site(str(def_path), def_line),
            )
        )

    return findings


def _mutated_state_message(subject: str, sites: list[Site], indices: tuple[int, ...]) -> str:
    """shared-mutable-module-state's message over the mutation sites at ``indices``."""
    shown = [sites[index] for index in indices]
    files = len({site.file for site in shown})
    return (
        f"{subject} is mutated at "
        f"module scope from {files} other "
        f"file{'s' if files != 1 else ''} "
        f"({', '.join(f'{site.file}:{site.line}' for site in shown)})"
    )


# --- write-only-attributes helpers ---


def _string_literals(node: ast.expr) -> set[str]:
    """The string literals of a list or tuple display; empty for anything else."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return set()
    return {
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }


def _dunder_all_value(node: ast.AST) -> ast.expr | None:
    """The right-hand side of an ``__all__ = ...`` statement, if that is what this is."""
    if not isinstance(node, ast.Assign) or "__all__" not in _assigned_names(node):
        return None
    return node.value


def _collect_exported_names(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect all names listed in __all__ assignments across the codebase."""
    names: set[str] = set()
    for tree in all_trees.values():
        for node in ast.iter_child_nodes(tree):
            value = _dunder_all_value(node)
            if value is not None:
                names |= _string_literals(value)
    return names


def _unread_field_message(record_field: RecordField, tests: TestSurface) -> str:
    """The finding's text: a TypedDict has keys, every other record fields; a field
    only tests read names the tests that read it."""
    record = record_field.record
    if record.kind is RecordKind.TYPEDDICT:
        subject, noun = f'{record.name}["{record_field.name}"]', "TypedDict key"
    else:
        subject = f"{record.name}.{record_field.name}"
        noun = "field" if record.kind is RecordKind.DATACLASS else f"{record.kind.value} field"
    if not tests:
        return f"{subject} is never read anywhere in the codebase — vestigial {noun}"
    return (
        f"{subject} is never read in production code; only tests read it, in "
        f"{tests.listing()} — test-only surface, vestigial {noun}"
    )


@check(
    "write-only-attributes",
    severity=Severity.MEDIUM,
    description=(
        "Record-class fields (dataclass, pydantic, attrs, NamedTuple, TypedDict) "
        "production code writes and no production code reads"
    ),
    advice=Advice(
        fix=("Delete the field, every write to it, and the test reads named."),
        dismiss=(
            "Dismiss only if a reader outside the analyzed code consumes the field: a"
            " serializer, an external client of an exported class, a template pysmelly"
            " does not read."
        ),
    ),
)
def check_write_only_attributes(ctx: AnalysisContext) -> list[Finding]:
    """Find record-class fields production code writes and no production code reads.

    Config classes accumulate fields as features iterate: each round adds
    parameters, but removal doesn't clean them up. Fields like
    async_max_connections or cache_compression persist long after the
    feature they configured was changed or dropped.

    A read in a test-role file does not count: a field production writes
    and only tests read is test-only surface, and the finding names the
    tests that read it.

    A record class is a ``@dataclass``, a pydantic model, an attrs class, a
    ``NamedTuple`` or a ``TypedDict`` (``checks/records.py`` says how each
    is recognised). A field is read when its name is read as an attribute
    anywhere in production code (``x.name``, ``getattr(x, "name")``; as a
    string key, for a TypedDict), or after a dot in a template under a
    ``templates`` directory (``{{ x.name }}``), or when its record is read
    wholesale: ``asdict``, ``fields()``, ``vars``, ``__dict__``,
    ``model_dump``, ``_asdict``, unpacking, ``**``. A field is written when production code
    names it at a write site (assignment, constructor keyword or positional,
    ``model_construct``) or builds the record leaving it to its default; a
    model only parsed into (``model_validate``) writes nothing, so its
    fields are not reported.

    Classes listed in __all__ are public API — their fields may be read
    by downstream consumers outside this codebase, so those findings are
    downgraded to LOW rather than suppressed: a field nothing in the
    defining repo reads is still worth an investigation, especially when
    a whole config class accretes them.
    """
    index = RecordIndex(ctx)
    record_fields = list(index.fields())
    if not record_fields:
        return []
    uses = collect_record_uses(ctx, index)
    exported = _collect_exported_names(ctx.all_trees)

    findings = []
    for record_field in record_fields:
        if record_field.is_read(uses.reads) or not record_field.is_written(uses.writes):
            continue
        tests = surface_of_sites(
            ctx, [site for site, read in uses.test_reads.items() if record_field.is_read(read)]
        )
        class_name = record_field.record.name
        is_exported = class_name in exported
        export_note = (
            f" ({class_name} is in __all__ — external readers possible)" if is_exported else ""
        )
        findings.append(
            Finding(
                file=str(record_field.record.file),
                line=record_field.line,
                check="write-only-attributes",
                message=f"{_unread_field_message(record_field, tests)}{export_note}",
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
    advice=Advice(
        fix="Delete the container and every mutation of it, or add the consumer it was built for.",
        dismiss=(
            "Dismiss only if code outside the analyzed tree reads the container, such as a"
            " plugin registry another package iterates."
        ),
    ),
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
    for filepath, tree in source_trees(ctx):
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
    for tree in ctx.all_trees.values():
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
                    f" anywhere in the codebase — dead bookkeeping"
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
        if is_bare_name(deco, "property"):
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
    for stmt, target, value in iter_simple_assigns(ast.iter_child_nodes(tree)):
        if isinstance(value, ast.Constant) and value.value is None:
            none_globals[target.id] = stmt.lineno
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
                    f" {reader_str} — behavior depends on call order"
                ),
                severity=Severity.MEDIUM,
            )
        )
    return findings


@check(
    "temporal-coupling",
    severity=Severity.MEDIUM,
    description="Methods reading self.x only set by another non-__init__ method",
    advice=Advice(
        fix=(
            "Set the value in __init__ (or pass it as an argument to the methods that read"
            " it) so no method depends on another having run first."
        ),
        dismiss=(
            "Dismiss only if a framework guarantees the order, such as a setUp or a"
            " lifecycle hook that always runs before the reader."
        ),
    ),
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

    for filepath, tree in source_trees(ctx):
        findings.extend(_module_temporal_coupling_findings(filepath, tree))

        for node in iter_module_classes(tree):
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
    advice=Advice(
        fix="Move the method, or the part reading the parameter, onto the parameter's class.",
        dismiss=(
            "Dismiss only if the parameter's class is outside the project (a library or"
            " framework type) and cannot take the method."
        ),
    ),
)
def check_feature_envy(ctx: AnalysisContext) -> list[Finding]:
    """Find methods that use another object's attributes more than self."""
    findings = []

    for filepath, tree in source_trees(ctx):
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
    if has_decorator_named(node, _DATA_CLASS_DECORATORS):
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
    advice=Advice(
        fix=(
            "Move the behavior the outside functions apply to these attributes into"
            " methods, or declare the class a dataclass if it is only data."
        ),
        dismiss=(
            "Dismiss only if the class is a data-transfer record whose consumers sit"
            " outside the analyzed code."
        ),
    ),
)
def check_anemic_domain(ctx: AnalysisContext) -> list[Finding]:
    """Find classes that are data bags with no behavior."""
    findings = []

    for filepath, tree in source_trees(ctx):
        for node in iter_module_classes(tree):
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
                if ctx.roles.is_test(other_path):
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
                    f" attributes"
                )
            else:
                msg = f"{node.name} has {len(init_attrs)} attributes but no behavior methods"

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
