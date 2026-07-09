"""Cross-file repetition checks — find patterns repeated across 3+ files."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import is_click_callback_signature, is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import MAX_DISPLAY_WIDTH, Finding, Severity, check

TRIVIAL_VALUES = frozenset({None, True, False, -1, 2, "", b""})

TRIVIAL_STRINGS = frozenset(
    {
        # Encodings
        "utf-8",
        "utf8",
        "ascii",
        "latin-1",
        "latin1",
        # Python idioms
        "self",
        "cls",
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


def _is_trivial(value: object) -> bool:
    """Check if a constant value is too common to be interesting."""
    if value in TRIVIAL_VALUES:
        return True
    if isinstance(value, str):
        if len(value) <= 2:
            return True
        if value in TRIVIAL_STRINGS:
            return True
        # Dunder names (__all__, __init__, etc.) are well-known Python protocol names
        if value.startswith("__") and value.endswith("__") and len(value) >= 5:
            return True
    if isinstance(value, int) and value in TRIVIAL_NUMBERS:
        return True
    return False


def _is_assignment_to_all(node: ast.AST) -> bool:
    """Check if a node is an Assign to __all__."""
    if not isinstance(node, ast.Assign):
        return False
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)


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
    # Assignment value (but not __all__ or default_auto_field)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)):
        if isinstance(parent, ast.Assign) and _is_assignment_to_all(parent):
            return False
        if isinstance(parent, ast.Assign):
            for target in parent.targets:
                if isinstance(target, ast.Name) and target.id == "default_auto_field":
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
        if is_test_file(filepath) or is_migration_file(filepath):
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
        display = (
            repr_value
            if len(repr_value) <= MAX_DISPLAY_WIDTH
            else repr_value[: MAX_DISPLAY_WIDTH - 3] + "..."
        )
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
                isinstance(node.func, ast.Name) and node.func.id in {"isinstance", "issubclass"}
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
        "pk",
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


def _build_library_bound_names(tree: ast.Module, file_imports: set[str]) -> set[str]:
    """Names bound to the return value of an imported library call,
    propagated transitively across chained calls.

    Picks up patterns like::

        log = logging.getLogger(__name__)   # log    → library-bound (1 hop)
        resp = requests.get(url)            # resp   → library-bound (1 hop)
        m = re.match(pattern, text)         # m      → library-bound (1 hop)

        # Chained: PAT is library-bound via re.compile(...), so m is too.
        PAT = re.compile(r"...")            # PAT    → library-bound
        m = PAT.search(text)                # m      → library-bound (2 hops)

    Reads of ``log.warning``/``resp.status_code``/``m.group`` are library API,
    not user-attribute reads we could propagate a refactor through.

    ``file_imports`` is expected to be the LIBRARY-only import set (imports
    of project modules already filtered out), so ``config = make_config()``
    where ``make_config`` is a project function does NOT bind ``config``.

    Fixed-point iteration handles chained calls of arbitrary depth (in
    practice 2-3).
    """
    bound: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            callee = node.value.func
            ref_name: str | None = None
            if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                ref_name = callee.value.id
            elif isinstance(callee, ast.Name):
                ref_name = callee.id
            if ref_name is None:
                continue
            if ref_name not in file_imports and ref_name not in bound:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in bound:
                    bound.add(target.id)
                    changed = True
    return bound


_LOGGING_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)


def _is_cosmetic_read(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> bool:
    """True if the attribute read is purely for display (f-string interpolation
    or argument to a logging-style call).

    The Fowler-original shotgun-surgery question is "if I change the type, who
    breaks?" — but a read inside ``f"...{obj.attr}..."`` doesn't break when
    ``obj.attr`` is renamed (you just adjust the format string with sed). Same
    for ``log.info("hi", obj.attr)`` — the call's contract is "render this
    value." These reads are weaker signal than reads that drive control flow
    or computation.
    """
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, ast.FormattedValue):
            return True
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
            if cur.func.attr in _LOGGING_METHODS:
                return True
        cur = parent_map.get(cur)
    return False


def _is_click_pass_context_decorator(node: ast.expr) -> bool:
    """True for ``@click.pass_context`` or bare ``@pass_context``."""
    if isinstance(node, ast.Attribute) and node.attr == "pass_context":
        return True
    if isinstance(node, ast.Name) and node.id == "pass_context":
        return True
    return False


def _click_context_param_name(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """If ``func`` is a Click callback or has ``@click.pass_context``, return
    the name of its Click ``Context`` parameter — the first positional arg
    after self/cls. Otherwise None.
    """
    is_click_func = is_click_callback_signature(func) or any(
        _is_click_pass_context_decorator(d) for d in func.decorator_list
    )
    if not is_click_func or not func.args.args:
        return None
    first = func.args.args[0].arg
    if first in {"self", "cls"}:
        if len(func.args.args) > 1:
            return func.args.args[1].arg
        return None
    return first


def _enclosing_click_context_param(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> str | None:
    """Return the Click-Context parameter name for the function enclosing
    ``node``, or None when no such function is found.
    """
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _click_context_param_name(cur)
        cur = parent_map.get(cur)
    return None


def _annotation_references_library(ann: ast.expr, file_imports: set[str]) -> bool:
    """True if a type annotation names a library-imported type, recursively
    looking through Optional/Union/Subscript wrappers and ``X | Y`` unions.

    Examples (with ``import click`` / ``from requests import Response`` in scope)::

        click.Context              → True  (click in file_imports)
        Response                   → True  (Response in file_imports)
        Optional[click.Context]    → True
        click.Context | None       → True
        Union[click.Context, str]  → True
        AnalysisContext            → False (project import, filtered out of file_imports)
    """
    if isinstance(ann, ast.Name):
        return ann.id in file_imports
    if isinstance(ann, ast.Attribute) and isinstance(ann.value, ast.Name):
        return ann.value.id in file_imports
    if isinstance(ann, ast.Subscript):
        slice_node = ann.slice
        if isinstance(slice_node, ast.Tuple):
            return any(_annotation_references_library(e, file_imports) for e in slice_node.elts)
        return _annotation_references_library(slice_node, file_imports)
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _annotation_references_library(
            ann.left, file_imports
        ) or _annotation_references_library(ann.right, file_imports)
    return False


def _library_typed_params(
    func: ast.FunctionDef | ast.AsyncFunctionDef, file_imports: set[str]
) -> set[str]:
    """Parameter names on ``func`` whose annotation references a library type.

    Captures the helper-function pattern: ``def helper(ctx: click.Context)`` or
    ``def handle(resp: requests.Response)``. The parameter is a library object
    by type contract, regardless of where it's called from — reads of its
    attributes are library API.
    """
    params: set[str] = set()
    args = list(func.args.posonlyargs) + list(func.args.args) + list(func.args.kwonlyargs)
    for arg in args:
        if arg.annotation is None:
            continue
        if _annotation_references_library(arg.annotation, file_imports):
            params.add(arg.arg)
    return params


def _enclosing_library_typed_params(
    node: ast.AST,
    parent_map: dict[ast.AST, ast.AST],
    library_typed_by_func_id: dict[int, set[str]],
) -> set[str]:
    """Library-typed parameter names for the nearest enclosing function, or
    the empty set if no enclosing function (or no library-typed params)."""
    cur = parent_map.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return library_typed_by_func_id.get(id(cur), set())
        cur = parent_map.get(cur)
    return set()


def _top_level_library_bound_names(tree: ast.Module, file_imports: set[str]) -> set[str]:
    """Library-bound names defined at the module's top level (importable as
    ``from this_module import X``). Subset of _build_library_bound_names that
    ignores assignments inside functions/classes — those aren't re-exportable.
    """
    bound: set[str] = set()
    changed = True
    while changed:
        changed = False
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            callee = stmt.value.func
            ref_name: str | None = None
            if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                ref_name = callee.value.id
            elif isinstance(callee, ast.Name):
                ref_name = callee.id
            if ref_name is None:
                continue
            if ref_name not in file_imports and ref_name not in bound:
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id not in bound:
                    bound.add(target.id)
                    changed = True
    return bound


def _resolve_module_to_filepath(module_name: str, all_trees: dict[Path, ast.Module]) -> Path | None:
    """Map a dotted module name to a project filepath, if one exists.

    Matches by path suffix: ``pkg.subpkg.module`` looks for a path that ends
    with ``pkg/subpkg/module.py``. Falls back to the leaf name (``module.py``)
    if no longer match exists — picks up cases where the codebase uses a flat
    layout but the import was written with a fuller dotted path.
    """
    target = module_name.replace(".", "/") + ".py"
    for filepath in all_trees:
        if str(filepath).endswith(target):
            return filepath
    leaf = module_name.rsplit(".", 1)[-1] + ".py"
    for filepath in all_trees:
        if filepath.name == leaf:
            return filepath
    return None


def _propagate_reexports(
    all_trees: dict[Path, ast.Module],
    library_bound: dict[str, set[str]],
    top_level_bound: dict[str, set[str]],
) -> None:
    """Add re-exported library symbols to per-file library-bound sets.

    If file A defines ``log = logging.getLogger(__name__)`` at the top level
    and file B does ``from A import log``, then ``log`` in B is effectively
    a library object — attribute reads on it (``log.warning``) are library
    API. Iterates to fixed point so re-exports-of-re-exports also propagate.

    Mutates both maps in place.
    """
    changed = True
    while changed:
        changed = False
        for filepath, tree in all_trees.items():
            fp_str = str(filepath)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                source_fp = _resolve_module_to_filepath(node.module, all_trees)
                if source_fp is None:
                    continue
                source_top = top_level_bound.get(str(source_fp), set())
                if not source_top:
                    continue
                for alias in node.names:
                    if alias.name not in source_top:
                        continue
                    bound_name = alias.asname or alias.name
                    if bound_name not in library_bound[fp_str]:
                        library_bound[fp_str].add(bound_name)
                        top_level_bound[fp_str].add(bound_name)
                        changed = True


def _collect_project_module_names(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Names that identify project modules/packages.

    Includes every analyzed file's stem and every parent-directory component
    of its path (excluding common source-root names like ``src``). Used to
    distinguish ``from pysmelly.checks.helpers import X`` (project import,
    X may be a project function) from ``from logging import getLogger``
    (library import, getLogger is library).
    """
    names: set[str] = set()
    skip_dirs = {"src", "lib"}
    for filepath in all_trees:
        names.add(filepath.stem)
        for part in filepath.parts[:-1]:
            if part not in skip_dirs and part not in {".", ".."}:
                names.add(part)
    return names


def _build_imports_per_file(all_trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """For each file, the set of local names introduced by *library* import
    statements — those whose source module is NOT a project module.

    Used to recognize "the receiver of this attribute read is a directly-imported
    library, not one of our value objects." Change-propagation risk doesn't apply
    to library APIs we don't own.

    Bound names accounted for:
      - ``import X``                       → ``X``
      - ``import X as Y``                  → ``Y``
      - ``import X.Y.Z``                   → ``X`` (the top of the dotted path)
      - ``import X.Y.Z as W``              → ``W``
      - ``from A import B``                → ``B``
      - ``from A import B as C``           → ``C``

    Imports whose source module looks like a project module are excluded so
    that ``from project_models import make_config`` does NOT mark
    ``make_config`` as a library symbol (and therefore won't cause
    ``config = make_config()`` to be treated as library-bound).
    """
    project_modules = _collect_project_module_names(all_trees)
    result: dict[str, set[str]] = {}
    for filepath, tree in all_trees.items():
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    source_top = alias.name.split(".")[0]
                    if source_top in project_modules:
                        continue
                    bound = alias.asname if alias.asname else source_top
                    names.add(bound)
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    # `from . import X` — relative; treat as project import.
                    continue
                source_top = node.module.split(".")[0]
                if source_top in project_modules:
                    continue
                for alias in node.names:
                    bound = alias.asname if alias.asname else alias.name
                    names.add(bound)
        result[str(filepath)] = names
    return result


def _is_trivial_method_body(stmts: list[ast.stmt]) -> bool:
    """A method body is trivial when it's a single statement that's either a
    pass, a constant return, a bare attribute return (``return self.x``), a
    bare-name return, or a container literal (``return {...}``/``[]``/etc.).
    """
    if len(stmts) != 1:
        return False
    stmt = stmts[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True
        return isinstance(
            stmt.value,
            (ast.Constant, ast.Name, ast.Attribute, ast.Dict, ast.List, ast.Tuple, ast.Set),
        )
    return False


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_behavior_bearing_class(cls: ast.ClassDef) -> bool:
    """True if a class has at least 2 non-trivial, non-dunder methods.

    Captures the value-object-with-behavior pattern (``SnapshotRef`` with
    ``s3_key``/``local_dir``/``summary_exists_locally``/...): real methods
    that operate on the data, not just a thin record. Reads of attributes
    on such classes are appropriate object-graph traversal, not the data-
    with-no-behavior shape shotgun-surgery is trying to surface.

    Threshold of 2 (rather than 1) avoids classifying a record with just
    ``to_dict`` or ``__repr__`` as behavior-bearing.
    """
    real_methods = 0
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_dunder(item.name):
            continue
        if _is_trivial_method_body(item.body):
            continue
        real_methods += 1
        if real_methods >= 2:
            return True
    return False


def _collect_attr_to_classes(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[tuple[str, ast.ClassDef]]]:
    """For each attribute name defined in project classes, the list of
    (file, class) pairs that define it (via ``self.X = ...`` or class-level
    annotation)."""
    result: dict[str, list[tuple[str, ast.ClassDef]]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            defines_here: set[str] = set()
            for item in ast.walk(node):
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.ctx, ast.Store)
                    and isinstance(item.value, ast.Name)
                    and item.value.id == "self"
                ):
                    defines_here.add(item.attr)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    defines_here.add(item.target.id)
            for attr_name in defines_here:
                result[attr_name].append((str(filepath), node))
    return result


def _collect_project_defined_attrs(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect attribute names defined in project classes (self.X = ... or annotations).

    Only attributes defined in the analyzed codebase are project-level concerns.
    Framework/stdlib attributes won't appear here.
    """
    attrs: set[str] = set()
    for tree in all_trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in ast.walk(node):
                # self.X = ... assignments
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.ctx, ast.Store)
                    and isinstance(item.value, ast.Name)
                    and item.value.id == "self"
                ):
                    attrs.add(item.attr)
                # Class-level annotations (X: type)
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    attrs.add(item.target.id)
    return attrs


@check(
    "shotgun-surgery",
    severity=Severity.MEDIUM,
    description="Same obj.attr accessed in 4+ files — change propagation risk",
)
def check_shotgun_surgery(ctx: AnalysisContext) -> list[Finding]:
    """Find attribute accesses repeated across many files."""
    findings = []
    min_files = 4

    # Only flag attributes defined in the project, not framework/stdlib APIs
    project_attrs = _collect_project_defined_attrs(ctx.all_trees)
    imports_per_file = _build_imports_per_file(ctx.all_trees)

    # For the behavior-bearing-class downweighter: which classes define each
    # attr, and which of those classes are behavior-bearing.
    attr_to_classes = _collect_attr_to_classes(ctx.all_trees)
    attrs_all_classes_have_behavior: set[str] = {
        attr_name
        for attr_name, classes in attr_to_classes.items()
        if classes and all(_is_behavior_bearing_class(c) for _, c in classes)
    }

    # Precompute per-file library-bound sets, then propagate re-exports
    # (`from project_logging import log` where project_logging.log was bound
    # from logging.getLogger). Done once across all trees because re-exports
    # cross file boundaries.
    library_bound_per_file: dict[str, set[str]] = {}
    top_level_bound_per_file: dict[str, set[str]] = {}
    for filepath, tree in ctx.all_trees.items():
        fp_str = str(filepath)
        file_imports = imports_per_file.get(fp_str, set())
        library_bound_per_file[fp_str] = _build_library_bound_names(tree, file_imports)
        top_level_bound_per_file[fp_str] = _top_level_library_bound_names(tree, file_imports)
    _propagate_reexports(ctx.all_trees, library_bound_per_file, top_level_bound_per_file)

    # Collect (var_name, attr_name) -> set of (file, line)
    accesses: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    # (var_name, attr_name) -> files that WRITE the attribute. Scattered
    # writes are the textbook shotgun-surgery shape (anemic model fields
    # mutated from many call sites), so they're called out in the message.
    write_files: dict[tuple[str, str], set[str]] = defaultdict(set)

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        file_str = str(filepath)
        file_imports = imports_per_file.get(file_str, set())
        library_bound = library_bound_per_file.get(file_str, set())
        parent_map = ctx.parent_map(tree)

        # Per-function map of parameter names whose type annotation is a
        # library type (e.g. `ctx: click.Context`, `resp: requests.Response`).
        # Looked up by id() when processing attribute reads.
        library_typed_by_func_id: dict[int, set[str]] = {}
        for func_node in ast.walk(tree):
            if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                typed = _library_typed_params(func_node, file_imports)
                if typed:
                    library_typed_by_func_id[id(func_node)] = typed

        # Track per-file to dedup
        seen_in_file: set[tuple[str, str]] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            # Reads AND writes both count: a rename breaks either, and
            # scattered writes to the same field are the stronger signal.
            if not isinstance(node.ctx, (ast.Load, ast.Store)):
                continue
            is_write = isinstance(node.ctx, ast.Store)
            if not isinstance(node.value, ast.Name):
                continue

            var_name = node.value.id
            attr_name = node.attr

            # Skip self/cls
            if var_name in {"self", "cls"}:
                continue
            # Skip private attrs
            if attr_name.startswith("_"):
                continue
            # Skip common/framework attrs (stable APIs like .pk, .save, .user)
            if attr_name in COMMON_ATTRS:
                continue
            # Only flag attributes defined in project classes
            if attr_name not in project_attrs:
                continue
            # Skip when the receiver is a name imported in this file —
            # `boto3.client`, `click.group`, etc. are library API, not user
            # attribute reads we could propagate a refactor through.
            if var_name in file_imports:
                continue
            # Skip when the receiver was bound from an imported library call
            # like `log = logging.getLogger(...)` or `m = re.match(...)`.
            if var_name in library_bound:
                continue
            # Skip Click Context parameters inside Click callbacks or
            # @click.pass_context functions — `ctx.invoke`, `ctx.obj`, etc.
            # are Click API, not user attribute reads.
            click_ctx = _enclosing_click_context_param(node, parent_map)
            if click_ctx == var_name:
                continue
            # Skip parameters whose type annotation references a library
            # import — `def helper(ctx: click.Context)` or `def handle(resp:
            # requests.Response)`. The parameter is a library object by
            # contract, even if this helper isn't itself decorated.
            typed_params = _enclosing_library_typed_params(
                node, parent_map, library_typed_by_func_id
            )
            if var_name in typed_params:
                continue
            # Skip when every class defining this attr is behavior-bearing —
            # the read is object-graph traversal through a value object that
            # has real methods, not the anemic-dataclass shape this check is
            # looking for.
            if attr_name in attrs_all_classes_have_behavior:
                continue
            # Skip cosmetic reads — inside f-strings or as args to logging
            # methods. Renaming the attr doesn't break these; they're
            # display-only and weaker signal than reads driving logic.
            # (Writes can't be cosmetic — the filter only applies to reads.)
            if not is_write and _is_cosmetic_read(node, parent_map):
                continue
            # Skip uppercase attr access (enum constants: Severity.HIGH)
            if attr_name[0].isupper():
                continue

            key = (var_name, attr_name)
            if is_write:
                write_files[key].add(file_str)
            if key not in seen_in_file:
                seen_in_file.add(key)
                if file_str not in accesses[key]:
                    accesses[key][file_str] = node.lineno

    for (var_name, attr_name), file_lines in sorted(accesses.items()):
        if len(file_lines) < min_files:
            continue

        sorted_files = sorted(file_lines.items())
        loc_strs = [f"{f}:{line}" for f, line in sorted_files[:5]]
        if len(sorted_files) > 5:
            loc_strs.append("...")

        # Anchor at the attribute's defining class when unambiguous, so the
        # finding maps to the file a fix would start from — not whichever
        # access site happens to sort first alphabetically.
        defining = attr_to_classes.get(attr_name, [])
        if len(defining) == 1:
            def_file, def_class = defining[0]
            anchor_file, anchor_line = def_file, def_class.lineno
            defined_note = f" (defined on {def_class.name})"
        else:
            anchor_file, anchor_line = sorted_files[0]
            defined_note = ""

        n_write_files = len(write_files.get((var_name, attr_name), ()))
        write_note = f", written from {n_write_files}" if n_write_files else ""

        findings.append(
            Finding(
                file=anchor_file,
                line=anchor_line,
                check="shotgun-surgery",
                message=(
                    f"{var_name}.{attr_name}{defined_note} accessed in"
                    f" {len(file_lines)} files{write_note}"
                    f" ({', '.join(loc_strs)})"
                    f" — changes to .{attr_name} require updating many files"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


# --- repeated-string-parsing ---


def _find_split_subscripts(tree: ast.Module) -> list[tuple[str, int, int]]:
    """Find .split(delim)[N] patterns, returning (delimiter, index, lineno) tuples.

    Detects both direct chaining (x.split("|")[1]) and intermediate variable
    patterns (parts = x.split("|") ... parts[1]).
    """
    results: list[tuple[str, int, int]] = []

    # Pattern 1: direct x.split(delim)[N]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if not isinstance(node.slice, ast.Constant):
            continue
        if not isinstance(node.slice.value, int):
            continue

        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != "split":
            continue
        if not call.args:
            continue
        delim_arg = call.args[0]
        if not isinstance(delim_arg, ast.Constant) or not isinstance(delim_arg.value, str):
            continue

        results.append((delim_arg.value, node.slice.value, node.lineno))

    # Pattern 2: parts = x.split(delim) ... parts[N]
    # Collect split-assigned variable names and their delimiters per function
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Map variable name -> delimiter for split assignments in this function
        split_vars: dict[str, str] = {}
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            if not isinstance(node.targets[0], ast.Name):
                continue
            val = node.value
            if not isinstance(val, ast.Call):
                continue
            if not isinstance(val.func, ast.Attribute):
                continue
            if val.func.attr != "split":
                continue
            if not val.args:
                continue
            delim_arg = val.args[0]
            if not isinstance(delim_arg, ast.Constant) or not isinstance(delim_arg.value, str):
                continue
            split_vars[node.targets[0].id] = delim_arg.value

        if not split_vars:
            continue

        # Find subscript access on those variables: parts[N]
        for node in ast.walk(func_node):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            if node.value.id not in split_vars:
                continue
            if not isinstance(node.slice, ast.Constant):
                continue
            if not isinstance(node.slice.value, int):
                continue
            results.append((split_vars[node.value.id], node.slice.value, node.lineno))

    return results


@check(
    "repeated-string-parsing",
    severity=Severity.MEDIUM,
    description="Same .split(delim)[N] pattern in 3+ locations — ad-hoc serialization format",
)
def check_repeated_string_parsing(ctx: AnalysisContext) -> list[Finding]:
    """Find repeated .split(delimiter)[index] patterns suggesting primitive obsession."""
    findings = []

    # Collect (delimiter, index) -> [(file, line), ...]
    occurrences: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        splits = _find_split_subscripts(tree)
        # Dedup per file per (delim, index) pair
        seen: set[tuple[str, int]] = set()

        for delim, idx, lineno in splits:
            key = (delim, idx)
            if key not in seen:
                seen.add(key)
                occurrences[key].append((str(filepath), lineno))

    # Strategy 1: same (delim, index) in 3+ locations
    reported_delims: set[str] = set()
    for (delim, idx), locs in sorted(occurrences.items()):
        if len(locs) < 3:
            continue
        loc_strs = [f"{f}:{line}" for f, line in sorted(locs)[:5]]
        if len(locs) > 5:
            loc_strs.append("...")
        reported_delims.add(delim)
        findings.append(
            Finding(
                file=locs[0][0],
                line=locs[0][1],
                check="repeated-string-parsing",
                message=(
                    f'.split("{delim}")[{idx}] appears in {len(locs)}'
                    f" locations ({', '.join(loc_strs)})"
                    f" — ad-hoc serialization; consider a dataclass"
                ),
                severity=Severity.MEDIUM,
            )
        )

    # Strategy 2: same delimiter with 3+ different indices (parsing a format)
    delim_indices: dict[str, set[int]] = defaultdict(set)
    delim_all_locs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (delim, idx), locs in occurrences.items():
        delim_indices[delim].add(idx)
        delim_all_locs[delim].extend(locs)

    for delim, indices in sorted(delim_indices.items()):
        if len(indices) < 3:
            continue
        if delim in reported_delims:
            continue  # already covered by strategy 1
        locs = delim_all_locs[delim]
        files = sorted({f for f, _ in locs})
        sorted_indices = sorted(indices)
        findings.append(
            Finding(
                file=files[0],
                line=locs[0][1],
                check="repeated-string-parsing",
                message=(
                    f'.split("{delim}") with {len(indices)} different'
                    f" indices ({', '.join(str(i) for i in sorted_indices)})"
                    f" across {len(files)}"
                    f" file{'s' if len(files) != 1 else ''}"
                    f" — ad-hoc format being parsed piecemeal;"
                    f" consider a dataclass"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
