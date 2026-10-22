"""Record classes, their fields, and the sites that write them or read them whole.

write-only-attributes asks of every field of a record class whether
production code writes it and no production code reads it. This module answers the
parts of that question that are not plain attribute reads:

- **Which classes are records.** ``@dataclass`` (any spelling, as before),
  pydantic models, ``typing.NamedTuple``, ``typing.TypedDict`` and attrs
  classes. Every kind but the dataclass is recognised by what its decorator
  or base resolves to through the module's import bindings
  (``ctx.module_imports``), never by the bare name: a project class called
  ``BaseModel`` is not pydantic's. A class whose base is a project pydantic
  model or TypedDict is one too, since those kinds inherit; a dataclass or
  attrs subclass needs its own decorator to add fields.
- **Which records are read wholesale.** ``dataclasses.asdict``/``astuple``/
  ``fields``, attrs' ``asdict``/``astuple``/``fields``/``fields_dict``,
  ``vars(x)``, ``x.__dict__``, pydantic's
  ``model_dump``/``model_dump_json``/``dict``/``json``, NamedTuple's
  ``_asdict`` and tuple unpacking, and for a TypedDict ``**x``, ``.items()``,
  ``.values()`` and ``json.dump(s)``. Each reads every field of the record it
  is handed, so none of those fields is write-only.
- **Which fields production code writes.** One rule for every kind: a field
  is written when a write site names it (an attribute assignment or
  ``setattr`` anywhere, by name; a keyword, a pydantic alias, or a positional
  argument mapped to field order in a call that builds the class, including
  ``model_construct`` and ``cls(...)`` in a classmethod; a copying helper's
  keyword; for a TypedDict, a key of a dict built where its annotation is
  the TypedDict, or set on a value tied to it), or when a construction that
  passes no ``*``/``**`` payload leaves it to its declared default. A class
  filled only by parsing (``model_validate``, ``parse_obj``, ``Cls(**data)``,
  a framework, or as a nested field of a parsed model) has no field
  production code writes, so none is reported: its fields mirror someone
  else's schema. Tests do not write: a test building a record builds a
  fixture.
- **Which fields production code reads.** A field's name read as an
  attribute anywhere in production code (``x.name``, or by a constant
  string: ``getattr(x, "name")``, ``operator.attrgetter("name")``), or as a
  string key for a
  TypedDict, or after a dot in a template (``{{ cut.unread_pages }}``;
  ``pysmelly.templates``), which reads attributes and keys alike. A string
  key never reads another kind's field: a dataclass's fields reach a
  mapping only through a wholesale read (``asdict``, a ``to_dict`` built
  on ``fields()``), and that already reads every field; matching keys by
  name anywhere would let any ``d["name"]`` keep every ``name`` field
  alive (the reason write-only-record-keys follows the dict instead).
  Tests do not read either: a field only a test reads is test-only
  surface, and its finding names the tests that read it (Phase 26-4).
- **Which annotations are not fields.** ``ClassVar`` (a class constant) and
  ``InitVar`` (an ``__init__`` argument that is never stored), for every
  kind, and pydantic's ``model_config``.

Tying a consumed expression to a record class is the hard part, and it is
done conservatively. ``self`` ties to the enclosing class; ``Cls(...)``,
``Cls.from_x(...)`` and a call to a project function or method tie to the
class or to the classes its return annotation names; a name ties through
its bindings in the enclosing scopes (parameter and variable annotations,
assigned values, the iterable a loop target walks); ``self.attr`` ties
through the class's annotation of ``attr``. An annotation ties to every
record class it mentions (``list[Cls]``, ``Cls | None``), a superset. A
record read wholesale has its subclasses' and its ancestors' fields read
too, since a subclass instance may be the one handed over and an instance
carries its ancestors' fields.

When the expression cannot be tied, an untied consumer reads the records of
its kind that the enclosing function (or module body) names. If that names
none, a *specific* consumer, which exists only for records of its kind
(``dataclasses.asdict``, ``model_dump``, ``_asdict``), reads the records of
its kind that its module names anywhere: some record certainly flows there.
A *generic* consumer (``vars``, ``__dict__``, ``**``, unpacking,
``.dict()``, ``.items()``, ``json.dumps``) is everywhere on plain objects,
so it reads nothing. Reading every record of the kind was rejected: one
untieable ``asdict`` would read every dataclass in the project.
"""

from __future__ import annotations

import ast
import builtins
import copy
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pysmelly.checks.callers import _reaches_module
from pysmelly.checks.helpers import (
    NESTED_SCOPES,
    bare_target,
    build_subscript_string_keys,
    filepath_to_module,
    has_dataclass_decorator,
    import_bindings,
    is_ellipsis,
    iter_module_classes,
    referenced_name,
    source_trees,
)
from pysmelly.context import AnalysisContext


class RecordKind(Enum):
    """The kinds of record class; the value names the kind in a finding."""

    DATACLASS = "dataclass"
    PYDANTIC = "pydantic model"
    ATTRS = "attrs class"
    NAMEDTUPLE = "NamedTuple"
    TYPEDDICT = "TypedDict"


_PYDANTIC_BASES = frozenset(
    {
        "pydantic.BaseModel",
        "pydantic.main.BaseModel",
        "pydantic.v1.BaseModel",
        "pydantic.BaseSettings",
        "pydantic_settings.BaseSettings",
    }
)
_NAMEDTUPLE_BASES = frozenset({"typing.NamedTuple", "typing_extensions.NamedTuple"})
_TYPEDDICT_BASES = frozenset({"typing.TypedDict", "typing_extensions.TypedDict"})
_ATTRS_DECORATORS = frozenset(
    {
        "attr.s",
        "attr.attrs",
        "attr.define",
        "attr.frozen",
        "attr.mutable",
        "attr.dataclass",
        "attrs.define",
        "attrs.frozen",
        "attrs.mutable",
    }
)
_ATTRS_FIELD_CALLS = frozenset({"attr.ib", "attr.attrib", "attr.field", "attrs.field"})

# Kinds a project subclass inherits without a decorator of its own.
_INHERITED_KINDS = frozenset({RecordKind.PYDANTIC, RecordKind.TYPEDDICT})

# Annotations that are not fields: class-level constants and init-only values.
_NON_FIELD_ANNOTATIONS = frozenset({"ClassVar", "InitVar"})
# pydantic v2's model configuration, annotated like a field but not one.
_PYDANTIC_CONFIG = "model_config"

# An annotation naming one of these ties to nothing in particular.
_OPEN_ANNOTATIONS = frozenset({"Any", "object"})

# Foreign calls that hand back the records of their arguments from the
# given position on: the container builtins (their items are its items),
# the copying helpers, ``filter`` (its iterable) and ``itertools.chain``.
_PASS_THROUGH_CALLS = {
    **dict.fromkeys(
        (
            "sorted",
            "list",
            "tuple",
            "set",
            "frozenset",
            "reversed",
            "iter",
            "next",
            "min",
            "max",
            "copy.copy",
            "copy.deepcopy",
            "copy.replace",
            "dataclasses.replace",
            "attr.evolve",
            "attrs.evolve",
        ),
        0,
    ),
    "filter": 1,
    "itertools.chain": 0,
}
# Foreign calls whose result is whatever a callable or a lookup makes of
# their arguments, so it ties to nothing; and casts, which say what it is.
_UNTIED_CALLS = frozenset({"map", "getattr", "functools.reduce"})
_CASTS = frozenset({"typing.cast", "typing_extensions.cast"})
_BUILTINS = frozenset(dir(builtins))

# Expressions whose value is never a record instance.
_NOT_RECORDS = (
    ast.Constant,
    ast.JoinedStr,
    ast.Dict,
    ast.DictComp,
    ast.Compare,
    ast.BinOp,
    ast.UnaryOp,
    ast.Lambda,
)

_ATTR_KINDS = frozenset({RecordKind.DATACLASS, RecordKind.PYDANTIC, RecordKind.ATTRS})
_SERIALIZER_KINDS = frozenset({RecordKind.DATACLASS, RecordKind.ATTRS})
_TYPEDDICT_KINDS = frozenset({RecordKind.TYPEDDICT})
_NAMEDTUPLE_KINDS = frozenset({RecordKind.NAMEDTUPLE})

# Specific consumers: a call that exists only for one kind of record.
_SPECIFIC_FUNCTIONS = {
    "dataclasses.asdict": frozenset({RecordKind.DATACLASS}),
    "dataclasses.astuple": frozenset({RecordKind.DATACLASS}),
    "dataclasses.fields": frozenset({RecordKind.DATACLASS}),
    "attr.asdict": frozenset({RecordKind.ATTRS}),
    "attr.astuple": frozenset({RecordKind.ATTRS}),
    "attr.fields": frozenset({RecordKind.ATTRS}),
    "attr.fields_dict": frozenset({RecordKind.ATTRS}),
    "attrs.asdict": frozenset({RecordKind.ATTRS}),
    "attrs.astuple": frozenset({RecordKind.ATTRS}),
    "attrs.fields": frozenset({RecordKind.ATTRS}),
    "attrs.fields_dict": frozenset({RecordKind.ATTRS}),
}
_SPECIFIC_METHODS = {
    "model_dump": frozenset({RecordKind.PYDANTIC}),
    "model_dump_json": frozenset({RecordKind.PYDANTIC}),
    "_asdict": frozenset({RecordKind.NAMEDTUPLE}),
}
# Generic consumers, by the kinds they read whole.
_GENERIC_SERIALIZERS = frozenset({"asdict", "astuple"})  # an unresolved asdict
_GENERIC_METHODS = {
    "dict": frozenset({RecordKind.PYDANTIC}),
    "json": frozenset({RecordKind.PYDANTIC}),
    "items": frozenset({RecordKind.TYPEDDICT}),
    "values": frozenset({RecordKind.TYPEDDICT}),
}
_JSON_DUMPERS = frozenset({"json.dump", "json.dumps"})
_FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_KEY_READ_METHODS = frozenset({"get", "pop", "setdefault"})


@dataclass(frozen=True)
class RecordClass:
    """A record class defined at module scope in production code."""

    name: str
    module: str
    file: Path
    kind: RecordKind
    node: ast.ClassDef = field(compare=False, hash=False, repr=False)

    def declared(self, names: _ModuleNames) -> Iterator[tuple[str, ast.stmt]]:
        """``(name, statement)`` for every field the class declares in its own body."""
        for item in self.node.body:
            name = _field_name(item, self.kind, names)
            if name is None:
                continue
            if self.kind is RecordKind.PYDANTIC and name == _PYDANTIC_CONFIG:
                continue
            yield name, item

    def positional_fields(self, index: RecordIndex) -> list[str]:
        """The fields a construction's positional arguments fill, in order.

        A NamedTuple's own fields; for a dataclass or attrs class its bases'
        fields first (the last base's first, as the MRO is walked backwards),
        then its own, leaving out ``init=False`` fields. A pydantic model and
        a TypedDict take no field positionally.
        """
        if self.kind in {RecordKind.PYDANTIC, RecordKind.TYPEDDICT}:
            return []
        names = index.names[self.file]
        order: list[str] = []
        bases = () if self.kind is RecordKind.NAMEDTUPLE else reversed(self.node.bases)
        for base in bases:
            for parent in index.resolve(base, names) - {self}:
                order += [name for name in parent.positional_fields(index) if name not in order]
        for name, item in self.declared(names):
            if name not in order and not _keyword_is_false(_field_spec(item), "init"):
                order.append(name)
        return order


@dataclass(frozen=True)
class RecordField:
    """One field a record class declares in its own body.

    ``aliases`` are the other names a constructor keyword may write it by
    (pydantic's ``Field(alias=...)``); ``has_default`` says whether a
    construction that does not pass it fills it from the declaration.
    """

    record: RecordClass
    name: str
    line: int
    has_default: bool
    aliases: frozenset[str] = frozenset()

    def is_read(self, reads: RecordReads) -> bool:
        """Read wholesale, or by name: as a string key for a TypedDict, else an attribute."""
        if self.record in reads.whole:
            return True
        by_name = reads.keys if self.record.kind is RecordKind.TYPEDDICT else reads.attrs
        return self.name in by_name

    def is_written(self, writes: RecordWrites) -> bool:
        """Written by production code: at a write site that names it, or by its default.

        A TypedDict key is only ever written by a site tied to its class;
        another record's field is also written by assigning an attribute of
        its name anywhere, as reads are counted by name. The default counts
        when production code builds the record (or a subclass) with a call
        that passes no ``*``/``**`` payload, since only then is it known
        that the declaration, not someone else's data, filled the field.
        """
        explicit = writes.fields.get(self.record, set())
        if self.name in explicit or self.aliases & explicit:
            return True
        if self.record.kind is not RecordKind.TYPEDDICT and self.name in writes.attrs:
            return True
        return self.has_default and self.record in writes.built


@dataclass
class RecordReads:
    """What the codebase reads of its records.

    ``attrs`` is every attribute name read anywhere; ``keys`` every string
    read as a mapping key (``x["k"]``, ``x.get("k")``, ``"k" in x``);
    ``whole`` the records some site reads wholesale.
    """

    attrs: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)
    whole: set[RecordClass] = field(default_factory=set)


@dataclass
class RecordWrites:
    """What production code writes into its records.

    ``attrs`` is every attribute name assigned anywhere (``x.f = v``,
    ``setattr(x, "f", v)``) or passed to a copying helper (``replace(x,
    f=v)``, ``x._replace(f=v)``, ``x.model_copy(update={"f": v})``), by name
    like attribute reads. ``fields`` holds, per record class, the names its
    constructions pass (keywords, positional arguments mapped to field
    order) and, for a TypedDict, the keys a site tied to it writes.
    ``built`` is every record some construction fills from its defaults.
    Both are carried up to the record's ancestors, whose fields a subclass
    construction fills too.
    """

    attrs: set[str] = field(default_factory=set)
    fields: dict[RecordClass, set[str]] = field(default_factory=lambda: defaultdict(set))
    built: set[RecordClass] = field(default_factory=set)


class _ModuleNames:
    """How one module's names resolve: its imports, classes and functions."""

    def __init__(self, path: Path, tree: ast.Module, ctx: AnalysisContext) -> None:
        self.path = path
        self.tree = tree
        self.module = filepath_to_module(str(path), ctx.package_dirs)
        self.bindings = import_bindings(ctx.module_imports[path])
        self.local_defs = {
            node.name
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def qualify_name(self, name: str) -> str | None:
        """The dotted path a bare name stands for: a local def's or an import's."""
        if name in self.local_defs:
            return f"{self.module}.{name}"
        return self.bindings.get(name)


def _qualify(expr: ast.expr, names: _ModuleNames) -> str | None:
    """The dotted path ``expr`` names in a module, through a local def or an import binding."""
    if isinstance(expr, ast.Attribute):
        owner = _qualify(expr.value, names)
        return None if owner is None else f"{owner}.{expr.attr}"
    return names.qualify_name(expr.id) if isinstance(expr, ast.Name) else None


def _non_field_annotation(annotation: ast.expr) -> bool:
    target = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return referenced_name(target) in _NON_FIELD_ANNOTATIONS


class RecordIndex:
    """Every record class in production code, and what resolves to them."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.names = {path: _ModuleNames(path, tree, ctx) for path, tree in ctx.all_trees.items()}
        self.records: list[RecordClass] = []
        self._by_name: dict[str, list[RecordClass]] = defaultdict(list)
        self._functions: dict[str, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, Path]]]
        self._functions = defaultdict(list)
        self.parents: dict[RecordClass, set[RecordClass]] = defaultdict(set)
        self.children: dict[RecordClass, set[RecordClass]] = defaultdict(set)
        self._module_mentions: dict[Path, set[RecordClass]] = {}
        for path, tree in ctx.all_trees.items():
            module = self.names[path].module
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._functions[node.name].append((module, node, path))
        self._project_segments = {
            segment for names in self.names.values() for segment in names.module.split(".")
        }
        self._find_records(ctx)

    # --- recognition ---

    def _add(self, path: Path, node: ast.ClassDef, kind: RecordKind) -> None:
        record = RecordClass(node.name, self.names[path].module, path, kind, node)
        self.records.append(record)
        self._by_name[node.name].append(record)

    def _find_records(self, ctx: AnalysisContext) -> None:
        pending: list[tuple[Path, ast.ClassDef]] = []
        for path, tree in source_trees(ctx):
            for node in iter_module_classes(tree):
                kind = _direct_kind(node, self.names[path])
                if kind is None:
                    pending.append((path, node))
                else:
                    self._add(path, node, kind)
        # A subclass of a project pydantic model or TypedDict is one too,
        # however many project classes stand between it and the library base.
        changed = True
        while changed:
            changed = False
            for path, node in list(pending):
                inherited = {
                    record.kind
                    for base in node.bases
                    for record in self.resolve(base, self.names[path])
                    if record.kind in _INHERITED_KINDS
                }
                if inherited:
                    self._add(path, node, min(inherited, key=lambda kind: kind.value))
                    pending.remove((path, node))
                    changed = True
        for record in self.records:
            for base in record.node.bases:
                for parent in self.resolve(base, self.names[record.file]):
                    if parent != record:
                        self.parents[record].add(parent)
                        self.children[parent].add(record)

    def resolve(self, expr: ast.expr, names: _ModuleNames) -> set[RecordClass]:
        """The record classes ``expr`` names in the module ``names`` describes."""
        qualified = _qualify(expr, names)
        if qualified is None:
            return set()
        module, _, name = qualified.rpartition(".")
        return {
            record
            for record in self._by_name.get(name, [])
            if module and _reaches_module(module, record.module)
        }

    def functions(
        self, expr: ast.expr, names: _ModuleNames
    ) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, _ModuleNames]]:
        """The module-level project functions a callee expression names."""
        qualified = _qualify(expr, names)
        if qualified is None:
            return
        module, _, name = qualified.rpartition(".")
        for defining, node, path in self._functions.get(name, []):
            if module and _reaches_module(module, defining):
                yield node, self.names[path]

    def is_library(self, qualified: str) -> bool:
        """Whether a dotted path lies outside the project.

        Its root must name no segment of any project module, since an import
        can reach a project module through a script directory on ``sys.path``
        (``from s3store import x`` for ``shared/s3store.py``).
        """
        return qualified.partition(".")[0] not in self._project_segments

    def module_mentions(self, path: Path) -> set[RecordClass]:
        """Every record class a module names anywhere, in any scope."""
        if path not in self._module_mentions:
            names = self.names[path]
            self._module_mentions[path] = {
                record
                for node in ast.walk(names.tree)
                if isinstance(node, (ast.Name, ast.Attribute))
                for record in self.resolve(node, names)
            }
        return self._module_mentions[path]

    def annotated(self, annotation: ast.expr, names: _ModuleNames) -> set[RecordClass]:
        """Every record class an annotation names, forward references included."""
        found: set[RecordClass] = set()
        for node in _annotation_nodes(annotation):
            if isinstance(node, (ast.Name, ast.Attribute)):
                found |= self.resolve(node, names)
        return found

    def class_records(self, cls: ast.ClassDef, names: _ModuleNames) -> set[RecordClass]:
        """What ``self`` (or ``cls``) is in a method: the class, or the records it extends."""
        records = {record for record in self.records if record.node is cls}
        if records:
            return records
        return {record for base in cls.bases for record in self.resolve(base, names)}

    def closure(self, records: set[RecordClass]) -> set[RecordClass]:
        """``records``, their subclasses, and every ancestor of those."""
        down = set(records)
        stack = list(records)
        while stack:
            for child in self.children.get(stack.pop(), ()):
                if child not in down:
                    down.add(child)
                    stack.append(child)
        result = set(down)
        stack = list(down)
        while stack:
            for parent in self.parents.get(stack.pop(), ()):
                if parent not in result:
                    result.add(parent)
                    stack.append(parent)
        return result

    # --- fields ---

    def fields(self) -> Iterator[RecordField]:
        """Every public field each record declares in its own body."""
        for record in self.records:
            for name, item in record.declared(self.names[record.file]):
                if name.startswith("_"):
                    continue
                spec = _field_spec(item)
                yield RecordField(
                    record,
                    name,
                    item.lineno,
                    has_default=_has_default(item, spec),
                    aliases=_aliases(spec) if record.kind is RecordKind.PYDANTIC else frozenset(),
                )

    def of_kinds(self, kinds: frozenset[RecordKind]) -> Iterator[tuple[RecordKind, RecordClass]]:
        """Every record of one of ``kinds``, with its kind."""
        for record in self.records:
            if record.kind in kinds:
                yield record.kind, record

    def ancestors(self, record: RecordClass) -> set[RecordClass]:
        """``record`` and every project record it extends."""
        found = {record}
        stack = [record]
        while stack:
            for parent in self.parents.get(stack.pop(), ()):
                if parent not in found:
                    found.add(parent)
                    stack.append(parent)
        return found


def _direct_kind(node: ast.ClassDef, names: _ModuleNames) -> RecordKind | None:
    """The record kind a class's own decorator or library base gives it."""
    if has_dataclass_decorator(node):
        return RecordKind.DATACLASS
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if _qualify(target, names) in _ATTRS_DECORATORS:
            return RecordKind.ATTRS
    for base in node.bases:
        qualified = _qualify(base, names)
        if qualified in _PYDANTIC_BASES:
            return RecordKind.PYDANTIC
        if qualified in _NAMEDTUPLE_BASES:
            return RecordKind.NAMEDTUPLE
        if qualified in _TYPEDDICT_BASES:
            return RecordKind.TYPEDDICT
    return None


def _annotation_nodes(annotation: ast.expr) -> Iterator[ast.AST]:
    """Every node of an annotation, reading a string as a forward reference.

    ``"Cls"`` and ``"list[Cls]"`` yield the nodes of the parsed expression;
    a string that does not parse yields nothing more.
    """
    for node in ast.walk(annotation):
        yield node
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval")
            except SyntaxError:
                continue
            yield from ast.walk(parsed.body)


def _field_name(item: ast.stmt, kind: RecordKind, names: _ModuleNames) -> str | None:
    if isinstance(item, ast.AnnAssign) and (target := bare_target(item)) is not None:
        if _non_field_annotation(item.annotation):
            return None
        return target.id
    if (
        kind is RecordKind.ATTRS
        and isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Name)
        and isinstance(item.value, ast.Call)
        and _qualify(item.value.func, names) in _ATTRS_FIELD_CALLS
    ):
        return item.targets[0].id
    return None


# The field specifiers of dataclasses (``field``), pydantic (``Field``) and
# attrs (``ib``/``attrib``/``field``), by bare name: their keywords say
# whether the field has a default, its alias, and whether ``__init__`` takes it.
_FIELD_SPECIFIERS = frozenset({"field", "Field", "ib", "attrib"})
_DEFAULT_KEYWORDS = frozenset({"default", "default_factory", "factory"})
_ALIAS_KEYWORDS = frozenset({"alias", "validation_alias"})


def _field_value(item: ast.stmt) -> ast.expr | None:
    """The value a field declaration assigns, if any."""
    return item.value if isinstance(item, (ast.AnnAssign, ast.Assign)) else None


def _field_spec(item: ast.stmt) -> ast.Call | None:
    """The field-specifier call a declaration assigns, if it assigns one."""
    value = _field_value(item)
    if isinstance(value, ast.Call) and referenced_name(value.func) in _FIELD_SPECIFIERS:
        return value
    return None


def _has_default(item: ast.stmt, spec: ast.Call | None) -> bool:
    """Whether the declaration gives the field a value when a construction does not.

    A plain value is the default. A specifier gives one by keyword, or, for
    pydantic's ``Field``, by a first positional argument other than ``...``.
    """
    if spec is None:
        return _field_value(item) is not None
    if any(keyword.arg in _DEFAULT_KEYWORDS for keyword in spec.keywords):
        return True
    first = spec.args[0] if spec.args else None
    return first is not None and not is_ellipsis(first)


def _aliases(spec: ast.Call | None) -> frozenset[str]:
    """The string aliases a pydantic ``Field(...)`` declares."""
    if spec is None:
        return frozenset()
    return frozenset(
        keyword.value.value
        for keyword in spec.keywords
        if keyword.arg in _ALIAS_KEYWORDS
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    )


def _keyword_is_false(spec: ast.Call | None, name: str) -> bool:
    return spec is not None and any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in spec.keywords
    )


# --- consumption ---


_ScopeNode = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """The nodes of one scope, not descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(child))


def _binding_targets(node: ast.AST) -> Iterator[tuple[str, str, ast.expr]]:
    """``(name, how, expr)`` for each simple name a statement binds.

    ``how`` is ``"ann"`` (the expr is an annotation), ``"value"`` (the name
    holds the expr's value) or ``"iter"`` (the name walks the expr's items).
    """
    if isinstance(node, ast.arguments):
        for arg in [*node.posonlyargs, *node.args, *node.kwonlyargs]:
            if arg.annotation is not None:
                yield arg.arg, "ann", arg.annotation
    elif isinstance(node, ast.AnnAssign) and (target := bare_target(node)) is not None:
        yield target.id, "ann", node.annotation
        if node.value is not None:
            yield target.id, "value", node.value
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id, "value", node.value
    elif isinstance(node, ast.NamedExpr):
        yield node.target.id, "value", node.value
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        if (target := bare_target(node)) is not None:
            yield target.id, "iter", node.iter


class _Scope:
    """One function (or the module body) and the records its names can hold."""

    def __init__(
        self,
        node: _ScopeNode,
        names: _ModuleNames,
        cls: ast.ClassDef | None,
        parent: _Scope | None,
    ) -> None:
        self.node = node
        self.names = names
        self.cls = cls
        self.parent = parent
        self._bindings: dict[str, list[tuple[str, ast.expr]]] | None = None
        self._mentions: set[RecordClass] | None = None

    def bindings(self) -> dict[str, list[tuple[str, ast.expr]]]:
        if self._bindings is None:
            self._bindings = defaultdict(list)
            # A function's ``arguments`` node is one of its scope nodes.
            for node in _scope_nodes(self.node):
                for name, how, expr in _binding_targets(node):
                    self._bindings[name].append((how, expr))
        return self._bindings

    def self_name(self) -> str | None:
        """The first parameter's name when this scope is a method."""
        if self.cls is None or isinstance(self.node, (ast.Module, ast.Lambda)):
            return None
        params = [*self.node.args.posonlyargs, *self.node.args.args]
        return params[0].arg if params else None

    def mentions(self, index: RecordIndex) -> set[RecordClass]:
        if self._mentions is None:
            self._mentions = set()
            for node in _scope_nodes(self.node):
                if isinstance(node, (ast.Name, ast.Attribute)):
                    self._mentions |= index.resolve(node, self.names)
        return self._mentions


@dataclass(frozen=True)
class _Consumption:
    """A site that reads every field of whatever ``expr`` evaluates to.

    For a loop target unpacked, ``expr`` is the iterable: an annotation
    ties a container to the records it mentions, so the iterable and its
    items tie alike.
    """

    expr: ast.expr
    kinds: frozenset[RecordKind]
    specific: bool


def _call_consumptions(call: ast.Call, names: _ModuleNames) -> Iterator[_Consumption]:
    func = call.func
    qualified = _qualify(func, names)
    first = call.args[0] if call.args else None
    if first is not None and qualified in _SPECIFIC_FUNCTIONS:
        yield _Consumption(first, _SPECIFIC_FUNCTIONS[qualified], specific=True)
    elif first is not None and referenced_name(func) in _GENERIC_SERIALIZERS:
        yield _Consumption(first, _SERIALIZER_KINDS, specific=False)
    elif (
        first is not None and isinstance(func, ast.Name) and func.id == "vars" and qualified is None
    ):
        yield _Consumption(first, _ATTR_KINDS, specific=False)
    elif first is not None and qualified in _JSON_DUMPERS:
        yield _Consumption(first, _TYPEDDICT_KINDS, specific=False)
    elif isinstance(func, ast.Attribute) and not call.args:
        if func.attr in _SPECIFIC_METHODS:
            yield _Consumption(func.value, _SPECIFIC_METHODS[func.attr], specific=True)
        elif func.attr in _GENERIC_METHODS:
            yield _Consumption(func.value, _GENERIC_METHODS[func.attr], specific=False)
    for keyword in call.keywords:
        if keyword.arg is None:
            yield _Consumption(keyword.value, _TYPEDDICT_KINDS, specific=False)
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            yield _Consumption(arg.value, _NAMEDTUPLE_KINDS, specific=False)


def _consumptions(node: ast.AST, names: _ModuleNames) -> Iterator[_Consumption]:
    """The wholesale reads one node makes (see the module docstring)."""
    if isinstance(node, ast.Call):
        yield from _call_consumptions(node, names)
    elif isinstance(node, ast.Attribute) and node.attr == "__dict__":
        if isinstance(node.ctx, ast.Load):
            yield _Consumption(node.value, _ATTR_KINDS, specific=False)
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                yield _Consumption(value, _TYPEDDICT_KINDS, specific=False)
    elif isinstance(node, ast.Assign) and not isinstance(node.value, (ast.Tuple, ast.List)):
        # ``a, b = x, y`` binds each element; nothing is unpacked whole.
        if any(isinstance(target, (ast.Tuple, ast.List)) for target in node.targets):
            yield _Consumption(node.value, _NAMEDTUPLE_KINDS, specific=False)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        if isinstance(node.target, (ast.Tuple, ast.List)):
            yield _Consumption(node.iter, _NAMEDTUPLE_KINDS, specific=False)


def _named_attribute_reads(node: ast.AST, names: _ModuleNames) -> Iterator[str]:
    """Attribute names a call reads by string: ``getattr(x, "name")`` (with or
    without a default) and ``operator.attrgetter("name", "a.b")``."""
    if not isinstance(node, ast.Call):
        return
    qualified = _qualify(node.func, names)
    if isinstance(node.func, ast.Name) and node.func.id == "getattr" and qualified is None:
        literal = _string(node.args[1]) if len(node.args) >= 2 else None
        if literal is not None:
            yield literal
    elif qualified == "operator.attrgetter":
        for arg in node.args:
            literal = _string(arg)
            if literal is not None:
                yield from literal.split(".")


def _key_reads(node: ast.AST) -> Iterator[str]:
    """String keys a node reads off a mapping, beyond plain subscripts."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _KEY_READ_METHODS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        yield node.args[0].value
    elif (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    ):
        yield node.left.value


class _Binder:
    """Ties expressions in one scope chain to the record classes they hold."""

    def __init__(self, index: RecordIndex) -> None:
        self.index = index
        self._seen: set[int] = set()

    def annotation(self, annotation: ast.expr, names: _ModuleNames) -> set[RecordClass] | None:
        records = self.index.annotated(annotation, names)
        if records:
            return records
        if any(
            isinstance(node, (ast.Name, ast.Attribute))
            and referenced_name(node) in _OPEN_ANNOTATIONS
            for node in _annotation_nodes(annotation)
        ):
            return None
        return set()

    def returns(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, names: _ModuleNames
    ) -> set[RecordClass] | None:
        if func.returns is None:
            return None
        return self.annotation(func.returns, names)

    def tie(self, expr: ast.expr, scope: _Scope) -> set[RecordClass] | None:
        """The records ``expr`` can hold, or None when nothing ties it."""
        if id(expr) in self._seen:
            return None
        self._seen.add(id(expr))
        try:
            return self._tie(expr, scope)
        finally:
            self._seen.discard(id(expr))

    def _tie(self, expr: ast.expr, scope: _Scope) -> set[RecordClass] | None:
        if isinstance(expr, _NOT_RECORDS):
            return set()
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            return self._tie_all(expr.elts, scope)
        if isinstance(expr, ast.BoolOp):
            return self._tie_all(expr.values, scope)
        if isinstance(expr, ast.IfExp):
            return self._tie_all([expr.body, expr.orelse], scope)
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self.tie(expr.elt, scope)
        if isinstance(expr, (ast.Subscript, ast.Starred, ast.Await, ast.NamedExpr)):
            return self.tie(expr.value, scope)
        if isinstance(expr, ast.Call):
            return self._tie_call(expr, scope)
        if isinstance(expr, ast.Attribute):
            return self._tie_attribute(expr, scope)
        if isinstance(expr, ast.Name):
            return self._tie_name(expr.id, scope)
        return None

    def _tie_all(self, exprs: list[ast.expr], scope: _Scope) -> set[RecordClass] | None:
        """The union of what each expression ties to; None when any is untied."""
        tied: set[RecordClass] = set()
        for expr in exprs:
            records = self.tie(expr, scope)
            if records is None:
                return None
            tied |= records
        return tied

    def _tie_call(self, call: ast.Call, scope: _Scope) -> set[RecordClass] | None:
        names = scope.names
        records = self.index.resolve(call.func, names)
        if records:
            return records
        if isinstance(call.func, ast.Attribute):
            owner = self.index.resolve(call.func.value, names)
            if owner:
                return owner  # Cls.from_x(...), Cls.model_validate(...)
            method = _self_method(call.func, scope)
            if method is not None:
                return self.returns(method, names)
        tied: set[RecordClass] = set()
        found = False
        for func, func_names in self.index.functions(call.func, names):
            found = True
            records = self.returns(func, func_names)
            if records is None:
                return None
            tied |= records
        return tied if found else self._tie_foreign_call(call, scope)

    def _tie_foreign_call(self, call: ast.Call, scope: _Scope) -> set[RecordClass] | None:
        """A call to a builtin, a library, or ``.items()``: none builds a project record.

        ``sorted(x)`` and its kin, and ``copy.copy(x)``/``dataclasses.replace(x)``
        and theirs, hand back their argument's records. A name bound to
        nothing (a builtin) or to a module outside the project is foreign;
        anything else is untied.
        """
        callee = _foreign_callee(call.func, scope.names, self.index)
        if callee is None or callee in _UNTIED_CALLS:
            return None
        args = call.args
        if callee in _CASTS:
            return self.annotation(args[0], scope.names) if args else None
        position = _PASS_THROUGH_CALLS.get(callee)
        return set() if position is None else self._tie_all(args[position:], scope)

    def _tie_attribute(self, expr: ast.Attribute, scope: _Scope) -> set[RecordClass] | None:
        """``self.attr`` through the class's annotation, ``rec.attr`` through the record's."""
        owner = _method_scope(scope)
        if owner is not None and owner.cls is not None and _is_name(expr.value, owner.self_name()):
            for node in ast.walk(owner.cls):
                if isinstance(node, ast.AnnAssign) and _attribute_target(
                    node.target, expr.attr, owner
                ):
                    return self.annotation(node.annotation, scope.names)
            return None
        holders = self.tie(expr.value, scope)
        if not holders:
            return None
        annotations = [
            (item.annotation, self.index.names[holder.file])
            for holder in self.index.closure(holders)
            for item in holder.node.body
            if isinstance(item, ast.AnnAssign) and _is_name(item.target, expr.attr)
        ]
        if not annotations:
            return None
        tied: set[RecordClass] = set()
        for annotation, names in annotations:
            records = self.annotation(annotation, names)
            if records is None:
                return None
            tied |= records
        return tied

    def _tie_name(self, name: str, scope: _Scope) -> set[RecordClass] | None:
        """A name through its innermost scope's bindings.

        An annotation declares what the name holds, so a name with one is
        tied by its annotations alone; otherwise by every value bound to it.
        """
        current: _Scope | None = scope
        while current is not None:
            if current.cls is not None and name == current.self_name():
                return self.index.class_records(current.cls, current.names)
            bindings = current.bindings().get(name)
            if bindings:
                annotations = [expr for how, expr in bindings if how == "ann"]
                if annotations:
                    return self._annotated_all(annotations, current.names)
                return self._tie_all([expr for _, expr in bindings], current)
            current = current.parent
        return None

    def _annotated_all(
        self, annotations: list[ast.expr], names: _ModuleNames
    ) -> set[RecordClass] | None:
        tied: set[RecordClass] = set()
        for annotation in annotations:
            records = self.annotation(annotation, names)
            if records is None:
                return None
            tied |= records
        return tied


def _self_method(
    func: ast.Attribute, scope: _Scope
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The method a ``self.method`` callee names in the enclosing class, if any."""
    owner = _method_scope(scope)
    if owner is None or owner.cls is None or not _is_name(func.value, owner.self_name()):
        return None
    for item in owner.cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == func.attr:
            return item
    return None


def _foreign_callee(func: ast.expr, names: _ModuleNames, index: RecordIndex) -> str | None:
    """The dotted name of a builtin or library callee; None for anything else.

    ``m.items`` on an unbound receiver is the mapping method, named
    ``.items``. Any other method on an unbound receiver is unknown.
    """
    qualified = _qualify(func, names)
    if qualified is not None:
        return qualified if index.is_library(qualified) else None
    if isinstance(func, ast.Attribute):
        return ".items" if func.attr == "items" else None
    if isinstance(func, ast.Name) and func.id in _BUILTINS:
        return func.id
    return None


def _is_name(expr: ast.expr, name: str | None) -> bool:
    return name is not None and isinstance(expr, ast.Name) and expr.id == name


def _method_scope(scope: _Scope) -> _Scope | None:
    """The nearest enclosing method scope, through nested functions."""
    current: _Scope | None = scope
    while current is not None:
        if current.self_name() is not None:
            return current
        current = current.parent
    return None


def _attribute_target(target: ast.expr, attr: str, owner: _Scope) -> bool:
    """Whether an annotated target declares ``attr`` on the class: in its body or on self."""
    if isinstance(target, ast.Name):
        return target.id == attr
    return (
        isinstance(target, ast.Attribute)
        and target.attr == attr
        and _is_name(target.value, owner.self_name())
    )


# --- writes ---

# Copying helpers whose keywords set fields on the copy they return.
_COPY_FUNCTIONS = frozenset({"dataclasses.replace", "copy.replace", "attr.evolve", "attrs.evolve"})
# Methods that put their arguments into a container.
_ADD_METHODS = frozenset({"append", "add", "insert", "extend"})


def _string(expr: ast.expr | None) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _dict_keys(expr: ast.expr) -> set[str]:
    """The string keys the dicts an expression builds carry.

    A dict display's constant keys and a ``dict(k=...)`` call's keywords,
    through containers, comprehensions and conditionals: a list of dicts
    annotated ``list[Row]`` writes ``Row``'s keys as surely as one dict does.
    """
    if isinstance(expr, ast.Dict):
        return {key for key in map(_string, expr.keys) if key is not None}
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "dict":
        return {keyword.arg for keyword in expr.keywords if keyword.arg is not None}
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return {key for elt in expr.elts for key in _dict_keys(elt)}
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _dict_keys(expr.elt)
    if isinstance(expr, ast.IfExp):
        return _dict_keys(expr.body) | _dict_keys(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return {key for value in expr.values for key in _dict_keys(value)}
    return set()


class _WriteCollector:
    """Records what production code writes into records (see ``RecordWrites``)."""

    def __init__(self, index: RecordIndex, writes: RecordWrites) -> None:
        self.index = index
        self.writes = writes

    def visit(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.Call):
            self._call(node, scope)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            self.writes.attrs.add(node.attr)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            self._subscript_store(node, scope)
        elif isinstance(node, ast.Assign):
            self._assign(node, scope)
        else:
            declared = _declared_value(node, scope)
            if declared is not None:
                annotation, value = declared
                self._keys_for(self.index.annotated(annotation, scope.names), value)

    def _subscript_store(self, node: ast.Subscript, scope: _Scope) -> None:
        key = _string(node.slice)
        if key is not None:
            self._keys_into(node.value, {key}, scope)

    def _assign(self, node: ast.Assign, scope: _Scope) -> None:
        keys = _dict_keys(node.value)
        for target in node.targets if keys else ():
            if isinstance(target, ast.Name):
                self._keys_into(target, keys, scope)

    def _keys_for(self, records: set[RecordClass], value: ast.expr) -> None:
        """The TypedDicts among ``records`` get the keys ``value``'s dicts carry."""
        typed = [record for record in records if record.kind is RecordKind.TYPEDDICT]
        keys = _dict_keys(value) if typed else set()
        for record in typed:
            self._add(record, keys)

    def _keys_into(self, target: ast.expr, keys: set[str], scope: _Scope) -> None:
        """``keys`` are written into whatever ``target`` holds, when it ties to TypedDicts."""
        for record in _Binder(self.index).tie(target, scope) or ():
            if record.kind is RecordKind.TYPEDDICT:
                self._add(record, keys)

    def _add(self, record: RecordClass, names: set[str]) -> None:
        for ancestor in self.index.ancestors(record):
            self.writes.fields[ancestor] |= names

    def _call(self, call: ast.Call, scope: _Scope) -> None:
        func = call.func
        keywords = {keyword.arg for keyword in call.keywords if keyword.arg is not None}
        name = referenced_name(func)
        if name in {"setattr", "__setattr__"} and len(call.args) > 1:
            attr = _string(call.args[1])
            if attr is not None:
                self.writes.attrs.add(attr)
        elif _qualify(func, scope.names) in _COPY_FUNCTIONS or name == "_replace":
            self.writes.attrs |= keywords
        elif name == "model_copy":
            for keyword in call.keywords:
                if keyword.arg == "update":
                    self.writes.attrs |= _dict_keys(keyword.value)
        elif isinstance(func, ast.Attribute) and name in _ADD_METHODS:
            keys = {key for arg in call.args for key in _dict_keys(arg)}
            if keys:
                self._keys_into(func.value, keys, scope)
        elif isinstance(func, ast.Attribute) and name == "update":
            # ``setdefault`` is not here: it reads the key it may write.
            keys = keywords | {key for arg in call.args for key in _dict_keys(arg)}
            if keys:
                self._keys_into(func.value, keys, scope)
        for record in self._constructed(func, scope):
            self._construction(record, call, keywords)

    def _construction(self, record: RecordClass, call: ast.Call, keywords: set[str]) -> None:
        """A call that builds ``record``: what it passes, and whether defaults fill the rest."""
        names = set(keywords)
        payload = any(keyword.arg is None for keyword in call.keywords)
        positional = record.positional_fields(self.index)
        for position, arg in enumerate(call.args):
            if isinstance(arg, ast.Starred):
                payload = True
                break
            if record.kind is RecordKind.TYPEDDICT:
                names |= _dict_keys(arg)
            elif position < len(positional):
                names.add(positional[position])
        self._add(record, names)
        if not payload:
            self.writes.built |= self.index.ancestors(record)

    def _constructed(self, func: ast.expr, scope: _Scope) -> set[RecordClass]:
        """The records a callee builds: ``Cls``, ``Cls.model_construct``, ``cls`` and kin."""
        target = func
        if isinstance(func, ast.Attribute) and func.attr == "model_construct":
            target = func.value
        records = self.index.resolve(target, scope.names)
        if records:
            return records
        owner = _method_scope(scope)
        if owner is None or owner.cls is None or not _builds_own_class(target, owner):
            return set()
        return self.index.class_records(owner.cls, owner.names)


def _declared_value(node: ast.AST, scope: _Scope) -> tuple[ast.expr, ast.expr] | None:
    """``(annotation, value)`` where an annotation declares what a value is.

    An annotated assignment's value, and a returned value under its
    function's return annotation.
    """
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.annotation, node.value
    function = scope.node
    if (
        isinstance(node, ast.Return)
        and node.value is not None
        and isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and function.returns is not None
    ):
        return function.returns, node.value
    return None


def _builds_own_class(target: ast.expr, owner: _Scope) -> bool:
    """``cls`` in a classmethod, ``type(self)`` or ``self.__class__``."""
    self_name = owner.self_name()
    if _is_name(target, self_name):
        return isinstance(owner.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            referenced_name(deco) == "classmethod" for deco in owner.node.decorator_list
        )
    if isinstance(target, ast.Attribute):
        return target.attr == "__class__" and _is_name(target.value, self_name)
    return (
        isinstance(target, ast.Call)
        and _is_name(target.func, "type")
        and len(target.args) == 1
        and _is_name(target.args[0], self_name)
    )


def _scan(
    node: ast.AST,
    scope: _Scope,
    cls: ast.ClassDef | None,
    index: RecordIndex,
    reads: RecordReads,
    writer: _WriteCollector | None,
) -> None:
    """Record every read (and, in production code, every write) under ``node``."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            reads.attrs.add(child.attr)
        reads.attrs.update(_named_attribute_reads(child, scope.names))
        reads.keys.update(_key_reads(child))
        for consumption in _consumptions(child, scope.names):
            reads.whole |= _consumed(consumption, scope, index)
        if writer is not None:
            writer.visit(child, scope)
        if isinstance(child, ast.ClassDef):
            _scan(child, scope, child, index, reads, writer)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            _scan(child, _Scope(child, scope.names, cls, scope), None, index, reads, writer)
        else:
            _scan(child, scope, cls, index, reads, writer)


def _consumed(consumption: _Consumption, scope: _Scope, index: RecordIndex) -> set[RecordClass]:
    """The records one wholesale read covers (see the module docstring)."""
    tied = _Binder(index).tie(consumption.expr, scope)
    if tied is not None:
        matched = {record for record in tied if record.kind in consumption.kinds}
    else:
        matched = {record for record in scope.mentions(index) if record.kind in consumption.kinds}
        if not matched and consumption.specific:
            matched = {
                record
                for record in index.module_mentions(scope.names.path)
                if record.kind in consumption.kinds
            }
    return index.closure(matched) if matched else set()


@dataclass(frozen=True)
class RecordUses:
    """What production code reads and writes of records, and what each test reads.

    ``test_reads`` holds what each part of a test-role file reads, keyed by
    the part's first line: a top-level statement, or, in a top-level class,
    each method and the rest of the class apart. The test-only surface
    (``surface_of_sites``) turns those lines into the tests that read a field.
    """

    reads: RecordReads
    writes: RecordWrites
    test_reads: dict[tuple[Path, int], RecordReads]


def collect_record_uses(ctx: AnalysisContext, index: RecordIndex) -> RecordUses:
    """One walk of every tree, collecting what is read of records and what is written.

    Neither tests' reads nor their writes count as production's. A test that
    builds a record is building a fixture, and a field only a fixture fills
    is not one the program writes; a test that reads a field checks what the
    program wrote, and a field only a test reads is not one the program
    uses. Test reads are kept apart, per test unit, so the finding can name
    them.
    """
    # A template reads by name, an attribute or a key alike (pysmelly.templates).
    reads = RecordReads(attrs=set(ctx.template_attributes), keys=set(ctx.template_attributes))
    writes = RecordWrites()
    writer = _WriteCollector(index, writes)
    test_reads: dict[tuple[Path, int], RecordReads] = {}
    for path, tree in ctx.all_trees.items():
        scope = _Scope(tree, index.names[path], None, None)
        if not ctx.roles.is_test(path):
            reads.keys |= ctx.subscript_string_keys[path]
            _scan(tree, scope, None, index, reads, writer)
            continue
        for line, part in _test_parts(tree):
            single = ast.Module(body=[part], type_ignores=[])
            part_reads = test_reads.setdefault((path, line), RecordReads())
            part_reads.keys |= build_subscript_string_keys({path: single})[path]
            _scan(single, scope, None, index, part_reads, None)
    return RecordUses(reads, writes, test_reads)


def _test_parts(tree: ast.Module) -> Iterator[tuple[int, ast.stmt]]:
    """A test file's top-level statements, a class split into one copy per
    method and one holding the rest of it, each with the line it starts on."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            yield stmt.lineno, stmt
            continue
        methods = [item for item in stmt.body if isinstance(item, _FUNCTION_DEFS)]
        rest = [item for item in stmt.body if not isinstance(item, _FUNCTION_DEFS)]
        yield stmt.lineno, _class_with(stmt, rest)
        for method in methods:
            yield method.lineno, _class_with(stmt, [method])


def _class_with(cls: ast.ClassDef, body: list[ast.stmt]) -> ast.ClassDef:
    """A shallow copy of ``cls`` holding ``body``, so a scan still sees the class around it."""
    part = copy.copy(cls)
    part.body = body
    return part


@dataclass(frozen=True)
class AttributeRead:
    """One production read of ``x.attr``, and the records ``x`` can hold.

    ``holders`` is every record class ``x`` ties to, with its subclasses and
    ancestors (``RecordIndex.closure``); empty when ``x`` ties to something
    that is not a record, and None when nothing ties it.
    """

    file: Path
    node: ast.Attribute
    holders: frozenset[RecordClass] | None


def attribute_reads(
    ctx: AnalysisContext, index: RecordIndex, attrs: frozenset[str]
) -> list[AttributeRead]:
    """Every production load of an attribute named in ``attrs``, tied as far as it can be.

    The receiver is tied the way a wholesale read's argument is (see the
    module docstring), so a check can tell ``block.attrs`` from
    ``provenance.attrs`` when the annotations say which is which.
    """
    found: list[AttributeRead] = []
    binder = _Binder(index)
    for path, tree in source_trees(ctx):
        scope = _Scope(tree, index.names[path], None, None)
        _collect_reads(tree, scope, None, binder, attrs, found)
    return found


def _collect_reads(
    node: ast.AST,
    scope: _Scope,
    cls: ast.ClassDef | None,
    binder: _Binder,
    attrs: frozenset[str],
    found: list[AttributeRead],
) -> None:
    """``attribute_reads`` under ``node``, scoped as ``_scan`` scopes its walk."""
    for child in ast.iter_child_nodes(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.ctx, ast.Load)
            and child.attr in attrs
        ):
            tied = binder.tie(child.value, scope)
            holders = None if tied is None else frozenset(binder.index.closure(tied))
            found.append(AttributeRead(scope.names.path, child, holders))
        if isinstance(child, ast.ClassDef):
            _collect_reads(child, scope, child, binder, attrs, found)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            child_scope = _Scope(child, scope.names, cls, scope)
            _collect_reads(child, child_scope, None, binder, attrs, found)
        else:
            _collect_reads(child, scope, cls, binder, attrs, found)
