"""Pattern checks for data structures.

Foo-equals-foo, dispatch dicts, trivial wrappers, dead constants.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.entry_points import RootKind
from pysmelly.checks.framework import is_settings_file
from pysmelly.checks.helpers import (
    bare_target,
    body_without_docstring,
    enclosing_functions,
    get_param_names,
    is_bare_name,
    is_constant_reassigned,
    is_in_dunder_all,
    iter_functions,
    iter_module_dicts,
    iter_simple_assigns,
    iter_uppercase_assigns,
    source_trees,
)
from pysmelly.checks.test_surface import TESTS_LABEL, surface_of_units
from pysmelly.context import AnalysisContext
from pysmelly.registry import MAX_DISPLAY_WIDTH, Advice, Finding, Severity, Subject, check


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
    advice=Advice(
        fix=(
            "Compute each single-use value inline in the call, or build the object"
            " directly where the values are produced."
        ),
        dismiss=(
            "Dismiss only if inlining a local would move its computation past a statement"
            " it must run before (a lock taken, a write, a call whose side effect it reads)."
        ),
    ),
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
                if is_bare_name(kw.value, kw.arg):
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
            enclosing = next(enclosing_functions(node, parents), None)
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
                message = f"{call_name}() has {len(foo_foo_names)} foo=foo args"
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
    advice=Advice(
        fix=(
            "Register each handler with a decorator beside its definition, so adding one"
            " cannot miss the table."
        ),
        dismiss=(
            "Dismiss only if the table is the single place the handlers are defined or"
            " imported, so a new handler cannot be added without editing it."
        ),
    ),
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
        for node, d in iter_module_dicts(tree):
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
                        f"mapping strings to functions ({', '.join(names[:3])}...)"
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
    """Describe a trivial return value, or None if it's not trivial.

    Three shapes count: a subscript lookup, an attribute access, and a call
    that forwards the wrapper's parameters. A constant return does not: on
    the fleet it was always a protocol method or an overridable hook
    default (``readable()`` returning True), not an inline candidate
    (DECISIONS.md, Phase 33-4).
    """
    # dict[key]
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
    return None


@check(
    "trivial-wrappers",
    severity=Severity.LOW,
    description=(
        "Functions whose body is a single return of a lookup, attribute or "
        "forwarding call (inline candidate)"
    ),
    advice=Advice(
        fix="Replace each call with the expression the wrapper returns, and delete the wrapper.",
        dismiss=(
            "Dismiss only if inlining would make callers reach into private state the"
            " wrapper exposes read-only, or the wrapper is a protocol method callers do not"
            " name."
        ),
    ),
)
def check_trivial_wrappers(ctx: AnalysisContext) -> list[Finding]:
    """Find functions whose only real statement is a return.

    Functions that just return a dict lookup, attribute access, or single
    forwarding call are candidates for inlining at call sites. A constant
    return is not reported (see ``_describe_trivial_return``).

    Suppresses:
    - Subclass method implementations (protocol overrides)
    - Self-method chains (return self.other_method())
    - Calls with complex args (from_dict doing data.get() mapping)
    - Multi-caller wrappers (3+ callers = intentional abstraction point)
    """
    findings = []
    multi_caller_threshold = 3

    for filepath, tree in ctx.all_trees.items():
        subclass_methods = _collect_subclass_methods(tree)

        for node in iter_functions(tree):
            if node.name.startswith("_"):
                continue
            if node.decorator_list:
                continue

            body = body_without_docstring(node.body)
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
                    message=(f"{node.name}() just returns {desc}"),
                    severity=Severity.LOW,
                )
            )

    return findings


# --- dead-constants helpers ---


def _collect_name_and_attr_loads(ctx: AnalysisContext) -> tuple[set[str], dict[str, set[Path]]]:
    """Every name used in Load context (bare names and attribute accesses): the
    names production code loads, and per name the test-role files loading it."""
    production: set[str] = set()
    tests: dict[str, set[Path]] = {}
    for filepath, tree in ctx.all_trees.items():
        is_test = ctx.roles.is_test(filepath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            else:
                continue
            if is_test:
                tests.setdefault(name, set()).add(filepath)
            else:
                production.add(name)
    return production, tests


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
    description=(
        "UPPER_CASE module-level constants no production code references "
        "(tests that use one are named)"
    ),
    advice=Advice(
        fix="Delete the constant and the tests named.",
        dismiss=(
            "Dismiss only if code outside the analyzed tree reads it, as part of a"
            " published API; name that consumer in the ignore."
        ),
    ),
)
def check_dead_constants(ctx: AnalysisContext) -> list[Finding]:
    """Find UPPER_CASE module-level names that are defined but never used.

    Covers both literal constants (strings, ints) and non-literal assignments
    (frozenset, dict, list constructors). Event name constants, skip lists,
    configuration keys — these accumulate as code evolves and become dead
    weight when the consuming code is changed or removed.

    Test code does not keep a constant alive: a constant only test-role
    files read or import is test-only surface, and the finding names the
    tests that go with it (``checks/test_surface.py``).
    """
    findings = []

    # Settings modules contain UPPER_CASE constants read by frameworks via
    # getattr() — they're not dead, just invisible to static analysis. Django
    # settings are found by path and by content (the entry-point index).
    django_settings = {
        root.file for root in ctx.entry_points.roots if root.kind is RootKind.DJANGO_SETTINGS
    }

    # Collect all UPPER_CASE module-level names: {name: [(filepath, lineno, desc)]}
    all_constants: dict[str, list[tuple[Path, int, str]]] = {}
    for filepath, tree in source_trees(ctx):
        if is_settings_file(filepath) or filepath in django_settings:
            continue
        rebindings = ctx.module_rebindings[filepath]
        for name, (lineno, desc) in _collect_module_level_names(tree).items():
            if not is_constant_reassigned(rebindings, name, lineno):
                all_constants.setdefault(name, []).append((filepath, lineno, desc))

    if not all_constants:
        return findings

    # Names production loads (Name.Load and Attribute.attr in Load), and the
    # test-role files loading each
    production_loads, test_loads = _collect_name_and_attr_loads(ctx)

    for const_name, defs in all_constants.items():
        # Skip if production code reads it as a name or attribute
        if const_name in production_loads:
            continue
        # Skip if production code imports it
        importers = ctx.references.import_index.get(const_name, set())
        if any(not ctx.roles.is_test(f) for f in importers):
            continue
        test_files = test_loads.get(const_name, set()) | {Path(f) for f in importers}

        for filepath, lineno, desc in defs:
            tree = ctx.all_trees[filepath]
            if is_in_dunder_all(const_name, tree):
                continue
            message = f"{const_name} = {desc} is never referenced anywhere in the codebase"
            if test_files:
                surface = surface_of_units(ctx, [(filepath, const_name)], test_files)
                message = (
                    f"{const_name} = {desc} is never referenced in production code — "
                    f"test-only surface. {TESTS_LABEL.format(it='it')}: {surface.describe()}"
                )
            findings.append(
                Finding(
                    file=str(filepath),
                    line=lineno,
                    check="dead-constants",
                    message=message,
                    severity=Severity.MEDIUM,
                    subjects=(Subject(const_name, str(filepath), None, None, vestigial=True),),
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
    for stmt, target, value in iter_simple_assigns(ast.iter_child_nodes(tree)):
        assign_counts[target.id] = assign_counts.get(target.id, 0) + 1
        lines[target.id] = stmt.lineno
        values[target.id] = value
    return {
        name: lines[name]
        for name, count in assign_counts.items()
        if count == 1 and isinstance(values[name], (ast.Dict, ast.List, ast.Set))
    }


@check(
    "return-mutable-constant",
    severity=Severity.MEDIUM,
    description="Function returns a module-level mutable constant by reference",
    advice=Advice(
        fix=(
            "Return a copy (dict(), list(), copy.deepcopy() for nested values), or make"
            " the constant immutable (MappingProxyType, tuple, frozenset)."
        ),
        dismiss=(
            "Dismiss only if the function is documented to hand out the shared object,"
            " such as a registry accessor whose callers are meant to mutate it."
        ),
    ),
)
def check_return_mutable_constant(ctx: AnalysisContext) -> list[Finding]:
    """Find functions that ``return`` a module-level mutable container directly.

    ``return DEFAULT_CONFIG`` (a module dict/list/set) hands every caller
    the same object: any caller that mutates the result mutates it for
    everyone, and defaults quietly drift. Returning ``dict(DEFAULT_CONFIG)``,
    ``DEFAULT_CONFIG.copy()``, or ``[*DEFAULT]`` is fine.
    """
    findings = []

    for filepath, tree in source_trees(ctx):
        mutable_consts = _collect_module_mutable_constants(tree)
        if not mutable_consts:
            continue

        for node in iter_functions(tree):
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
                            f" the shared constant"
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
    negated = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        negated = True
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
    target = bare_target(node)
    if len(node.body) != 1 or target is None:
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
        stmt.value.args[0].id == target.id
        and isinstance(receiver, ast.Name)
        and receiver.id in dict_names
    )


@check(
    "reimplemented-stdlib",
    severity=Severity.LOW,
    description="Loops that hand-roll collections.Counter/defaultdict/dict-merge",
    advice=Advice(
        fix="Replace the loop with the standard-library form the message names.",
        dismiss=(
            "Dismiss only if the loop does more per item than the standard form expresses,"
            " such as validating or logging each entry."
        ),
    ),
)
def check_reimplemented_stdlib(ctx: AnalysisContext) -> list[Finding]:
    """Find loops that reimplement a one-liner from the standard library.

    A count-into-a-dict loop is ``collections.Counter``; an
    if-not-in-then-append loop is ``collections.defaultdict(list)``; a
    ``for d in dicts: result.update(d)`` loop is ``{**a, **b}`` / dict
    union. The stdlib forms are clearer and less bug-prone.
    """
    findings = []

    for filepath, tree in source_trees(ctx):
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
                            message=(f"loop reimplements {suggestion}"),
                            severity=Severity.LOW,
                        )
                    )
                    break

    return findings


# --- dict-key-typo ---


def _within_edit_distance_1(a: str, b: str) -> bool:
    """True if a and b differ by exactly one insertion, deletion, or
    substitution (edit distance exactly 1 — identical strings return False)."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # Exactly one substitution
        diffs = sum(1 for x, y in zip(a, b, strict=True) if x != y)
        return diffs == 1
    # One insertion/deletion: the shorter is the longer with one char removed
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _is_deliberate_key_variant(a: str, b: str) -> bool:
    """True when two edit-distance-1 keys differ in a way that signals two
    intentional keys rather than a typo: case only (Location/location),
    singular/plural (subject/subjects), or separator/case style
    (ContentType/Content-Type). These dominate the false positives."""
    if a.lower() == b.lower():
        return True
    if a + "s" == b or b + "s" == a:
        return True

    def norm(s: str) -> str:
        return s.replace("-", "").replace("_", "").lower()

    return norm(a) == norm(b)


def _collect_dict_literal_keys(all_trees: dict[Path, ast.Module]) -> set[str]:
    """String keys defined in dict literals anywhere — the 'known shapes'."""
    keys: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
    return keys


def _collect_subscript_string_keys(
    tree: ast.Module,
) -> tuple[list[tuple[str, int]], set[str]]:
    """Return (writes, reads) of string subscript keys in one tree.

    writes: [(key, lineno), ...] for Store-context subscripts.
    reads: {key, ...} for Load-context subscripts.
    """
    writes: list[tuple[str, int]] = []
    reads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        sl = node.slice
        if not (isinstance(sl, ast.Constant) and isinstance(sl.value, str)):
            continue
        if isinstance(node.ctx, ast.Store):
            writes.append((sl.value, node.lineno))
        elif isinstance(node.ctx, ast.Load):
            reads.add(sl.value)
    return writes, reads


@check(
    "dict-key-typo",
    severity=Severity.MEDIUM,
    description="Subscript key that looks like a typo of a defined dict key",
    advice=Advice(
        fix="Correct the key to the defined spelling, or delete the write if nothing reads it.",
        dismiss=(
            "Dismiss only if both spellings are distinct keys a consumer outside the"
            " analyzed code reads."
        ),
    ),
)
def check_dict_key_typo(ctx: AnalysisContext) -> list[Finding]:
    """Find dict-key writes that look like typos of a known key.

    ``user["actve"] = False`` when the shape defines ``"active"`` is a
    silent bug: the write lands in a new key nobody reads, and the real
    field never changes. Flags a string key written via subscript that
    (a) never appears in any dict literal, (b) is never read anywhere,
    and (c) is edit-distance-1 from a key that IS defined in a dict
    literal. All keys involved are 4+ chars to avoid short-key noise.
    """
    findings = []

    literal_keys = _collect_dict_literal_keys(ctx.all_trees)
    long_literal_keys = {k for k in literal_keys if len(k) >= 4}
    if not long_literal_keys:
        return findings

    # Global read set — a key read in any file is a real key, not a typo.
    all_reads: set[str] = set()
    per_file: dict[Path, list[tuple[str, int]]] = {}
    for filepath, tree in ctx.all_trees.items():
        writes, reads = _collect_subscript_string_keys(tree)
        all_reads |= reads
        if not ctx.roles.is_test(filepath):
            per_file[filepath] = writes

    reported: set[tuple[str, str, int]] = set()
    for filepath, writes in per_file.items():
        for key, lineno in writes:
            if len(key) < 4:
                continue
            if key in literal_keys or key in all_reads:
                continue
            neighbors = sorted(
                t
                for t in long_literal_keys
                if _within_edit_distance_1(key, t) and not _is_deliberate_key_variant(key, t)
            )
            if not neighbors:
                continue
            dedup_key = (str(filepath), key, lineno)
            if dedup_key in reported:
                continue
            reported.add(dedup_key)
            findings.append(
                Finding(
                    file=str(filepath),
                    line=lineno,
                    check="dict-key-typo",
                    message=(
                        f'key "{key}" is written but never read, and looks'
                        f' like a typo of defined key "{neighbors[0]}"'
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings
