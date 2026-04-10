"""Pattern checks for control flow.

Suspicious fallbacks, exception flow control, unreachable code.
"""

from __future__ import annotations

import ast

from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


@check(
    "suspicious-fallbacks",
    severity=Severity.HIGH,
    description="dict.get()/setdefault() with non-trivial defaults on constant dicts",
)
def check_suspicious_fallbacks(ctx: AnalysisContext) -> list[Finding]:
    """Find .get()/.setdefault() on module-level constant dicts with non-trivial defaults.

    A default of None/0/False/"" is normal. A non-trivial default suggests
    the caller expects a miss — which may mean the constant dict is incomplete
    or the fallback masks a bug. If the key should always exist, use [] indexing.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
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
            if node.func.attr not in {"get", "setdefault"}:
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id not in constant_names:
                continue
            if len(node.args) < 2:
                continue

            default = node.args[1]
            if isinstance(default, ast.Constant) and default.value in {
                None,
                0,
                "",
            }:
                continue

            method = node.func.attr
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="suspicious-fallbacks",
                    message=(
                        f"{node.func.value.id}.{method}() has a non-trivial fallback default — "
                        f"if the key should always exist, use [] indexing and fail fast"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


# --- exception-flow-control helpers ---


def _inherits_from_exception(class_node: ast.ClassDef) -> bool:
    """Check if a class inherits from an exception type."""
    for base in class_node.bases:
        name = None
        if isinstance(base, ast.Name):
            name = base.id
        elif isinstance(base, ast.Attribute):
            name = base.attr
        if name is not None:
            if name.endswith(("Error", "Exception")) or name == "BaseException":
                return True
    return False


def _collect_exception_classes_in_scope(tree: ast.AST) -> set[str]:
    """Collect exception class names defined anywhere in the given AST scope."""
    exceptions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _inherits_from_exception(node):
            exceptions.add(node.name)
    return exceptions


def _collect_raised_in_try_body(body: list[ast.stmt]) -> set[str]:
    """Collect exception names raised within a try body, skipping nested scopes."""
    raised: set[str] = set()
    todo: list[ast.AST] = list(body)
    while todo:
        child = todo.pop()
        if isinstance(child, ast.Raise) and child.exc:
            exc = child.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                raised.add(exc.func.id)
            elif isinstance(exc, ast.Name):
                raised.add(exc.id)
        for sub in ast.iter_child_nodes(child):
            if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                todo.append(sub)
    return raised


def _collect_caught_names(handlers: list[ast.ExceptHandler]) -> set[str]:
    """Collect exception names caught by except handlers."""
    caught: set[str] = set()
    for handler in handlers:
        if handler.type is None:
            continue
        if isinstance(handler.type, ast.Name):
            caught.add(handler.type.id)
        elif isinstance(handler.type, ast.Tuple):
            for elt in handler.type.elts:
                if isinstance(elt, ast.Name):
                    caught.add(elt.id)
    return caught


@check(
    "exception-flow-control",
    severity=Severity.MEDIUM,
    description="Custom exceptions raised and caught in the same function for flow control",
)
def check_exception_flow_control(ctx: AnalysisContext) -> list[Finding]:
    """Find custom exceptions used as goto/control flow within a single function.

    When a locally-defined exception is raised in a try body and caught in
    the same try's except handler, it's being used as a goto — the exception
    represents a jump, not an error. Consider return/break/flag instead.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        local_exceptions = _collect_exception_classes_in_scope(tree)
        if not local_exceptions:
            continue

        for func_node in ast.walk(tree):
            if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Also collect exceptions defined inside this function
            func_exceptions: set[str] = set()
            for node in ast.walk(func_node):
                if isinstance(node, ast.ClassDef) and _inherits_from_exception(node):
                    func_exceptions.add(node.name)
            all_exceptions = local_exceptions | func_exceptions

            for node in ast.walk(func_node):
                if not isinstance(node, ast.Try):
                    continue

                raised = _collect_raised_in_try_body(node.body)
                caught = _collect_caught_names(node.handlers)
                flow_control = raised & caught & all_exceptions

                for exc_name in sorted(flow_control):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="exception-flow-control",
                            message=(
                                f"{exc_name} is raised and caught within "
                                f"{func_node.name}() — consider using "
                                f"return/break/flag instead"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

    return findings


# --- unreachable-after-return helpers ---


def _is_terminating_block(stmts: list[ast.stmt]) -> bool:
    """Check if a block of statements always terminates (return/raise in all paths)."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _is_terminating_block(last.body) and _is_terminating_block(last.orelse)
    return False


def _find_unreachable_in_body(
    stmts: list[ast.stmt], filepath: str, func_name: str
) -> list[Finding]:
    """Check a function body for unreachable tail code after terminators."""
    for i, stmt in enumerate(stmts):
        remaining = len(stmts) - i - 1
        if remaining == 0:
            continue

        if isinstance(stmt, (ast.Return, ast.Raise)):
            kind = "return" if isinstance(stmt, ast.Return) else "raise"
            return [
                Finding(
                    file=filepath,
                    line=stmts[i + 1].lineno,
                    check="unreachable-after-return",
                    message=(
                        f"{remaining} statement(s) in {func_name}() after "
                        f"{kind} at line {stmt.lineno} can never execute"
                    ),
                    severity=Severity.HIGH,
                )
            ]

        if isinstance(stmt, ast.If) and stmt.orelse:
            if _is_terminating_block(stmt.body) and _is_terminating_block(stmt.orelse):
                return [
                    Finding(
                        file=filepath,
                        line=stmts[i + 1].lineno,
                        check="unreachable-after-return",
                        message=(
                            f"{remaining} statement(s) in {func_name}() after "
                            f"exhaustive if/else at line {stmt.lineno} "
                            f"can never execute"
                        ),
                        severity=Severity.HIGH,
                    )
                ]

    return []


@check(
    "unreachable-after-return",
    severity=Severity.HIGH,
    description="Code after return/raise that can never execute",
)
def check_unreachable_after_return(ctx: AnalysisContext) -> list[Finding]:
    """Find code after unconditional return/raise or exhaustive if/else branches.

    When someone refactors a function to return early in each branch, the
    original code at the bottom becomes unreachable. This dead tail code
    accumulates silently because each branch individually looks correct.
    """
    findings = []
    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            findings.extend(_find_unreachable_in_body(node.body, str(filepath), node.name))
    return findings
