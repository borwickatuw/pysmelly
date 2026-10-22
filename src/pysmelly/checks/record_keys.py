"""write-only-record-keys: dict keys production code writes and no consumer reads.

The dict counterpart of write-only-attributes. A dict built with constant
string keys is a record in all but name; when one of its keys is written and
nothing that receives the dict ever reads it, the key is vestigial, as an
unread dataclass field is. A dataclass field is matched by name across the
codebase. A dict has no class to hang a name on, so this check follows the
dict itself, and reports only when it has seen every place the dict goes.

**Origins.** A dict display with only constant string keys (``**spread``
entries allowed), or a ``dict(k=...)`` call, written in a production
function, where it is bound to a local name, returned (alone or in a tuple),
appended to a local list, passed to a call or built by a list comprehension.
``d = {}`` filled by ``d["k"] = v`` is one too. Module-level dicts are not
origins: they are tables, which the dispatch and constant checks own. A
dict a ``TypedDict`` annotates is write-only-attributes' subject and is
left to it.

**Writes** are the origin's keys, ``d["k"] = v``, ``d.update(k=...)``,
``d.update({...})`` and ``d |= {...}``, in production code. A store under a
key that is not a constant (``d[name] = v``) makes the dict a mapping, not a
record, and nothing is reported for it.

**Reads** are ``d["k"]`` (including ``del`` and ``+=``), ``d.get("k")``,
``d.pop("k")``, ``d.setdefault("k")`` and ``"k" in d``.

**Following the dict.** Every use of the dict is classified; a use that
hands it on is followed:

- a local name bound once (``row = {...}``, ``x = row``, a loop target, a
  tuple-unpacking target of a returned tuple), through every load of it in
  its scope;
- a call that resolves to a project function (``f(d)``, ``mod.f(d)``, or
  ``self.m(d)`` for a method no other class defines), into the parameter the
  argument binds;
- ``return d`` (alone or in a tuple), to every call site of the function: a
  module-level function's resolving calls, or a method's ``self.m()`` calls
  when the class has no base and nothing else defines the name;
- ``items.append(d)`` onto a list built locally, then every element that
  list yields (``for x in items``, a comprehension over it, ``items[i]``,
  ``items.pop()``, ``sorted(items)``, a filtering comprehension), and the
  list itself through returns and calls.

Each call followed into, or return followed out of, is one hop. The flow is
followed for at most ``MAX_HOPS`` (4) hops; a dict that would go further is
treated as escaped. A name rebound in its scope, captured by a nested
function, declared ``global``/``nonlocal``, or bound at module level (any
importer can read a module's names) is not followed either.

**Wholesale reads and escapes.** Some uses read every key, or hand the dict
to something this analysis cannot see into. Either way nothing is reported
for the dict. Wholesale: ``**d``, ``{**d}``, ``.items()``/``.keys()``/
``.values()``/``.copy()``, iterating it, unpacking it, comparing it with
``==``, formatting it in an f-string, a key read whose key is not a
constant. Escape: any call that does not resolve to a project function and
is not one of the inspectors ``len``/``bool``/``isinstance``/``id``/``type(d)``.
That covers ``json.dump(s)`` and every serializer, template rendering
(``render``, ``render_template``, ``TemplateResponse``), logging's
``extra=``, ``pandas.DataFrame``, an HTTP client's ``json=``, ``print``, a
method on an object the analysis cannot resolve, and any other external
call. Storing the dict in an attribute, another container, or a module
global; yielding it; returning it from a decorated function, a function
referenced as a value, or one with no production caller (dead-code's
question); and passing it to a decorated function or to ``*args``/
``**kwargs`` all escape too.

**Roles.** Writes count in production only; a test that fills a dict builds
a fixture. Reads count in production only: a key only a test reads is still
reported, and the message cites the test reads, as the caller-counting
checks cite test callers. A test that reads the dict whole is cited the
same way. What decides is whether the dict reached a test: once it is
returned to a test-role caller, everything done with it is done on the
test's behalf. Production code that hands the dict to a test-role helper
(an example generator calling a test factory) is still production.

**The dict-builder note.** When the origin is bound to a name, keys are
assigned to it under three or more separate ``if`` blocks, and its one
other use is as an argument to a single call after them, the finding says
so and suggests building it in one expression or splitting the function.
"""

from __future__ import annotations

import ast
import builtins
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from pysmelly.checks.callers import _CallTarget, _resolving_calls
from pysmelly.checks.helpers import (
    bare_target,
    class_methods,
    clause_binding,
    is_bare_name,
    is_referenced_as_value,
    iter_functions,
    own_scope_nodes,
    referenced_name,
    source_trees,
)
from pysmelly.checks.records import RecordIndex, RecordKind
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

CHECK_NAME = "write-only-record-keys"

# Calls followed into plus returns followed out of, from the origin.
MAX_HOPS = 4
# Separate ``if`` blocks filling one dict before the builder note is added.
BUILDER_IF_BLOCKS = 3
# How many followed functions a message names before counting the rest.
_NAMED_FUNCTIONS = 4

_BUILTINS = frozenset(dir(builtins))
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Comprehensions whose result holds the element expression's values.
_ELEMENT_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp)
# Dict methods that read the key they are given, and that write keys.
_KEY_READ_METHODS = frozenset({"get", "pop", "setdefault"})
_KEY_WRITE_METHODS = frozenset({"update"})
# Builtins that look at the value they are handed without reading any key
# of it. ``type`` only with one argument: ``type(name, bases, d)`` builds a
# class from ``d``.
_INSPECTORS = frozenset({"len", "bool", "isinstance", "id"})
# Builtins that hand back a container of the same elements.
_CONTAINER_COPIES = frozenset({"sorted", "list", "reversed", "tuple"})
# List methods that put their argument (at this position) into the list.
_LIST_ADDERS = {"append": 0, "insert": 1}
# List methods that change the list without handing out an element.
_LIST_MUTATORS = frozenset({"append", "extend", "insert", "sort", "reverse", "clear"})
# Statements whose test only asks whether a value is truthy.
_TESTED = (ast.If, ast.While, ast.IfExp, ast.Assert)

_ScopeNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.Module
_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _string(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


# --- scopes ---


@dataclass
class _ScopeInfo:
    """The names one scope binds and loads.

    ``bindings`` counts every binding of a name (parameters, assignments,
    loop and ``with`` targets, imports, nested definitions; ``global`` and
    ``nonlocal`` count twice, so such a name is never "bound once").
    ``loads`` lists the loads of each name, comprehension-bound names
    excepted; ``augmented`` the ``name op= value`` statements; ``nested``
    every name a nested function or class mentions.
    """

    bindings: Counter[str] = field(default_factory=Counter)
    loads: dict[str, list[ast.Name]] = field(default_factory=lambda: defaultdict(list))
    augmented: dict[str, list[ast.AugAssign]] = field(default_factory=lambda: defaultdict(list))
    nested: set[str] = field(default_factory=set)

    def uses(self, name: str) -> tuple[list[ast.Name], list[ast.AugAssign]] | None:
        """The loads and augmented assignments of a name bound once and never captured.

        None for any other name: what it holds is not known from here.
        """
        if self.bindings[name] != 1 or name in self.nested:
            return None
        return self.loads.get(name, []), self.augmented.get(name, [])


def _parameter_names(args: ast.arguments) -> Iterator[str]:
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        yield arg.arg
    for arg in (args.vararg, args.kwarg):
        if arg is not None:
            yield arg.arg


def _target_names(target: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _bound_by(node: ast.AST) -> Iterator[str]:
    """Names a statement binds other than through a ``Name`` target."""
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        for name in node.names:
            yield name
            yield name
    elif name := clause_binding(node):
        yield name


def _scope_info(roots: Iterable[ast.AST], params: Iterable[str]) -> _ScopeInfo:
    """Walk ``roots`` as one scope (see ``_ScopeInfo``)."""
    info = _ScopeInfo(bindings=Counter(params))
    stack: list[tuple[ast.AST, frozenset[str]]] = [(root, frozenset()) for root in roots]
    while stack:
        node, hidden = stack.pop()
        if isinstance(node, (*_SCOPES, ast.ClassDef)):
            info.nested |= _target_names(node)
            if not isinstance(node, ast.Lambda):
                info.bindings[node.name] += 1
            continue
        if isinstance(node, _COMPREHENSIONS):
            hidden = hidden.union(*(_target_names(gen.target) for gen in node.generators))
        children = list(ast.iter_child_nodes(node))
        if isinstance(node, ast.AugAssign) and (aug_target := bare_target(node)) is not None:
            info.augmented[aug_target.id].append(node)
            children.remove(aug_target)
        elif isinstance(node, ast.Name) and node.id not in hidden:
            if isinstance(node.ctx, ast.Load):
                info.loads[node.id].append(node)
            else:
                info.bindings[node.id] += 1
        info.bindings.update(_bound_by(node))
        stack.extend((child, hidden) for child in children)
    return info


def _comprehension_roots(comp: ast.expr, binding: ast.comprehension) -> Iterator[ast.AST]:
    """The parts of a comprehension evaluated in its own scope, less ``binding``'s target."""
    if isinstance(comp, ast.DictComp):
        yield comp.key
        yield comp.value
    elif isinstance(comp, _ELEMENT_COMPREHENSIONS):
        yield comp.elt
    generators: list[ast.comprehension] = getattr(comp, "generators", [])
    for position, gen in enumerate(generators):
        if position:
            yield gen.iter
        if gen is not binding:
            yield gen.target
        yield from gen.ifs


def _own_nodes(scope: _ScopeNode) -> Iterator[ast.AST]:
    """The nodes of one scope's body, not descending into nested functions or classes."""
    return own_scope_nodes([scope.body] if isinstance(scope, ast.Lambda) else scope.body)


def _is_generator(func: _Function) -> bool:
    return any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in _own_nodes(func))


# --- the shapes of a use ---


def _merged_displays(expr: ast.expr) -> Iterator[ast.expr]:
    """The operands of an ``a | b | ...`` chain (or ``expr`` itself)."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        yield from _merged_displays(expr.left)
        yield from _merged_displays(expr.right)
    else:
        yield expr


def _truth_only(node: ast.expr, parent: ast.AST | None) -> bool:
    """Whether ``parent`` uses ``node`` as a statement or for its truth alone."""
    if isinstance(parent, ast.Expr):
        return True
    if isinstance(parent, ast.UnaryOp):
        return isinstance(parent.op, ast.Not)
    if isinstance(parent, _TESTED):
        return parent.test is node
    if isinstance(parent, ast.comprehension):  # ``[x for x in xs if x]``
        return any(test is node for test in parent.ifs)
    return False


def _collecting(node: ast.expr, parent: ast.AST | None) -> ast.expr | None:
    """``[d]``, or a comprehension whose elements are ``d``: the list of the dict."""
    if isinstance(parent, ast.List):
        return parent
    if isinstance(parent, _ELEMENT_COMPREHENSIONS) and parent.elt is node:
        return parent
    return None


def _comparison(node: ast.expr, compare: ast.Compare) -> tuple[bool, str | None]:
    """``(identity, key)`` for a comparison ``node`` takes part in.

    ``identity`` when every operator beside it is ``is``/``is not`` (``d is
    None`` reads nothing); ``key`` when it is ``"k" in node``.
    """
    operands = [compare.left, *compare.comparators]
    position = next(i for i, operand in enumerate(operands) if operand is node)
    ops = [compare.ops[position - 1]] if position else []
    if position < len(compare.ops):
        ops.append(compare.ops[position])
    identity = all(isinstance(op, (ast.Is, ast.IsNot)) for op in ops)
    membership = position > 0 and isinstance(ops[0], (ast.In, ast.NotIn))
    return identity, _string(operands[position - 1]) if membership else None


def _parameter(func: _Function, offset: int, call: ast.Call, argument: int | str) -> ast.arg | None:
    """The parameter an argument (a position or a keyword) binds.

    None for ``*args``/``**kwargs``, or a position a ``*spread`` shifts.
    """
    args = func.args
    if isinstance(argument, str):
        named = [*args.args, *args.kwonlyargs]
        return next((arg for arg in named if arg.arg == argument), None)
    if any(isinstance(arg, ast.Starred) for arg in call.args[:argument]):
        return None
    positional = [*args.posonlyargs, *args.args][offset:]
    return positional[argument] if argument < len(positional) else None


def _unpacked_name(parent: ast.AST | None, unpack: tuple[int, int]) -> str | None:
    """The name ``a, b = f()`` binds at ``unpack = (index, size)``, if that is the shape."""
    index, size = unpack
    if not (isinstance(parent, ast.Assign) and len(parent.targets) == 1):
        return None
    target = parent.targets[0]
    if not isinstance(target, ast.Tuple) or len(target.elts) != size:
        return None
    if any(isinstance(elt, ast.Starred) for elt in target.elts):
        return None
    name = target.elts[index]
    return name.id if isinstance(name, ast.Name) else None


def _assigned_name(assign: ast.AST | None) -> ast.Name | None:
    """The one plain name an assignment binds, if it binds exactly that."""
    if isinstance(assign, ast.Assign) and len(assign.targets) == 1:
        target = assign.targets[0]
    elif isinstance(assign, ast.AnnAssign):
        target = assign.target
    else:
        return None
    return target if isinstance(target, ast.Name) else None


def _display_keys(display: ast.Dict) -> list[tuple[str, ast.AST]] | None:
    """``(key, node)`` for each key of a dict display; None when one is not a string."""
    keys: list[tuple[str, ast.AST]] = []
    for key in display.keys:
        if key is None:
            continue
        text = _string(key)
        if text is None:
            return None
        keys.append((text, key))
    return keys


def _keyword_keys(call: ast.Call) -> list[tuple[str, ast.AST]] | None:
    """``(key, keyword)`` for each keyword of ``dict(k=...)``; None when it has positionals."""
    if call.args:
        return None
    return [(keyword.arg, keyword) for keyword in call.keywords if keyword.arg is not None]


# --- what the codebase knows ---


class _Model:
    """Per-run lookups: parents, scopes, record displays, callees and callers, cached."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.index = RecordIndex(ctx)
        self._infos: dict[int, _ScopeInfo] = {}
        self._callers: dict[int, list[tuple[Path, ast.Call]] | None] = {}
        self._method_counts = Counter(
            method.name
            for tree in ctx.all_trees.values()
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for method in class_methods(node)
        )

    def parent(self, path: Path, node: ast.AST) -> ast.AST | None:
        return self.ctx.parent_map(self.ctx.all_trees[path]).get(node)

    def scope_of(self, path: Path, node: ast.AST) -> _ScopeNode | None:
        """The function, lambda or module whose names ``node`` sees; None in a class body."""
        current = self.parent(path, node)
        while current is not None:
            if isinstance(current, (*_SCOPES, ast.Module)):
                return current
            if isinstance(current, ast.ClassDef):
                return None
            current = self.parent(path, current)
        return None

    def info(self, scope: _ScopeNode) -> _ScopeInfo:
        if id(scope) not in self._infos:
            if isinstance(scope, ast.Module):
                self._infos[id(scope)] = _scope_info(scope.body, ())
            else:
                roots = [scope.body] if isinstance(scope, ast.Lambda) else scope.body
                self._infos[id(scope)] = _scope_info(roots, _parameter_names(scope.args))
        return self._infos[id(scope)]

    def builds_list(self, scope: _ScopeNode, name: str) -> bool:
        """Whether ``name``'s one binding in ``scope`` assigns a new list."""
        if self.info(scope).uses(name) is None:
            return False
        for node in _own_nodes(scope):
            target = _assigned_name(node)
            if target is not None and target.id == name:
                value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
                return isinstance(value, (ast.List, ast.ListComp))
        return False

    def is_builtin(self, name: str, path: Path) -> bool:
        return name in _BUILTINS and self.index.names[path].qualify_name(name) is None

    def is_typeddict(self, annotation: ast.expr | None, path: Path) -> bool:
        if annotation is None:
            return False
        records = self.index.annotated(annotation, self.index.names[path])
        return any(record.kind is RecordKind.TYPEDDICT for record in records)

    def is_test(self, path: Path) -> bool:
        return self.ctx.roles.is_test(path)

    # --- record displays ---

    def display_keys(self, node: ast.AST, path: Path) -> list[tuple[str, ast.AST]] | None:
        """``(key, node)`` for each key a record display writes; None for anything else.

        A dict display whose keys are all constant strings (``**spread``
        entries aside), or a call to the builtin ``dict`` with keywords only.
        """
        if isinstance(node, ast.Dict):
            return _display_keys(node)
        if isinstance(node, ast.Call) and self._is_dict_builtin(node.func, path):
            return _keyword_keys(node)
        return None

    def _is_dict_builtin(self, func: ast.expr, path: Path) -> bool:
        return (
            isinstance(func, ast.Name)
            and self.is_builtin(func.id, path)
            and getattr(builtins, func.id) is dict
        )

    def origin_position(self, node: ast.expr, path: Path) -> bool:
        """Whether a display sits where a record is built: bound, returned, collected or passed."""
        parent = self.parent(path, node)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            return _assigned_name(parent) is not None
        if isinstance(parent, ast.Call):
            return node is not parent.func
        if isinstance(parent, ast.keyword):
            return parent.arg is not None
        if isinstance(parent, ast.Tuple):  # ``return {...}, extra``
            return isinstance(self.parent(path, parent), ast.Return)
        return isinstance(parent, (ast.Return, ast.List)) or (
            isinstance(parent, ast.ListComp) and parent.elt is node
        )

    # --- the call graph ---

    def _method_of(self, func: _Function, path: Path) -> ast.ClassDef | None:
        """The module-level class ``func`` is a method of, if it is one."""
        cls = self.parent(path, func)
        if isinstance(cls, ast.ClassDef) and isinstance(self.parent(path, cls), ast.Module):
            return cls
        return None

    def _self_call_class(self, call: ast.Call, path: Path) -> ast.ClassDef | None:
        """The class whose method makes ``call`` as ``self.m(...)``, if it is such a call."""
        scope = self.scope_of(path, call)
        if not isinstance(scope, _FUNCTIONS) or not isinstance(call.func, ast.Attribute):
            return None
        cls = self._method_of(scope, path)
        params = [*scope.args.posonlyargs, *scope.args.args]
        receiver = call.func.value
        if cls is None or not params or scope.decorator_list:
            return None
        if is_bare_name(receiver, params[0].arg):
            return cls
        return None

    def callees(self, call: ast.Call, path: Path) -> list[tuple[_Function, Path, int]]:
        """The project functions ``call`` resolves to, each with its implicit-argument offset."""
        cls = self._self_call_class(call, path)
        if cls is not None and isinstance(call.func, ast.Attribute):
            name = call.func.attr
            for method in class_methods(cls):
                if method.name == name and self._method_counts[name] == 1:
                    return [(method, path, 1)]
            return []
        names = self.index.names[path]
        return [(func, found.path, 0) for func, found in self.index.functions(call.func, names)]

    def callers(self, func: _Function, path: Path) -> list[tuple[Path, ast.Call]] | None:
        """Every call site of ``func``, or None when some caller cannot be seen."""
        if id(func) not in self._callers:
            self._callers[id(func)] = self._find_callers(func, path)
        return self._callers[id(func)]

    def _find_callers(self, func: _Function, path: Path) -> list[tuple[Path, ast.Call]] | None:
        if func.decorator_list or is_referenced_as_value(func.name, self.ctx):
            return None
        if isinstance(self.parent(path, func), ast.Module):
            target = _CallTarget(func.name, path, self.index.names[path].module)
            return [(where, call) for where, _, call in _resolving_calls(self.ctx, target)]
        cls = self._method_of(func, path)
        if (
            cls is None
            or cls.bases
            or cls.keywords
            or func.name.startswith("__")
            or self._method_counts[func.name] != 1
        ):
            return None
        calls = []
        for entry in self.ctx.call_index.get(func.name, []):
            where, call = Path(entry["file"]), entry["node"]
            if where != path or self._self_call_class(call, where) is not cls:
                return None
            calls.append((where, call))
        return calls


# --- following one dict ---


@dataclass(frozen=True)
class _Held:
    """Where an expression on the dict's path is, and what it holds there.

    ``path`` is the file the expression is in. ``collection`` is set when it
    holds a list of the dicts rather than the dict; ``hops`` counts the
    calls followed into and the returns followed out of since the origin;
    ``tested`` is set once the dict has been returned to a test-role caller
    (see the module docstring's "Roles").
    """

    path: Path
    collection: bool = False
    hops: int = 0
    tested: bool = False

    def element(self) -> _Held:
        return replace(self, collection=False)

    def gathered(self) -> _Held:
        return replace(self, collection=True)

    def hop(self, path: Path, *, into_test: bool) -> _Held:
        return replace(self, path=path, hops=self.hops + 1, tested=self.tested or into_test)


@dataclass
class _Flow:
    """What following one origin found.

    ``escaped`` is set by a production use that reads the dict whole or
    hands it where the analysis cannot follow; nothing is reported then.
    """

    writes: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    reads: set[str] = field(default_factory=set)
    test_reads: dict[str, list[Site]] = field(default_factory=lambda: defaultdict(list))
    test_whole: list[Site] = field(default_factory=list)
    reached: set[str] = field(default_factory=set)
    escaped: bool = False

    def read(self, key: str, node: ast.AST, held: _Held) -> None:
        if held.tested:
            self.test_reads[key].append(Site(str(held.path), getattr(node, "lineno", 0)))
        else:
            self.reads.add(key)

    def whole(self, node: ast.AST, held: _Held) -> None:
        """A wholesale read, or an escape: in production, nothing is reported."""
        if held.tested:
            self.test_whole.append(Site(str(held.path), getattr(node, "lineno", 0)))
        else:
            self.escaped = True

    def write(self, key: str, node: ast.AST, held: _Held) -> None:
        if not held.tested:
            self.writes[key].append(Site(str(held.path), getattr(node, "lineno", 0)))


class _Follower:
    """Follows one dict through every use (see the module docstring)."""

    def __init__(self, model: _Model, flow: _Flow) -> None:
        self.model = model
        self.flow = flow
        self._seen: set[tuple[int, bool, bool]] = set()
        self._seen_names: set[tuple[int, str, bool, bool]] = set()

    def write_display(self, display: ast.expr, held: _Held) -> None:
        for key, node in self.model.display_keys(display, held.path) or ():
            self.flow.write(key, node, held)

    def value(self, node: ast.expr, held: _Held) -> None:
        """Classify the use made of ``node``, an expression holding what ``held`` says."""
        seen = (id(node), held.collection, held.tested)
        if self.flow.escaped or seen in self._seen:
            return
        self._seen.add(seen)
        parent = self.model.parent(held.path, node)
        if isinstance(parent, ast.Subscript) and parent.value is node:
            self._subscript(parent, held)
        elif isinstance(parent, ast.Attribute):
            self._method(parent, held)
        elif isinstance(parent, ast.Compare):
            identity, key = _comparison(node, parent)
            if identity:
                return
            if key is not None and not held.collection:
                self.flow.read(key, parent, held)
            else:
                self.flow.whole(parent, held)
        elif isinstance(parent, ast.Call) and node is not parent.func:
            position = next(i for i, arg in enumerate(parent.args) if arg is node)
            self._argument(parent, position, held)
        elif isinstance(parent, ast.keyword):
            call = self.model.parent(held.path, parent)
            if parent.arg is None or not isinstance(call, ast.Call):
                self.flow.whole(parent, held)  # ``f(**d)`` reads every key
            else:
                self._argument(call, parent.arg, held)
        else:
            self._binding_use(node, parent, held)

    def _binding_use(self, node: ast.expr, parent: ast.AST | None, held: _Held) -> None:
        """Uses that bind, return, iterate or collect the value, and the inert ones."""
        if isinstance(parent, ast.Return):
            self._returned(parent, held, None)
        elif isinstance(parent, ast.Tuple) and isinstance(
            ret := self.model.parent(held.path, parent), ast.Return
        ):
            index = next(i for i, elt in enumerate(parent.elts) if elt is node)
            self._returned(ret, held, (index, len(parent.elts)))
        elif isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
            self._assigned(parent, held)
        elif isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension)) and parent.iter is node:
            self._iterated(parent, held)
        elif (gathering := _collecting(node, parent)) is not None and not held.collection:
            self.value(gathering, held.gathered())
        elif not self._inert(node, parent, held.path):
            self.flow.whole(node, held)

    def _inert(self, node: ast.expr, parent: ast.AST | None, path: Path) -> bool:
        """A use that reads no key: a bare expression statement or a truth test.

        ``a and b`` is inert where the whole expression is.
        """
        if isinstance(parent, ast.BoolOp):
            return self._inert(parent, self.model.parent(path, parent), path)
        return _truth_only(node, parent)

    def _subscript(self, sub: ast.Subscript, held: _Held) -> None:
        if held.collection:
            if isinstance(sub.ctx, ast.Load):
                kept = held if isinstance(sub.slice, ast.Slice) else held.element()
                self.value(sub, kept)
            return
        key = _string(sub.slice)
        if key is None:
            # A computed key: a read of any key, or a mapping filled by
            # computed keys rather than a record.
            self.flow.whole(sub, held)
        elif isinstance(sub.ctx, ast.Store):
            self.flow.write(key, sub, held)
            if isinstance(self.model.parent(held.path, sub), ast.AugAssign):
                self.flow.read(key, sub, held)
        else:
            self.flow.read(key, sub, held)

    def _method(self, attr: ast.Attribute, held: _Held) -> None:
        call = self.model.parent(held.path, attr)
        name = attr.attr
        if not (isinstance(call, ast.Call) and call.func is attr):
            self.flow.whole(attr, held)
        elif held.collection:
            if name == "pop":
                self.value(call, held.element())
            elif name == "copy":
                self.value(call, held)
            elif name not in _LIST_MUTATORS:
                self.flow.whole(call, held)
        elif name in _KEY_READ_METHODS:
            key = _string(call.args[0]) if call.args else None
            if key is None:
                self.flow.whole(call, held)
            else:
                self.flow.read(key, call, held)
        elif name in _KEY_WRITE_METHODS:
            for keyword in call.keywords:
                if keyword.arg is not None:
                    self.flow.write(keyword.arg, keyword, held)
            for arg in call.args:
                self.write_display(arg, held)
        else:
            self.flow.whole(call, held)

    def _argument(self, call: ast.Call, argument: int | str, held: _Held) -> None:
        """The dict passed to a call, at a position or by keyword."""
        func = call.func
        if isinstance(func, ast.Name) and self.model.is_builtin(func.id, held.path):
            if func.id in _INSPECTORS or (func.id == "type" and len(call.args) == 1):
                return
            copies = func.id in _CONTAINER_COPIES and argument == 0 and not call.keywords
            if held.collection and copies:
                self.value(call, held)
            else:
                self.flow.whole(call, held)
            return
        if self._collected(call, argument, held):
            return
        callees = self.model.callees(call, held.path)
        if not callees or held.hops >= MAX_HOPS:
            self.flow.whole(call, held)
            return
        for callee, callee_path, offset in callees:
            param = _parameter(callee, offset, call, argument)
            if (
                param is None
                or callee.decorator_list
                or self.model.is_typeddict(param.annotation, callee_path)
            ):
                self.flow.whole(call, held)
                return
            self.flow.reached.add(callee.name)
            self._name(callee, param.arg, held.hop(callee_path, into_test=False))

    def _collected(self, call: ast.Call, argument: int | str, held: _Held) -> bool:
        """``items.append(d)`` (or ``items.extend(ds)``) onto a list this scope builds.

        True when the call was such a use and the list is now followed.
        """
        func = call.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return False
        if held.collection:
            adds = func.attr == "extend" and argument == 0
        else:
            adds = _LIST_ADDERS.get(func.attr) == argument
        if not adds:
            return False
        scope = self.model.scope_of(held.path, call)
        if scope is None or not self.model.builds_list(scope, func.value.id):
            self.flow.whole(call, held)
        else:
            self._name(scope, func.value.id, held.gathered())
        return True

    def _returned(self, ret: ast.Return, held: _Held, unpack: tuple[int, int] | None) -> None:
        scope = self.model.scope_of(held.path, ret)
        if (
            not isinstance(scope, _FUNCTIONS)
            or held.hops >= MAX_HOPS
            or _is_generator(scope)
            or self.model.is_typeddict(scope.returns, held.path)
        ):
            self.flow.whole(ret, held)
            return
        callers = self.model.callers(scope, held.path)
        if callers is None or all(self.model.is_test(where) for where, _ in callers):
            self.flow.whole(ret, held)
            return
        self.flow.reached.add(scope.name)
        for where, call in callers:
            returned = held.hop(where, into_test=self.model.is_test(where))
            if unpack is None:
                self.value(call, returned)
            else:
                self._unpacked(call, unpack, returned)

    def _unpacked(self, call: ast.Call, unpack: tuple[int, int], held: _Held) -> None:
        """A call returning the dict at ``unpack = (index, size)`` of a tuple."""
        parent = self.model.parent(held.path, call)
        if isinstance(parent, ast.Expr):
            return
        name = _unpacked_name(parent, unpack)
        scope = self.model.scope_of(held.path, call)
        if name is not None and scope is not None:
            self._name(scope, name, held)
        elif (
            isinstance(parent, ast.Subscript)
            and isinstance(parent.slice, ast.Constant)
            and parent.slice.value == unpack[0]
        ):
            self.value(parent, held)
        else:
            self.flow.whole(call, held)

    def _assigned(self, assign: ast.Assign | ast.AnnAssign, held: _Held) -> None:
        target = _assigned_name(assign)
        scope = self.model.scope_of(held.path, assign)
        typed = isinstance(assign, ast.AnnAssign) and self.model.is_typeddict(
            assign.annotation, held.path
        )
        if target is None or scope is None or typed:
            self.flow.whole(assign, held)
        else:
            self._name(scope, target.id, held)

    def _iterated(self, loop: ast.For | ast.AsyncFor | ast.comprehension, held: _Held) -> None:
        target = loop.target
        if not held.collection or not isinstance(target, ast.Name):
            self.flow.whole(loop.iter, held)  # iterating a dict reads its keys
            return
        if isinstance(loop, ast.comprehension):
            comp = self.model.parent(held.path, loop)
            if not isinstance(comp, _COMPREHENSIONS):
                self.flow.whole(loop.iter, held)
                return
            info = _scope_info(_comprehension_roots(comp, loop), [target.id])
            self._follow(info, target.id, held.element(), anchor=loop.iter)
            return
        scope = self.model.scope_of(held.path, loop)
        if scope is None:
            self.flow.whole(loop.iter, held)
        else:
            self._name(scope, target.id, held.element())

    def _name(self, scope: _ScopeNode, name: str, held: _Held) -> None:
        """Every use of a local name bound to the dict (or list) in ``scope``.

        A module-level name is an attribute of the module, which any
        importer can read (``settings.SAML_CONFIG``), so it escapes.
        """
        key = (id(scope), name, held.collection, held.tested)
        if key in self._seen_names:
            return
        self._seen_names.add(key)
        if isinstance(scope, ast.Module):
            self.flow.whole(scope, held)
        else:
            self._follow(self.model.info(scope), name, held, anchor=scope)

    def _follow(self, info: _ScopeInfo, name: str, held: _Held, anchor: ast.AST) -> None:
        uses = info.uses(name)
        if uses is None:
            self.flow.whole(anchor, held)
            return
        loads, augmented = uses
        for load in loads:
            self.value(load, held)
        for aug in augmented if not held.collection else ():
            for operand in _merged_displays(aug.value):
                self.write_display(operand, held)


# --- the dict-builder note ---


def _outermost_if(model: _Model, path: Path, node: ast.AST, scope: _Function) -> ast.If | None:
    found = None
    current = model.parent(path, node)
    while current is not None and current is not scope:
        if isinstance(current, ast.If):
            found = current
        current = model.parent(path, current)
    return found


def _is_key_write(model: _Model, path: Path, load: ast.Name) -> bool:
    """``d["k"] = v`` or ``d.update(...)`` on the loaded name."""
    use = model.parent(path, load)
    if isinstance(use, ast.Subscript):
        return isinstance(use.ctx, ast.Store)
    return isinstance(use, ast.Attribute) and use.attr in _KEY_WRITE_METHODS


def _handed_to(model: _Model, path: Path, load: ast.Name) -> ast.Call | None:
    """The call ``load`` is an argument of, if it is one."""
    use = model.parent(path, load)
    if isinstance(use, ast.keyword):
        use = model.parent(path, use)
    if isinstance(use, ast.Call) and use.func is not load:
        return use
    return None


def _builder_note(model: _Model, path: Path, scope: _Function, display: ast.expr) -> str:
    """The dict-builder sentence, when the origin is filled under 3+ ifs and handed to one call."""
    target = _assigned_name(model.parent(path, display))
    uses = model.info(scope).uses(target.id) if target is not None else None
    if target is None or uses is None:
        return ""
    blocks: set[int] = set()
    last_write = 0
    others: list[ast.Name] = []
    for load in uses[0]:
        if _is_key_write(model, path, load):
            block = _outermost_if(model, path, load, scope)
            if block is not None:
                blocks.add(id(block))
            last_write = max(last_write, load.lineno)
        else:
            others.append(load)
    if len(blocks) < BUILDER_IF_BLOCKS or len(others) != 1:
        return ""
    call = _handed_to(model, path, others[0])
    if call is None or call.lineno < last_write:
        return ""
    callee = referenced_name(call.func) or "a call"
    return (
        f" {target.id} is filled under {len(blocks)} separate `if` blocks and handed to one"
        f" call, {callee}() at line {call.lineno}: build it in one expression, or split"
        f" the function."
    )


# --- the check ---


def _origins(model: _Model, path: Path, tree: ast.Module) -> Iterator[tuple[_Function, ast.expr]]:
    """Each record display a production function builds in an origin position."""
    for func in iter_functions(tree):
        for node in _own_nodes(func):
            if not isinstance(node, (ast.Dict, ast.Call)):
                continue
            keys = model.display_keys(node, path)
            if keys is None or not model.origin_position(node, path):
                continue
            if keys or _assigned_name(model.parent(path, node)) is not None:
                yield func, node


def _site_order(site: Site) -> tuple[str, int]:
    return site.file, site.line


def _written(key: str, site: Site, origin_file: str) -> str:
    """``"k" (:12)``, with the file named when it is not the origin's."""
    where = f":{site.line}" if site.file == origin_file else f"{site.file}:{site.line}"
    return f'"{key}" ({where})'


def _cite(sites: list[Site]) -> str:
    return ", ".join(f"{site.file}:{site.line}" for site in sorted(set(sites), key=_site_order))


@dataclass(frozen=True)
class _Unread:
    """One finding's facts, from which any subset of its keys renders a message."""

    func: str
    path: Path
    line: int
    keys: list[tuple[str, Site]]
    flow: _Flow
    note: str

    def render(self, kept: tuple[int, ...]) -> str:
        keys = [self.keys[i] for i in kept]
        written = ", ".join(_written(key, site, str(self.path)) for key, site in keys)
        noun = "key" if len(keys) == 1 else "keys"
        message = (
            f"dict built in {self.func}() at line {self.line}: {noun} {written} written,"
            f" never read by anything that receives the dict — vestigial record {noun}."
        )
        reached = sorted(self.flow.reached)
        if reached:
            shown = ", ".join(f"{name}()" for name in reached[:_NAMED_FUNCTIONS])
            hidden = len(reached) - _NAMED_FUNCTIONS
            more = f" (+{hidden} more)" if hidden > 0 else ""
            message += f" Followed through {shown}{more}."
        tested = [
            (key, self.flow.test_reads[key]) for key, _ in keys if key in self.flow.test_reads
        ]
        for key, sites in tested:
            message += f' "{key}" is read only by tests, at {_cite(sites)}.'
        if self.flow.test_whole:
            message += f" Tests read the whole dict at {_cite(self.flow.test_whole)}."
        if tested or self.flow.test_whole:
            message += " A test read does not keep a production key alive."
        return message + self.note


@check(
    CHECK_NAME,
    severity=Severity.MEDIUM,
    description=(
        "Dict-as-record keys production code writes and nothing that receives the dict "
        "reads, followed through assignments, calls and returns"
    ),
    advice=Advice(
        fix="Delete the key writes cited, and the test reads named.",
        dismiss="Dismiss only if a consumer outside the analyzed code reads this dict.",
    ),
)
def check_write_only_record_keys(ctx: AnalysisContext) -> list[Finding]:
    """Find dict keys written as a record and never read (see the module docstring)."""
    model = _Model(ctx)
    findings: list[Finding] = []
    reported: set[tuple[str, Site]] = set()
    for path, tree in source_trees(ctx):
        for func, display in _origins(model, path, tree):
            flow = _Flow()
            follower = _Follower(model, flow)
            follower.write_display(display, _Held(path))
            follower.value(display, _Held(path))
            if flow.escaped:
                continue
            keys = [
                (key, sites[0])
                for key, sites in flow.writes.items()
                if key not in flow.reads and (key, sites[0]) not in reported
            ]
            if not keys:
                continue
            keys.sort(key=lambda item: (_site_order(item[1]), item[0]))
            reported.update(keys)
            note = _builder_note(model, path, func, display)
            unread = _Unread(func.name, path, display.lineno, keys, flow, note)
            findings.append(
                multi_site_finding(
                    CHECK_NAME, Severity.MEDIUM, [site for _, site in keys], unread.render
                )
            )
    return findings
