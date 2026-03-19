"""Pattern-based checks — detect specific code idioms that suggest refactoring."""

import ast
from pathlib import Path

from pysmelly.registry import Finding, Severity, check


@check("foo-equals-foo", severity=Severity.MEDIUM)
def check_foo_equals_foo(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find constructor calls where many kwargs have name=name pattern.

    When a function call has 4+ arguments of the form foo=foo, it suggests
    the caller has accumulated too many local variables that mirror a
    constructor's parameters — consider bundling into a dataclass.
    """
    findings = []
    threshold = 4

    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not node.keywords:
                continue

            foo_foo_count = 0
            foo_foo_names = []
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
                    foo_foo_count += 1
                    foo_foo_names.append(kw.arg)

            if foo_foo_count >= threshold:
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                else:
                    call_name = "?"

                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="foo-equals-foo",
                        message=(
                            f"{call_name}() has {foo_foo_count} foo=foo args: "
                            f"{', '.join(foo_foo_names[:5])}"
                            f"{'...' if len(foo_foo_names) > 5 else ''}"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


@check("suspicious-fallbacks", severity=Severity.HIGH)
def check_suspicious_fallbacks(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
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


@check("temp-accumulators", severity=Severity.MEDIUM)
def check_temp_accumulators(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find temporary lists used only to accumulate and join/check.

    Pattern: name = [], then conditional appends, then join() or 'if name:'.
    Often replaceable with a comprehension or direct string formatting.
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

            if append_count >= 2 and join_or_check and other_uses == 0:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=assign_line,
                        check="temp-accumulators",
                        message=(
                            f"'{var_name}' is a temporary accumulator "
                            f"({append_count} appends then join/check) — "
                            f"consider a comprehension or direct approach"
                        ),
                        severity=Severity.MEDIUM,
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


@check("constant-dispatch-dicts", severity=Severity.MEDIUM)
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
