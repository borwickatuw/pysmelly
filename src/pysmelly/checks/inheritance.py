"""Inheritance checks — cross-file class-hierarchy smells.

Refused bequest, deep inheritance chains, multiple-inheritance method
collisions. All build on a shared project-class hierarchy index, which
``OverrideResolver`` also reads to tell the caller-value checks which
methods may override a base outside the analyzed code.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from pysmelly.checks.helpers import (
    MAJORITY,
    ModuleResolver,
    base_class_name,
    is_ellipsis,
    raises_not_implemented,
    sole_statement,
    source_trees,
)
from pysmelly.checks.stdlib_classes import StdlibClasses
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, check


def _index_classes(
    trees: Iterable[tuple[Path, ast.Module]],
) -> tuple[dict[str, ast.ClassDef], dict[str, str], set[str]]:
    """Index the top-level classes of ``trees`` by name.

    Returns (index, file_of, ambiguous) where index maps name -> ClassDef,
    file_of maps name -> file path, and ambiguous is the set of names
    defined by more than one class (skipped — can't resolve reliably).
    """
    index: dict[str, ast.ClassDef] = {}
    file_of: dict[str, str] = {}
    ambiguous: set[str] = set()
    for filepath, tree in trees:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                if node.name in index:
                    ambiguous.add(node.name)
                index[node.name] = node
                file_of[node.name] = str(filepath)
    return index, file_of, ambiguous


def _build_class_index(
    ctx: AnalysisContext,
) -> tuple[dict[str, ast.ClassDef], dict[str, str], set[str]]:
    """Index production classes by name (test-role files are left out)."""
    return _index_classes(source_trees(ctx))


def _base_names(node: ast.ClassDef) -> list[str]:
    return [name for name in map(base_class_name, node.bases) if name is not None]


def _project_base_defs(
    node: ast.ClassDef, index: dict[str, ast.ClassDef], ambiguous: set[str]
) -> list[ast.ClassDef]:
    result = []
    for name in _base_names(node):
        if name in ambiguous:
            continue
        base = index.get(name)
        if base is not None and base is not node:
            result.append(base)
    return result


def _method_names(node: ast.ClassDef) -> set[str]:
    return {
        item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _ancestors(
    node: ast.ClassDef,
    index: dict[str, ast.ClassDef],
    ambiguous: set[str],
    seen: set[int] | None = None,
) -> list[ast.ClassDef]:
    """Project-class ancestors of node, depth-first, cycle-safe."""
    if seen is None:
        seen = set()
    result = []
    for base in _project_base_defs(node, index, ambiguous):
        if id(base) in seen:
            continue
        seen.add(id(base))
        result.append(base)
        result.extend(_ancestors(base, index, ambiguous, seen))
    return result


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_private(name: str) -> bool:
    """A ``_``-prefixed name that is not a dunder: the class's own helper."""
    return name.startswith("_") and not _is_dunder(name)


class OverrideResolver:
    """Which methods may override a name defined on a base the analysis cannot resolve.

    Such a method's signature is the base's: the code outside the analysis
    calls it, so the author cannot change it however the analyzed callers
    use it. Every caller-value check skips it (DECISIONS.md, "Framework
    overrides skip the caller-value checks (Phase 25-2)").

    Bases resolve through the hierarchy index the inheritance checks share,
    built here over every analyzed file, since a test's base class is as
    much analyzed code as any other. A base naming an analyzed class is
    followed to that class's own bases (``_ancestors``). A builtin or
    standard-library base is read without importing anything
    (``StdlibClasses``), and the method overrides it only when that class,
    or one it inherits other than ``object``, defines the name. Any other
    base cannot be resolved: imported from a third-party package
    (``admin.ModelAdmin``), bound by an assignment (``Base = declarative_base()``),
    a name more than one analyzed class has, or not a name at all
    (``with_metaclass(M)``). The analysis cannot see what it defines, so
    every method name that could be an override counts as one. A
    ``_``-prefixed name that is not a dunder could not: it is the class's
    own helper, not part of an interface a base class declares.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self._index, self._file_of, self._ambiguous = _index_classes(ctx.all_trees.items())
        # No module name: a bare name no import binds stays as spelled, for builtins.
        self._resolvers = {
            path: ModuleResolver("", found) for path, found in ctx.module_imports.items()
        }
        self._lineage: dict[int, list[tuple[Path, ast.ClassDef]]] = {}
        self._stdlib = StdlibClasses()

    def overrides_unresolved_base(self, path: Path, owner: ast.ClassDef, name: str) -> bool:
        """Whether method ``name`` of ``owner``, defined in ``path``, may override such a base."""
        if _is_private(name):
            return False
        return any(
            self._may_define(cls_path, base, name)
            for cls_path, cls in self._with_ancestors(path, owner)
            for base in self._unresolved_bases(cls)
        )

    def _unresolved_bases(self, cls: ast.ClassDef) -> list[ast.expr]:
        """The bases of ``cls`` that ``_project_base_defs`` does not resolve to an analyzed class.

        A class never resolves to itself, so ``class Handler(logging.Handler)``
        leaves the library's ``Handler`` here.
        """
        resolved = {base.name for base in _project_base_defs(cls, self._index, self._ambiguous)}
        return [base for base in cls.bases if base_class_name(base) not in resolved]

    def _with_ancestors(self, path: Path, owner: ast.ClassDef) -> list[tuple[Path, ast.ClassDef]]:
        """``owner`` and its analyzed ancestors, each with the file that defines it."""
        if id(owner) not in self._lineage:
            ancestors = _ancestors(owner, self._index, self._ambiguous)
            self._lineage[id(owner)] = [(path, owner)] + [
                (Path(self._file_of[ancestor.name]), ancestor) for ancestor in ancestors
            ]
        return self._lineage[id(owner)]

    def _may_define(self, path: Path, base: ast.expr, name: str) -> bool:
        """Whether ``base``, unresolved and on a class defined in ``path``, may define ``name``.

        A base that is not a name chain (``with_metaclass(M)``) may define anything.
        """
        unwrapped = base.value if isinstance(base, ast.Subscript) else base
        paths = self._resolvers[path].resolve_all([unwrapped])
        if not paths:
            return True
        defined = self._stdlib.defined_names(paths[0])
        return defined is None or name in defined


def _is_trivial_override_body(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """If the method body is a pure stub, return a label ('pass' or
    'raise NotImplementedError'); else None. A leading docstring is
    ignored."""
    stmt = sole_statement(method)
    if stmt is None:
        return None
    if isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and is_ellipsis(stmt.value)):
        return "pass"
    if raises_not_implemented(stmt):
        return "raise NotImplementedError"
    return None


@check(
    "refused-bequest",
    severity=Severity.MEDIUM,
    description="Subclass that stubs out most of the methods it inherits",
    advice=Advice(
        fix=(
            "Stop inheriting from the base: hold an instance of it (composition) or"
            " subclass a narrower base that lacks the stubbed methods."
        ),
        dismiss=(
            "Dismiss only if the base is a framework class the subclass must extend, and"
            " the stubs are the framework's documented way to opt out of a hook."
        ),
    ),
)
def check_refused_bequest(ctx: AnalysisContext) -> list[Finding]:
    """Find subclasses that reject most of what they inherit.

    A subclass whose overrides are mostly ``pass`` or
    ``raise NotImplementedError`` isn't specializing its base — it's
    declaring the base is the wrong parent (a skateboard is not a
    vehicle). The inheritance buys nothing and misleads callers who
    expect the base's contract.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx)

    for name, node in index.items():
        ancestors = _ancestors(node, index, ambiguous)
        if not ancestors:
            continue
        ancestor_methods: set[str] = set()
        for anc in ancestors:
            ancestor_methods |= _method_names(anc)

        overrides = [
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name in ancestor_methods
            and not _is_dunder(item.name)
        ]
        if len(overrides) < 3:
            continue
        trivial = [(m.name, _is_trivial_override_body(m)) for m in overrides]
        stubbed = [(n, label) for n, label in trivial if label is not None]
        if len(stubbed) < 3 or len(stubbed) / len(overrides) < MAJORITY:
            continue

        base_name = _base_names(node)[0] if _base_names(node) else "base"
        stub_names = ", ".join(n for n, _ in stubbed[:5])
        if len(stubbed) > 5:
            stub_names += ", ..."
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="refused-bequest",
                message=(
                    f"{name} stubs out {len(stubbed)} of {len(overrides)}"
                    f" methods it inherits from {base_name}"
                    f" ({stub_names}) — {base_name} is likely the wrong base"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _is_subclassed_by_project(
    name: str, index: dict[str, ast.ClassDef], ambiguous: set[str]
) -> bool:
    for other in index.values():
        if other.name == name:
            continue
        if name in _base_names(other) and name not in ambiguous:
            return True
    return False


@check(
    "deep-inheritance",
    severity=Severity.LOW,
    description="Project inheritance chain 5+ classes deep",
    advice=Advice(
        fix=(
            "Flatten the chain: merge levels that add little, or replace a level with a"
            " component the class holds."
        ),
        dismiss=(
            "Dismiss only if each level is a public extension point that code outside the"
            " analyzed tree subclasses."
        ),
    ),
)
def check_deep_inheritance(ctx: AnalysisContext) -> list[Finding]:
    """Find deep in-codebase inheritance chains.

    Each level adds behavior scattered across a class the reader must
    hold in their head at once (the yo-yo problem: understanding one
    method means bouncing up and down five classes). Reported once per
    chain, at the deepest (leaf) class. Framework base classes don't
    count — only links between classes defined in this codebase.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx)
    min_depth = 5

    def longest_path(node: ast.ClassDef, seen: frozenset[int] = frozenset()) -> int:
        if id(node) in seen:
            return 0
        bases = _project_base_defs(node, index, ambiguous)
        if not bases:
            return 1
        return 1 + max(longest_path(b, seen | {id(node)}) for b in bases)

    for name, node in index.items():
        if _is_subclassed_by_project(name, index, ambiguous):
            continue  # not a leaf — its subclass will report the chain
        chain_len = longest_path(node)
        if chain_len < min_depth:
            continue
        # Build the longest root-to-leaf chain for the message.
        chain = [name]
        current = node
        while True:
            bases = _project_base_defs(current, index, ambiguous)
            if not bases:
                break
            deepest = max(bases, key=longest_path)
            chain.append(deepest.name)
            current = deepest
        chain_str = " -> ".join(reversed(chain))
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="deep-inheritance",
                message=(
                    f"{name} sits at the bottom of a {chain_len}-class"
                    f" inheritance chain ({chain_str}) — behavior is"
                    f" scattered across levels"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


def _has_incomparable_pair(classes: list[ast.ClassDef], anc_ids: dict[int, set[int]]) -> bool:
    """True if any two classes are incomparable — neither an ancestor of
    the other — meaning they are genuinely competing definitions rather
    than an override down a single linear chain."""
    for i, c1 in enumerate(classes):
        for c2 in classes[i + 1 :]:
            if id(c2) not in anc_ids.get(id(c1), set()) and id(c1) not in anc_ids.get(
                id(c2), set()
            ):
                return True
    return False


@check(
    "mi-method-collision",
    severity=Severity.LOW,
    description="Multiple bases define the same method; subclass picks via MRO",
    advice=Advice(
        fix=(
            "Override each colliding method in the subclass and call the base whose"
            " behavior it wants explicitly."
        ),
        dismiss=(
            "Dismiss only if the bases are cooperative mixins whose methods call super(),"
            " so the MRO runs all of them by design."
        ),
    ),
)
def check_mi_method_collision(ctx: AnalysisContext) -> list[Finding]:
    """Find multiple-inheritance classes where 2+ bases define the same
    method and the subclass doesn't override it.

    Which implementation wins is decided silently by the MRO, not by the
    author. When the base implementations differ, one branch's behavior
    is quietly dropped — an easy source of "why isn't my override
    running" bugs. Reported at LOW: cooperative super() chains make this
    correct often enough to warrant a look, not an alarm.

    A method defined only by a shared diamond ancestor is NOT a collision
    — both branches inherit the same definition, so the MRO is
    unambiguous. Only genuinely competing definitions (from classes where
    neither is an ancestor of the other) count.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx)

    for name, node in index.items():
        bases = _project_base_defs(node, index, ambiguous)
        if len(bases) < 2:
            continue
        own_methods = _method_names(node)

        # The ancestor closure: every project class this one inherits from.
        closure = _ancestors(node, index, ambiguous)
        anc_ids: dict[int, set[int]] = {
            id(c): {id(a) for a in _ancestors(c, index, ambiguous)} for c in closure
        }

        # method name -> classes in the closure that define it
        definers: dict[str, list[ast.ClassDef]] = {}
        for cls in closure:
            for m in _method_names(cls):
                if not _is_dunder(m):
                    definers.setdefault(m, []).append(cls)

        collisions = sorted(
            m
            for m, classes in definers.items()
            if m not in own_methods
            and len(classes) >= 2
            and _has_incomparable_pair(classes, anc_ids)
        )
        if not collisions:
            continue

        base_list = ", ".join(_base_names(node))
        shown = ", ".join(collisions[:5])
        if len(collisions) > 5:
            shown += ", ..."
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="mi-method-collision",
                message=(
                    f"{name}({base_list}) inherits {len(collisions)}"
                    f" method(s) defined in multiple bases ({shown}) and"
                    f" overrides none — the MRO silently picks one"
                ),
                severity=Severity.LOW,
            )
        )

    return findings
