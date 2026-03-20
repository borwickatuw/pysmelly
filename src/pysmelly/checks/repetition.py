"""Cross-file repetition checks — find patterns repeated across 3+ files."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

TRIVIAL_VALUES = frozenset({None, True, False, 0, 1, -1, 2, 0.0, 1.0, "", b""})

TRIVIAL_STRINGS = frozenset(
    {
        # Encodings
        "utf-8",
        "utf8",
        "ascii",
        "latin-1",
        "latin1",
        # Python idioms
        "__main__",
        # Argparse action constants
        "store_true",
        "store_false",
        "store_const",
        "append",
        "count",
        # HTTP methods
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
        # HTTP headers
        "Content-Type",
        "content-type",
        "Content-Length",
        "content-length",
        "Authorization",
        "authorization",
        "Accept",
        "accept",
        "Cache-Control",
        "cache-control",
        "ETag",
        "etag",
        "Location",
        "location",
        "Content-Disposition",
        "content-disposition",
        "X-Requested-With",
        # Common media types
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/octet-stream",
        "application/pdf",
        "text/html",
        "text/plain",
        "text/xml",
        "text/csv",
        "multipart/form-data",
        "image/png",
        "image/jpeg",
    }
)

# Numbers too common to be interesting across files
TRIVIAL_NUMBERS = frozenset(
    {
        # HTTP status codes
        200,
        201,
        204,
        301,
        302,
        304,
        400,
        401,
        403,
        404,
        500,
        502,
        503,
        # Single-digit integers (almost always coincidental across files)
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        # Common powers of 2 (buffer sizes, field lengths)
        64,
        128,
        256,
        512,
        1024,
        2048,
        4096,
        # Round numbers (pagination, limits)
        10,
        100,
        1000,
        10000,
        # Common timeouts/durations in seconds
        60,
        300,
        3600,
        86400,
    }
)

STDLIB_TYPES = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "list",
        "dict",
        "tuple",
        "set",
        "frozenset",
        "type",
        "object",
        "Exception",
        "BaseException",
        "Path",
        "datetime",
        "date",
        "time",
        "timedelta",
        "Decimal",
        "UUID",
        "Pattern",
        "Match",
        "Callable",
        "Iterator",
        "Generator",
        "Sequence",
        "Mapping",
        "MutableMapping",
        "Iterable",
        "AsyncIterator",
        "Coroutine",
        "NoneType",
        "complex",
        "memoryview",
        "bytearray",
        "range",
        "slice",
        "property",
        "classmethod",
        "staticmethod",
        "super",
    }
)

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})

# Dict-access methods where the first positional arg is a data-schema key,
# not a developer choice worth extracting to a named constant.
DICT_ACCESS_METHODS = frozenset({"get", "pop", "setdefault"})


def _is_dict_access_key(node: ast.Constant, call: ast.Call) -> bool:
    """Check if a constant is the first positional arg to a dict-access method."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr in DICT_ACCESS_METHODS):
        return False
    return len(call.args) >= 1 and call.args[0] is node


def _is_migration_file(filepath: Path) -> bool:
    """Check if a file is a Django migration (migrations/0001_*.py pattern)."""
    parts = filepath.parts
    for i, part in enumerate(parts):
        if part == "migrations" and i + 1 < len(parts):
            # Next part is the filename — Django migrations start with digits
            return parts[i + 1][:1].isdigit()
    return False


def _is_trivial(value: object) -> bool:
    """Check if a constant value is too common to be interesting."""
    if value in TRIVIAL_VALUES:
        return True
    if isinstance(value, str):
        if len(value) <= 2:
            return True
        if value in TRIVIAL_STRINGS:
            return True
    if isinstance(value, int) and value in TRIVIAL_NUMBERS:
        return True
    return False


def _is_assignment_to_all(node: ast.AST) -> bool:
    """Check if a node is an Assign to __all__."""
    if not isinstance(node, ast.Assign):
        return False
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id == "__all__":
            return True
    return False


def _is_log_call(node: ast.AST) -> bool:
    """Check if a Call node is a logging call (logger.info, logging.warning, etc.)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        return True
    return False


def _is_interesting_constant_context(
    node: ast.Constant, parent: ast.AST, grandparent: ast.AST | None
) -> bool:
    """Check if a constant is in a context worth flagging (assignment, comparison, etc.)."""
    # Assignment value (but not __all__)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        if isinstance(parent, ast.Assign) and _is_assignment_to_all(parent):
            return False
        return True

    # List/tuple element inside __all__ assignment
    if isinstance(parent, (ast.List, ast.Tuple)) and grandparent is not None:
        if _is_assignment_to_all(grandparent):
            return False

    # Comparator
    if isinstance(parent, ast.Compare):
        return True

    # Subscript slice (d["key"]) — skipped: dict keys are often API contracts
    # or data-schema fields, not scattered constants worth extracting.

    # Default parameter value
    if isinstance(parent, ast.arguments):
        if node in parent.defaults or node in parent.kw_defaults:
            return True

    # Keyword argument value (but not in log calls or dict-access methods)
    if isinstance(parent, ast.keyword):
        if grandparent is not None and _is_log_call(grandparent):
            return False
        if isinstance(grandparent, ast.Call) and _is_dict_access_key(node, grandparent):
            return False
        return True

    # First positional arg to dict-access methods (config.get("key"), d.pop("id"))
    if isinstance(parent, ast.Call) and _is_dict_access_key(node, parent):
        return False

    return False


def _get_negative_value(node: ast.AST, parent: ast.AST | None) -> object | None:
    """If node is a Constant inside UnaryOp(USub), return the negated value."""
    if parent is None:
        return None
    if not isinstance(parent, ast.UnaryOp):
        return None
    if not isinstance(parent.op, ast.USub):
        return None
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, (int, float)):
        return -node.value
    return None


@check(
    "scattered-constants",
    severity=Severity.LOW,
    description="Same literal value appears in assignments/comparisons across 3+ files",
)
def check_scattered_constants(ctx: AnalysisContext) -> list[Finding]:
    """Find literal values repeated in 3+ files in assignment/comparison contexts."""
    findings = []
    # key: (type_name, repr_value), value: list of (filepath, line)
    occurrences: dict[tuple[str, str], list[tuple[Path, int]]] = defaultdict(list)

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath) or _is_migration_file(filepath):
            continue

        parents = ctx.parent_map(tree)
        # Track which values we've already recorded for this file
        seen_in_file: set[tuple[str, str]] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue

            value = node.value
            parent = parents.get(node)
            if parent is None:
                continue
            grandparent = parents.get(parent)

            # Check for negative numbers: UnaryOp(USub, Constant)
            # The constant itself isn't interesting, we handle it from the parent
            neg_val = _get_negative_value(node, parent)
            if neg_val is not None:
                # This constant is inside a negation — skip, handled below
                continue

            if not _is_interesting_constant_context(node, parent, grandparent):
                continue

            if _is_trivial(value):
                continue

            key = (type(value).__name__, repr(value))
            if key not in seen_in_file:
                seen_in_file.add(key)
                occurrences[key].append((filepath, node.lineno))

        # Also check for negative number literals via UnaryOp
        for node in ast.walk(tree):
            if not isinstance(node, ast.UnaryOp):
                continue
            if not isinstance(node.op, ast.USub):
                continue
            if not isinstance(node.operand, ast.Constant):
                continue
            if not isinstance(node.operand.value, (int, float)):
                continue

            neg_value = -node.operand.value
            if neg_value in TRIVIAL_VALUES:
                continue

            parent = parents.get(node)
            if parent is None:
                continue
            grandparent = parents.get(parent)

            if not _is_interesting_constant_context(node, parent, grandparent):
                continue

            key = (type(neg_value).__name__, repr(neg_value))
            if key not in seen_in_file:
                seen_in_file.add(key)
                occurrences[key].append((filepath, node.lineno))

    for key, locs in sorted(occurrences.items()):
        if len(locs) < 3:
            continue
        type_name, repr_value = key
        locs_sorted = sorted(locs, key=lambda x: str(x[0]))
        loc_strs = [f"{loc[0]}:{loc[1]}" for loc in locs_sorted]
        display = repr_value if len(repr_value) <= 40 else repr_value[:37] + "..."
        findings.append(
            Finding(
                file=str(locs_sorted[0][0]),
                line=locs_sorted[0][1],
                check="scattered-constants",
                message=(
                    f"Literal {display} appears in {len(locs)} files "
                    f"({', '.join(loc_strs)}) — consider a named constant"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


@check(
    "scattered-isinstance",
    severity=Severity.MEDIUM,
    description="isinstance checks for project-defined types scattered across 3+ files",
)
def check_scattered_isinstance(ctx: AnalysisContext) -> list[Finding]:
    """Find isinstance/issubclass checks for project types repeated in 3+ files."""
    findings = []

    # Build project class set — skip classes defined in multiple files (ambiguous)
    class_defs: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_defs[node.name].append((filepath, node.lineno))

    project_classes: dict[str, tuple[Path, int]] = {}
    for name, defs in class_defs.items():
        if len(defs) == 1:
            project_classes[name] = defs[0]

    # Collect isinstance/issubclass calls per class
    # key: class_name, value: list of (filepath, line)
    isinstance_locs: dict[str, list[tuple[Path, int]]] = defaultdict(list)

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        seen_in_file: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name) and node.func.id in ("isinstance", "issubclass")
            ):
                continue
            if len(node.args) < 2:
                continue

            target = node.args[1]
            names: list[str] = []

            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
                    elif isinstance(elt, ast.Attribute):
                        names.append(elt.attr)
            elif isinstance(target, ast.Attribute):
                names.append(target.attr)

            for name in names:
                if name in STDLIB_TYPES:
                    continue
                if name not in project_classes:
                    continue
                if name not in seen_in_file:
                    seen_in_file.add(name)
                    isinstance_locs[name].append((filepath, node.lineno))

    for class_name, locs in sorted(isinstance_locs.items()):
        if len(locs) < 3:
            continue
        locs_sorted = sorted(locs, key=lambda x: str(x[0]))
        loc_strs = [f"{loc[0]}:{loc[1]}" for loc in locs_sorted]
        # Anchor at class definition
        def_path, def_line = project_classes[class_name]
        findings.append(
            Finding(
                file=str(def_path),
                line=def_line,
                check="scattered-isinstance",
                message=(
                    f"isinstance(x, {class_name}) checks appear in {len(locs)} files "
                    f"({', '.join(loc_strs)}) — consider polymorphism or a protocol"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


# --- shotgun-surgery ---

# Common attribute names that are too generic to be meaningful
COMMON_ATTRS = frozenset(
    {
        "name",
        "id",
        "value",
        "data",
        "key",
        "type",
        "path",
        "status",
        "result",
        "error",
        "message",
        "code",
        "text",
        "title",
        "label",
        "description",
        "url",
        "file",
        "line",
        "index",
        "count",
        "size",
        "length",
        "width",
        "height",
        "start",
        "end",
        "args",
        "kwargs",
        "config",
        "settings",
        "options",
        "params",
        "body",
        "content",
        "items",
        "values",
        "keys",
        "fields",
        "attrs",
        "info",
        "meta",
        "context",
        "state",
        "format",
        "mode",
        "level",
        "version",
        "default",
        # AST node attributes (very common in AST-walking code)
        "attr",
        "func",
        "lineno",
        "ctx",
        "targets",
        "bases",
        "keywords",
        "handlers",
        "decorator_list",
        "ops",
        "left",
        "right",
        "operand",
        "op",
        "arg",
        "module",
        "names",
        "slice",
        "elts",
        "comparators",
        "orelse",
        "test",
        "returns",
        "parent",
        # Method-like accesses too generic to be meaningful
        "append",
        "extend",
        "get",
        "set",
        "update",
        "add",
        "remove",
        "pop",
        "clear",
        "close",
        "read",
        "write",
        "send",
        # ORM/model field access (stable API, not design-level coupling)
        "pk",
        "id",
        "slug",
        "save",
        "delete",
        "filter",
        "exclude",
        "create",
        "all",
        "exists",
        "first",
        "last",
        "order_by",
        "select_related",
        "prefetch_related",
        "objects",
        "queryset",
        # Web framework (request/response/timezone — stable APIs)
        "user",
        "method",
        "session",
        "headers",
        "now",
        "filename",
        "add_argument",
    }
)


_STDLIB_MODULES = frozenset(
    {
        "ast",
        "os",
        "sys",
        "re",
        "io",
        "json",
        "math",
        "time",
        "logging",
        "pathlib",
        "typing",
        "collections",
        "functools",
        "itertools",
        "operator",
        "abc",
        "enum",
        "dataclasses",
        "subprocess",
        "shutil",
        "tempfile",
        "urllib",
        "http",
        "socket",
        "threading",
        "multiprocessing",
        "datetime",
        "hashlib",
        "hmac",
        "copy",
        "inspect",
        "importlib",
        "contextlib",
        "textwrap",
        "string",
        "struct",
        "signal",
        "asyncio",
        "unittest",
        "pytest",
        "np",
        "pd",
        "tf",
        "torch",
    }
)


@check(
    "shotgun-surgery",
    severity=Severity.MEDIUM,
    description="Same obj.attr accessed in 4+ files — change propagation risk",
)
def check_shotgun_surgery(ctx: AnalysisContext) -> list[Finding]:
    """Find attribute accesses repeated across many files."""
    findings = []
    min_files = 4

    # Collect (var_name, attr_name) -> set of (file, line)
    accesses: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        # Track per-file to dedup
        seen_in_file: set[tuple[str, str]] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            if not isinstance(node.value, ast.Name):
                continue

            var_name = node.value.id
            attr_name = node.attr

            # Skip self/cls
            if var_name in ("self", "cls"):
                continue
            # Skip private attrs
            if attr_name.startswith("_"):
                continue
            # Skip common attrs
            if attr_name in COMMON_ATTRS:
                continue
            # Skip stdlib module attrs (ast.Name, os.path, etc.)
            if var_name in _STDLIB_MODULES:
                continue
            # Skip uppercase attr access (enum constants: Severity.HIGH)
            if attr_name[0].isupper():
                continue

            key = (var_name, attr_name)
            if key not in seen_in_file:
                seen_in_file.add(key)
                file_str = str(filepath)
                if file_str not in accesses[key]:
                    accesses[key][file_str] = node.lineno

    for (var_name, attr_name), file_lines in sorted(accesses.items()):
        if len(file_lines) < min_files:
            continue

        sorted_files = sorted(file_lines.items())
        loc_strs = [f"{f}:{line}" for f, line in sorted_files[:5]]
        if len(sorted_files) > 5:
            loc_strs.append("...")

        findings.append(
            Finding(
                file=sorted_files[0][0],
                line=sorted_files[0][1],
                check="shotgun-surgery",
                message=(
                    f"{var_name}.{attr_name} accessed in {len(file_lines)}"
                    f" files ({', '.join(loc_strs)})"
                    f" — changes to .{attr_name} require updating many files"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
