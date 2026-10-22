"""The module graph: one resolved import graph of the analyzed project's own modules.

For each analyzed file a ``ModuleNode`` records the module's dotted name and
package, every dotted path its import statements name (``imports``), every
name those statements bind (``bindings``, the ``ctx.module_imports`` entry),
and the names its module scope binds, in source order (``scope``). From
those, ``ModuleGraph`` answers three questions:

- which analyzed files a file's imports load (``loads``, which
  ``ctx.import_graph`` exposes);
- which analyzed files each dotted module name refers to (``by_module``);
- where a name a module binds really comes from (``origin``): for a name
  re-exported through an ``__init__.py`` (``from .impl import X``), the
  module that defines it, followed through any chain of re-exports.

Everything is read from the parsed trees. Nothing named by the input is
imported, so a name bound at runtime (a module ``__getattr__``, a star import
from a module outside the analysis, a dynamic ``__all__``) resolves to
``OriginKind.UNKNOWN`` rather than to a guess.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from pysmelly.checks.helpers import (
    INIT_FILE,
    ImportReader,
    ModuleImport,
    bare_target,
    dotted_names,
    filepath_to_module,
    filepath_to_package,
    is_bare_name,
)

# The module-level hook that makes any attribute lookup on a module dynamic.
MODULE_GETATTR = "__getattr__"
DUNDER_ALL = "__all__"
STAR = "*"


class ImportedPath(NamedTuple):
    """One dotted path an import statement names.

    ``import a.b`` names ``a.b``. ``from s import n`` names ``s`` (with
    ``from_source`` set: it is certainly a module) and ``s.n``, since ``n``
    may be a submodule; a star names only ``s``. A relative import is
    resolved against the importing file's package and is never ``absolute``.
    """

    dotted: str
    absolute: bool
    from_source: bool


class ScopeBinding(NamedTuple):
    """One name a module-scope statement binds, in source order.

    ``imported`` is the binding's import record when an import statement
    binds it (``bound`` is ``*`` for a star import), and None when the module
    binds the name itself: a ``def``, a ``class``, an assignment target, a
    ``for``/``with``/``except`` target, a walrus or a ``match`` capture.
    ``line`` is the binding statement's line. ``optional`` marks a binding
    in the body of a ``try`` whose handlers catch a failed import (an
    import the module expects may fail), and ``fallback`` one in an
    ``except`` handler, which runs only when its ``try`` body failed.
    """

    name: str
    imported: ModuleImport | None
    line: int
    optional: bool = False
    fallback: bool = False


class OriginKind(Enum):
    """How a name a module binds resolves (``ModuleGraph.origin``)."""

    DEFINED = "defined"  # an analyzed module binds it itself
    MODULE = "module"  # it is an analyzed module (a submodule, or ``import pkg.mod``)
    EXTERNAL = "external"  # the chain leaves the analysis: a library or an unanalyzed module
    MISSING = "missing"  # an analyzed module is asked for it and nothing binds it
    UNKNOWN = "unknown"  # bound at runtime in a way a static reading cannot follow


class NameOrigin(NamedTuple):
    """Where a chain of imports for one name ends.

    ``module`` and ``name`` are the last link: the defining module and the
    name there for DEFINED, the module itself (``name`` empty) for MODULE,
    the import's source outside the analysis for EXTERNAL, and the analyzed
    module that should bind ``name`` for MISSING and UNKNOWN. ``path`` is
    that module's file when it is analyzed.
    """

    kind: OriginKind
    module: str
    name: str
    path: Path | None


@dataclass(frozen=True)
class ModuleNode:
    """One analyzed file's place in the module graph."""

    path: Path
    module: str
    package: str
    imports: tuple[ImportedPath, ...]
    bindings: list[ModuleImport]
    scope: tuple[ScopeBinding, ...]
    # The names a static ``__all__`` lists, each with the line of its string:
    # None when there is no ``__all__``, and None too, with ``dynamic_all``
    # set, when it is built some other way.
    exports: dict[str, int] | None
    dynamic_all: bool
    # The dotted name each base of a module-level class spells, in source
    # order: ``Base``, ``base.Base``; ``Base[T]`` spells ``Base``. A base that
    # is not a name chain (``with_metaclass(M)``) is left out.
    class_bases: tuple[str, ...]

    @property
    def bound_names(self) -> frozenset[str]:
        """Every name the module scope binds (a star import's names are not listed)."""
        return frozenset(binding.name for binding in self.scope if binding.name != STAR)

    def lookup(self, name: str) -> Iterator[ScopeBinding]:
        """The module-scope bindings that may bind ``name``, last first.

        Those are the bindings of ``name`` itself and every star import,
        which may bind it too. A ``fallback`` binding (in an ``except``
        handler) comes after every other: ``try: from .opt import f`` /
        ``except ImportError: f = None`` binds ``f`` from ``.opt`` whenever
        that import works, so the handler's assignment is not the
        definition a static reading should report.
        """
        candidates = [binding for binding in reversed(self.scope) if binding.name in {name, STAR}]
        yield from (binding for binding in candidates if not binding.fallback)
        yield from (binding for binding in candidates if binding.fallback)

    def here(self, kind: OriginKind, name: str) -> NameOrigin:
        """``name`` in this module, resolved as ``kind``."""
        return NameOrigin(kind, self.module, name, self.path)

    def unbound(self, name: str) -> NameOrigin:
        """A name no module-scope binding binds: unknown behind a module ``__getattr__``."""
        kind = OriginKind.UNKNOWN if MODULE_GETATTR in self.bound_names else OriginKind.MISSING
        return self.here(kind, name)

    def star_binds(self, name: str) -> bool:
        """Whether ``from <this module> import *`` binds ``name`` (for a static ``__all__``)."""
        if self.exports is not None:
            return name in self.exports
        return not name.startswith("_")


class ModuleGraph:
    """The analyzed modules, what each imports, and where the names they bind come from."""

    def __init__(
        self,
        nodes: dict[Path, ModuleNode],
        by_module: dict[str, list[Path]],
        loads: dict[Path, frozenset[Path]],
        package_dirs: Collection[tuple[str, ...]],
    ) -> None:
        self.nodes = nodes
        self.by_module = by_module
        self.loads = loads
        self._package_dirs = package_dirs
        self._reexports: dict[Path, dict[str, NameOrigin]] = {}
        self._defining_files: dict[Path, frozenset[Path]] = {}
        self._base_classes: dict[Path, frozenset[NameOrigin]] = {}

    def module_of(self, filepath: str) -> str:
        """The dotted module name of any path, analyzed or not (``filepath_to_module``)."""
        return filepath_to_module(filepath, self._package_dirs)

    def origin(self, module: str, name: str) -> NameOrigin:
        """Where ``name``, looked up on module ``module``, comes from.

        A module-scope binding is followed as Python looks it up: the last
        binding of the name in source order wins, except that an ``except``
        handler's fallback yields to the import its ``try`` guards unless
        that import binds nothing; a from-import is followed
        to its source; a star import binds the names its source exports (its
        static ``__all__``, or its public names). A name nothing binds is a
        submodule when ``module.name`` is analyzed (``from . import sub``).
        A module known by several files (``by_module``) is resolved in each,
        in order, and the first that binds the name answers.
        """
        return self._origin(module, name, frozenset())

    def reexports(self, path: Path) -> dict[str, NameOrigin]:
        """Each name the file's module scope binds by a from-import, with its origin."""
        if path not in self._reexports:
            node = self.nodes[path]
            self._reexports[path] = {
                binding.name: self.origin(node.module, binding.name)
                for binding in node.scope
                if binding.imported is not None
                and binding.imported.source is not None
                and binding.name != STAR
            }
        return self._reexports[path]

    def defining_files(self, path: Path) -> frozenset[Path]:
        """The analyzed files that define what the file's from-imports bind.

        Every ``from S import n`` anywhere in the file (inside functions too,
        as ``ModuleNode.bindings`` lists them) is followed through
        re-exports to the file that defines ``n``, or to the submodule ``n``
        names. A star import is not followed: it names no one binding.
        """
        if path not in self._defining_files:
            found: set[Path] = set()
            for record in self.nodes[path].bindings:
                if record.source is None or record.name is None or record.name == STAR:
                    continue
                origin = self.origin(record.source, record.name)
                if origin.kind in {OriginKind.DEFINED, OriginKind.MODULE} and origin.path:
                    found.add(origin.path)
            found.discard(path)
            self._defining_files[path] = frozenset(found)
        return self._defining_files[path]

    def resolve(self, module: str, dotted: str) -> NameOrigin:
        """What the dotted name ``dotted``, read in module ``module``, refers to.

        The first name is looked up in ``module`` (``origin``), and each
        further attribute on the analyzed module the chain has reached so
        far: ``base.Handler`` is ``Handler`` in whatever module ``base``
        names. A chain that leaves the analysis stays EXTERNAL. An attribute
        of anything else that is not an analyzed module (a class, a name the
        graph cannot follow) is not followed: the chain is UNKNOWN there.
        """
        head, *attributes = dotted.split(".")
        found = self.origin(module, head)
        for attribute in attributes:
            if found.kind is OriginKind.EXTERNAL:
                return found
            if found.kind is not OriginKind.MODULE:
                return NameOrigin(OriginKind.UNKNOWN, found.module, attribute, found.path)
            found = self.origin(found.module, attribute)
        return found

    def base_classes(self, path: Path) -> frozenset[NameOrigin]:
        """The analyzed classes the file's module-level classes inherit from directly.

        Each base (``ModuleNode.class_bases``) is resolved where the file
        reads it (``resolve``), so a base imported through a re-export is
        the class its defining module binds. A base from a library, or one
        the graph cannot follow, is not listed.
        """
        if path not in self._base_classes:
            node = self.nodes[path]
            resolved = (self.resolve(node.module, base) for base in node.class_bases)
            self._base_classes[path] = frozenset(
                origin for origin in resolved if origin.kind is OriginKind.DEFINED
            )
        return self._base_classes[path]

    def enclosing_package(self, dotted: str) -> tuple[str, Path] | None:
        """The deepest analyzed package strictly above ``dotted``, with its ``__init__.py``.

        A module path under an analyzed package belongs to the project even
        when no analyzed file is that module, which is how a re-export from a
        deleted module (``from .gone import x``) is told from a library's.
        """
        parts = dotted.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            package = ".".join(parts[:depth])
            for candidate in self.by_module.get(package, ()):
                if candidate.name == INIT_FILE:
                    return package, candidate
        return None

    def _origin(self, module: str, name: str, visiting: frozenset[tuple[str, str]]) -> NameOrigin:
        paths = self.by_module.get(module)
        if paths is None:
            return NameOrigin(OriginKind.EXTERNAL, module, name, None)
        visiting |= {(module, name)}
        missing: list[NameOrigin] = []
        for path in paths:
            found = self._origin_in(self.nodes[path], name, visiting)
            if found.kind is not OriginKind.MISSING:
                return found
            missing.append(found)
        submodule = f"{module}.{name}"
        if submodule in self.by_module:
            return NameOrigin(OriginKind.MODULE, submodule, "", self.by_module[submodule][0])
        return missing[0]

    def _origin_in(
        self, node: ModuleNode, name: str, visiting: frozenset[tuple[str, str]]
    ) -> NameOrigin:
        """``name`` looked up in one file's module scope, last binding first.

        ``except`` fallbacks come after the rest (``ModuleNode.lookup``), and
        a guarded import that binds nothing gives way to them, as it does
        when the module runs.
        """
        for binding in node.lookup(name):
            imported = binding.imported
            if imported is None:
                return node.here(OriginKind.DEFINED, name)
            if imported.source is None or imported.name is None:
                return self._module_origin(imported.dotted)
            if binding.name == STAR:
                found = self._star_origin(imported.source, name, visiting)
                if found is not None:
                    return found
                continue
            if (imported.source, imported.name) not in visiting:
                found = self._origin(imported.source, imported.name, visiting)
                if found.kind is OriginKind.MISSING and binding.optional:
                    # The guarded import fails, so its ``except`` fallback binds the name.
                    continue
                return found
            # The chain came back to a lookup already under way: ``from . import
            # sub`` in a package's own ``__init__.py`` names the submodule, and
            # a cycle of re-exports that never reaches a definition binds nothing.
            if imported.dotted in self.by_module:
                return self._module_origin(imported.dotted)
            return node.here(OriginKind.MISSING, name)
        return node.unbound(name)

    def _star_origin(
        self, source: str, name: str, visiting: frozenset[tuple[str, str]]
    ) -> NameOrigin | None:
        """What ``from source import *`` binds ``name`` to, or None when it does not bind it."""
        paths = self.by_module.get(source)
        if paths is None:
            return NameOrigin(OriginKind.UNKNOWN, source, name, None)
        if (source, name) in visiting:
            return None
        for path in paths:
            node = self.nodes[path]
            if node.dynamic_all:
                return node.here(OriginKind.UNKNOWN, name)
            if not node.star_binds(name):
                continue
            found = self._origin(source, name, visiting)
            if found.kind is not OriginKind.MISSING:
                return found
        return None

    def _module_origin(self, dotted: str) -> NameOrigin:
        if dotted in self.by_module:
            return NameOrigin(OriginKind.MODULE, dotted, "", self.by_module[dotted][0])
        return NameOrigin(OriginKind.EXTERNAL, dotted, "", None)


def _imported_paths(tree: ast.Module, reader: ImportReader) -> Iterator[ImportedPath]:
    """Every dotted path an import statement anywhere in ``tree`` names, in walk order."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = reader.source(node)
            absolute = node.level == 0
            yield ImportedPath(source, absolute, from_source=True)
            for alias in node.names:
                if alias.name != STAR:
                    yield ImportedPath(f"{source}.{alias.name}", absolute, from_source=False)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportedPath(alias.name, absolute=True, from_source=False)


def _target_names(target: ast.expr) -> Iterator[str]:
    """The bare names an assignment target binds (unpacking followed)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _pattern_names(pattern: ast.pattern) -> Iterator[str]:
    """The names a ``match`` case pattern captures."""
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest


def _walrus_names(stmt: ast.stmt) -> Iterator[str]:
    """Names a walrus in the statement's own expressions binds (not in nested bodies)."""
    pending: list[ast.AST] = [
        child for child in ast.iter_child_nodes(stmt) if isinstance(child, ast.expr)
    ]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.NamedExpr) and (target := bare_target(node)) is not None:
            yield target.id
        pending.extend(ast.iter_child_nodes(node))


def _statement_targets(stmt: ast.stmt) -> Iterator[str]:
    """The names a simple (or compound statement's own header) binds at module scope."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            yield from _target_names(target)
    elif (isinstance(stmt, ast.AnnAssign) and stmt.value is not None) or isinstance(
        stmt, (ast.AugAssign, ast.For, ast.AsyncFor)
    ):
        yield from _target_names(stmt.target)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                yield from _target_names(item.optional_vars)
    elif isinstance(stmt, ast.TypeAlias):
        yield from _target_names(stmt.name)
    yield from _walrus_names(stmt)


# The exceptions a failed import raises, and the broader ones that catch it.
_IMPORT_FAILURES = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _catches_failed_import(handler: ast.ExceptHandler) -> bool:
    """Whether an ``except`` clause catches the exception a failed import raises."""
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        isinstance(exc, (ast.Name, ast.Attribute))
        and (exc.id if isinstance(exc, ast.Name) else exc.attr) in _IMPORT_FAILURES
        for exc in caught
    )


class _Guard(NamedTuple):
    """Where in a ``try`` statement the bindings being read sit."""

    optional: bool = False
    fallback: bool = False

    def binding(self, name: str, imported: ModuleImport | None, line: int) -> ScopeBinding:
        return ScopeBinding(name, imported, line, self.optional, self.fallback)


_UNGUARDED = _Guard()


def _scope_bindings(
    stmts: list[ast.stmt], reader: ImportReader, guard: _Guard = _UNGUARDED
) -> Iterator[ScopeBinding]:
    """Every name the statements bind in the module's own scope, in source order.

    Compound statements (``if``, ``try``, ``for``, ``with``, ``while``,
    ``match``) run in the module's scope, so their bodies are followed;
    function and class bodies are not. A ``del`` is not recorded. A
    binding in a ``try`` body whose handlers catch a failed import is
    ``optional``, and one in an ``except`` handler is a ``fallback``, at any
    depth below them.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for record in reader.records(stmt):
                yield guard.binding(record.bound, record, stmt.lineno)
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield guard.binding(stmt.name, None, stmt.lineno)
            continue
        for name in _statement_targets(stmt):
            yield guard.binding(name, None, stmt.lineno)
        handlers = getattr(stmt, "handlers", [])
        body_guard = (
            guard._replace(optional=True)
            if any(_catches_failed_import(handler) for handler in handlers)
            else guard
        )
        yield from _scope_bindings(getattr(stmt, "body", []), reader, body_guard)
        yield from _scope_bindings(getattr(stmt, "orelse", []), reader, guard)
        for handler in handlers:
            handler_guard = guard._replace(fallback=True)
            if handler.name:
                yield handler_guard.binding(handler.name, None, handler.lineno)
            yield from _scope_bindings(handler.body, reader, handler_guard)
        yield from _scope_bindings(getattr(stmt, "finalbody", []), reader, guard)
        for case in getattr(stmt, "cases", []):
            for name in _pattern_names(case.pattern):
                yield guard.binding(name, None, case.pattern.lineno)
            yield from _scope_bindings(case.body, reader, guard)


def _static_exports(tree: ast.Module) -> tuple[dict[str, int] | None, bool]:
    """The names a module-level ``__all__`` lists, and whether it is built dynamically.

    ``__all__ = [...]`` (or a tuple) of string constants sets the list, and
    ``__all__ += [...]`` extends it; each name maps to its string's line.
    Any other form (a name, a call, a concatenation, a non-string element)
    makes it dynamic.
    """
    exports: dict[str, int] | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            is_bare_name(target, DUNDER_ALL) for target in node.targets
        ):
            extends = False
        elif (
            isinstance(node, (ast.AnnAssign, ast.AugAssign))
            and (target := bare_target(node)) is not None
            and target.id == DUNDER_ALL
        ):
            extends = isinstance(node, ast.AugAssign)
        else:
            continue
        value = node.value
        if value is None:  # ``__all__: list[str]`` declares the name and binds nothing
            continue
        exports = exports if extends and exports is not None else {}
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None, True
        for element in value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                return None, True
            exports.setdefault(element.value, element.lineno)
    return exports, False


def _class_bases(tree: ast.Module) -> Iterator[str]:
    """The dotted name each base of a module-level class spells (``ModuleNode.class_bases``)."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            continue
        yield from dotted_names(
            base.value if isinstance(base, ast.Subscript) else base for base in stmt.bases
        )


def _module_under(filepath: Path, root: Path) -> str | None:
    """The module name ``filepath`` has with ``root`` on ``sys.path``, or None outside it."""
    try:
        rel = filepath.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.parent.parts)
    if rel.name != INIT_FILE:
        parts.append(rel.stem)
    return ".".join(parts) or None


def _module_paths(
    paths: Collection[Path],
    package_dirs: Collection[tuple[str, ...]],
    import_roots: Collection[Path],
) -> dict[str, list[Path]]:
    """Every dotted module name an analyzed file can be imported by, with its files.

    A file is known by its path from the analyzed root, less any source root
    (``filepath_to_module``), and by its path from each of ``import_roots``.
    """
    by_module: dict[str, list[Path]] = defaultdict(list)
    for filepath in paths:
        names = [filepath_to_module(str(filepath), package_dirs)]
        names += [_module_under(filepath, root) for root in import_roots]
        for module in {name for name in names if name}:
            by_module[module].append(filepath)
    return dict(by_module)


def _loaded_files(
    nodes: dict[Path, ModuleNode],
    by_module: dict[str, list[Path]],
    package_dirs: Collection[tuple[str, ...]],
) -> dict[Path, frozenset[Path]]:
    """Per file, the other analyzed files its import statements load.

    Importing ``a.b.c`` loads the packages ``a`` and ``a.b`` as well, as
    Python does. An absolute name whose first component matches no analyzed
    module is tried against the importing file's own directory, which is
    where a script, or a test module under pytest's default import mode,
    finds its siblings (``from helpers import x`` beside
    ``tests/test_x.py``). Imports anywhere in the tree count, including
    those inside functions.
    """
    roots = {module.partition(".")[0] for module in by_module}
    loads: dict[Path, frozenset[Path]] = {}
    for filepath, node in nodes.items():
        local = filepath_to_module(str(filepath.parent / INIT_FILE), package_dirs)
        loaded: set[Path] = set()
        for imported in node.imports:
            name = imported.dotted
            resolved = (
                f"{local}.{name}"
                if imported.absolute and local and name.partition(".")[0] not in roots
                else name
            )
            parts = resolved.split(".")
            for depth in range(1, len(parts) + 1):
                loaded.update(by_module.get(".".join(parts[:depth]), ()))
        loaded.discard(filepath)
        loads[filepath] = frozenset(loaded)
    return loads


def build_module_graph(
    all_trees: dict[Path, ast.Module],
    package_dirs: Collection[tuple[str, ...]],
    import_roots: Collection[Path],
    module_imports: dict[Path, list[ModuleImport]],
) -> ModuleGraph:
    """The module graph of the analyzed files.

    A module is known by its path from the analyzed root, less any source
    root (``filepath_to_module``), and also by its path from each of
    ``import_roots``, the directories the test runner puts on ``sys.path``
    (pytest's ``pythonpath``). ``module_imports`` is ``ctx.module_imports``,
    which each node carries as its ``bindings``.
    """
    nodes: dict[Path, ModuleNode] = {}
    for filepath, tree in all_trees.items():
        reader = ImportReader(filepath_to_package(str(filepath), package_dirs))
        exports, dynamic_all = _static_exports(tree)
        nodes[filepath] = ModuleNode(
            path=filepath,
            module=filepath_to_module(str(filepath), package_dirs),
            package=reader.package,
            imports=tuple(_imported_paths(tree, reader)),
            bindings=module_imports[filepath],
            scope=tuple(_scope_bindings(tree.body, reader)),
            exports=exports,
            dynamic_all=dynamic_all,
            class_bases=tuple(_class_bases(tree)),
        )
    by_module = _module_paths(all_trees.keys(), package_dirs, import_roots)
    loads = _loaded_files(nodes, by_module, package_dirs)
    return ModuleGraph(nodes, by_module, loads, package_dirs)
