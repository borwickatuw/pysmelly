"""Pattern checks for naming and stringly-typed access.

Hungarian notation, getattr, passwords, closures.
"""

from __future__ import annotations

import ast
import re

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

# --- hungarian-notation ---

# Apps Hungarian (type-as-prefix): strName, intCount, lstItems
# Systems Hungarian (storage-as-prefix): szName, lpBuffer, dwFlags, fnCallback
_HUNGARIAN_RE = re.compile(
    r"^(str|int|bool|lst|dict|arr|obj|flt|tpl|set"  # Apps Hungarian
    r"|sz|lp|dw|fn|cb|rg|pi|pf|pp|lpsz|pfn)[A-Z]"  # Systems Hungarian
)


def _to_snake_case(name: str) -> str:
    """Convert camelCase hungarian name to snake_case suggestion."""
    # Split on uppercase letter boundaries
    parts: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch.isupper() and current:
            parts.append("".join(current).lower())
            current = [ch]
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).lower())
    return "_".join(parts)


@check(
    "hungarian-notation",
    severity=Severity.LOW,
    description="Variables using Hungarian notation (strName, intCount, lstItems)",
)
def check_hungarian_notation(ctx: AnalysisContext) -> list[Finding]:
    """Find variables using Hungarian notation prefixes."""
    findings = []
    seen: set[tuple[str, int]] = set()  # (file, line) dedup

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            names_to_check: list[tuple[str, int]] = []

            # Assignment targets
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names_to_check.append((target.id, node.lineno))

            # Annotated assignment targets
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names_to_check.append((node.target.id, node.lineno))

            # Function parameters
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    names_to_check.append((arg.arg, node.lineno))

            # For-loop targets
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                names_to_check.append((node.target.id, node.lineno))

            for name, line in names_to_check:
                # Skip UPPER_CASE names
                if name.isupper() or name.upper() == name:
                    continue
                m = _HUNGARIAN_RE.match(name)
                if m:
                    key = (str(filepath), line)
                    if key not in seen:
                        seen.add(key)
                        snake = _to_snake_case(name)
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=line,
                                check="hungarian-notation",
                                message=(
                                    f"{name} uses Hungarian notation — consider snake_case: {snake}"
                                ),
                                severity=Severity.LOW,
                            )
                        )

    return findings


# --- plaintext-passwords ---

_PASSWORD_NAMES = frozenset({"password", "passwd", "secret", "token", "api_key", "apikey"})


def _has_password_name(node: ast.expr) -> bool:
    """Check if an expression refers to a password-related variable."""
    name = None
    if isinstance(node, ast.Name):
        name = node.id.lower()
    elif isinstance(node, ast.Attribute):
        name = node.attr.lower()
    if name is None:
        return False
    return any(pw in name for pw in _PASSWORD_NAMES)


@check(
    "plaintext-passwords",
    severity=Severity.HIGH,
    description="Equality comparison on password/secret/token variables",
)
def check_plaintext_passwords(ctx: AnalysisContext) -> list[Finding]:
    """Find plaintext password comparisons using == or !=."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            # Check == and != comparisons
            if isinstance(node, ast.Compare):
                for op in node.ops:
                    if not isinstance(op, (ast.Eq, ast.NotEq)):
                        continue
                    # Check left side and all comparators
                    all_operands = [node.left] + node.comparators
                    for operand in all_operands:
                        if _has_password_name(operand):
                            op_str = "==" if isinstance(op, ast.Eq) else "!="
                            if isinstance(operand, ast.Name):
                                var_name = operand.id
                            else:
                                var_name = operand.attr  # type: ignore[union-attr]
                            findings.append(
                                Finding(
                                    file=str(filepath),
                                    line=node.lineno,
                                    check="plaintext-passwords",
                                    message=(
                                        f"{var_name} compared with {op_str}"
                                        f" — possible plaintext comparison; use"
                                        f" hmac.compare_digest() or hash comparison"
                                    ),
                                    severity=Severity.HIGH,
                                )
                            )
                            break  # one finding per Compare node
                    else:
                        continue
                    break

    return findings


# --- getattr-strings ---


@check(
    "getattr-strings",
    severity=Severity.MEDIUM,
    description="getattr(obj, 'literal') without default or hasattr(obj, 'literal')",
)
def check_getattr_strings(ctx: AnalysisContext) -> list[Finding]:
    """Find stringly-typed getattr/hasattr calls with literal strings."""
    findings = []
    # For cross-file aggregation
    cross_file: dict[str, list[tuple[str, int]]] = {}

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue

            func_name = node.func.id

            if func_name == "getattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # Skip if 3rd arg or default= keyword provided
                has_default = len(node.args) >= 3 or any(
                    kw.arg == "default" for kw in node.keywords
                )
                attr_name = attr_arg.value
                # Track for cross-file
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if has_default:
                    continue
                if not is_test_file(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(
                                f"getattr(obj, '{attr_name}') without default"
                                f" — use obj.{attr_name} directly"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

            elif func_name == "hasattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # hasattr(self, ...) is legitimate introspection (e.g. Django
                # reverse OneToOneField checks), not stringly-typed access
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Name) and first_arg.id == "self":
                    continue
                attr_name = attr_arg.value
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if not is_test_file(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(
                                f"hasattr(obj, '{attr_name}')"
                                f" — stringly-typed attribute check;"
                                f" consider a Protocol or isinstance()"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

    # Cross-file shotgun surgery check
    for attr_name, locs in cross_file.items():
        files = {loc[0] for loc in locs}
        if len(locs) >= 3 and len(files) >= 3:
            loc_strs = [f"{f}:{line}" for f, line in sorted(locs)[:5]]
            findings.append(
                Finding(
                    file=locs[0][0],
                    line=locs[0][1],
                    check="getattr-strings",
                    message=(
                        f"'{attr_name}' used in getattr/hasattr across"
                        f" {len(locs)} locations in {len(files)} files"
                        f" ({', '.join(loc_strs)}) — shotgun surgery risk"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings


# --- late-binding-closures ---

_LOOP_STMTS = (ast.For, ast.AsyncFor)


def _get_loop_var_names(loop: ast.For | ast.AsyncFor) -> set[str]:
    """Extract variable names from a for-loop target."""
    names: set[str] = set()
    target = loop.target
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Tuple):
        for elt in target.elts:
            if isinstance(elt, ast.Name):
                names.add(elt.id)
    return names


def _find_free_names_in_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """Find names read (Load) inside a function/lambda that aren't its own params or locals."""
    # Collect parameter names
    if isinstance(func, ast.Lambda):
        args = func.args
        body_nodes: list[ast.AST] = [func.body]
    else:
        args = func.args
        body_nodes = func.body  # type: ignore[assignment]

    param_names: set[str] = set()
    for a in args.args + args.posonlyargs + args.kwonlyargs:
        param_names.add(a.arg)
    if args.vararg:
        param_names.add(args.vararg.arg)
    if args.kwarg:
        param_names.add(args.kwarg.arg)
    # Default args with same name as param are captures (x=x pattern)
    captured_via_default: set[str] = set()
    all_defaults = args.defaults + args.kw_defaults
    for d in all_defaults:
        if isinstance(d, ast.Name):
            captured_via_default.add(d.id)

    # Collect local assignments
    local_names: set[str] = set()
    for node in ast.walk(func if isinstance(func, ast.Lambda) else ast.Module(body=func.body)):  # type: ignore[arg-type]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            local_names.add(node.target.id)

    # Collect all Name loads
    read_names: set[str] = set()
    for start_node in body_nodes:
        for node in ast.walk(start_node):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                read_names.add(node.id)

    # Free names = reads that aren't params, locals, or captured via defaults
    return read_names - param_names - local_names - captured_via_default


@check(
    "late-binding-closures",
    severity=Severity.HIGH,
    description="Lambda/closure in loop captures loop variable by reference, not value",
)
def check_late_binding_closures(ctx: AnalysisContext) -> list[Finding]:
    """Find closures in loops that capture the loop variable by late binding."""
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, _LOOP_STMTS):
                continue

            loop_vars = _get_loop_var_names(node)
            if not loop_vars:
                continue

            # Walk the loop body looking for lambdas and nested function defs
            for child in ast.walk(node):
                if child is node:
                    continue

                if isinstance(child, ast.Lambda):
                    free = _find_free_names_in_func(child)
                    captured = loop_vars & free
                    if captured:
                        var_str = ", ".join(sorted(captured))
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=child.lineno,
                                check="late-binding-closures",
                                message=(
                                    f"lambda captures loop variable {var_str}"
                                    f" by reference — all closures will see"
                                    f" the final value; use default arg"
                                    f" ({var_str}={var_str}) to capture"
                                ),
                                severity=Severity.HIGH,
                            )
                        )

                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    free = _find_free_names_in_func(child)
                    captured = loop_vars & free
                    if captured:
                        var_str = ", ".join(sorted(captured))
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=child.lineno,
                                check="late-binding-closures",
                                message=(
                                    f"{child.name}() captures loop variable"
                                    f" {var_str} by reference — all closures"
                                    f" will see the final value"
                                ),
                                severity=Severity.HIGH,
                            )
                        )

    return findings
