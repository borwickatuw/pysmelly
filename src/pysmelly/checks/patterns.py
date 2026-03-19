"""Pattern-based checks — detect specific code idioms that suggest refactoring."""

import ast
from pathlib import Path

from pysmelly.checks.helpers import find_calls_to_function
from pysmelly.registry import Finding, Severity, check


def _build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Build a child→parent mapping for an AST."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Walk up the parent chain to find the enclosing function."""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _get_param_names(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get all parameter names from a function definition."""
    names = {a.arg for a in func_node.args.args}
    names |= {a.arg for a in func_node.args.posonlyargs}
    names |= {a.arg for a in func_node.args.kwonlyargs}
    if func_node.args.vararg:
        names.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        names.add(func_node.args.kwarg.arg)
    return names


def _count_name_loads(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, int]:
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
def check_foo_equals_foo(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
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

    for filepath, tree in all_trees.items():
        parents = _build_parent_map(tree)
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
            enclosing = _enclosing_function(node, parents)
            if enclosing:
                fid = id(enclosing)
                if fid not in func_cache:
                    func_cache[fid] = (
                        _get_param_names(enclosing),
                        _count_name_loads(enclosing),
                    )
                param_names, load_counts = func_cache[fid]

                single_use = [
                    n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) == 1
                ]
                forwarded = [n for n in foo_foo_names if n in param_names]
                multi_use = [
                    n for n in foo_foo_names if n not in param_names and load_counts.get(n, 0) > 1
                ]

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
    "suspicious-fallbacks",
    severity=Severity.HIGH,
    description="dict.get() with non-trivial defaults on constant dicts",
)
def check_suspicious_fallbacks(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find .get() on module-level constant dicts with non-trivial defaults.

    A default of None/0/False/"" is normal. A non-trivial default suggests
    the caller expects a miss — which may mean the constant dict is incomplete
    or the fallback masks a bug. If the key should always exist, use [] indexing.
    """
    findings = []

    for filepath, tree in all_trees.items():
        constant_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        constant_names.add(target.id)

        if not constant_names:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "get":
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id not in constant_names:
                continue
            if len(node.args) < 2:
                continue

            default = node.args[1]
            if isinstance(default, ast.Constant) and default.value in (None, 0, False, ""):
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="suspicious-fallbacks",
                    message=(
                        f"{node.func.value.id}.get() has a non-trivial fallback default — "
                        f"if the key should always exist, use [] indexing and fail fast"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


def _has_append_to(node: ast.AST, var_name: str) -> bool:
    """Check if an AST node contains an append call to var_name."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == var_name
            and child.func.attr == "append"
        ):
            return True
    return False


def _has_batch_flush_in_loop(siblings: list[ast.AST], var_name: str) -> bool:
    """Check if any loop body both appends to and resets var_name (batch-flush pattern)."""
    for sibling in siblings:
        if not isinstance(sibling, (ast.For, ast.AsyncFor)):
            continue
        if not _has_append_to(sibling, var_name):
            continue
        for child in ast.walk(sibling):
            # var_name = []
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
                and child.targets[0].id == var_name
                and isinstance(child.value, ast.List)
                and not child.value.elts
            ):
                return True
            # var_name.clear()
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == var_name
                and child.func.attr == "clear"
            ):
                return True
    return False


def _classify_appends(siblings: list[ast.AST], var_name: str) -> tuple[int, int, int]:
    """Classify appends by context: (loop_appends, conditional_appends, bare_appends).

    A sibling For/AsyncFor containing appends → loop_appends.
    A sibling If containing appends → conditional_appends.
    A bare Expr with an append → bare_appends.
    """
    loop = 0
    conditional = 0
    bare = 0
    for sibling in siblings:
        if not _has_append_to(sibling, var_name):
            continue
        if isinstance(sibling, (ast.For, ast.AsyncFor)):
            loop += 1
        elif isinstance(sibling, ast.If):
            conditional += 1
        else:
            bare += 1
    return loop, conditional, bare


@check(
    "temp-accumulators",
    severity=Severity.MEDIUM,
    description="Lists built by append then joined (use comprehension)",
)
def check_temp_accumulators(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find temporary lists used only to accumulate and join/check.

    Pattern: name = [], then appends, then join() or 'if name:'.

    Distinguishes two sub-patterns:
    - Loop appending a transform → high confidence, use a comprehension (MEDIUM)
    - Multiple independent conditional appends → low confidence, accumulator
      is often the right choice for heterogeneous conditions (LOW)
    """
    findings = []

    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            if not isinstance(node.targets[0], ast.Name):
                continue
            if not isinstance(node.value, ast.List):
                continue
            if node.value.elts:
                continue

            var_name = node.targets[0].id
            assign_line = node.lineno

            siblings = _find_siblings_after(tree, node)
            if not siblings:
                continue

            append_count = 0
            other_uses = 0
            join_or_check = False

            for subsequent in siblings:
                for child in ast.walk(subsequent):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == var_name
                    ):
                        if child.func.attr == "append":
                            append_count += 1
                        elif child.func.attr == "join":
                            join_or_check = True
                        else:
                            other_uses += 1

                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "join"
                        and child.args
                        and isinstance(child.args[0], ast.Name)
                        and child.args[0].id == var_name
                    ):
                        join_or_check = True

                    if isinstance(child, ast.If):
                        if isinstance(child.test, ast.Name) and child.test.id == var_name:
                            join_or_check = True

            loop_appends, conditional_appends, bare_appends = _classify_appends(siblings, var_name)

            # Suppress batch-flush pattern: append + reset within same loop
            if _has_batch_flush_in_loop(siblings, var_name):
                continue

            # A loop body runs N times, so 1 append in a loop is sufficient
            min_appends = 1 if loop_appends > 0 else 2

            if append_count >= min_appends and join_or_check and other_uses == 0:

                if loop_appends > 0:
                    severity = Severity.MEDIUM
                    message = (
                        f"'{var_name}' is a loop-and-append accumulator "
                        f"— replace with a comprehension"
                    )
                elif conditional_appends > 0 and bare_appends == 0:
                    severity = Severity.LOW
                    message = (
                        f"'{var_name}' is built from {conditional_appends} "
                        f"independent conditions then joined/checked "
                        f"— accumulator may be appropriate here"
                    )
                else:
                    severity = Severity.MEDIUM
                    message = (
                        f"'{var_name}' is a temporary accumulator "
                        f"({append_count} appends then join/check) — "
                        f"consider a comprehension or direct approach"
                    )

                findings.append(
                    Finding(
                        file=str(filepath),
                        line=assign_line,
                        check="temp-accumulators",
                        message=message,
                        severity=severity,
                    )
                )

    return findings


def _find_siblings_after(tree: ast.Module, target: ast.AST) -> list[ast.AST]:
    """Find statements that come after target in the same block."""
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "handlers", "finalbody"):
            block = getattr(parent, attr, None)
            if not isinstance(block, list):
                continue
            for i, child in enumerate(block):
                if child is target:
                    return block[i + 1 :]
    return []


@check(
    "constant-dispatch-dicts",
    severity=Severity.MEDIUM,
    description="Module-level string-to-function dispatch tables",
)
def check_constant_dispatch_dicts(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find module-level dicts where all values are bare name references.

    These dispatch/registration tables can get out of sync with the functions
    they reference. Consider a decorator pattern that colocates the name
    with the definition.
    """
    findings = []
    min_entries = 3

    for filepath, tree in all_trees.items():
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

            if isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id
            else:
                var_name = "?"

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
    param_names = {a.arg for a in func_node.args.args if a.arg not in ("self", "cls")}
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


@check(
    "trivial-wrappers",
    severity=Severity.LOW,
    description="Functions whose body is a single return (inline candidate)",
)
def check_trivial_wrappers(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
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

    for filepath, tree in all_trees.items():
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
            callers = find_calls_to_function(all_trees, node.name)
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


def _describe_trivial_return(value: ast.expr) -> str | None:
    """Describe a trivial return value, or None if it's not trivial."""
    # dict[key] or dict.get(key)
    if isinstance(value, ast.Subscript):
        if isinstance(value.value, ast.Name):
            return f"{value.value.id}[...]"
    # obj.attr
    if isinstance(value, ast.Attribute):
        if isinstance(value.value, ast.Name):
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
    "env-fallbacks",
    severity=Severity.HIGH,
    description="os.environ.get() or os.getenv() with non-None defaults",
)
def check_env_fallbacks(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Find environment variable lookups with non-None fallback defaults.

    If the config is required, it should fail fast on missing values rather
    than silently falling back to a default that masks misconfiguration.
    """
    findings = []

    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            key_name = _get_env_call_key(node)
            if key_name is None:
                continue

            # Check for non-None default
            default = _get_env_default(node)
            if default is None:
                continue

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="env-fallbacks",
                    message=(
                        f"os.environ.get('{key_name}', {default}) has a fallback default — "
                        f"if this config is required, use os.environ['{key_name}'] and fail fast"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


def _attr_chain(node: ast.expr) -> str:
    """Build a dotted string from nested Attribute nodes."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_chain(node.value)}.{node.attr}"
    return "?"


@check(
    "runtime-monkey-patch",
    severity=Severity.MEDIUM,
    description="Function assigned to attribute of external object at module scope",
)
def check_runtime_monkey_patch(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find module-level monkey-patches: obj.attr = local_function.

    Monkey-patching replaces behavior at runtime, making code harder to
    trace and debug. Consider subclassing, decoration, or dependency
    injection instead.
    """
    findings = []

    for filepath, tree in all_trees.items():
        # Collect locally-defined function names at module level
        local_funcs: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_funcs.add(node.name)

        if not local_funcs:
            continue

        # Collect captured originals: name = obj.attr at module level
        captured_attrs: dict[str, str] = {}
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
            ):
                captured_attrs[_attr_chain(node.value)] = node.targets[0].id

        # Find module-level: obj.attr = local_func
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            if node.value.id not in local_funcs:
                continue

            target_str = _attr_chain(target)
            func_name = node.value.id

            capture_name = captured_attrs.get(target_str)
            if capture_name:
                message = (
                    f"{target_str} = {func_name} — "
                    f"monkey-patch at module scope "
                    f"(original captured as '{capture_name}')"
                )
            else:
                message = f"{target_str} = {func_name} — monkey-patch at module scope"

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="runtime-monkey-patch",
                    message=message,
                    severity=Severity.MEDIUM,
                )
            )

    return findings


def _get_env_call_key(node: ast.Call) -> str | None:
    """Return the env var name if this is an os.environ.get() or os.getenv() call."""
    # os.environ.get("KEY", ...)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
    ):
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    # os.getenv("KEY", ...)
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ):
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    return None


def _get_env_default(node: ast.Call) -> str | None:
    """Return repr of the default value if it's non-None, or None otherwise."""
    # Second positional arg
    if len(node.args) >= 2:
        default = node.args[1]
        if isinstance(default, ast.Constant) and default.value is None:
            return None
        if isinstance(default, ast.Constant):
            return repr(default.value)
        # Non-constant default (variable, call, etc.) — still suspicious
        if isinstance(default, ast.Name):
            return default.id
        return "..."
    # default= keyword arg
    for kw in node.keywords:
        if kw.arg == "default":
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return None
            if isinstance(kw.value, ast.Constant):
                return repr(kw.value.value)
            return "..."
    return None
