"""Pattern checks for data structures.

Foo-equals-foo, dispatch dicts, trivial wrappers, dead constants.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.framework import is_settings_file
from pysmelly.checks.helpers import (
    enclosing_function,
    get_param_names,
    is_constant_reassigned,
    is_in_dunder_all,
    iter_uppercase_assigns,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import MAX_DISPLAY_WIDTH, Finding, Severity, check


def _count_name_loads(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, int]:
    """Count Load occurrences of each name in a function body."""
    counts: dict[str, int] = {}
    for child in ast.walk(func_node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            counts[child.id] = counts.get(child.id, 0) + 1
    return counts


@check(
    "foo-equals-foo",
    severity=Severity.MEDIUM,
    description="Single-use locals gathered into an object — inline or build directly",
)
def check_foo_equals_foo(ctx: AnalysisContext) -> list[Finding]:
    """Find calls where many kwargs match local variable names (name=name).

    Distinguishes three cases:
    - Single-use locals (x = compute(); g(x=x) where x isn't used again) — the
      real smell, these intermediates can be inlined.
    - Forwarded parameters (def f(x): g(x=x)) — just passing through, not a smell.
    - Multi-use locals — used elsewhere too, less clear-cut.

    Pure parameter forwarding is suppressed. Single-use locals are MEDIUM severity.
    """
    findings = []
    threshold = 4

    for filepath, tree in ctx.all_trees.items():
        parents = ctx.parent_map(tree)
        func_cache: dict[int, tuple[set[str], dict[str, int]]] = {}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not node.keywords:
                continue

            foo_foo_names = []
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
                    foo_foo_names.append(kw.arg)

            if len(foo_foo_names) < threshold:
                continue

            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = "?"

            # Classify each foo=foo name
            enclosing = enclosing_function(node, parents)
            if enclosing:
                fid = id(enclosing)
                if fid not in func_cache:
                    func_cache[fid] = (
                        get_param_names(enclosing),
                        _count_name_loads(enclosing),
                    )
                param_names, load_counts = func_cache[fid]

                single_use = [
                    n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) == 1
                ]
                [n for n in foo_foo_names if n in param_names]
                [n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) > 1]

                # Only report when there are single-use locals to inline
                if not single_use:
                    continue

                names_str = ", ".join(single_use[:5])
                if len(single_use) > 5:
                    names_str += "..."
                message = (
                    f"{call_name}() has {len(foo_foo_names)} foo=foo args, "
                    f"{len(single_use)} are single-use locals "
                    f"({names_str}) that could be inlined"
                )
                severity = Severity.MEDIUM
            else:
                # Module-level call — no function context for classification
                message = (
                    f"{call_name}() has {len(foo_foo_names)} foo=foo args "
                    f"— consider building the object directly"
                )
                severity = Severity.MEDIUM

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="foo-equals-foo",
                    message=message,
                    severity=severity,
                )
            )

    return findings


@check(
    "constant-dispatch-dicts",
    severity=Severity.MEDIUM,
    description="Module-level string-to-function dispatch tables",
)
def check_constant_dispatch_dicts(ctx: AnalysisContext) -> list[Finding]:
    """Find module-level dicts where all values are bare name references.

    These dispatch/registration tables can get out of sync with the functions
    they reference. Consider a decorator pattern that colocates the name
    with the definition.
    """
    findings = []
    min_entries = 3

    for filepath, tree in ctx.all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            if not node.value.keys:
                continue

            d = node.value
            if not all(isinstance(k, ast.Constant) for k in d.keys):
                continue
            if not all(isinstance(v, ast.Name) for v in d.values):
                continue
            if len(d.keys) < min_entries:
                continue

            # Skip when all values are UPPER_CASE — constants/config, not dispatch
            if all(v.id.isupper() for v in d.values):  # type: ignore[union-attr]
                continue

            var_name = node.targets[0].id if isinstance(node.targets[0], ast.Name) else "?"

            names = [v.id for v in d.values]  # type: ignore[union-attr]
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="constant-dispatch-dicts",
                    message=(
                        f"{var_name} is a {len(d.keys)}-entry dispatch dict "
                        f"mapping strings to functions ({', '.join(names[:3])}...) — "
                        f"consider decorator registration"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


def _is_subclass_method(func_node: ast.AST, subclass_methods: set[int]) -> bool:
    """Check if a function node is a method in a class with base classes."""
    return id(func_node) in subclass_methods


def _is_self_method_chain(value: ast.expr) -> bool:
    """Check if the return is self.method(...) — part of a deliberate API chain."""
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "self"
    )


def _is_pure_forwarding_call(
    call_node: ast.Call, func_node: ast.FunctionDef | ast.AsyncFunctionDef
) -> bool:
    """Check if a call only forwards the wrapper's own parameters.

    Returns False when the call adds any extra arguments (constants,
    expressions, etc.) beyond what the wrapper receives — the wrapper
    is adding configuration, not just forwarding.
    """
    param_names = {a.arg for a in func_node.args.args if a.arg not in {"self", "cls"}}
    param_names |= {a.arg for a in func_node.args.posonlyargs}
    param_names |= {a.arg for a in func_node.args.kwonlyargs}

    for arg in call_node.args:
        if isinstance(arg, ast.Starred):
            continue  # *args pass-through
        if not (isinstance(arg, ast.Name) and arg.id in param_names):
            return False
    for kw in call_node.keywords:
        if kw.arg is None:
            continue  # **kwargs pass-through
        if not (isinstance(kw.value, ast.Name) and kw.value.id in param_names):
            return False
    return True


def _collect_subclass_methods(tree: ast.Module) -> set[int]:
    """Collect ids of methods defined in classes that have base classes."""
    methods: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.bases:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(id(item))
    return methods


def _describe_trivial_return(value: ast.expr) -> str | None:
    """Describe a trivial return value, or None if it's not trivial."""
    # dict[key] or dict.get(key)
    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
        return f"{value.value.id}[...]"
    # obj.attr
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        return f"{value.value.id}.{value.attr}"
    # single function call: func(...)
    if isinstance(value, ast.Call):
        if isinstance(value.func, ast.Name):
            return f"{value.func.id}(...)"
        if isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
            return f"{value.func.value.id}.{value.func.attr}(...)"
    # constant
    if isinstance(value, ast.Constant):
        return repr(value.value)
    return None


@check(
    "trivial-wrappers",
    severity=Severity.LOW,
    description="Functions whose body is a single return (inline candidate)",
)
def check_trivial_wrappers(ctx: AnalysisContext) -> list[Finding]:
    """Find functions whose only real statement is a return.

    Functions that just return a dict lookup, attribute access, or single
    function call are candidates for inlining at call sites.

    Suppresses:
    - Abstract method implementations (constant returns in subclass methods)
    - Self-method chains (return self.other_method())
    - Calls with complex args (from_dict doing data.get() mapping)
    - Multi-caller wrappers (3+ callers = intentional abstraction point)
    """
    findings = []
    multi_caller_threshold = 3

    for filepath, tree in ctx.all_trees.items():
        subclass_methods = _collect_subclass_methods(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            if node.decorator_list:
                continue

            # Strip docstring from body
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]

            if len(body) != 1:
                continue
            stmt = body[0]
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue

            ret_value = stmt.value

            # Suppress: subclass methods (protocol implementations can't be inlined)
            if _is_subclass_method(node, subclass_methods):
                continue

            # Suppress: self-method chains (return self.to_dict() etc.)
            if _is_self_method_chain(ret_value):
                continue

            # Suppress: calls that add arguments beyond parameter forwarding
            if isinstance(ret_value, ast.Call) and not _is_pure_forwarding_call(ret_value, node):
                continue

            desc = _describe_trivial_return(ret_value)
            if desc is None:
                continue

            # Suppress: multi-caller wrappers (central point for change)
            callers = ctx.call_index.get(node.name, [])
            if len(callers) >= multi_caller_threshold:
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="trivial-wrappers",
                    message=(
                        f"{node.name}() just returns {desc} — consider inlining at call sites"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings


# --- dead-constants helpers ---


def _collect_all_name_and_attr_loads(
    all_trees: dict[Path, ast.Module],
) -> set[str]:
    """Collect all names used in Load context (bare names and attribute accesses)."""
    names: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
    return names


_AST_TYPE_DESCRIPTIONS: dict[type, str] = {
    ast.Dict: "{...}",
    ast.List: "[...]",
    ast.Set: "{...}",
    ast.Tuple: "(...)",
}


def _describe_ast_value(val: ast.expr) -> str:
    """Build a short human-readable description of an AST value node."""
    if isinstance(val, ast.Constant):
        return repr(val.value)
    if isinstance(val, ast.Call):
        if isinstance(val.func, ast.Name):
            return f"{val.func.id}(...)"
        if isinstance(val.func, ast.Attribute):
            return f"{val.func.attr}(...)"
    desc = _AST_TYPE_DESCRIPTIONS.get(type(val))
    if desc is not None:
        return desc
    return "..."


def _collect_module_level_names(tree: ast.Module) -> dict[str, tuple[int, str]]:
    """Find all UPPER_CASE module-level names (including non-literal assignments).

    Returns {name: (lineno, description)} where description is a short
    representation of the assigned value for the finding message.
    """
    names: dict[str, tuple[int, str]] = {}
    for name, lineno, val in iter_uppercase_assigns(tree):
        desc = _describe_ast_value(val)
        if len(desc) > MAX_DISPLAY_WIDTH:
            desc = desc[: MAX_DISPLAY_WIDTH - 3] + "..."
        names[name] = (lineno, desc)
    return names


@check(
    "dead-constants",
    severity=Severity.MEDIUM,
    description="UPPER_CASE module-level constants never referenced anywhere",
)
def check_dead_constants(ctx: AnalysisContext) -> list[Finding]:
    """Find UPPER_CASE module-level names that are defined but never used.

    Covers both literal constants (strings, ints) and non-literal assignments
    (frozenset, dict, list constructors). Event name constants, skip lists,
    configuration keys — these accumulate as code evolves and become dead
    weight when the consuming code is changed or removed.
    """
    findings = []

    # Collect all UPPER_CASE module-level names: {name: [(filepath, lineno, desc)]}
    all_constants: dict[str, list[tuple[Path, int, str]]] = {}
    for filepath, tree in ctx.all_trees.items():
        # Settings files contain UPPER_CASE constants read by frameworks
        # via getattr() — they're not dead, just invisible to static analysis
        if is_settings_file(filepath):
            continue
        for name, (lineno, desc) in _collect_module_level_names(tree).items():
            if not is_constant_reassigned(tree, name, lineno):
                all_constants.setdefault(name, []).append((filepath, lineno, desc))

    if not all_constants:
        return findings

    # Build set of all referenced names (Name.Load and Attribute.attr in Load)
    referenced_names = _collect_all_name_and_attr_loads(ctx.all_trees)

    for const_name, defs in all_constants.items():
        # Skip if referenced anywhere as a name or attribute
        if const_name in referenced_names:
            continue
        # Skip if imported elsewhere
        if ctx.import_index.get(const_name):
            continue

        for filepath, lineno, desc in defs:
            tree = ctx.all_trees[filepath]
            if is_in_dunder_all(const_name, tree):
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=lineno,
                    check="dead-constants",
                    message=(f"{const_name} = {desc} is never referenced anywhere in the codebase"),
                    severity=Severity.MEDIUM,
                )
            )

    return findings
