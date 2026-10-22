"""Shared AST helpers used by multiple checks."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeGuard

from pysmelly.pytest_config import is_collected_test_name

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext

# "At least half" — shared by checks that gate on majority behavior
# (fix-commit ratio, stubbed-override ratio, commit-message quality).
MAJORITY = 0.5

# Directory names that can hold a source root rather than a Python package.
# Under a src layout, ``src/pkg/mod.py`` is imported as ``pkg.mod`` — the
# ``src`` component is a layout artifact and is not part of any module path.
SOURCE_ROOT_DIRS = frozenset({"src", "lib"})

# The file whose presence makes a directory a regular package.
INIT_FILE = "__init__.py"

# Appended to a finding whose evidence is that nothing uses something, when
# no test-role file was analyzed: the project excludes its tests, so a test
# use is invisible (away-run Q13/Q16). ``use`` is "passes it" or the like.
TESTS_UNSEEN = (
    "no test files were analyzed, so a test that {use} would not be seen"
    " (include the tests in the analysis to rule that out)"
)


def package_dirs(paths: Iterable[Path]) -> frozenset[tuple[str, ...]]:
    """The directories, as split path parts, that hold an ``__init__.py`` among ``paths``."""
    return frozenset(path.parent.parts for path in paths if path.name == INIT_FILE)


def _strip_source_root(
    parts: Sequence[str], package_dirs: Collection[tuple[str, ...]]
) -> list[str]:
    """Drop everything up to and including the source root of a split directory path.

    A ``src``/``lib`` component is a source root wherever it sits, provided
    neither it nor the directory holding it is a package — ``package_dirs``
    is the set of directories (as split parts) with an ``__init__.py``. So
    ``("src", "pkg", "sub")`` -> ``["pkg", "sub"]`` and a uv workspace
    member's ``("home", "src", "member")`` -> ``["member"]``, while a ``lib``
    inside a package keeps its place: ``("pkg", "lib")`` stays whole when
    ``("pkg",)`` is a package. The deepest source root wins, since a module's
    import name starts right below the nearest one.
    The repository root is never a package, even with an ``__init__.py``: it
    has no name to import it by.
    """
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] not in SOURCE_ROOT_DIRS or tuple(parts[: i + 1]) in package_dirs:
            continue
        if i > 0 and tuple(parts[:i]) in package_dirs:
            continue
        return list(parts[i + 1 :])
    return list(parts)


def filepath_to_module(filepath: str, package_dirs: Collection[tuple[str, ...]]) -> str:
    """Convert billing/invoice.py -> billing.invoice.

    The source root is dropped wherever it sits (see ``_strip_source_root``),
    so ``src/billing/invoice.py`` and a workspace member's
    ``member/src/billing/invoice.py`` both map to ``billing.invoice`` — the
    name the project actually imports. Only directories can be source roots,
    so a module named ``lib.py`` keeps its own name.
    """
    p = Path(filepath)
    parts = _strip_source_root(p.parent.parts, package_dirs)
    if p.name != INIT_FILE:
        parts.append(p.stem)
    return ".".join(parts)


def filepath_to_package(filepath: str, package_dirs: Collection[tuple[str, ...]]) -> str:
    """Convert billing/invoice.py -> billing (the containing package).

    A package's ``__init__.py`` is its own package; any other module's
    package is its module path less the last name.
    """
    module = filepath_to_module(filepath, package_dirs)
    if Path(filepath).name == INIT_FILE:
        return module
    return module.rpartition(".")[0]


def resolve_relative_import(node_module: str | None, level: int, package: str) -> str:
    """Resolve a relative import to an absolute module path.

    E.g., `from .utils import x` in package `myapp.works.models`
    -> `myapp.works.models.utils`
    """
    if level == 0 or not package:
        return node_module or ""
    # Go up `level - 1` packages (level=1 means current package)
    parts = package.split(".")
    if level > 1:
        parts = parts[: -(level - 1)] if level - 1 < len(parts) else []
    base = ".".join(parts)
    if node_module:
        return f"{base}.{node_module}" if base else node_module
    return base


def source_trees(ctx: AnalysisContext) -> Iterator[tuple[Path, ast.Module]]:
    """Yield ``(path, tree)`` for every analyzed file that does not have the test role.

    Most checks describe the shape of production code; a test file's shape is
    dictated by what it tests and by pytest, so it is excluded by default.
    A check that deliberately looks at tests walks ``all_trees`` itself.
    """
    for filepath, tree in ctx.all_trees.items():
        if ctx.roles.is_test(filepath):
            continue
        yield filepath, tree


def iter_module_classes(tree: ast.Module) -> Iterator[ast.ClassDef]:
    """Yield the classes defined at module scope, skipping nested ones."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield node


def class_methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions defined directly in a class body, in source order.

    Nested classes' methods are not included — only what this class defines.
    Callers that care about ``__init__`` or the dunders filter by name.
    """
    return [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]


def iter_module_dicts(tree: ast.Module) -> Iterator[tuple[ast.Assign, ast.Dict]]:
    """Yield ``(stmt, dict_literal)`` for each module-level ``x = {...}``.

    Empty dict literals are skipped: a table with no entries tells a
    dispatch- or registry-shaped check nothing.
    """
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not node.value.keys:
            continue
        yield node, node.value


def iter_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every function and method in *tree*, at any nesting depth."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def referenced_name(expr: ast.expr) -> str | None:
    """The bare name an expression refers to: ``f``, ``f()`` and ``o.f`` all "f".

    The one place the ``ast.Name`` -> ``.id`` / ``ast.Attribute`` -> ``.attr``
    resolution lives. Callers pass a call's callee, a dict value, a list
    element, a decorator, or any other expression in reference position.

    Returns:
        The name, or None when the expression is neither a name nor an
        attribute — a subscript or another call's result has no name to
        read. None means "no name here", never "lookup failed".
    """
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


# Statements and clauses that bind through a single ``target`` field.
SingleTargetBinding = (
    ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor | ast.comprehension | ast.NamedExpr
)


def bare_target(node: SingleTargetBinding) -> ast.Name | None:
    """The bare name *node* binds through its ``target``: ``x`` in ``x: int``,
    ``x += 1``, ``for x in ...``, ``[... for x in ...]`` or ``(x := ...)``.

    Returns:
        The ``ast.Name`` node, or None when the target is an attribute, a
        subscript or an unpacking, which bind no bare name.
    """
    target = node.target
    return target if isinstance(target, ast.Name) else None


def assigned_pairs(stmt: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """Each ``(target, value)`` a plain or annotated assignment binds, in target order.

    ``a = b = v`` gives ``[(a, v), (b, v)]``; ``a: int = v`` gives
    ``[(a, v)]``. Any other node binds nothing this way and gives no pair,
    including a bare annotation (``a: int``) and an augmented assignment.
    """
    if isinstance(stmt, ast.Assign):
        return [(target, stmt.value) for target in stmt.targets]
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return [(stmt.target, stmt.value)]
    return []


def own_binding(node: ast.AST) -> str | None:
    """The name *node* binds by being itself: a ``Name`` stored to, a
    ``def``/``class``, a parameter, or an ``except ... as`` handler's name.

    Imports and ``match`` captures are not included (see
    :func:`clause_binding`); callers that want them resolve them separately.
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return node.id
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.ExceptHandler):
        return node.name
    return None


def clause_binding(node: ast.AST) -> str | None:
    """The name a clause binds without a ``Name`` target: an ``except ... as``
    handler, an import alias (:func:`import_binding`), a ``match`` capture
    (``case x``, ``case [*rest]``) or a mapping pattern's ``**rest``.
    """
    if isinstance(node, ast.ExceptHandler):
        return node.name
    if isinstance(node, ast.alias):
        return import_binding(node)
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def is_ellipsis(expr: ast.AST | None) -> bool:
    """True for the literal ``...`` (a stub body, ``Field(...)``, ``tuple[T, ...]``)."""
    return isinstance(expr, ast.Constant) and expr.value is ...


def is_bare_name(expr: ast.AST, name: str) -> TypeGuard[ast.Name]:
    """True for the bare name *name* — ``x``, but not ``obj.x``.

    A ``TypeGuard`` rather than a ``TypeIs``: True narrows *expr* to
    ``ast.Name``, but False says nothing, since a Name with another id is
    still a Name.
    """
    return isinstance(expr, ast.Name) and expr.id == name


def _is_literal(expr: ast.expr, value: object) -> bool:
    """True for a literal equal to *value*."""
    return isinstance(expr, ast.Constant) and expr.value == value


def decorator_target(decorator: ast.expr) -> ast.expr:
    """The expression a decorator names, unwrapping the call in ``@d(...)``.

    ``@d`` and ``@d()`` both target ``d``; ``@m.d`` and ``@m.d()`` both target
    ``m.d``. Callers wanting the bare name pass the result to
    :func:`referenced_name`.
    """
    return decorator.func if isinstance(decorator, ast.Call) else decorator


def attr_root_name(node: ast.expr) -> str | None:
    """The root name of an attribute chain: ``a.b.c`` -> ``a``.

    Returns:
        The leftmost name, or None when the chain bottoms out in something
        that is not a name (a subscript or a call).
    """
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


def name_chain(node: ast.expr) -> list[str]:
    """The names a ``name`` or ``name.attr...`` chain spells, root first:
    ``a.b.c`` -> ``["a", "b", "c"]``.

    Empty when the chain bottoms out in something that is not a name (a
    subscript or a call): such an expression spells no chain.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return []
    parts.append(current.id)
    parts.reverse()
    return parts


def dotted_name(node: ast.expr) -> str | None:
    """The dotted path an attribute chain spells: ``a.b.c`` -> ``"a.b.c"``.

    Returns:
        The path, or None when the chain bottoms out in something that is
        not a name (a subscript or a call), so it spells no import path.
    """
    parts = name_chain(node)
    if not parts:
        return None
    return ".".join(parts)


def dotted_names(exprs: Iterable[ast.expr]) -> list[str]:
    """The dotted paths the name chains among ``exprs`` spell, in order.

    An expression that is not a name chain (a call, a subscript) spells no
    path and contributes nothing (``dotted_name``).
    """
    return [dotted for expr in exprs if (dotted := dotted_name(expr))]


def body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """A function, class or module body with its leading docstring dropped.

    Returns the body unchanged when its first statement is not a bare string
    literal — "no docstring" is the normal case, not a failure to find one.
    """
    if body and isinstance(body[0], ast.Expr):
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return body[1:]
    return body


def sole_statement(func: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt | None:
    """The one statement *func*'s body holds besides its docstring, or None when
    it holds more than one."""
    statements = body_without_docstring(func.body)
    return statements[0] if len(statements) == 1 else None


def loaded_names(tree: ast.AST, quiet: set[int]) -> Iterator[str]:
    """Every name *tree* loads, bare (``f``) or as an attribute (``obj.f``),
    outside the nodes whose ids are in *quiet*.
    """
    for node in ast.walk(tree):
        if id(node) in quiet:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            yield node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            yield node.attr


# The nodes that open a scope of their own inside a function or module.
NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def own_scope_nodes(body: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Every node of *body* that runs in the scope owning it.

    *body* is a function's or module's statements. A ``def``, ``class`` or
    ``lambda`` among them, or anywhere below them, is skipped whole: its
    returns, yields and bindings belong to the scope it opens.
    """
    todo = [node for node in body if not isinstance(node, NESTED_SCOPES)]
    while todo:
        node = todo.pop()
        yield node
        todo.extend(
            child for child in ast.iter_child_nodes(node) if not isinstance(child, NESTED_SCOPES)
        )


def nested_function_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every function defined inside another function in *tree*.

    A walk of a module yields nested functions alongside top-level ones with
    nothing to tell them apart; checks that only mean the top level filter on
    this set.
    """
    nested: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested.add(id(child))
    return nested


def iter_isinstance_targets(tree: ast.Module) -> Iterator[tuple[ast.Call, ast.expr]]:
    """Yield ``(call, type_expr)`` for every ``isinstance``/``issubclass`` call.

    The second element is the call's type argument as written — a name, an
    attribute, or a tuple of them; pass it to `type_expr_names`. Calls with
    fewer than two arguments are skipped; they cannot name a type.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id in {"isinstance", "issubclass"}):
            continue
        if len(node.args) < 2:
            continue
        yield node, node.args[1]


def type_expr_names(node: ast.expr | None) -> list[str]:
    """The type names an ``except`` clause or ``isinstance`` argument names.

    A single ``Name`` or ``Attribute`` yields one entry; a tuple yields one
    per element. Anything else — a call, a subscript — yields ``"?"``: the
    expression names a type this analysis cannot read, which is not the same
    as naming none.

    Returns:
        The names in source order. Empty only for a bare ``except:``, which
        names nothing.
    """
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [_type_expr_name(elt) for elt in node.elts]
    return [_type_expr_name(node)]


def _type_expr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "?"


def annotation_arms(annotation: ast.expr) -> list[ast.expr]:
    """The arms of an annotation's ``|`` union, in order; a lone type is one arm.

    A string annotation (``"X | None"``), whole or as an arm, is parsed and
    read the same way. A string that does not parse names nothing and gives
    no arm.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return []
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return [*annotation_arms(annotation.left), *annotation_arms(annotation.right)]
    return [annotation]


def raises_not_implemented(stmt: ast.stmt) -> bool:
    """True for ``raise NotImplementedError`` or ``raise NotImplementedError(...)``."""
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    exc = stmt.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return is_bare_name(exc, "NotImplementedError")


def build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Build a child→parent mapping for an AST."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def is_click_callback_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Click ``callback=`` functions have the fixed signature (ctx, param, value).

    Underscore prefixes (``_ctx``, ``_param``) indicate the author isn't using
    that arg — Click still passes them. The three parameters are dictated by
    Click, not chosen by the author.
    """
    args = list(node.args.posonlyargs) + list(node.args.args)
    if args and args[0].arg in {"self", "cls"}:
        args = args[1:]
    if len(args) != 3:
        return False
    return [a.arg.lstrip("_") for a in args] == ["ctx", "param", "value"]


def has_decorator_named(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, names: Collection[str]
) -> bool:
    """True when a decorator of *node* names one of *names*, in any form.

    ``@d``, ``@d()``, ``@m.d`` and ``@m.d()`` all name ``d``: the call is
    unwrapped (:func:`decorator_target`) and the bare name read
    (:func:`referenced_name`).
    """
    return any(referenced_name(decorator_target(deco)) in names for deco in node.decorator_list)


def has_dataclass_decorator(node: ast.ClassDef) -> bool:
    """Check if a class has @dataclass or @dataclasses.dataclass (any form)."""
    return any(
        referenced_name(decorator_target(deco)) == "dataclass" for deco in node.decorator_list
    )


# --- secret-shaped names ---
#
# One vocabulary, shared by every check that has to decide whether a name
# denotes a secret: ``secrets-in-logs`` (MEDIUM — a leak into a log line) and
# ``plaintext-passwords`` (HIGH — a timing-attackable comparison). Both ask
# the same question of a name, so both get the same answer.

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
_SECRET_METADATA_SUFFIXES = frozenset(
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


def is_secret_name(name: str) -> bool:
    """True when *name* denotes a secret value rather than a fact about one.

    Whole snake_case segments match, so ``max_tokens`` and ``s3_key`` do not;
    the multi-segment idioms in ``_SECRET_SUBSTRINGS`` match anywhere in the
    lowered name. A trailing metadata segment disqualifies the name outright:
    ``token_id``, ``api_key_name`` and ``password_expiry`` name something
    *about* a secret, which is neither a leak when logged nor a secret when
    compared.
    """
    lowered = name.lower()
    segments = lowered.split("_")
    if segments[-1] in _SECRET_METADATA_SUFFIXES:
        return False
    if any(part in lowered for part in _SECRET_SUBSTRINGS):
        return True
    return any(seg in _SECRET_SEGMENTS for seg in segments)


_EXCEPTION_BASES = frozenset(
    {
        "Exception",
        "BaseException",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "KeyError",
        "AttributeError",
        "IOError",
        "OSError",
        "LookupError",
        "IndexError",
        "NotImplementedError",
        "StopIteration",
        "ArithmeticError",
        "PermissionError",
        "FileNotFoundError",
        "ConnectionError",
        "TimeoutError",
    }
)


def main_guard_blocks(tree: ast.Module) -> Iterator[ast.If]:
    """Each top-level ``if __name__ == "__main__":`` block in ``tree``."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            yield node


def _find_main_entry_points(all_trees: dict[Path, ast.Module]) -> set[str]:
    """Find function names called from ``if __name__ == "__main__":`` blocks."""
    entry_points: set[str] = set()
    for tree in all_trees.values():
        for block in main_guard_blocks(tree):
            for child in ast.walk(block):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    entry_points.add(child.func.id)
    return entry_points


def _is_main_guard(test: ast.expr) -> bool:
    """Check if an expression is ``__name__ == "__main__"``, in either order."""
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    operands = (test.left, test.comparators[0])
    return any(
        is_bare_name(name_side, "__name__") and _is_literal(literal_side, "__main__")
        for name_side, literal_side in (operands, operands[::-1])
    )


def _class_body_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every statement written directly in a class body.

    A function among them is a method: reached through its class rather than
    called by name, so the cross-file function index leaves it out. Collected
    once per tree — the previous per-function scan re-walked the whole module
    for every candidate.
    """
    return {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for child in ast.iter_child_nodes(node)
    }


def build_function_index(
    all_trees: dict[Path, ast.Module], test_files: Collection[Path]
) -> dict[str, list[dict]]:
    """Build map of public function definitions across the codebase.

    Returns only non-private, non-decorated, non-method, non-test, non-entry-point
    functions that are defined at the top level (not nested inside other functions).
    A test function is one pytest collects: a ``test*`` function in one of
    ``test_files``; elsewhere such a name is an ordinary function.
    """
    entry_points = _find_main_entry_points(all_trees)
    func_defs: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        # Nested functions and methods are both reached through something
        # other than a bare cross-file call, so neither belongs in the index.
        excluded_ids = nested_function_ids(tree) | _class_body_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(node) in excluded_ids or node.name in entry_points:
                continue
            # Private, dunder and test functions are not called across files,
            # and a decorated function is registered rather than called by name.
            if node.name.startswith("_") or node.decorator_list:
                continue
            if filepath in test_files and is_collected_test_name(node.name):
                continue
            func_defs[node.name].append({"file": str(filepath), "line": node.lineno, "node": node})
    return func_defs


def collect_imported_names(tree: ast.Module) -> set[str]:
    """Collect names bound by imports in a module.

    Covers ``import a.b.c`` (binds ``a``), ``import x as y`` (binds ``y``),
    and ``from m import n`` (binds ``n``). These are the roots of library
    namespace access — a ``mod.func()`` call whose root is one of these is
    a module-qualified reference, not a method call on an instance.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(import_binding(alias))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def top_level_package(dotted: str) -> str:
    """The first component of a dotted module path: ``a`` for ``a.b.c``."""
    return dotted.partition(".")[0]


def import_binding(alias: ast.alias) -> str:
    """The name an ``import`` statement's alias binds.

    ``import x as y`` binds ``y``; ``import a.b.c`` binds only ``a``.
    """
    return alias.asname or top_level_package(alias.name)


class ModuleImport(NamedTuple):
    """One name an import statement binds in the importing module.

    ``bound`` is the name bound: ``y`` for ``import x as y``, ``a`` for
    ``import a.b``, ``m`` for ``from s import n as m``, ``*`` for a star
    import. ``dotted`` is the dotted path that name refers to: the module
    for ``import x as y`` (``x``), the root for ``import a.b`` (``a``), and
    ``s.n`` for a from-import. ``source`` (the from-import's module, a
    relative import resolved against the importing file's package) and
    ``name`` (the name it imports) are None for a plain ``import``.
    """

    bound: str
    dotted: str
    source: str | None
    name: str | None


def import_bindings(imports: Iterable[ModuleImport]) -> dict[str, str]:
    """Each name a module's imports bind, mapped to the dotted path it refers to.

    ``imports`` is a ``ctx.module_imports`` entry; replayed in its walk
    order, a later binding of a name wins.
    """
    return {imp.bound: imp.dotted for imp in imports}


# The dotted path ``@pytest.fixture`` resolves to, however it was imported.
PYTEST_FIXTURE = "pytest.fixture"


class ModuleResolver:
    """Resolves a name expression in one module to the dotted path it refers to.

    A name the module's imports bind resolves to what it was imported from,
    aliases included (``from pytest import fixture as fx``: ``fx`` is
    ``pytest.fixture``). Any other name is the module's own, so it resolves
    under ``module``.
    """

    def __init__(self, module: str, imports: list[ModuleImport]) -> None:
        self.module = module
        self.bindings = import_bindings(imports)
        self.imports_pytest = any(
            imp.dotted == "pytest" or imp.dotted.startswith("pytest.") for imp in imports
        )

    def _resolve(self, dotted: str) -> str:
        head, _, rest = dotted.partition(".")
        if head in self.bindings:
            target = self.bindings[head]
            return f"{target}.{rest}" if rest else target
        return f"{self.module}.{dotted}" if self.module else dotted

    def resolve_all(self, exprs: Iterable[ast.expr]) -> list[str]:
        """The dotted paths the name chains among ``exprs`` refer to, in order.

        An expression that is not a name chain (a call, a subscript) names no
        path and contributes nothing.
        """
        return [self._resolve(dotted) for dotted in dotted_names(exprs)]


class ImportReader:
    """Reads import statements as one module sees them.

    ``package`` is the importing file's package (``filepath_to_package``),
    which a relative from-import is resolved against.
    """

    def __init__(self, package: str) -> None:
        self.package = package

    def source(self, node: ast.ImportFrom) -> str:
        """The absolute dotted path of the module a from-import imports from."""
        return resolve_relative_import(node.module, node.level, self.package)

    def records(self, node: ast.Import | ast.ImportFrom) -> Iterator[ModuleImport]:
        """The names one import statement binds, in its alias order."""
        if isinstance(node, ast.ImportFrom):
            source = self.source(node)
            for alias in node.names:
                yield ModuleImport(
                    bound=alias.asname or alias.name,
                    dotted=f"{source}.{alias.name}",
                    source=source,
                    name=alias.name,
                )
            return
        for alias in node.names:
            bound = import_binding(alias)
            yield ModuleImport(
                bound=bound, dotted=alias.name if alias.asname else bound, source=None, name=None
            )


def build_module_imports(
    all_trees: dict[Path, ast.Module], package_dirs: Collection[tuple[str, ...]]
) -> dict[Path, list[ModuleImport]]:
    """Every import binding in each module, in ``ast.walk`` order.

    Imports anywhere in the tree count, including those inside functions.
    The order is the walk's so that a caller replaying the list, last
    binding wins, sees what one walk of the tree would have seen.
    """
    index: dict[Path, list[ModuleImport]] = {}
    for filepath, tree in all_trees.items():
        reader = ImportReader(filepath_to_package(str(filepath), package_dirs))
        index[filepath] = [
            record
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for record in reader.records(node)
        ]
    return index


# The module-level name pytest reads for the plugins a module requires.
PYTEST_PLUGINS = "pytest_plugins"


def declared_pytest_plugins(tree: ast.Module) -> tuple[str, ...]:
    """The module names a module-level ``pytest_plugins`` assignment lists.

    pytest accepts one string or a sequence of them; the last assignment is
    the one pytest reads. An entry that is not a string literal names
    nothing pysmelly can follow.
    """
    declared: tuple[str, ...] = ()
    for node in tree.body:
        values = [v for target, v in assigned_pairs(node) if is_bare_name(target, PYTEST_PLUGINS)]
        if not values:
            continue
        value = values[0]
        items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        declared = tuple(
            item.value
            for item in items
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return declared


def build_plugin_graph(
    all_trees: dict[Path, ast.Module], by_module: dict[str, list[Path]]
) -> dict[Path, frozenset[Path]]:
    """Per file, the analyzed modules its ``pytest_plugins`` declaration names.

    Names resolve as imports do, through ``by_module``
    (``ModuleGraph.by_module``). A file declaring nothing, or naming only
    modules outside the analysis, is left out.
    """
    graph: dict[Path, frozenset[Path]] = {}
    for filepath, tree in all_trees.items():
        named = {
            path
            for module in declared_pytest_plugins(tree)
            for path in by_module.get(module, ())
            if path != filepath
        }
        if named:
            graph[filepath] = frozenset(named)
    return graph


def build_subscript_string_keys(all_trees: dict[Path, ast.Module]) -> dict[Path, frozenset[str]]:
    """Per file, every string constant read as a subscript key (``x["key"]``).

    Only loads count: ``x["key"] = v`` and ``del x["key"]`` do not.
    """
    index: dict[Path, frozenset[str]] = {}
    for filepath, tree in all_trees.items():
        index[filepath] = frozenset(
            node.slice.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        )
    return index


def build_call_index(all_trees: dict[Path, ast.Module]) -> dict[str, list[dict]]:
    """Build map of all function call sites across the codebase in a single pass.

    Returns {func_name: [{file, line, node, receiver_kind}, ...]} for every
    called name. ``receiver_kind`` is ``"name"`` for a bare ``f()`` call,
    ``"module"`` for a module-qualified ``mod.f()`` call (receiver root is an
    imported name), or ``"instance"`` for a method call ``self.f()`` / ``x.f()``
    on a value. The kind is a guess from the receiver's root name alone:
    ``os.environ.get()`` and ``Model.objects.get()`` are "module" because
    ``os`` and ``Model`` are imported. Which calls reach a given function is
    the caller-value index's question (``ctx.caller_values``, resolved by
    the Phase 21-3 rule), not this index's.
    """
    calls: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        imported = collect_imported_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                calls[node.func.id].append(
                    {
                        "file": str(filepath),
                        "line": node.lineno,
                        "node": node,
                        "receiver_kind": "name",
                    }
                )
            elif isinstance(node.func, ast.Attribute):
                # A receiver with no root name (a call, a literal) is never an import.
                kind = "module" if attr_root_name(node.func.value) in imported else "instance"
                calls[node.func.attr].append(
                    {
                        "file": str(filepath),
                        "line": node.lineno,
                        "node": node,
                        "receiver_kind": kind,
                    }
                )
    return calls


class ReferenceIndices(NamedTuple):
    """Indices built in a single pass over all AST trees."""

    import_index: dict[str, set[str]]
    value_references: dict[str, set[str]]
    dotted_string_suffixes: set[str]
    decorator_names: set[str]


@dataclass
class _ReferenceIndexBuild:
    """Mutable accumulators for the single-pass reference-index build."""

    import_index: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    value_references: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    dotted_string_suffixes: set[str] = field(default_factory=set)
    decorator_names: set[str] = field(default_factory=set)

    def finish(self) -> ReferenceIndices:
        """Freeze the accumulators into the public, plain-dict index tuple."""
        return ReferenceIndices(
            import_index=dict(self.import_index),
            value_references=dict(self.value_references),
            dotted_string_suffixes=self.dotted_string_suffixes,
            decorator_names=self.decorator_names,
        )


def referenced_names(exprs: Iterable[ast.expr]) -> Iterator[str]:
    """The names *exprs* refer to, skipping expressions that name nothing.

    The one place the "resolve a sequence of expressions to names" filter
    lives: :func:`referenced_name` returning None means "no name here", so
    callers want the surviving names, not a per-expression None check.
    """
    for expr in exprs:
        name = referenced_name(expr)
        if name is not None:
            yield name


def _index_import_from(node: ast.ImportFrom, file_str: str, build: _ReferenceIndexBuild) -> None:
    """Record every name a ``from x import a, b`` statement pulls into *file_str*."""
    for alias in node.names:
        build.import_index[alias.name].add(file_str)


def _index_value_references(
    node: ast.Dict | ast.List | ast.Tuple | ast.Call,
    file_str: str,
    build: _ReferenceIndexBuild,
) -> None:
    """Record the names *node* references from a value position.

    Value positions are dict values, list/tuple elements, and call arguments
    and keyword values — a name there refers to something rather than binding
    it. Dict *values* are never ``None``: only a ``key`` is, for a
    ``{**expansion}`` entry whose operand is the value.
    """
    if isinstance(node, ast.Dict):
        operands: list[ast.expr] = list(node.values)
    elif isinstance(node, (ast.List, ast.Tuple)):
        operands = list(node.elts)
    else:
        operands = [*node.args, *(keyword.value for keyword in node.keywords)]
    for name in referenced_names(operands):
        build.value_references[name].add(file_str)


# A string naming a definition or module: ``pkg.mod.func`` or ``pkg.mod:func``.
_DOTTED_STRING = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?")


def dotted_strings(node: ast.AST) -> list[str]:
    """The dotted path a string constant spells (``"pkg.mod:func"`` as ``pkg.mod.func``).

    The whole string must be the path, with at least one ``.`` or ``:``; any
    other node, and any other string, spells none.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return []
    text = node.value
    if ("." in text or ":" in text) and _DOTTED_STRING.fullmatch(text):
        return [text.replace(":", ".")]
    return []


def _index_dotted_string(node: ast.Constant, _file_str: str, build: _ReferenceIndexBuild) -> None:
    """Record the final dotted component of a dotted string constant.

    ``"pkg.module.name"`` -> ``"name"``. Non-string constants, strings with no
    dot, and strings ending in a dot contribute nothing.
    """
    if not isinstance(node.value, str) or "." not in node.value:
        return
    suffix = node.value.rsplit(".", 1)[-1]
    if suffix:
        build.dotted_string_suffixes.add(suffix)


def _index_decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    _file_str: str,
    build: _ReferenceIndexBuild,
) -> None:
    """Record the name of each decorator applied to *node*.

    Covers all four spellings — ``@d``, ``@d()``, ``@m.d`` and ``@m.d()`` — by
    resolving a decorator call's callee exactly as a bare decorator is
    resolved.
    """
    targets = [decorator_target(decorator) for decorator in node.decorator_list]
    build.decorator_names.update(referenced_names(targets))


# Node type -> the indexer that reads it. Exact-type dispatch: every entry is a
# concrete ast node class, so a dict lookup replaces a walk-long isinstance chain.
_REFERENCE_INDEXERS: dict[type[ast.AST], Callable[[Any, str, _ReferenceIndexBuild], None]] = {
    ast.ImportFrom: _index_import_from,
    ast.Dict: _index_value_references,
    ast.List: _index_value_references,
    ast.Tuple: _index_value_references,
    ast.Call: _index_value_references,
    ast.Constant: _index_dotted_string,
    ast.FunctionDef: _index_decorators,
    ast.AsyncFunctionDef: _index_decorators,
    ast.ClassDef: _index_decorators,
}


def build_reference_indices(all_trees: dict[Path, ast.Module]) -> ReferenceIndices:
    """Single-pass builder for import, value-reference, dotted-string, and decorator indices.

    ``value_references`` maps a name → the set of files where the name appears
    as a dict value, list/tuple element, or call argument. The per-file map
    lets callers distinguish production-only references from test-only ones.

    All four indices share one ``ast.walk`` per tree — four walks cost four
    times as much — so the loop body only looks the node's type up in
    :data:`_REFERENCE_INDEXERS`; each indexer owns one index's knowledge.
    """
    build = _ReferenceIndexBuild()
    for filepath, tree in all_trees.items():
        file_str = str(filepath)
        for node in ast.walk(tree):
            indexer = _REFERENCE_INDEXERS.get(type(node))
            if indexer is not None:
                indexer(node, file_str, build)
    return build.finish()


def is_imported_elsewhere(func_name: str, def_file: str, ctx: AnalysisContext) -> bool:
    """Check if a function is imported in any other file (O(1) via cached index)."""
    importing_files = ctx.references.import_index.get(func_name, set())
    return bool(importing_files - {def_file})


def is_imported_in_production_elsewhere(
    func_name: str, def_file: str, ctx: AnalysisContext
) -> bool:
    """Check if a function is imported by a *non-test* file other than its own.

    Test code calling production code does not justify the production code's
    existence — so checks like single-call-site, which gate on real usage,
    should ignore imports that live under test/ trees.
    """
    importing_files = ctx.references.import_index.get(func_name, set()) - {def_file}
    return any(not ctx.roles.is_test(f) for f in importing_files)


def is_referenced_as_dotted_string(func_name: str, ctx: AnalysisContext) -> bool:
    """Check if a function name appears as the final component of a dotted-path string."""
    return func_name in ctx.references.dotted_string_suffixes


def is_used_as_decorator(func_name: str, ctx: AnalysisContext) -> bool:
    """Check if a function name is used as a decorator anywhere."""
    return func_name in ctx.references.decorator_names


def _is_name_or_attr(node: ast.expr, name: str) -> bool:
    """Check if a node is a reference to ``name`` (bare or via attribute access).

    Matches ``func_name`` (ast.Name) and ``obj.func_name`` (ast.Attribute),
    e.g. ``views.home`` in Django URL confs.
    """
    return referenced_name(node) == name


def is_referenced_as_value(func_name: str, ctx: AnalysisContext) -> bool:
    """Check if a function name appears as a dict value, list element, or argument.

    O(1) via cached index.
    """
    return bool(ctx.references.value_references.get(func_name))


def is_referenced_as_value_in_production(func_name: str, ctx: AnalysisContext) -> bool:
    """Check if a function name is referenced as a value in any *non-test* file.

    Production dispatch tables (e.g. ``CATEGORY_LENS = {"key": my_func, ...}``)
    are the real fan-out for callbacks — the single direct call we see is
    only one path among many. Test-only references don't count.
    """
    return any(
        not ctx.roles.is_test(f) for f in ctx.references.value_references.get(func_name, set())
    )


def _dunder_all_elements(tree: ast.Module) -> Iterator[ast.expr]:
    """The element expressions of every module-level ``__all__`` list or tuple.

    An ``__all__`` built any other way — a concatenation, a comprehension, a
    name — yields nothing, because there are no elements to read statically.
    """
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(is_bare_name(target, "__all__") for target in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            yield from node.value.elts


def is_in_dunder_all(name: str, tree: ast.Module) -> bool:
    """Check if a name is listed in the module's ``__all__``."""
    return any(_is_literal(elt, name) for elt in _dunder_all_elements(tree))


def _is_exception_base(name: str) -> bool:
    """Check if a class name looks like an exception base class."""
    if name in _EXCEPTION_BASES:
        return True
    return name.endswith("Error") or name.endswith("Exception")


def build_exception_index(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[dict]]:
    """Build map of custom exception class definitions across the codebase.

    Returns classes whose bases include known exception types or names
    ending in Error/Exception.
    """
    exc_defs: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.bases:
                continue
            is_exc = False
            for base in node.bases:
                if isinstance(base, ast.Name) and _is_exception_base(base.id):
                    is_exc = True
                    break
                if isinstance(base, ast.Attribute) and _is_exception_base(base.attr):
                    is_exc = True
                    break
            if is_exc:
                exc_defs[node.name].append({"file": str(filepath), "line": node.lineno})
    return exc_defs


class RaiseSite(NamedTuple):
    """One ``raise`` statement, located by file, line and column."""

    file: Path
    lineno: int
    col_offset: int


# One scope's variables, each mapped to the names its assignments construct or
# alias: ``err = E(...)`` and ``err = mod.E`` both map ``err`` to ``["E"]``.
_ScopeBindings = dict[str, list[str]]

_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _bound_name(value: ast.expr) -> str | None:
    """The name an assigned value constructs or aliases, as ``raise`` would read it."""
    if isinstance(value, ast.Call):
        value = value.func
    return referenced_name(value)


def _record_bindings(node: ast.AST, bindings: _ScopeBindings) -> None:
    """Add what a plain or annotated assignment to a bare name binds it to."""
    for target, value in assigned_pairs(node):
        name = _bound_name(value)
        if name is not None and isinstance(target, ast.Name):
            bindings.setdefault(target.id, []).append(name)


def _raises_with_scope_chains(
    tree: ast.Module,
) -> list[tuple[ast.Raise, list[_ScopeBindings]]]:
    """Each ``raise`` in *tree*, in source order, with its scope's bindings and its enclosing ones'.

    Chains run innermost first: a function, the functions around it, then the
    module. Class bodies are not scopes of their own (their methods cannot
    see class-level names), so their assignments count toward the enclosing
    scope. The bindings are filled during the same walk, so a chain is only
    complete once the whole tree has been visited. The walk keeps its own
    stack, as ``ast.walk`` does, so deeply nested expressions cannot exhaust
    the interpreter's recursion limit.
    """
    raises: list[tuple[ast.Raise, list[_ScopeBindings]]] = []
    stack: list[tuple[ast.AST, list[_ScopeBindings]]] = [(tree, [{}])]
    while stack:
        node, chain = stack.pop()
        if isinstance(node, _FUNCTION_SCOPES):
            chain = [{}, *chain]
        _record_bindings(node, chain[0])
        if isinstance(node, ast.Raise):
            raises.append((node, chain))
        children = list(ast.iter_child_nodes(node))
        stack.extend((child, chain) for child in reversed(children))
    return raises


def _raised_names(node: ast.Raise, chain: list[_ScopeBindings]) -> list[str]:
    """The names a ``raise`` statement raises; empty when it names none.

    ``raise E`` and ``raise E(...)`` give ``E``; ``raise mod.E`` and
    ``raise mod.E(...)`` give ``E``. ``raise err``, where the nearest scope
    that assigns ``err`` binds it to ``E(...)`` or ``E``, gives ``E`` (every
    such name, when it is assigned more than once); a name that scope does
    not bind that way, such as the target of ``except E as err``, gives
    itself. A bare ``raise``, or a raise of any other expression (a
    subscript, a call of a call), names nothing: a re-raise does not
    originate an exception.
    """
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    if isinstance(exc, ast.Name):
        for bindings in chain:
            if exc.id in bindings:
                return bindings[exc.id]
        return [exc.id]
    if isinstance(exc, ast.Attribute):
        return [exc.attr]
    return []


def build_raise_index(all_trees: dict[Path, ast.Module]) -> dict[str, list[RaiseSite]]:
    """Every ``raise`` site across the codebase, keyed by the name it raises.

    One walk per tree answers "is this exception raised anywhere, and where"
    for every candidate, instead of one walk of every tree per candidate.
    A raise of a variable is keyed by the exception its assignment builds
    (:func:`_raised_names`). Sites are listed in file order, then source
    order within a file.
    """
    index: dict[str, list[RaiseSite]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        for node, chain in _raises_with_scope_chains(tree):
            for name in dict.fromkeys(_raised_names(node, chain)):
                index[name].append(RaiseSite(filepath, node.lineno, node.col_offset))
    return dict(index)


def caught_exception_exprs(handler: ast.ExceptHandler) -> list[ast.expr]:
    """The exception expressions one ``except`` clause names.

    ``except E:`` gives one, ``except (A, B):`` gives each element, and a bare
    ``except:`` gives none.
    """
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return list(handler.type.elts)
    return [handler.type]


def is_caught_anywhere(exc_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if an exception class is caught in any except handler."""
    return any(
        _is_name_or_attr(caught, exc_name)
        for tree in all_trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        for caught in caught_exception_exprs(node)
    )


def base_class_name(base: ast.expr) -> str | None:
    """The class name a base expression names: ``B``, ``mod.B`` and ``B[T]`` give ``B``.

    Returns:
        The name, or None for a base that names no class (a call's result).
    """
    target = base.value if isinstance(base, ast.Subscript) else base
    return referenced_name(target)


class ProjectClasses:
    """Every project class by name, and whether a method call's receiver ties
    the call to a given class.

    ``Cls.m()``, and ``self.m()`` or ``cls.m()`` inside a class, are tied to
    *owner* when that class is *owner* or reaches it through project bases.
    Any other receiver (``obj.m()``, ``super().m()``) is not tied: it may be
    the class's instance or a library object.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.by_name: dict[str, list[ast.ClassDef]] = defaultdict(list)
        for tree in ctx.all_trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.by_name[node.name].append(node)

    def bases_of(self, cls: ast.ClassDef) -> Iterator[ast.ClassDef]:
        """The project classes ``cls``'s bases name (every class of each name)."""
        for base in cls.bases:
            name = base_class_name(base)
            if name is not None:
                yield from self.by_name.get(name, [])

    def descends(self, cls: ast.ClassDef, owner: ast.ClassDef) -> bool:
        """Whether ``cls`` is ``owner`` or reaches it through project bases."""
        pending, seen = [cls], set()
        while pending:
            current = pending.pop()
            if current is owner:
                return True
            if id(current) in seen:
                continue
            seen.add(id(current))
            pending.extend(self.bases_of(current))
        return False

    def tied(self, call: dict, owner: ast.ClassDef) -> bool:
        """Whether the method call ``call`` (a call-index entry) is tied to ``owner``."""
        receiver = call["node"].func.value
        if not isinstance(receiver, ast.Name):
            return False
        if receiver.id not in {"self", "cls"}:
            return any(self.descends(cls, owner) for cls in self.by_name.get(receiver.id, []))
        parents = self.ctx.parent_map(self.ctx.all_trees[Path(call["file"])])
        current: ast.AST = call["node"]
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.ClassDef):
                return self.descends(current, owner)
        return False


def _inherits_from(cls: ast.ClassDef, class_name: str) -> bool:
    """True when *cls* lists *class_name* among its bases.

    A bare-name base is not counted when the class's own name matches it — a
    self-reference cannot be a real subclass — while a dotted base
    (``mod.Thing``) always counts, since it names a definition elsewhere.
    """
    for base in cls.bases:
        if isinstance(base, ast.Attribute):
            if base.attr == class_name:
                return True
        elif is_bare_name(base, class_name) and cls.name != class_name:
            return True
    return False


def is_subclassed(class_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a class is used as a base class anywhere."""
    return any(
        _inherits_from(node, class_name)
        for tree in all_trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    )


def _isinstance_operands(target: ast.expr) -> list[ast.expr]:
    """The type expressions an isinstance target names: one, or a tuple's elements."""
    if isinstance(target, ast.Tuple):
        return list(target.elts)
    return [target]


def is_isinstance_target(class_name: str, all_trees: dict[Path, ast.Module]) -> bool:
    """Check if a class is used as a target of isinstance() or issubclass()."""
    return class_name in {
        operand.id
        for tree in all_trees.values()
        for _call, target in iter_isinstance_targets(tree)
        for operand in _isinstance_operands(target)
        if isinstance(operand, ast.Name)
    }


# --- Shared helpers for pattern-based checks ---


class SimpleAssign(NamedTuple):
    """An assignment statement whose sole target is a bare name."""

    stmt: ast.Assign
    target: ast.Name
    value: ast.expr


def iter_simple_assigns(nodes: Iterable[ast.AST]) -> Iterator[SimpleAssign]:
    """Yield every ``name = <value>`` statement among *nodes*.

    Pass ``ast.iter_child_nodes(tree)`` for module scope or ``ast.walk(...)``
    for a whole subtree. Everything else is skipped: tuple unpacking, multiple
    targets, attribute and subscript targets, annotated and augmented
    assignment, and every node that is not an assignment at all.
    """
    for node in nodes:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        yield SimpleAssign(node, target, node.value)


def enclosing_functions(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every function enclosing ``node``, innermost first.

    Nothing is yielded for a module-level node. Python resolves a free name
    through these scopes in this order, so a caller deciding what a name
    refers to walks all of them; one that wants only the innermost takes
    ``next(..., None)``.
    """
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield current


def get_param_names(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Get all parameter names from a function definition."""
    names = {a.arg for a in func_node.args.args}
    names |= {a.arg for a in func_node.args.posonlyargs}
    names |= {a.arg for a in func_node.args.kwonlyargs}
    if func_node.args.vararg:
        names.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        names.add(func_node.args.kwarg.arg)
    return names


def iter_uppercase_assigns(
    tree: ast.Module,
) -> list[tuple[str, int, ast.expr]]:
    """Return (name, lineno, value_node) for UPPER_CASE module-level assignments."""
    results: list[tuple[str, int, ast.expr]] = []
    for stmt, target, value in iter_simple_assigns(ast.iter_child_nodes(tree)):
        name = target.id
        if not name.isupper() or name.startswith("_"):
            continue
        results.append((name, stmt.lineno, value))
    return results


# Statements that open a new scope: a walk looking for assignments to one
# name must not descend into them.
_SCOPE_STMTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _bare_names_assigned(node: ast.AST) -> list[str]:
    """The bare names an assignment statement *node* binds, in target order.

    Covers ``name = ...`` (chained targets included) and ``name += ...``. An
    attribute, subscript or unpacking target binds no bare name here, and an
    annotated assignment is a declaration this rule has never counted.
    """
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AugAssign) and (target := bare_target(node)) is not None:
        return [target.id]
    return []


def _assigns_to_name(node: ast.AST, name: str) -> bool:
    """True when *node* is an assignment binding the bare *name*."""
    return name in _bare_names_assigned(node)


def _walk_function_scope(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node of a function *body* outside the scopes nested below it.

    The filter screens the children of each node it pops, so a ``def`` or
    ``class`` written straight into *body* is itself descended into one
    level; one nested under any other statement is not.
    """
    todo: list[ast.AST] = list(body)
    while todo:
        child = todo.pop()
        yield child
        todo.extend(sub for sub in ast.iter_child_nodes(child) if not isinstance(sub, _SCOPE_STMTS))


def walk_name_assignments(body: list[ast.stmt], name: str) -> tuple[bool, bool]:
    """Walk a function body for global declarations and assignments to *name*.

    Skips nested function/class scopes.  Returns (has_global, has_assign).
    """
    has_global = False
    has_assign = False
    for child in _walk_function_scope(body):
        if isinstance(child, ast.Global) and name in child.names:
            has_global = True
        if _assigns_to_name(child, name):
            has_assign = True
    return has_global, has_assign


RebindingKind = Literal["assign", "aug-assign", "delete", "global-assign"]


class NameRebinding(NamedTuple):
    """One statement in a module that (re)binds or deletes a module-level name.

    ``lineno`` is the statement's line, except for ``global-assign``, where it
    is the line of the function that declares the name ``global`` and assigns it.
    """

    lineno: int
    kind: RebindingKind


def _module_level_rebindings(tree: ast.Module) -> Iterator[tuple[str, NameRebinding]]:
    """Top-level ``Assign``/``AugAssign``/``Delete`` statements, one entry per bare name.

    Only the module's own statements count: a class body, or a statement
    nested under ``if``/``try``/``for`` at module level, is not visited.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            kind: RebindingKind = "assign"
        elif isinstance(node, ast.AugAssign):
            kind = "aug-assign"
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, NameRebinding(node.lineno, "delete")
            continue
        else:
            continue
        for name in _bare_names_assigned(node):
            yield name, NameRebinding(node.lineno, kind)


def _global_assignments(tree: ast.Module) -> Iterator[tuple[str, NameRebinding]]:
    """Names a function (at any depth) both declares ``global`` and assigns.

    One entry per function and name, both looked for in the same
    :func:`_walk_function_scope` as :func:`walk_name_assignments` uses.
    """
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared: set[str] = set()
        assigned: set[str] = set()
        for child in _walk_function_scope(func.body):
            if isinstance(child, ast.Global):
                declared.update(child.names)
            assigned.update(_bare_names_assigned(child))
        for name in sorted(declared & assigned):
            yield name, NameRebinding(func.lineno, "global-assign")


def build_module_rebindings(
    all_trees: dict[Path, ast.Module],
) -> dict[Path, dict[str, list[NameRebinding]]]:
    """Every site in each module that rebinds a module-level name, by file then name.

    One pass per tree answers :func:`is_constant_reassigned` for every
    candidate in that file, instead of one walk of the tree per candidate.
    """
    index: dict[Path, dict[str, list[NameRebinding]]] = {}
    for filepath, tree in all_trees.items():
        by_name: dict[str, list[NameRebinding]] = defaultdict(list)
        for name, rebinding in _module_level_rebindings(tree):
            by_name[name].append(rebinding)
        for name, rebinding in _global_assignments(tree):
            by_name[name].append(rebinding)
        index[filepath] = dict(by_name)
    return index


def is_constant_reassigned(
    rebindings: dict[str, list[NameRebinding]], name: str, def_lineno: int
) -> bool:
    """Whether a module-level constant defined at *def_lineno* is rebound in its file.

    *rebindings* is the file's entry in ``ctx.module_rebindings``. Every site
    counts except the plain assignment on the defining line itself; an
    augmented assignment or ``del`` there still does.
    """
    return any(
        not (site.kind == "assign" and site.lineno == def_lineno)
        for site in rebindings.get(name, ())
    )
