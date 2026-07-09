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
    is_test_file,
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


# --- return-mutable-constant ---


def _collect_module_mutable_constants(tree: ast.Module) -> dict[str, int]:
    """Module-level names bound to a mutable literal (dict/list/set).

    Returns {name: lineno}. Only names assigned exactly once at module
    scope are considered — a name reassigned elsewhere is less clearly a
    shared constant.
    """
    assign_counts: dict[str, int] = {}
    lines: dict[str, int] = {}
    values: dict[str, ast.expr] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        assign_counts[target.id] = assign_counts.get(target.id, 0) + 1
        lines[target.id] = node.lineno
        values[target.id] = node.value
    return {
        name: lines[name]
        for name, count in assign_counts.items()
        if count == 1 and isinstance(values[name], (ast.Dict, ast.List, ast.Set))
    }


@check(
    "return-mutable-constant",
    severity=Severity.MEDIUM,
    description="Function returns a module-level mutable constant by reference",
)
def check_return_mutable_constant(ctx: AnalysisContext) -> list[Finding]:
    """Find functions that ``return`` a module-level mutable container directly.

    ``return DEFAULT_CONFIG`` (a module dict/list/set) hands every caller
    the same object: any caller that mutates the result mutates it for
    everyone, and defaults quietly drift. Returning ``dict(DEFAULT_CONFIG)``,
    ``DEFAULT_CONFIG.copy()``, or ``[*DEFAULT]`` is fine.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        mutable_consts = _collect_module_mutable_constants(tree)
        if not mutable_consts:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # A name reassigned inside the function is a local shadow, not
            # the shared constant.
            local_names = {
                t.id
                for n in ast.walk(node)
                if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for stmt in ast.walk(node):
                if not (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name)):
                    continue
                name = stmt.value.id
                if name in local_names:
                    continue
                if name not in mutable_consts:
                    continue
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=stmt.lineno,
                        check="return-mutable-constant",
                        message=(
                            f"{node.name}() returns module-level {name} by"
                            f" reference — callers mutating the result mutate"
                            f" the shared constant; return a copy"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


# --- reimplemented-stdlib ---


def _skip_leading_assigns(body: list[ast.stmt]) -> list[ast.stmt]:
    """Drop leading plain assignments (e.g. `key = key_func(item)`) so the
    core dict-building shape can be matched regardless of a precomputed
    key local."""
    i = 0
    while i < len(body) and isinstance(body[i], ast.Assign):
        i += 1
    return body[i:]


def _is_counter_loop(node: ast.For) -> bool:
    """Match `if k in d: d[k] = d[k] + 1 (or += 1) else: d[k] = 1` — Counter."""
    body = _skip_leading_assigns(node.body)
    if len(body) != 1 or not isinstance(body[0], ast.If):
        return False
    if_node = body[0]
    if not (len(if_node.body) == 1 and len(if_node.orelse) == 1):
        return False
    # The test is `k in d` or `k not in d`
    test = if_node.test
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    if negated:
        test = test.operand
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], (ast.In, ast.NotIn))
    ):
        return False

    inc_branch = if_node.orelse[0] if negated else if_node.body[0]
    init_branch = if_node.body[0] if negated else if_node.orelse[0]

    def is_increment(stmt: ast.stmt) -> bool:
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
            return isinstance(stmt.target, ast.Subscript)
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Subscript)
            and isinstance(stmt.value, ast.BinOp)
            and isinstance(stmt.value.op, ast.Add)
        ):
            return True
        return False

    def is_init_one(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Subscript)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value == 1
        )

    return is_increment(inc_branch) and is_init_one(init_branch)


def _is_setdefault_append_loop(node: ast.For) -> bool:
    """Match `if k not in d: d[k] = []` then `d[k].append(...)` — defaultdict(list)."""
    body = _skip_leading_assigns(node.body)
    if not (2 <= len(body) <= 3):
        return False
    guard = body[0]
    if not (isinstance(guard, ast.If) and len(guard.body) == 1 and not guard.orelse):
        return False
    test = guard.test
    if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
        # allow `k not in d`
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.NotIn)
        ):
            return False
    init = guard.body[0]
    if not (
        isinstance(init, ast.Assign)
        and isinstance(init.targets[0], ast.Subscript)
        and isinstance(init.value, (ast.List, ast.Dict, ast.Set))
    ):
        return False
    # Some later statement appends/updates the subscript
    for stmt in body[1:]:
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in {"append", "add", "extend"}
                and isinstance(sub.func.value, ast.Subscript)
            ):
                return True
    return False


def _dict_initialized_names(tree: ast.Module) -> set[str]:
    """Names ever assigned a dict literal / dict() / dict comprehension.

    Used to distinguish `result.update(d)` (dict merge) from
    `sha256.update(chunk)` (hashlib) and `some_set.update(items)` (set),
    which share the ``.update()`` spelling but aren't dict merges.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        is_dict = isinstance(value, (ast.Dict, ast.DictComp)) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "dict"
        )
        if not is_dict:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _is_dict_merge_loop(node: ast.For, dict_names: set[str]) -> bool:
    """Match `for d in dicts: result.update(d)` where result is a dict and
    each iterated element is merged in — dict union / {**a, **b}."""
    if len(node.body) != 1 or not isinstance(node.target, ast.Name):
        return False
    stmt = node.body[0]
    if not (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == "update"
        and len(stmt.value.args) == 1
        and isinstance(stmt.value.args[0], ast.Name)
        and isinstance(node.iter, ast.Name)
    ):
        return False
    # The merged value must be the loop element itself, and the receiver
    # must be a name known to hold a dict.
    receiver = stmt.value.func.value
    return (
        stmt.value.args[0].id == node.target.id
        and isinstance(receiver, ast.Name)
        and receiver.id in dict_names
    )


@check(
    "reimplemented-stdlib",
    severity=Severity.LOW,
    description="Loops that hand-roll collections.Counter/defaultdict/dict-merge",
)
def check_reimplemented_stdlib(ctx: AnalysisContext) -> list[Finding]:
    """Find loops that reimplement a one-liner from the standard library.

    A count-into-a-dict loop is ``collections.Counter``; an
    if-not-in-then-append loop is ``collections.defaultdict(list)``; a
    ``for d in dicts: result.update(d)`` loop is ``{**a, **b}`` / dict
    union. The stdlib forms are clearer and less bug-prone.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        dict_names = _dict_initialized_names(tree)
        checks = (
            (_is_counter_loop, "collections.Counter"),
            (_is_setdefault_append_loop, "collections.defaultdict(list)"),
            (
                lambda n, dn=dict_names: _is_dict_merge_loop(n, dn),
                "dict unpacking `{**a, **b}` or `|`",
            ),
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            for predicate, suggestion in checks:
                if predicate(node):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="reimplemented-stdlib",
                            message=(
                                f"loop reimplements {suggestion} — use the stdlib form instead"
                            ),
                            severity=Severity.LOW,
                        )
                    )
                    break

    return findings
