"""The names a builtin or standard-library class defines, read without running anything.

pysmelly never imports code named by its input (DECISIONS.md, "pysmelly never
imports code named by its input"). A base class the analyzed code names is
read three ways, none of which imports or executes a module:

- a bare name is looked up in ``builtins``, which is already loaded;
- a module compiled into the interpreter (``sys.builtin_module_names``,
  ``_typing``, ``_collections``) is read only if the interpreter has already
  loaded it, from the object in ``sys.modules``. A compiled-in module cannot
  be shadowed by a file on ``sys.path``;
- any other standard-library module is located under
  ``sysconfig.get_paths()["stdlib"]`` by path arithmetic (never
  ``importlib.util.find_spec``, which imports parent packages, and never
  ``sys.path``), parsed with ``ast``, and the class body's names collected,
  following its bases through that file and other standard-library files
  the same way.

Anything else, a C-extension class with no source, a non-stdlib module, or
a binding the static reading cannot follow, is unresolved. The caller treats
an unresolved base conservatively.
"""

from __future__ import annotations

import ast
import builtins
import sys
import sysconfig
import types
from dataclasses import dataclass
from pathlib import Path

from pysmelly.checks.helpers import import_binding, resolve_relative_import, top_level_package

# Bindings followed for one dotted name before it is given up as unresolved.
MAX_HOPS = 64


class _UnresolvedError(Exception):
    """The static reading cannot follow this name."""


@dataclass(frozen=True)
class _ModuleRef:
    """A module, by absolute name: bound by ``import``, or reached as a package attribute."""

    name: str


@dataclass(frozen=True)
class _FromImport:
    """``from module import attr``."""

    module: str
    attr: str


@dataclass(frozen=True)
class _StarImport:
    """``from module import *``."""

    module: str


@dataclass(frozen=True)
class _Loaded:
    """An object already loaded in this interpreter: a builtin, or from a compiled-in module."""

    obj: object


# What one module-level binding holds, in source order.
_Bound = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.expr | _ModuleRef | _FromImport


@dataclass
class _Module:
    name: str
    is_package: bool
    # (name, what it is bound to), and star imports, in source order.
    bindings: list[tuple[str, _Bound] | _StarImport]
    mro_entries: dict[str, ast.expr]


def type_names(cls: type) -> frozenset[str]:
    """The names ``cls``, or a class it inherits other than ``object``, defines.

    ``object`` is left out because every class inherits from it: an explicit
    ``class C(object)`` says no more than ``class C``.
    """
    return frozenset(name for klass in cls.__mro__ if klass is not object for name in vars(klass))


def _loaded_compiled_in(module: str) -> types.ModuleType | None:
    """A compiled-in module this interpreter has already loaded. Never imported here."""
    if module not in sys.builtin_module_names:
        return None
    return sys.modules.get(module)


def _executed_blocks(body: list[ast.stmt]) -> list[ast.stmt]:
    """Module-level statements in source order, through ``if`` and ``try``.

    A ``try`` is read as succeeding on the interpreter that ships this
    standard library, so its ``except`` handlers are skipped (``abc`` binds
    ``ABCMeta`` from ``_py_abc`` only when ``_abc`` fails to import). Both
    branches of an ``if`` are read, the later binding winning.
    """
    result: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, ast.If):
            result.extend(_executed_blocks(stmt.body + stmt.orelse))
        elif isinstance(stmt, ast.Try | ast.TryStar):
            result.extend(_executed_blocks(stmt.body + stmt.orelse + stmt.finalbody))
        else:
            result.append(stmt)
    return result


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for elt in target.elts for name in _target_names(elt)]
    return []


def _class_body_names(node: ast.ClassDef) -> frozenset[str]:
    """The names a class body binds, which is what ``vars()`` of the class holds.

    A body that defines ``__eq__`` also gets ``__hash__``, which the class
    statement sets to None when the body does not define it.
    """
    names: set[str] = set()
    for stmt in _executed_blocks(node.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(name for target in stmt.targets for name in _target_names(target))
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            names.update(_target_names(stmt.target))
        elif isinstance(stmt, ast.Import | ast.ImportFrom):
            names.update(import_binding(alias) for alias in stmt.names if alias.name != "*")
    if "__eq__" in names:
        names.add("__hash__")
    return frozenset(names)


def _class_names(found: object) -> frozenset[str]:
    """``found`` as a class's names: a statically read class, or a loaded type."""
    if isinstance(found, frozenset):
        return found
    if isinstance(found, _Loaded) and isinstance(found.obj, type):
        return type_names(found.obj)
    raise _UnresolvedError(repr(found))


def _loaded_attribute(found: object, attr: str) -> _Loaded:
    """``attr`` of a loaded compiled-in module, read from its namespace; nothing runs."""
    if not isinstance(found, _Loaded) or not isinstance(found.obj, types.ModuleType):
        raise _UnresolvedError(attr)
    namespace = vars(found.obj)
    if attr not in namespace:
        raise _UnresolvedError(attr)
    return _Loaded(namespace[attr])


def _last_return(func: ast.FunctionDef) -> ast.expr:
    returns = [stmt for stmt in func.body if isinstance(stmt, ast.Return)]
    if not returns or returns[-1].value is None:
        raise _UnresolvedError(func.name)
    return returns[-1].value


def _record_assign(module: _Module, stmt: ast.Assign) -> None:
    for target in stmt.targets:
        if isinstance(target, ast.Name):
            module.bindings.append((target.id, stmt.value))
        elif (
            isinstance(target, ast.Attribute)
            and target.attr == "__mro_entries__"
            and isinstance(target.value, ast.Name)
        ):
            module.mro_entries[target.value.id] = stmt.value


def _record_import_from(module: _Module, stmt: ast.ImportFrom) -> None:
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    source = resolve_relative_import(stmt.module, stmt.level, package)
    for alias in stmt.names:
        if alias.name == "*":
            module.bindings.append(_StarImport(source))
        else:
            module.bindings.append((alias.asname or alias.name, _FromImport(source, alias.name)))


def _record(module: _Module, stmt: ast.stmt) -> None:
    """Add what one module-level statement binds to ``module``."""
    if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        module.bindings.append((stmt.name, stmt))
    elif isinstance(stmt, ast.Assign):
        _record_assign(module, stmt)
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        module.bindings.extend((name, stmt.value) for name in _target_names(stmt.target))
    elif isinstance(stmt, ast.Import):
        for alias in stmt.names:
            bound = import_binding(alias)
            module.bindings.append((bound, _ModuleRef(alias.name if alias.asname else bound)))
    elif isinstance(stmt, ast.ImportFrom):
        _record_import_from(module, stmt)


def _type_call_parts(call: ast.Call) -> tuple[ast.expr, ast.Tuple, ast.Dict]:
    """``(callee, bases, namespace)`` of a call that makes a class.

    The call is ``type(name, bases, ns)`` or ``type.__new__(meta, name,
    bases, ns)``; the callee is the expression that must resolve to the
    builtin ``type``.
    """
    callee, args = call.func, call.args
    if isinstance(callee, ast.Attribute) and callee.attr == "__new__":
        callee, args = callee.value, args[1:]
    if len(args) != 3:
        raise _UnresolvedError("type call")
    _, bases, namespace = args
    if not isinstance(bases, ast.Tuple) or not isinstance(namespace, ast.Dict):
        raise _UnresolvedError("type call")
    return callee, bases, namespace


def _namespace_keys(namespace: ast.Dict) -> set[str]:
    keys = {
        key.value
        for key in namespace.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    if len(keys) != len(namespace.keys):
        raise _UnresolvedError("type call namespace")
    return keys


class StdlibClasses:
    """Reads builtin and standard-library classes statically; one instance per run.

    Parsed standard-library files and resolved names are cached on the
    instance.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(sysconfig.get_paths()["stdlib"]) if root is None else root
        self._modules: dict[str, _Module | None] = {}
        self._names: dict[str, frozenset[str] | None] = {}
        self._hops = 0

    def defined_names(self, dotted: str) -> frozenset[str] | None:
        """The names the class spelled ``dotted`` defines, with its ancestors but ``object``.

        ``dotted`` is a bare builtin (``dict``) or a path whose top-level
        package is in ``sys.stdlib_module_names`` (``enum.Enum``). None means
        unresolved: not a class, not standard library, or not readable
        without running code.
        """
        if dotted not in self._names:
            self._hops = 0
            try:
                self._names[dotted] = self._resolve_dotted(dotted)
            except _UnresolvedError:
                self._names[dotted] = None
        return self._names[dotted]

    def _resolve_dotted(self, dotted: str) -> frozenset[str]:
        head, _, rest = dotted.partition(".")
        if not rest:
            return _class_names(_builtin(head))
        if head not in sys.stdlib_module_names:
            raise _UnresolvedError(dotted)
        found: object = _ModuleRef(head)
        for attr in rest.split("."):
            found = self._attribute(found, attr)
        return _class_names(found)

    def _spend(self, what: str) -> None:
        self._hops += 1
        if self._hops > MAX_HOPS:
            raise _UnresolvedError(what)

    # --- modules ---------------------------------------------------------

    def _source(self, name: str) -> tuple[Path, bool] | None:
        """``name``'s source file under the stdlib root, and whether it is a package."""
        parts = name.split(".")
        if top_level_package(name) not in sys.stdlib_module_names or not all(
            part.isidentifier() for part in parts
        ):
            return None
        package = self._root.joinpath(*parts, "__init__.py")
        if package.is_file():
            return package, True
        module = self._root.joinpath(*parts[:-1], parts[-1] + ".py")
        if module.is_file():
            return module, False
        return None

    def _module(self, name: str) -> _Module | None:
        if name not in self._modules:
            self._modules[name] = self._parse_module(name)
        return self._modules[name]

    def _parse_module(self, name: str) -> _Module | None:
        found = self._source(name)
        if found is None:
            return None
        path, is_package = found
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, ValueError):
            # The stdlib's own test suite ships deliberately broken files.
            return None
        module = _Module(name, is_package, [], {})
        for stmt in _executed_blocks(tree.body):
            _record(module, stmt)
        return module

    def _lookup(self, module: _Module, name: str) -> tuple[_Module, _Bound] | None:
        """What ``name`` is last bound to in ``module``, through ``*`` imports."""
        for binding in reversed(module.bindings):
            if isinstance(binding, _StarImport):
                self._spend(binding.module)
                source = self._module(binding.module)
                if source is not None and (found := self._lookup(source, name)) is not None:
                    return found
            elif binding[0] == name:
                return module, binding[1]
        return None

    def _attribute(self, found: object, attr: str) -> object:
        self._spend(attr)
        if isinstance(found, _ModuleRef):
            return self._module_attribute(found.name, attr)
        return _loaded_attribute(found, attr)

    def _module_attribute(self, name: str, attr: str) -> object:
        loaded = _loaded_compiled_in(name)
        if loaded is not None:
            return _loaded_attribute(_Loaded(loaded), attr)
        module = self._module(name)
        if module is None:
            raise _UnresolvedError(name)
        submodule = f"{name}.{attr}"
        if module.is_package and self._source(submodule) is not None:
            return _ModuleRef(submodule)
        found = self._lookup(module, attr)
        if found is None:
            raise _UnresolvedError(submodule)
        return self._bound_value(*found)

    # --- bindings --------------------------------------------------------

    def _bound_value(self, module: _Module, bound: _Bound) -> object:
        self._spend(module.name)
        match bound:
            case ast.ClassDef():
                return self._class_def_names(module, bound)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                return self._mro_entries_names(module, bound.name)
            case _FromImport(module=source, attr=attr):
                return self._attribute(_ModuleRef(source), attr)
            case _ModuleRef():
                return bound
            case _:
                return self._expr_value(module, bound)

    def _name_value(self, module: _Module, name: str) -> object:
        found = self._lookup(module, name)
        if found is None:
            return _builtin(name)
        return self._bound_value(*found)

    def _expr_value(self, module: _Module, expr: ast.expr) -> object:
        if isinstance(expr, ast.Name):
            return self._name_value(module, expr.id)
        if isinstance(expr, ast.Attribute):
            return self._attribute(self._expr_value(module, expr.value), expr.attr)
        if isinstance(expr, ast.Subscript):
            return self._expr_value(module, expr.value)
        if isinstance(expr, ast.Call):
            return self._type_call_names(module, expr)
        raise _UnresolvedError(ast.dump(expr))

    def _class_def_names(self, module: _Module, node: ast.ClassDef) -> frozenset[str]:
        names = set(_class_body_names(node))
        for base in node.bases:
            names |= _class_names(self._expr_value(module, base))
        return frozenset(names)

    def _mro_entries_names(self, module: _Module, name: str) -> frozenset[str]:
        """A function standing in for a class (``typing.NamedTuple``) gives its ``__mro_entries__``.

        The entries are read from a ``lambda`` or a module function's last
        ``return``, as a tuple of names, as the class statement would use them.
        """
        entries = module.mro_entries.get(name)
        if isinstance(entries, ast.Name):
            found = self._lookup(module, entries.id)
            if found is None or not isinstance(found[1], ast.FunctionDef):
                raise _UnresolvedError(entries.id)
            entries = _last_return(found[1])
        elif isinstance(entries, ast.Lambda):
            entries = entries.body
        if not isinstance(entries, ast.Tuple):
            raise _UnresolvedError(name)
        names: set[str] = set()
        for elt in entries.elts:
            names |= _class_names(self._expr_value(module, elt))
        return frozenset(names)

    def _type_call_names(self, module: _Module, call: ast.Call) -> frozenset[str]:
        """A class made by calling the builtin ``type``, its bases and namespace literal."""
        callee, bases, namespace = _type_call_parts(call)
        found = self._expr_value(module, callee)
        if not isinstance(found, _Loaded) or found.obj is not type:
            raise _UnresolvedError(ast.dump(callee))
        names = _namespace_keys(namespace)
        for base in bases.elts:
            names |= _class_names(self._expr_value(module, base))
        return frozenset(names)


def _builtin(name: str) -> _Loaded:
    namespace = vars(builtins)
    if name not in namespace:
        raise _UnresolvedError(name)
    return _Loaded(namespace[name])
