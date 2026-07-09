"""Logging smells.

Secrets flowing into log lines, and logging configuration performed
where it hijacks or multiplies — import-time basicConfig() in library
modules, per-call addHandler().
"""

from __future__ import annotations

import ast
import re

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

# --- secrets-in-logs ---

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# Matched as exact snake_case segments so counts like max_tokens (plural
# "tokens") and object keys like s3_key don't fire.
_SECRET_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "secrets",
        "token",
        "cvv",
        "cvc",
        "ssn",
        "credentials",
        "creds",
    }
)

# Matched as substrings of the lowered name (multi-segment idioms).
_SECRET_SUBSTRINGS = (
    "api_key",
    "apikey",
    "private_key",
    "secret_key",
    "card_number",
    "cardnumber",
    "credit_card",
    "social_security",
)

# A trailing metadata segment means the value is *about* the secret, not
# the secret itself: token_id, api_key_name, password_expiry.
_METADATA_SUFFIXES = frozenset(
    {
        "id",
        "ids",
        "name",
        "names",
        "count",
        "size",
        "len",
        "length",
        "type",
        "kind",
        "status",
        "url",
        "path",
        "file",
        "filename",
        "prefix",
        "suffix",
        "expiry",
        "expires",
        "ttl",
        "age",
        "scope",
        "scopes",
    }
)

# Calls that reduce or redact a secret — logging their result is fine.
_SANITIZERS = frozenset(
    {
        "len",
        "type",
        "bool",
        "hash",
        "mask",
        "masked",
        "redact",
        "redacted",
        "sanitize",
        "scrub",
        "hexdigest",
    }
)


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    segments = lowered.split("_")
    if segments[-1] in _METADATA_SUFFIXES:
        return False
    if any(part in lowered for part in _SECRET_SUBSTRINGS):
        return True
    return any(seg in _SECRET_SEGMENTS for seg in segments)


def _receiver_looks_like_logger(func: ast.Attribute) -> bool:
    """True when the call receiver is named like a logger (logger, log,
    self._logger, ...)."""
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    if isinstance(receiver, ast.Attribute):
        return "log" in receiver.attr.lower()
    return False


def _is_logger_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and _receiver_looks_like_logger(node.func)
    )


def _collect_secret_names(node: ast.AST, found: list[str]) -> None:
    """Collect secret-shaped names whose *value* flows into the expression.

    Extracting a named non-secret field from a secret-named container
    (``api_token.name``, ``token["scope"]``, ``token.get("scope")``) is
    metadata access, not a leak, so those receivers are not descended
    into. Sanitizing calls (``len(password)``, ``mask(card_number)``)
    are skipped entirely.
    """
    if isinstance(node, ast.Name):
        if _is_secret_name(node.id):
            found.append(node.id)
        return
    if isinstance(node, ast.Attribute):
        if _is_secret_name(node.attr):
            found.append(node.attr)
        elif not isinstance(node.value, ast.Name):
            _collect_secret_names(node.value, found)
        return
    if isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if _is_secret_name(node.slice.value):
                found.append(node.slice.value)
            return
        _collect_secret_names(node.value, found)
        _collect_secret_names(node.slice, found)
        return
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr
        else:
            func_name = None
        if func_name in _SANITIZERS:
            return
        if isinstance(func, ast.Attribute):
            if (
                func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                # dict.get("key") — same as a string-keyed subscript
                if _is_secret_name(node.args[0].value):
                    found.append(node.args[0].value)
                return
            # Method call: the result still derives from the receiver
            _collect_secret_names(func.value, found)
        for arg in node.args:
            _collect_secret_names(arg, found)
        for kw in node.keywords:
            if kw.value is not None:
                _collect_secret_names(kw.value, found)
        return
    for child in ast.iter_child_nodes(node):
        _collect_secret_names(child, found)


def _secret_names_in(expr: ast.expr) -> list[str]:
    found: list[str] = []
    _collect_secret_names(expr, found)
    return found


@check(
    "secrets-in-logs",
    severity=Severity.HIGH,
    description="Secret-shaped values (password, token, card number) in log calls",
)
def check_secrets_in_logs(ctx: AnalysisContext) -> list[Finding]:
    """Find secret-shaped names flowing into logging call arguments.

    Log archives outlive requests, get shipped to third parties, and are
    readable by people who can't read the database. A card number or
    token in an f-string handed to ``logger.info`` is a durable leak
    even when the code around it is otherwise correct.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_logger_call(node)):
                continue
            secrets: list[str] = []
            for arg in node.args:
                secrets.extend(_secret_names_in(arg))
            for kw in node.keywords:
                if kw.value is not None:
                    secrets.extend(_secret_names_in(kw.value))
            if not secrets:
                continue
            unique = sorted(set(secrets))
            method = node.func.attr  # type: ignore[union-attr]
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="secrets-in-logs",
                    message=(
                        f"{', '.join(unique)} flows into a .{method}() log call"
                        f" — logs outlive requests and leak to log readers;"
                        f" redact or drop the value"
                    ),
                    severity=Severity.HIGH,
                )
            )

    return findings


# --- logging-config-hijack ---


def _is_basicconfig_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "basicConfig"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        )
    return isinstance(func, ast.Name) and func.id == "basicConfig"


def _has_main_guard(tree: ast.Module) -> bool:
    """True if the module has an ``if __name__ == "__main__"`` block —
    a script entry point rather than a pure library module."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        for child in ast.walk(node.test):
            if isinstance(child, ast.Name) and child.id == "__name__":
                return True
    return False


_ENTRY_POINT_NAMES = frozenset({"__main__.py", "manage.py", "conftest.py"})

# Function names that are by convention called once at process startup.
_SETUP_FUNC_RE = re.compile(r"setup|config|init|bootstrap|^main$", re.IGNORECASE)


def _adds_null_handler(node: ast.Call) -> bool:
    """True for ``addHandler(logging.NullHandler())`` — the recommended
    library pattern, not a smell."""
    for arg in node.args:
        if not isinstance(arg, ast.Call):
            continue
        func = arg.func
        name = func.attr if isinstance(func, ast.Attribute) else None
        if name is None and isinstance(func, ast.Name):
            name = func.id
        if name == "NullHandler":
            return True
    return False


def _has_once_guard(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the function has an ``if <flag>: return`` early exit —
    the once-flag idempotency pattern (``if _initialized: return``).
    Only bare returns count; ``if cached: return cached`` is a normal
    branch, not a guard."""
    for stmt in ast.walk(func):
        if not isinstance(stmt, ast.If):
            continue
        if not (stmt.body and isinstance(stmt.body[0], ast.Return)):
            continue
        if stmt.body[0].value is not None:
            continue
        test = stmt.test
        if isinstance(test, ast.UnaryOp):
            test = test.operand
        if isinstance(test, (ast.Name, ast.Attribute)):
            return True
    return False


def _handler_guarded(node: ast.Call, parent_map: dict) -> bool:
    """True when the addHandler call sits under an ``if`` that inspects
    existing handlers (``if not logger.handlers``, ``hasHandlers()``)."""
    current: ast.AST | None = node
    while current is not None:
        current = parent_map.get(current)
        if isinstance(current, ast.If):
            for child in ast.walk(current.test):
                if isinstance(child, ast.Attribute) and child.attr in {
                    "handlers",
                    "hasHandlers",
                }:
                    return True
        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            break
    return False


@check(
    "logging-config-hijack",
    severity=Severity.MEDIUM,
    description="basicConfig() at import time in a library, or addHandler() per call",
)
def check_logging_config_hijack(ctx: AnalysisContext) -> list[Finding]:
    """Find logging configuration performed in the wrong place.

    ``logging.basicConfig()`` at module scope in a library module
    silently reconfigures logging for every application that imports
    it. ``addHandler()`` inside a function body attaches a new handler
    on every call, so each log line prints once per call ever made.
    Configuration belongs in the application entry point, once.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        is_entry_point = filepath.name in _ENTRY_POINT_NAMES or _has_main_guard(tree)
        if not is_entry_point:
            for stmt in tree.body:
                if isinstance(stmt, ast.Expr) and _is_basicconfig_call(stmt.value):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=stmt.lineno,
                            check="logging-config-hijack",
                            message=(
                                "logging.basicConfig() at import time in a"
                                " library module — hijacks the importing"
                                " application's logging config; configure"
                                " logging in the entry point instead"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

        parent_map: dict | None = None
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addHandler"
            ):
                continue
            if _adds_null_handler(node):
                continue
            if parent_map is None:
                parent_map = ctx.parent_map(tree)
            # Only function-body calls multiply; module-scope addHandler
            # runs once per import and is normal library setup.
            enclosing_func: ast.FunctionDef | ast.AsyncFunctionDef | None = None
            current: ast.AST | None = parent_map.get(node)
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing_func = current
                    break
                current = parent_map.get(current)
            if enclosing_func is None:
                continue
            # Dedicated setup functions (setup_logging, configure_console,
            # init_logging, main) are the once-per-process place the
            # message tells people to use — flagging them is wrong. The
            # smell is handler-adding in functions called repeatedly
            # (get_logger, request handlers).
            if _SETUP_FUNC_RE.search(enclosing_func.name):
                continue
            if _has_once_guard(enclosing_func):
                continue
            if _handler_guarded(node, parent_map):
                continue
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="logging-config-hijack",
                    message=(
                        "addHandler() inside a function body — adds a new"
                        " handler on every call and log lines multiply;"
                        " configure handlers once at startup or guard with"
                        " `if not logger.handlers`"
                    ),
                    severity=Severity.MEDIUM,
                )
            )

    return findings
