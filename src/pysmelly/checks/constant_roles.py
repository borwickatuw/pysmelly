"""Named constants the surrounding code bypasses or decodes.

Two "B arrived, A still does it the old way" checks over module constants:

- ``unnamed-constant-use``: a module constant exists, and its literal value
  is spelled inline in a *role* the constant itself fills somewhere: the
  same keyword argument or parameter, compared with the same field, stored
  under the same dict key or the same container's keys, returned from a
  function of that name.
- ``type-encoded-as-data``: right after a call, the caller compares a name
  field of the result (``.tool.name``, ``.kind``, ``params["engine"]``)
  with another module's constant to learn which branch the call took.

A role is an identifier, read from where a value sits: the keyword's name
in ``f(stamp_key=X)``, the other operand's last name in ``x.stamp_key ==
X``, the key in ``{"stamp_key": X}``, the function's name for ``return X``.
A value used as a key reads ``key:`` plus the container's role. Equal values
in different roles are coincidences, which scattered-constants and not this
check is about.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from pysmelly.checks.helpers import (
    assigned_pairs,
    filepath_to_module,
    is_constant_reassigned,
    referenced_name,
    source_trees,
)
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check

# UPPER_CASE, optionally private: ``STAMP_POLICY``, ``_GRANULARITY``.
_CONSTANT_NAME = re.compile(r"_*[A-Z][A-Z0-9_]*")

# Methods whose first argument is a key into the receiver.
_KEY_METHODS = frozenset({"get", "pop", "setdefault"})

# The role prefix for a value used as a key: ``key:params`` is "a key of params".
_KEY_ROLE = "key:"

LiteralValue = str | bytes | int | float


class NamedConstant(NamedTuple):
    """A module-level UPPER_CASE name bound once to a literal."""

    file: Path
    name: str
    line: int
    value: LiteralValue

    def offered_to(
        self, filepath: Path, imported: frozenset[Path], in_reach: frozenset[Path]
    ) -> bool:
        """Whether a literal in *filepath* bypasses this constant rather than coinciding with it.

        Its own module always is offered it. Elsewhere, a private constant
        (``_NAME``) is its module's business, and a script nobody imports
        (*imported* is every file some file imports) offers its constants to
        nobody. A number must also be in reach (*in_reach* is what
        *filepath* imports): ``timeout=60`` is a different minute in every
        module, so a numeric constant is offered only where its module is
        already imported.
        """
        if self.file == filepath:
            return True
        if self.name.startswith("_") or self.file not in imported:
            return False
        return isinstance(self.value, (str, bytes)) or self.file in in_reach


class RoleUse(NamedTuple):
    """Where a constant was seen filling a role."""

    file: Path
    line: int


def _literal_value(node: ast.AST) -> LiteralValue | None:
    """The str/bytes/int/float a literal expression spells, else None.

    ``-1`` parses as ``USub`` over ``1``, so a negated number is read here.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal_value(node.operand)
        if isinstance(inner, (int, float)):
            return -inner
        return None
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return None
    if isinstance(node.value, (str, bytes, int, float)):
        return node.value
    return None


def _is_distinctive(value: LiteralValue) -> bool:
    """Whether a value says enough to be worth naming: no 0, 1, 2, -1 or one-character strings."""
    if isinstance(value, (str, bytes)):
        return len(value) > 1
    return abs(value) > 2


def _value_key(value: LiteralValue) -> tuple[str, LiteralValue]:
    """Equality key that keeps ``1`` and ``1.0`` (and ``"1"``) apart."""
    return type(value).__name__, value


def _normalize(identifier: str | None) -> str | None:
    """A role spelled the way every site compares it: no leading underscores, lower case.

    None, and a name that is only underscores, normalize to None: no role.
    """
    if not identifier:
        return None
    return identifier.lstrip("_").lower() or None


def _key_role(container: str | None) -> str | None:
    """The role of a value used as a key into *container*."""
    return _KEY_ROLE + container if container else None


# --- identifiers and roles ---------------------------------------------------


def _string_key(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class KeyAccess(NamedTuple):
    """``receiver.get(key, ...)``: the receiver and the key expression."""

    receiver: ast.expr
    key: ast.expr


def _key_method_call(node: ast.AST) -> KeyAccess | None:
    """The receiver and key of ``obj.get(key, ...)`` (or ``pop``/``setdefault``), else None."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _KEY_METHODS:
        return KeyAccess(func.value, node.args[0])
    return None


def _identifier(node: ast.expr) -> str | None:
    """The last name an operand spells: ``x``, ``a.x``, ``a["x"]``, ``a.get("x")``.

    A call's result has no field name to read (``len(x)`` is not a role),
    so any other call spells none.
    """
    if isinstance(node, ast.Subscript):
        return _normalize(_string_key(node.slice))
    getter = _key_method_call(node)
    if getter is not None:
        return _normalize(_string_key(getter.key))
    return _normalize(referenced_name(node))


class Position(NamedTuple):
    """Where a value sits: the node, its parent, and the tree's parent map.

    ``parent`` is typed ``Any`` because each reader in ``_ROLE_READERS`` is
    chosen by the parent's exact type and reads that type's fields.
    """

    child: ast.AST
    parent: Any
    parents: dict[ast.AST, ast.AST]


def _keyword_role(pos: Position) -> str | None:
    return _normalize(pos.parent.arg)


def _default_role(pos: Position) -> str | None:
    args: ast.arguments = pos.parent
    positional = [*args.posonlyargs, *args.args]
    defaulted = positional[len(positional) - len(args.defaults) :]
    pairs = [
        *zip(defaulted, args.defaults, strict=True),
        *zip(args.kwonlyargs, args.kw_defaults, strict=True),
    ]
    for arg, default in pairs:
        if default is pos.child:
            return _normalize(arg.arg)
    return None


def _compare_role(pos: Position) -> str | None:
    compare: ast.Compare = pos.parent
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], (ast.Eq, ast.NotEq)):
        return None
    other = compare.comparators[0] if pos.child is compare.left else compare.left
    return _identifier(other)


def _membership_role(pos: Position) -> str | None:
    """``x in ("a", B)``: each element is compared with ``x``."""
    compare = pos.parents.get(pos.parent)
    if not isinstance(compare, ast.Compare) or len(compare.ops) != 1:
        return None
    if not isinstance(compare.ops[0], (ast.In, ast.NotIn)):
        return None
    return _identifier(compare.left) if compare.comparators[0] is pos.parent else None


def _dict_role(pos: Position) -> str | None:
    """A value's role is its string key; a key's role is ``key:`` and the dict's role."""
    mapping: ast.Dict = pos.parent
    for key, value in zip(mapping.keys, mapping.values, strict=True):
        if value is pos.child:
            return _normalize(_string_key(key))
        if key is pos.child:
            return _key_role(_role_of(mapping, pos.parents))
    return None


def _subscript_role(pos: Position) -> str | None:
    subscript: ast.Subscript = pos.parent
    return _key_role(_identifier(subscript.value)) if subscript.slice is pos.child else None


def _call_role(pos: Position) -> str | None:
    getter = _key_method_call(pos.parent)
    if getter is None or getter.key is not pos.child:
        return None
    return _key_role(_identifier(getter.receiver))


def _target_role(target: ast.expr, pos: Position) -> str | None:
    """The role of an assignment's single target; a module constant's own value has none."""
    if (
        isinstance(target, ast.Name)
        and isinstance(pos.parents.get(pos.parent), ast.Module)
        and _CONSTANT_NAME.fullmatch(target.id)
    ):
        return None
    return _identifier(target)


def _assign_role(pos: Position) -> str | None:
    assign: ast.Assign = pos.parent
    if assign.value is not pos.child or len(assign.targets) != 1:
        return None
    return _target_role(assign.targets[0], pos)


def _ann_assign_role(pos: Position) -> str | None:
    assign: ast.AnnAssign = pos.parent
    return _target_role(assign.target, pos) if assign.value is pos.child else None


def _return_role(pos: Position) -> str | None:
    """A returned value fills the role its function is named for (``_stamp_key``)."""
    current = pos.parents.get(pos.parent)
    while current is not None and not isinstance(current, ast.Lambda):
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _normalize(current.name)
        current = pos.parents.get(current)
    return None


def _match_role(pos: Position) -> str | None:
    case = pos.parents.get(pos.parent)
    match = pos.parents.get(case) if isinstance(case, ast.match_case) else None
    return _identifier(match.subject) if isinstance(match, ast.Match) else None


_ROLE_READERS: dict[type[ast.AST], Callable[[Position], str | None]] = {
    ast.keyword: _keyword_role,
    ast.arguments: _default_role,
    ast.Compare: _compare_role,
    ast.Tuple: _membership_role,
    ast.List: _membership_role,
    ast.Set: _membership_role,
    ast.Dict: _dict_role,
    ast.Subscript: _subscript_role,
    ast.Call: _call_role,
    ast.Assign: _assign_role,
    ast.AnnAssign: _ann_assign_role,
    ast.Return: _return_role,
    ast.MatchValue: _match_role,
}


def _role_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    """The role the value at *node* fills, or None when its position names none.

    A conditional expression passes its branches' role through:
    ``applied = "rtl" if rtl else "ltr"`` gives both literals the role
    ``applied``.
    """
    child = node
    parent = parents.get(child)
    while isinstance(parent, ast.IfExp) and child is not parent.test:
        child, parent = parent, parents.get(parent)
    if parent is None:
        return None
    reader = _ROLE_READERS.get(type(parent))
    return reader(Position(child, parent, parents)) if reader else None


def _describe_role(role: str) -> str:
    if role.startswith(_KEY_ROLE):
        return f"a key of `{role.removeprefix(_KEY_ROLE)}`"
    return f"`{role}`"


# --- constants and how a file refers to them ---------------------------------


def _module_constants(
    filepath: Path, tree: ast.Module, ctx: AnalysisContext
) -> Iterator[NamedConstant]:
    """Every module-level UPPER_CASE name bound once, to a literal, in *tree*."""
    rebindings = ctx.module_rebindings[filepath]
    for stmt in tree.body:
        pairs = assigned_pairs(stmt)
        if len(pairs) != 1:
            continue
        ((target, value),) = pairs
        if not isinstance(target, ast.Name) or not _CONSTANT_NAME.fullmatch(target.id):
            continue
        literal = _literal_value(value)
        if literal is None or is_constant_reassigned(rebindings, target.id, stmt.lineno):
            continue
        yield NamedConstant(filepath, target.id, stmt.lineno, literal)


def _module_path(node: ast.expr, modules: dict[str, str]) -> str | None:
    """The dotted module an expression names through a file's imports: ``mod``, ``pkg.mod``."""
    if isinstance(node, ast.Name):
        return modules.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _module_path(node.value, modules)
        return f"{parent}.{node.attr}" if parent else None
    return None


class ConstantIndex:
    """Every project constant, and the resolver from a file's names to them."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.by_file: dict[Path, dict[str, NamedConstant]] = {}
        for filepath, tree in source_trees(ctx):
            constants = {c.name: c for c in _module_constants(filepath, tree, ctx)}
            if constants:
                self.by_file[filepath] = constants
        self.module_files = {
            filepath_to_module(str(filepath), ctx.package_dirs): filepath
            for filepath in ctx.all_trees
        }
        self._scopes: dict[Path, tuple[dict[str, NamedConstant], dict[str, str]]] = {}
        self._imported_files: frozenset[Path] = frozenset().union(*ctx.import_graph.values())

    def offers(self, constant: NamedConstant, filepath: Path) -> bool:
        """Whether *filepath* is offered *constant* (see ``NamedConstant.offered_to``)."""
        return constant.offered_to(
            filepath, self._imported_files, self.ctx.import_graph.get(filepath, frozenset())
        )

    def _scope(self, filepath: Path) -> tuple[dict[str, NamedConstant], dict[str, str]]:
        """A file's names for constants, and its names for modules (bound name -> dotted)."""
        if filepath not in self._scopes:
            names: dict[str, NamedConstant] = {}
            modules: dict[str, str] = {}
            for entry in self.ctx.module_imports[filepath]:
                if entry.source is None:
                    modules[entry.bound] = entry.dotted
                    continue
                if entry.dotted in self.module_files:
                    modules[entry.bound] = entry.dotted
                source_file = self.module_files.get(entry.source)
                imported = self.by_file.get(source_file, {}) if source_file else {}
                if entry.name in imported:
                    names[entry.bound] = imported[entry.name]
            names.update(self.by_file.get(filepath, {}))
            self._scopes[filepath] = (names, modules)
        return self._scopes[filepath]

    def resolve(self, filepath: Path, node: ast.expr) -> NamedConstant | None:
        """The constant *node* names in *filepath*: ``NAME``, ``mod.NAME`` or ``pkg.mod.NAME``."""
        names, modules = self._scope(filepath)
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if not isinstance(node, ast.Attribute):
            return None
        module = _module_path(node.value, modules)
        module_file = self.module_files.get(module) if module else None
        return self.by_file.get(module_file, {}).get(node.attr) if module_file else None


def _constant_roles(
    index: ConstantIndex,
) -> dict[NamedConstant, dict[str, RoleUse]]:
    """Every role each constant is seen filling, with its first site."""
    roles: dict[NamedConstant, dict[str, RoleUse]] = defaultdict(dict)
    for filepath, tree in index.ctx.all_trees.items():
        parents = index.ctx.parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(
                node.ctx, ast.Load
            ):
                continue
            constant = index.resolve(filepath, node)
            if constant is None:
                continue
            role = _role_of(node, parents)
            if role is not None:
                roles[constant].setdefault(role, RoleUse(filepath, node.lineno))
    return roles


# --- unnamed-constant-use ----------------------------------------------------


def _format_constant(constant: NamedConstant) -> str:
    return f"`{constant.name}` ({constant.file}:{constant.line})"


class LiteralSite(NamedTuple):
    """A literal in a role: where, what value, which role."""

    line: int
    value: LiteralValue
    role: str


def _literal_sites(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> Iterator[LiteralSite]:
    """Every str/bytes/int/float literal in *tree* whose position names a role.

    The constant inside ``-5`` is skipped; the ``-5`` around it is the literal.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.UnaryOp)):
            continue
        value = _literal_value(node)
        if value is None or isinstance(parents.get(node), ast.UnaryOp):
            continue
        role = _role_of(node, parents)
        if role is not None:
            yield LiteralSite(node.lineno, value, role)


def _constants_by_value(
    index: ConstantIndex,
) -> dict[tuple[str, LiteralValue], list[NamedConstant]]:
    """The distinctive constants (see ``_is_distinctive``), grouped by value."""
    by_value: dict[tuple[str, LiteralValue], list[NamedConstant]] = defaultdict(list)
    for constants in index.by_file.values():
        for constant in constants.values():
            if _is_distinctive(constant.value):
                by_value[_value_key(constant.value)].append(constant)
    return by_value


def _unnamed_message(
    site: LiteralSite, matched: list[NamedConstant], roles: dict[NamedConstant, dict[str, RoleUse]]
) -> str:
    first, *others = sorted(matched, key=lambda c: (str(c.file), c.line))
    use = roles[first][site.role]
    alternatives = "".join(f", or {_format_constant(c)}" for c in others)
    return (
        f"Literal {site.value!r} fills {_describe_role(site.role)} here, the role "
        f"{_format_constant(first)}{alternatives} fills at {use.file}:{use.line}"
    )


@check(
    "unnamed-constant-use",
    severity=Severity.MEDIUM,
    description=(
        "A literal spelled inline where a module constant with that value fills the same role"
    ),
    advice=Advice(
        fix="Replace the literal with the constant named, importing it where needed.",
        dismiss=(
            "Dismiss only if this value only shares its spelling: a different domain that"
            " would not change with the constant."
        ),
    ),
)
def check_unnamed_constant_use(ctx: AnalysisContext) -> list[Finding]:
    """Find literals that bypass a named constant in the role the constant fills.

    The constant is UPPER_CASE, module level, bound once to a str/bytes/int/
    float literal, and seen at least once in a role (keyword, comparison,
    dict key, assignment target, return). A literal of the same value in the
    same role elsewhere is the finding; the same value in another role is
    not.
    """
    index = ConstantIndex(ctx)
    by_value = _constants_by_value(index)
    if not by_value:
        return []
    roles = _constant_roles(index)

    findings = []
    for filepath, tree in source_trees(ctx):
        for site in _literal_sites(tree, ctx.parent_map(tree)):
            matched = [
                constant
                for constant in by_value.get(_value_key(site.value), ())
                if site.role in roles.get(constant, {}) and index.offers(constant, filepath)
            ]
            if matched:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=site.line,
                        check="unnamed-constant-use",
                        message=_unnamed_message(site, matched, roles),
                        severity=Severity.MEDIUM,
                    )
                )
    return findings


# --- type-encoded-as-data ----------------------------------------------------

# Fields whose value names what produced a result or which kind it is.
_NAME_FIELDS = frozenset(
    {"name", "kind", "type", "tag", "variant", "engine", "backend", "provider", "strategy", "mode"}
)
# A ``*_type`` field is left out: ``mime_type`` and ``content_type`` are data.
_NAME_FIELD_SUFFIXES = ("_name", "_kind", "_tag")

# How many statements after the call a comparison still reads its result
# "right after" it: an unpack and a field pick, then the branch.
_FOLLOWING_STATEMENTS = 3


def _is_name_field(identifier: str | None) -> bool:
    if identifier is None:
        return False
    return identifier in _NAME_FIELDS or identifier.endswith(_NAME_FIELD_SUFFIXES)


def _chain_root(node: ast.expr) -> ast.expr:
    """The expression an attribute/subscript/``.get`` chain starts from."""
    while True:
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
            continue
        getter = _key_method_call(node)
        if getter is None:
            return node
        node = getter.receiver


def _unwrap_call(node: ast.expr) -> ast.Call | None:
    if isinstance(node, ast.Await):
        node = node.value
    return node if isinstance(node, ast.Call) else None


def _bound_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)


def _walk_statement(stmt: ast.stmt) -> Iterator[ast.AST]:
    """Every node of *stmt*, not descending into nested functions, lambdas or classes."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
            ):
                stack.append(child)


class DecodedCompare(NamedTuple):
    """A comparison of a result's name field with another module's constant."""

    line: int
    field: str
    constant: NamedConstant
    constant_source: str


def _decoded_compares(
    stmt: ast.stmt, results: set[str], filepath: Path, index: ConstantIndex
) -> Iterator[DecodedCompare]:
    """Comparisons in *stmt* of a name field read off one of *results* with a foreign constant."""
    for node in _walk_statement(stmt):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op = node.ops[0]
        if isinstance(op, (ast.Eq, ast.NotEq)):
            pairs = [(node.left, node.comparators[0]), (node.comparators[0], node.left)]
        elif isinstance(op, (ast.In, ast.NotIn)) and isinstance(
            node.comparators[0], (ast.Tuple, ast.List, ast.Set)
        ):
            pairs = [(node.left, element) for element in node.comparators[0].elts]
        else:
            continue
        for field, other in pairs:
            if field is _chain_root(field) or not _is_name_field(_identifier(field)):
                continue
            root = _chain_root(field)
            if not (isinstance(root, ast.Name) and root.id in results):
                continue
            constant = index.resolve(filepath, other)
            if constant is None or constant.file == filepath:
                continue
            yield DecodedCompare(node.lineno, ast.unparse(field), constant, ast.unparse(other))


def _derived_names(stmt: ast.stmt, tainted: set[str]) -> tuple[set[str], set[str]]:
    """Names *stmt* binds to a piece of a tainted result, and names it rebinds to anything else."""
    pairs = assigned_pairs(stmt)
    if not pairs:
        return set(), set()
    bound = {name for target, _ in pairs for name in _bound_names(target)}
    root = _chain_root(pairs[0][1])
    if isinstance(root, ast.Name) and root.id in tainted:
        return bound, set()
    return set(), bound


def _call_findings(
    body: list[ast.stmt], filepath: Path, index: ConstantIndex
) -> Iterator[tuple[ast.stmt, ast.Call, list[DecodedCompare]]]:
    """Each call in *body* whose result is decoded by name within the following statements."""
    for position, stmt in enumerate(body):
        pairs = assigned_pairs(stmt)
        if not pairs:
            continue
        call = _unwrap_call(pairs[0][1])
        if call is None:
            continue
        tainted = {name for target, _ in pairs for name in _bound_names(target)}
        compares: list[DecodedCompare] = []
        for following in body[position + 1 : position + 1 + _FOLLOWING_STATEMENTS]:
            if not tainted:
                break
            compares.extend(_decoded_compares(following, tainted, filepath, index))
            derived, rebound = _derived_names(following, tainted)
            tainted = (tainted - rebound) | derived
        if compares:
            yield stmt, call, compares


def _statement_lists(tree: ast.Module) -> Iterator[list[ast.stmt]]:
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
                yield stmts


@check(
    "type-encoded-as-data",
    severity=Severity.LOW,
    description=(
        "A caller compares a name field of a call's result with another module's "
        "constant right after the call, to learn which branch the call took"
    ),
    advice=Advice(
        fix=(
            "Have the called function return the answer as a type, an enum member or a"
            " flag, and branch on that instead of the name."
        ),
        dismiss=(
            "Dismiss only if the field is data the caller only routes on and never had a"
            " typed alternative."
        ),
    ),
)
def check_type_encoded_as_data(ctx: AnalysisContext) -> list[Finding]:
    """Find callers that decode which branch a call took from a name in its result.

    "Right after" is the statements following the call's assignment in the
    same block, up to three of them, with the result followed through plain
    re-bindings of its pieces (``(finder,) = header.segmenters``). Further
    away, the result has usually been handed on, and a comparison reads
    state rather than the call's branch.
    """
    index = ConstantIndex(ctx)
    findings = []
    for filepath, tree in source_trees(ctx):
        for body in _statement_lists(tree):
            for stmt, call, compares in _call_findings(body, filepath, index):
                callee = ast.unparse(call.func)
                described = "; ".join(
                    f"line {c.line}: `{c.field}` vs `{c.constant_source}` "
                    f"({c.constant.file}:{c.constant.line})"
                    for c in compares
                )
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=stmt.lineno,
                        check="type-encoded-as-data",
                        message=(
                            f"The result of `{callee}(...)` is decoded by name right after "
                            f"the call ({described}) — the caller learns which branch ran "
                            f"from a string"
                        ),
                        severity=Severity.LOW,
                    )
                )
    return findings
