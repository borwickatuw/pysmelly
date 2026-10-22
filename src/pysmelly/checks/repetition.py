"""Cross-file repetition checks — find patterns repeated across 3+ files."""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

from pysmelly.checks.callers import _reaches_module
from pysmelly.checks.entry_points import RootKind
from pysmelly.checks.framework import is_migration_file
from pysmelly.checks.helpers import (
    SOURCE_ROOT_DIRS,
    annotation_arms,
    assigned_pairs,
    bare_target,
    dotted_name,
    enclosing_functions,
    filepath_to_module,
    import_binding,
    import_bindings,
    is_bare_name,
    is_click_callback_signature,
    iter_isinstance_targets,
    iter_simple_assigns,
    own_binding,
    own_scope_nodes,
    referenced_name,
    referenced_names,
    source_trees,
    top_level_package,
    type_expr_names,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import (
    MAX_DISPLAY_WIDTH,
    Advice,
    Finding,
    Severity,
    Site,
    check,
    multi_site_finding,
)

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


def _is_dict_access_key(node: ast.expr, call: ast.Call) -> bool:
    """Check if a constant expression is the first positional arg to a dict-access method."""
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


def _is_log_call(node: ast.AST) -> bool:
    """Check if a Call node is a logging call (logger.info, logging.warning, etc.)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        return True
    return False


# Assignment targets whose literal value repeats across files by design:
# ``__all__`` is a module protocol, ``default_auto_field`` a Django setting.
_EXEMPT_ASSIGN_TARGETS = frozenset({"__all__", "default_auto_field"})


# Assignments whose value is a Django identifier, by where they sit: the app
# registry reads ``name`` (the app's module path) and ``label`` off an
# AppConfig, and ``include()`` reads a URLconf's ``app_name`` as its URL
# namespace. By convention all of them spell the app's label, so they repeat
# across an app's files without being one value (36-3).
_DJANGO_IDENTIFIER_TARGETS = {
    RootKind.DJANGO_APP_CONFIG: frozenset({"name", "label"}),
    RootKind.DJANGO_URLCONF: frozenset({"app_name"}),
}


def _django_identifier_assigns(ctx: AnalysisContext) -> set[ast.stmt]:
    """Every assignment of a Django identifier, by the entry-point index.

    An AppConfig is a ``django app config`` root (a subclass of
    ``django.apps.AppConfig`` or of a project class that is one), and its
    class body holds the attributes; a URLconf is a whole-module root, and
    its module body holds ``app_name``.
    """
    assigns: set[ast.stmt] = set()
    for root in ctx.entry_points.roots:
        targets = _DJANGO_IDENTIFIER_TARGETS.get(root.kind)
        if targets is None or root.file is None:
            continue
        owner = root.node if isinstance(root.node, ast.ClassDef) else ctx.all_trees[root.file]
        assigns.update(
            stmt
            for stmt in owner.body
            if any(
                isinstance(target, ast.Name) and target.id in targets
                for target, _value in assigned_pairs(stmt)
            )
        )
    return assigns


def _flags_assign_value(_node: ast.expr, parent: ast.Assign, _grandparent: ast.AST | None) -> bool:
    """An assigned literal is interesting unless its target is exempt."""
    return not any(
        isinstance(target, ast.Name) and target.id in _EXEMPT_ASSIGN_TARGETS
        for target in parent.targets
    )


def _flags_default_value(
    node: ast.expr, parent: ast.arguments, _grandparent: ast.AST | None
) -> bool:
    """A parameter default is interesting; nothing else in a signature is.

    An annotation is also a child of ``ast.arguments``, so membership in the
    two default lists — by identity, as everywhere in this module — is what
    separates ``def f(x=42)`` from ``def f(x: Literal[42])``.
    """
    return node in parent.defaults or node in parent.kw_defaults


def _flags_keyword_value(node: ast.expr, _parent: ast.keyword, grandparent: ast.AST | None) -> bool:
    """A keyword argument's literal is interesting outside two call shapes.

    Log calls (``log.info(..., extra=...)``) and dict-access methods
    (``config.get("key", default=...)``) carry API strings, not constants a
    reader would want named.
    """
    if grandparent is not None and _is_log_call(grandparent):
        return False
    return not (isinstance(grandparent, ast.Call) and _is_dict_access_key(node, grandparent))


# Parent node types whose literal child is interesting with no further test.
_ALWAYS_INTERESTING_PARENTS = frozenset({ast.AnnAssign, ast.Compare})

# Parent node type -> the predicate deciding whether its literal child counts.
# Exact-type dispatch: every key is a concrete ast node class, so one dict
# lookup replaces the chain of isinstance() tests this used to be.
_ContextPredicate = Callable[[ast.expr, Any, ast.AST | None], bool]
_CONSTANT_CONTEXT_PREDICATES: dict[type[ast.AST], _ContextPredicate] = {
    ast.Assign: _flags_assign_value,
    ast.arguments: _flags_default_value,
    ast.keyword: _flags_keyword_value,
}


def _is_interesting_constant_context(
    node: ast.expr, parent: ast.AST, grandparent: ast.AST | None
) -> bool:
    """Check if a constant expression is in a context worth flagging.

    ``node`` is the whole literal expression — an ``ast.Constant``, or the
    ``ast.UnaryOp`` wrapping a negative literal — and is only ever compared by
    identity against the parent's children.

    Every parent type absent from both tables answers False, which covers the
    three positions this check deliberately stays out of: a subscript slice
    (``d["key"]`` — dict keys are API contracts or schema fields), a positional
    call argument, and a list or tuple element.
    """
    parent_type = type(parent)
    if parent_type in _ALWAYS_INTERESTING_PARENTS:
        return True
    predicate = _CONSTANT_CONTEXT_PREDICATES.get(parent_type)
    if predicate is None:
        return False
    return predicate(node, parent, grandparent)


def _constant_name(node: ast.expr, parent: ast.AST, grandparent: ast.AST | None) -> str | None:
    """The UPPER_CASE name ``node`` is bound to at module or class level, else None.

    ``MIN_TOKENS = 40`` has already given 40 a name, so it is not a scattered
    use of the value: scattered-constants counts it only against definitions
    of the same name, which catches one constant copied into several modules.
    A bare ``40`` that should have used the name is unnamed-constant-use's
    business, which matches by role rather than by value.
    """
    if not isinstance(grandparent, (ast.Module, ast.ClassDef)):
        return None
    if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
        target = parent.targets[0]
    elif isinstance(parent, ast.AnnAssign):
        target = parent.target
    else:
        return None
    if parent.value is not node or not isinstance(target, ast.Name) or not target.id.isupper():
        return None
    return target.id


def _negated_literal(node: ast.AST) -> int | float | None:
    """The value a ``-<number>`` expression evaluates to, else None.

    ``ast`` has no negative literals: ``-1`` parses as ``USub`` over the
    constant ``1``, so both halves of scattered-constants read the value from
    here rather than from the constant the walk hands them.
    """
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.USub):
        return None
    operand = node.operand
    if not isinstance(operand, ast.Constant) or not isinstance(operand.value, (int, float)):
        return None
    return -operand.value


# scattered-constants and scattered-isinstance report a pattern once it is in
# this many files.
MIN_SCATTERED_FILES = 3

_BUILTIN_NAMES = frozenset(dir(builtins))


class _LibraryCalls:
    """Whether a call resolves outside the analyzed code (Phase 33-2).

    A literal passed by keyword to such a call (``ConfigDict(extra="allow")``,
    ``click.option(..., metavar="FILE")``) fills a library's parameter: it is
    that API's contract, not a domain constant. Names resolve by the Phase
    21-3 rule, through the module's own import bindings:

    - a callee whose root name an import binds is the library's when no
      prefix of the dotted path it names reaches an analyzed module
      (``callers._reaches_module``);
    - a bare name nothing in the module binds is the library's when it is a
      builtin (``dict(mode="r")``);
    - a method call on anything else (``parser.add_argument(...)``,
      ``model.model_dump(mode="json")``) is the library's when no production
      file defines a function, method or class of that name, the rule the
      caller-value index uses for methods.

    Everything else (a call the module defines or binds itself, a project
    import, a method name the project defines) may be project code and keeps
    its keyword literals counted.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self._ctx = ctx
        modules = {filepath_to_module(str(path), ctx.package_dirs) for path in ctx.all_trees}
        self._modules = frozenset(module for module in modules if module)
        self._defined = frozenset(
            node.name
            for _path, tree in source_trees(ctx)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        self._bindings: dict[Path, dict[str, str]] = {}
        self._local_names: dict[Path, frozenset[str]] = {}
        self._reaches: dict[str, bool] = {}

    def _reaches_project(self, dotted: str) -> bool:
        if dotted not in self._reaches:
            parts = dotted.split(".")
            self._reaches[dotted] = any(
                _reaches_module(".".join(parts[:end]), module)
                for end in range(len(parts), 0, -1)
                for module in self._modules
            )
        return self._reaches[dotted]

    def _module_names(self, path: Path) -> tuple[dict[str, str], frozenset[str]]:
        """The module's import bindings, and every other name it binds anywhere."""
        if path not in self._bindings:
            self._bindings[path] = import_bindings(self._ctx.module_imports[path])
            self._local_names[path] = frozenset(
                name for node in ast.walk(self._ctx.all_trees[path]) if (name := own_binding(node))
            )
        return self._bindings[path], self._local_names[path]

    def is_library_call(self, path: Path, call: ast.Call) -> bool:
        bindings, local_names = self._module_names(path)
        callee = call.func
        # A callee that is no name chain (``f()()``, ``d[k]()``) has no root to bind.
        root, _, rest = (dotted_name(callee) or "").partition(".")
        if root in bindings:
            return not self._reaches_project(f"{bindings[root]}.{rest}" if rest else bindings[root])
        name = referenced_name(callee)
        if isinstance(callee, ast.Name):
            return name in _BUILTIN_NAMES and name not in local_names
        return isinstance(callee, ast.Attribute) and name not in self._defined

    def exempts(self, path: Path, parent: ast.AST, grandparent: ast.AST | None) -> bool:
        """Whether a literal under ``parent`` is a keyword argument to a library call."""
        return (
            isinstance(parent, ast.keyword)
            and isinstance(grandparent, ast.Call)
            and self.is_library_call(path, grandparent)
        )


_LiteralKey = tuple[str, str, str]


def _is_exempt_literal(
    django_identifiers: set[ast.stmt],
    library_calls: _LibraryCalls,
    path: Path,
    parent: ast.AST,
    grandparent: ast.AST | None,
) -> bool:
    """Whether a literal under ``parent`` in ``path`` is excused by its position:
    a Django identifier's assignment, or a keyword argument to a library call."""
    return parent in django_identifiers or library_calls.exempts(path, parent, grandparent)


def _file_literal_sites(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    exempt: Callable[[ast.AST, ast.AST | None], bool],
) -> Iterator[tuple[_LiteralKey, int]]:
    """Each counted literal in ``tree``, keyed for scattered-constants, with its line.

    The key is (constant name or "" for a bare literal, type name, repr of
    the value). ``exempt`` takes the literal's parent and grandparent and
    answers whether the position is excused.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue

        value = node.value
        parent = parents.get(node)
        if parent is None:
            continue
        grandparent = parents.get(parent)

        # The constant inside `-1` is not interesting on its own; the
        # UnaryOp wrapping it is, and the second walk below handles it.
        if _negated_literal(parent) is not None:
            continue

        if not _is_interesting_constant_context(node, parent, grandparent):
            continue

        if _is_trivial(value) or exempt(parent, grandparent):
            continue

        name = _constant_name(node, parent, grandparent) or ""
        yield (name, type(value).__name__, repr(value)), node.lineno

    # Also check for negative number literals via UnaryOp
    for node in ast.walk(tree):
        if not isinstance(node, ast.UnaryOp):
            continue
        neg_value = _negated_literal(node)
        if neg_value is None or neg_value in TRIVIAL_VALUES:
            continue

        parent = parents.get(node)
        if parent is None:
            continue
        grandparent = parents.get(parent)

        if not _is_interesting_constant_context(node, parent, grandparent):
            continue
        if exempt(parent, grandparent):
            continue

        name = _constant_name(node, parent, grandparent) or ""
        yield (name, type(neg_value).__name__, repr(neg_value)), node.lineno


@check(
    "scattered-constants",
    severity=Severity.LOW,
    description="Same literal value appears in assignments/comparisons across 3+ files",
    advice=Advice(
        fix=(
            "Define the value once as a named constant (or Enum member) and use it at every"
            " site listed."
        ),
        dismiss=(
            "Dismiss only if the sites mean different things that merely share a spelling,"
            " and would not change together."
        ),
    ),
)
def check_scattered_constants(ctx: AnalysisContext) -> list[Finding]:
    """Find literal values repeated in 3+ files in assignment/comparison contexts.

    A literal passed by keyword to a call that resolves outside the analyzed
    code is not counted (``_LibraryCalls``). See DECISIONS.md, "Entry-point
    catches and library keywords (Phase 33-2)". A literal bound to an
    UPPER_CASE name counts only with definitions of that same name
    (``_constant_name``). See DECISIONS.md, "Named constants are not
    scattered literals". A Django identifier (an AppConfig's ``name`` or
    ``label``, a URLconf's ``app_name``) is not counted
    (``_django_identifier_assigns``).
    """
    findings = []
    library_calls = _LibraryCalls(ctx)
    django_identifiers = _django_identifier_assigns(ctx)
    # key: (constant name or "" for a bare literal, type_name, repr_value),
    # value: list of (filepath, line)
    occurrences: dict[tuple[str, str, str], list[tuple[Path, int]]] = defaultdict(list)

    for filepath, tree in ctx.all_trees.items():
        if ctx.roles.is_test(filepath) or is_migration_file(filepath):
            continue

        exempt = partial(_is_exempt_literal, django_identifiers, library_calls, filepath)
        # Track which values we've already recorded for this file
        seen_in_file: set[tuple[str, str, str]] = set()
        for key, line in _file_literal_sites(tree, ctx.parent_map(tree), exempt):
            if key not in seen_in_file:
                seen_in_file.add(key)
                occurrences[key].append((filepath, line))

    for key, locs in sorted(occurrences.items()):
        if len(locs) < MIN_SCATTERED_FILES:
            continue
        name, _type_name, repr_value = key
        locs_sorted = sorted(locs, key=lambda x: str(x[0]))
        display = (
            repr_value
            if len(repr_value) <= MAX_DISPLAY_WIDTH
            else repr_value[: MAX_DISPLAY_WIDTH - 3] + "..."
        )
        display = f"Constant {name} = {display}" if name else f"Literal {display}"
        sites = [Site(str(path), line) for path, line in locs_sorted]
        findings.append(
            multi_site_finding(
                "scattered-constants",
                Severity.LOW,
                sites,
                partial(_scattered_constant_message, display, sites),
            )
        )

    return findings


def _site_list(sites: list[Site], limit: int | None = None) -> str:
    """``file:line`` for each site, comma-separated, cut at ``limit`` with "..."."""
    shown = sites if limit is None else sites[:limit]
    parts = [f"{site.file}:{site.line}" for site in shown]
    if limit is not None and len(sites) > limit:
        parts.append("...")
    return ", ".join(parts)


def _scattered_constant_message(
    display: str, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """scattered-constants' message over the sites at ``indices``; None below
    the file floor."""
    shown = [sites[index] for index in indices]
    if len(shown) < MIN_SCATTERED_FILES:
        return None
    return f"{display} appears in {len(shown)} files ({_site_list(shown)})"


@check(
    "scattered-isinstance",
    severity=Severity.MEDIUM,
    description="isinstance checks for project-defined types scattered across 3+ files",
    advice=Advice(
        fix=(
            "Move the type-specific behavior the checks guard into methods on the class (or"
            " a Protocol it implements), and replace the checks with calls."
        ),
        dismiss=(
            "Dismiss only if the checks validate input at boundaries (deserialization, API"
            " arguments) rather than choose behavior."
        ),
    ),
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

    for filepath, tree in source_trees(ctx):
        seen_in_file: set[str] = set()

        for call, target in iter_isinstance_targets(tree):
            for name in type_expr_names(target):
                if name == "?" or name in STDLIB_TYPES:
                    continue
                if name not in project_classes:
                    continue
                if name not in seen_in_file:
                    seen_in_file.add(name)
                    isinstance_locs[name].append((filepath, call.lineno))

    for class_name, locs in sorted(isinstance_locs.items()):
        if len(locs) < MIN_SCATTERED_FILES:
            continue
        locs_sorted = sorted(locs, key=lambda x: str(x[0]))
        sites = [Site(str(path), line) for path, line in locs_sorted]
        # Anchor at class definition
        def_path, def_line = project_classes[class_name]
        findings.append(
            multi_site_finding(
                "scattered-isinstance",
                Severity.MEDIUM,
                sites,
                partial(_scattered_isinstance_message, class_name, sites),
                anchor=Site(str(def_path), def_line),
            )
        )

    return findings


def _scattered_isinstance_message(
    class_name: str, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """scattered-isinstance's message over the sites at ``indices``; None
    below the file floor."""
    shown = [sites[index] for index in indices]
    if len(shown) < MIN_SCATTERED_FILES:
        return None
    return f"isinstance(x, {class_name}) checks appear in {len(shown)} files ({_site_list(shown)})"


# --- shotgun-surgery ---

# An attribute is reported once this many files access it.
SHOTGUN_MIN_FILES = 4

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
    assigns = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    return _library_bound_fixed_point(assigns, file_imports)


def _library_bound_fixed_point(assigns: list[ast.Assign], file_imports: set[str]) -> set[str]:
    """Fixed-point propagation of library-boundness over assignment statements."""
    bound: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in assigns:
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


def _is_called(node: ast.Attribute, parent_map: dict[ast.AST, ast.AST]) -> bool:
    """Whether ``node`` is the callee of a call: ``obj.attr(...)``."""
    parent = parent_map.get(node)
    return isinstance(parent, ast.Call) and parent.func is node


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
    if is_bare_name(node, "pass_context"):
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
    assigns = [stmt for stmt in tree.body if isinstance(stmt, ast.Assign)]
    return _library_bound_fixed_point(assigns, file_imports)


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


def _iter_reexported_names(
    tree: ast.Module,
    all_trees: dict[Path, ast.Module],
    top_level_bound: dict[str, set[str]],
) -> Iterator[str]:
    """Local names *tree* imports that the defining file bound to a library object.

    One pass over the module's ``from X import ...`` statements: a name the
    source file lists in *top_level_bound* arrives here still bound to the
    same library object, under its ``as`` alias when it has one.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        source_fp = _resolve_module_to_filepath(node.module, all_trees)
        if source_fp is None:
            continue
        source_top = top_level_bound.get(str(source_fp), set())
        for alias in node.names:
            if alias.name in source_top:
                yield alias.asname or alias.name


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
            for name in _iter_reexported_names(tree, all_trees, top_level_bound):
                if name not in library_bound[fp_str]:
                    library_bound[fp_str].add(name)
                    top_level_bound[fp_str].add(name)
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
    for filepath in all_trees:
        names.add(filepath.stem)
        for part in filepath.parts[:-1]:
            if part not in SOURCE_ROOT_DIRS and part not in {".", ".."}:
                names.add(part)
    return names


def _library_import_bindings(node: ast.AST, project_modules: set[str]) -> set[str]:
    """Local names *node* binds, when it is an import of a non-project module.

    Bound names accounted for:
      - ``import X``                       → ``X``
      - ``import X as Y``                  → ``Y``
      - ``import X.Y.Z``                   → ``X`` (the top of the dotted path)
      - ``import X.Y.Z as W``              → ``W``
      - ``from A import B``                → ``B``
      - ``from A import B as C``           → ``C``

    A ``from . import X`` has no module to classify and is treated as a
    project import, as is anything whose root module names a project module.
    """
    if isinstance(node, ast.Import):
        return {
            import_binding(alias)
            for alias in node.names
            if top_level_package(alias.name) not in project_modules
        }
    if isinstance(node, ast.ImportFrom):
        if node.module is None or top_level_package(node.module) in project_modules:
            return set()
        return {alias.asname or alias.name for alias in node.names}
    return set()


def _build_imports_per_file(all_trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """For each file, the set of local names introduced by *library* import
    statements — those whose source module is NOT a project module.

    Used to recognize "the receiver of this attribute read is a directly-imported
    library, not one of our value objects." Change-propagation risk doesn't apply
    to library APIs we don't own.

    Imports whose source module looks like a project module are excluded so
    that ``from project_models import make_config`` does NOT mark
    ``make_config`` as a library symbol (and therefore won't cause
    ``config = make_config()`` to be treated as library-bound).
    """
    project_modules = _collect_project_module_names(all_trees)
    return {
        str(filepath): {
            name
            for node in ast.walk(tree)
            for name in _library_import_bindings(node, project_modules)
        }
        for filepath, tree in all_trees.items()
    }


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


def _is_self_attribute_store(node: ast.AST) -> TypeGuard[ast.Attribute]:
    """True for the ``self.X`` of a ``self.X = ...`` assignment."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _class_defined_attrs(cls: ast.ClassDef) -> set[str]:
    """Attribute names *cls* defines: ``self.X = ...`` stores and class-level
    annotations (``X: type``). Nested classes count toward the outer one, which
    is what ``ast.walk`` over the class body has always done here."""
    attrs: set[str] = set()
    for item in ast.walk(cls):
        if _is_self_attribute_store(item):
            attrs.add(item.attr)
        elif isinstance(item, ast.AnnAssign) and (target := bare_target(item)) is not None:
            attrs.add(target.id)
    return attrs


# Types whose values are never called: a field annotated with only these
# (``Path | None``, ``list[str]``) is not the callee of ``obj.field(...)``.
_VALUE_TYPES = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "complex",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "Path",
        "datetime",
        "date",
        "time",
        "timedelta",
        "Decimal",
    }
)


def _is_value_annotation(annotation: ast.expr) -> bool:
    """Whether an annotation names only ``_VALUE_TYPES`` (and None), never a callable."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_value_annotation(annotation.left) and _is_value_annotation(annotation.right)
    if isinstance(annotation, ast.Constant):
        return annotation.value is None
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in _VALUE_TYPES
    return isinstance(annotation, ast.Name) and annotation.id in _VALUE_TYPES


def _declares_value_field(cls: ast.ClassDef, attr_name: str) -> bool:
    """Whether ``cls``'s body annotates ``attr_name`` with a plain value type."""
    return any(
        isinstance(item, ast.AnnAssign)
        and (target := bare_target(item)) is not None
        and target.id == attr_name
        and _is_value_annotation(item.annotation)
        for item in cls.body
    )


def _iter_project_classes(all_trees: dict[Path, ast.Module]) -> Iterator[tuple[str, ast.ClassDef]]:
    """Every class defined anywhere in the analyzed files, with its file."""
    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield str(filepath), node


def _collect_attr_to_classes(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[tuple[str, ast.ClassDef]]]:
    """For each attribute name defined in project classes, the list of
    (file, class) pairs that define it (via ``self.X = ...`` or class-level
    annotation)."""
    result: dict[str, list[tuple[str, ast.ClassDef]]] = defaultdict(list)
    for file_str, cls in _iter_project_classes(all_trees):
        for attr_name in _class_defined_attrs(cls):
            result[attr_name].append((file_str, cls))
    return result


def _collect_project_defined_attrs(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Collect attribute names defined in project classes (self.X = ... or annotations).

    Only attributes defined in the analyzed codebase are project-level concerns.
    Framework/stdlib attributes won't appear here.
    """
    return {
        attr
        for _file, cls in _iter_project_classes(all_trees)
        for attr in _class_defined_attrs(cls)
    }


class _ShotgunIndices(NamedTuple):
    """Cross-file indices built once per run for shotgun-surgery."""

    project_attrs: set[str]
    imports_per_file: dict[str, set[str]]
    attr_to_classes: dict[str, list[tuple[str, ast.ClassDef]]]
    attrs_all_behavior: set[str]
    library_bound_per_file: dict[str, set[str]]
    # Fields every defining class annotates with a plain value type.
    value_fields: set[str]


class _FileFilters(NamedTuple):
    """Per-file state consulted when deciding whether an attribute access counts."""

    file_imports: set[str]
    library_bound: set[str]
    parents: dict[ast.AST, ast.AST]
    library_typed_by_func_id: dict[int, set[str]]


def _shotgun_indices(ctx: AnalysisContext) -> _ShotgunIndices:
    """Build the cross-file indices the access filter consults."""
    imports_per_file = _build_imports_per_file(ctx.all_trees)

    # For the behavior-bearing-class downweighter: which classes define each
    # attr, and which of those classes are behavior-bearing.
    attr_to_classes = _collect_attr_to_classes(ctx.all_trees)

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

    return _ShotgunIndices(
        # Only flag attributes defined in the project, not framework/stdlib APIs
        project_attrs=_collect_project_defined_attrs(ctx.all_trees),
        imports_per_file=imports_per_file,
        attr_to_classes=attr_to_classes,
        attrs_all_behavior={
            attr_name
            for attr_name, classes in attr_to_classes.items()
            if classes and all(_is_behavior_bearing_class(c) for _, c in classes)
        },
        library_bound_per_file=library_bound_per_file,
        value_fields={
            attr_name
            for attr_name, classes in attr_to_classes.items()
            if all(_declares_value_field(cls, attr_name) for _, cls in classes)
        },
    )


def _is_counted_access(
    node: ast.Attribute, idx: _ShotgunIndices, filters: _FileFilters, *, is_write: bool
) -> bool:
    """Whether an ``obj.attr`` access counts toward shotgun-surgery."""
    if not isinstance(node.value, ast.Name):
        return False
    var_name = node.value.id
    attr_name = node.attr

    # Skip self/cls
    if var_name in {"self", "cls"}:
        return False
    # Skip private attrs
    if attr_name.startswith("_"):
        return False
    # Skip common/framework attrs (stable APIs like .pk, .save, .user)
    if attr_name in COMMON_ATTRS:
        return False
    # Only flag attributes defined in project classes
    if attr_name not in idx.project_attrs:
        return False
    # Skip when the receiver is a name imported in this file —
    # `boto3.client`, `click.group`, etc. are library API, not user
    # attribute reads we could propagate a refactor through.
    if var_name in filters.file_imports:
        return False
    # Skip when the receiver was bound from an imported library call
    # like `log = logging.getLogger(...)` or `m = re.match(...)`.
    if var_name in filters.library_bound:
        return False
    # Skip Click Context parameters inside Click callbacks or
    # @click.pass_context functions — `ctx.invoke`, `ctx.obj`, etc.
    # are Click API, not user attribute reads.
    click_ctx = _enclosing_click_context_param(node, filters.parents)
    if click_ctx == var_name:
        return False
    # Skip parameters whose type annotation references a library
    # import — `def helper(ctx: click.Context)` or `def handle(resp:
    # requests.Response)`. The parameter is a library object by
    # contract, even if this helper isn't itself decorated.
    typed_params = _enclosing_library_typed_params(
        node, filters.parents, filters.library_typed_by_func_id
    )
    if var_name in typed_params:
        return False
    # Skip a call (``x.attr(...)``) of a field every defining class declares
    # as a plain value: nothing calls a datetime, so ``dotted.split(".")`` is
    # a str's method, not ``WindowHalves.split``. A field declared callable,
    # or not declared at all, still counts when called.
    if attr_name in idx.value_fields and _is_called(node, filters.parents):
        return False
    # Skip when every class defining this attr is behavior-bearing —
    # the read is object-graph traversal through a value object that
    # has real methods, not the anemic-dataclass shape this check is
    # looking for.
    if attr_name in idx.attrs_all_behavior:
        return False
    # Skip cosmetic reads — inside f-strings or as args to logging
    # methods. Renaming the attr doesn't break these; they're
    # display-only and weaker signal than reads driving logic.
    # (Writes can't be cosmetic — the filter only applies to reads.)
    if not is_write and _is_cosmetic_read(node, filters.parents):
        return False
    # Skip uppercase attr access (enum constants: Severity.HIGH)
    return not attr_name[0].isupper()


def _declared_receiver_classes(
    receiver: str, scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef
) -> set[str]:
    """The class names ``scope`` itself declares or constructs ``receiver`` as.

    A parameter annotation (``config: Config``, each arm of a ``|`` union,
    string annotations included), an annotated assignment
    (``config: Config = ...``) and a constructor call (``config = Config()``)
    are the evidence. Anything else (``cur = conn.cursor()``, a loop
    variable, a name bound in another scope) declares nothing.
    """
    annotations: list[ast.expr] = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = scope.args
        annotations.extend(
            arg.annotation
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            if arg.arg == receiver and arg.annotation is not None
        )
    callees: list[ast.expr] = []
    for node in own_scope_nodes(scope.body):
        if (
            isinstance(node, ast.AnnAssign)
            and (target := bare_target(node)) is not None
            and target.id == receiver
        ):
            annotations.append(node.annotation)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and any(is_bare_name(target, receiver) for target in node.targets)
        ):
            callees.append(node.value.func)
    arms = [arm for annotation in annotations for arm in annotation_arms(annotation)]
    return set(referenced_names([*arms, *callees]))


class _ShotgunAccesses(NamedTuple):
    """What the access walk records, each keyed by ``(receiver, attr)``."""

    # The first line each file accesses the attribute on.
    lines: dict[tuple[str, str], dict[str, int]]
    # The files that write the attribute.
    write_files: dict[tuple[str, str], set[str]]
    # The class names some accessing scope declares the receiver as.
    receiver_classes: dict[tuple[str, str], set[str]]


def _collect_shotgun_accesses(ctx: AnalysisContext, idx: _ShotgunIndices) -> _ShotgunAccesses:
    """Collect counted attribute accesses, the files that write each attr,
    and the classes the accessing scopes declare each receiver as.

    Scattered writes are the textbook shotgun-surgery shape (anemic model
    fields mutated from many call sites), so they're called out in the
    message. The receiver classes decide which defining class, if any, the
    finding names (``_defining_class``).
    """
    accesses: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    write_files: dict[tuple[str, str], set[str]] = defaultdict(set)
    receiver_classes: dict[tuple[str, str], set[str]] = defaultdict(set)

    for filepath, tree in source_trees(ctx):
        file_str = str(filepath)
        file_imports = idx.imports_per_file.get(file_str, set())

        # Per-function map of parameter names whose type annotation is a
        # library type (e.g. `ctx: click.Context`, `resp: requests.Response`).
        # Looked up by id() when processing attribute reads.
        library_typed_by_func_id: dict[int, set[str]] = {}
        for func_node in ast.walk(tree):
            if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                typed = _library_typed_params(func_node, file_imports)
                if typed:
                    library_typed_by_func_id[id(func_node)] = typed

        filters = _FileFilters(
            file_imports=file_imports,
            library_bound=idx.library_bound_per_file.get(file_str, set()),
            parents=ctx.parent_map(tree),
            library_typed_by_func_id=library_typed_by_func_id,
        )

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
            if not isinstance(node.value, ast.Name) or not _is_counted_access(
                node, idx, filters, is_write=is_write
            ):
                continue

            key = (node.value.id, node.attr)
            if is_write:
                write_files[key].add(file_str)
            scope = next(enclosing_functions(node, filters.parents), tree)
            receiver_classes[key] |= _declared_receiver_classes(node.value.id, scope)
            if key not in seen_in_file:
                seen_in_file.add(key)
                if file_str not in accesses[key]:
                    accesses[key][file_str] = node.lineno

    return _ShotgunAccesses(accesses, write_files, receiver_classes)


def _defining_class(
    defining: list[tuple[str, ast.ClassDef]], receiver_classes: set[str]
) -> tuple[str, ast.ClassDef] | None:
    """The one class defining the attribute that an accessing scope declares
    the receiver as, else None.

    Defining the attribute's name is no evidence on its own: a
    ``cur.execute`` is a DB cursor's, not that of ``OpHandler``, the one
    project class with an ``execute`` field (36-3).
    """
    evidenced = [(file, cls) for file, cls in defining if cls.name in receiver_classes]
    return evidenced[0] if len(evidenced) == 1 else None


def _shotgun_findings(accesses: _ShotgunAccesses, idx: _ShotgunIndices) -> list[Finding]:
    """Emit findings for attrs accessed in enough files."""
    findings = []

    for (var_name, attr_name), file_lines in sorted(accesses.lines.items()):
        if len(file_lines) < SHOTGUN_MIN_FILES:
            continue

        sites = [Site(f, line) for f, line in sorted(file_lines.items())]

        # Anchor at the attribute's defining class when the receiver is known
        # to be it, so the finding maps to the file a fix would start from —
        # not whichever access site happens to sort first alphabetically.
        defining = _defining_class(
            idx.attr_to_classes.get(attr_name, []),
            accesses.receiver_classes[(var_name, attr_name)],
        )
        if defining is not None:
            def_file, def_class = defining
            anchor = Site(def_file, def_class.lineno)
            defined_note = f" (defined on {def_class.name})"
        else:
            anchor = sites[0]
            defined_note = ""

        n_write_files = len(accesses.write_files.get((var_name, attr_name), ()))
        write_note = f", written from {n_write_files}" if n_write_files else ""

        findings.append(
            multi_site_finding(
                "shotgun-surgery",
                Severity.MEDIUM,
                sites,
                partial(
                    _shotgun_message,
                    f"{var_name}.{attr_name}{defined_note}",
                    attr_name,
                    write_note,
                    sites,
                ),
                anchor=anchor,
            )
        )

    return findings


def _shotgun_message(
    subject: str, attr_name: str, write_note: str, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """shotgun-surgery's message over the access sites at ``indices``; None
    below the file floor."""
    shown = [sites[index] for index in indices]
    if len(shown) < SHOTGUN_MIN_FILES:
        return None
    return (
        f"{subject} accessed in {len(shown)} files{write_note}"
        f" ({_site_list(shown, limit=5)})"
        f" — changes to .{attr_name} require updating many files"
    )


@check(
    "shotgun-surgery",
    severity=Severity.MEDIUM,
    description="Same obj.attr accessed in 4+ files — change propagation risk",
    advice=Advice(
        fix=(
            "Give the owning class a method for what the sites do with the attribute, and"
            " have the sites listed call it."
        ),
        dismiss=(
            "Dismiss only if the attribute is a stable public field of a library or"
            " framework object, such as a response's status_code."
        ),
    ),
)
def check_shotgun_surgery(ctx: AnalysisContext) -> list[Finding]:
    """Find attribute accesses repeated across many files."""
    idx = _shotgun_indices(ctx)
    return _shotgun_findings(_collect_shotgun_accesses(ctx, idx), idx)


# --- repeated-string-parsing ---

# The same .split(delim)[N] is reported once it appears in this many places.
MIN_SPLIT_LOCATIONS = 3


def _split_index_message(
    delim: str, idx: int, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """repeated-string-parsing's same-index message over the sites at
    ``indices``; None below the location floor."""
    shown = [sites[index] for index in indices]
    if len(shown) < MIN_SPLIT_LOCATIONS:
        return None
    return (
        f'.split("{delim}")[{idx}] appears in {len(shown)}'
        f" locations ({_site_list(shown, limit=5)})"
        f" — ad-hoc serialization"
    )


def _split_delimiter(node: ast.expr) -> str | None:
    """The literal delimiter of ``<expr>.split("<delim>")``, else None.

    A split on a non-literal (``s.split(sep)``) or on no argument at all
    (``s.split()`` — whitespace) has no delimiter to group occurrences by.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != "split" or not node.args:
        return None
    delimiter = node.args[0]
    if not isinstance(delimiter, ast.Constant) or not isinstance(delimiter.value, str):
        return None
    return delimiter.value


def _constant_int_index(node: ast.Subscript) -> int | None:
    """The literal integer index of ``<expr>[N]``, else None (slices, names)."""
    index = node.slice
    if isinstance(index, ast.Constant) and isinstance(index.value, int):
        return index.value
    return None


def _iter_direct_split_subscripts(tree: ast.Module) -> Iterator[tuple[str, int, int]]:
    """Occurrences of the chained form: ``x.split("|")[1]``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        index = _constant_int_index(node)
        delimiter = _split_delimiter(node.value)
        if index is not None and delimiter is not None:
            yield delimiter, index, node.lineno


def _split_assigned_delimiters(func_node: ast.AST) -> dict[str, str]:
    """Local name -> delimiter for every ``parts = x.split("|")`` in a function."""
    return {
        target.id: delimiter
        for _stmt, target, value in iter_simple_assigns(ast.walk(func_node))
        if (delimiter := _split_delimiter(value)) is not None
    }


def _iter_indexed_split_vars(
    func_node: ast.AST, split_vars: dict[str, str]
) -> Iterator[tuple[str, int, int]]:
    """Occurrences of the two-step form: ``parts[1]`` after a split assignment."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
            continue
        delimiter = split_vars.get(node.value.id)
        index = _constant_int_index(node)
        if delimiter is not None and index is not None:
            yield delimiter, index, node.lineno


def _find_split_subscripts(tree: ast.Module) -> list[tuple[str, int, int]]:
    """Find .split(delim)[N] patterns, returning (delimiter, index, lineno) tuples.

    Detects both direct chaining (x.split("|")[1]) and intermediate variable
    patterns (parts = x.split("|") ... parts[1]). The two forms are collected
    separately and in that order, so a chained occurrence keeps sorting ahead
    of a two-step one from the same file.
    """
    results = list(_iter_direct_split_subscripts(tree))
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        split_vars = _split_assigned_delimiters(func_node)
        if split_vars:
            results.extend(_iter_indexed_split_vars(func_node, split_vars))
    return results


@check(
    "repeated-string-parsing",
    severity=Severity.MEDIUM,
    description="Same .split(delim)[N] pattern in 3+ locations — ad-hoc serialization format",
    advice=Advice(
        fix=(
            "Parse the string once into a dataclass (or NamedTuple) with named fields, and"
            " have the sites read the fields."
        ),
        dismiss=(
            "Dismiss only if the string is an external standard format the sites parse"
            " in unrelated contexts, such as a MIME type."
        ),
    ),
)
def check_repeated_string_parsing(ctx: AnalysisContext) -> list[Finding]:
    """Find repeated .split(delimiter)[index] patterns suggesting primitive obsession."""
    findings = []

    # Collect (delimiter, index) -> [(file, line), ...]
    occurrences: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)

    for filepath, tree in source_trees(ctx):
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
        if len(locs) < MIN_SPLIT_LOCATIONS:
            continue
        reported_delims.add(delim)
        sites = [Site(f, line) for f, line in sorted(locs)]
        findings.append(
            multi_site_finding(
                "repeated-string-parsing",
                Severity.MEDIUM,
                sites,
                partial(_split_index_message, delim, idx, sites),
                anchor=Site(*locs[0]),
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
                    f" — ad-hoc format being parsed piecemeal"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings
