"""The import catalog: its schema, the named conditions an entry may cite, the
shipped entries, and a project's own entries from config.

A catalog holds two arrays. ``patterns`` drive ``stdlib-alternatives``:
imports that have a better replacement. ``pairs`` drive
``dual-stack-dependency``: two libraries that do one job, both in use.

One validator, ``_parse_catalog``, reads both the shipped ``catalog.toml`` and
the ``catalog`` table of a project's config, so an entry means the same thing
wherever it is written.
"""

from __future__ import annotations

import ast
import functools
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

SHIPPED_SOURCE = "pysmelly catalog.toml"

ALSO_IMPORTS = "also-imports:"


class CatalogError(Exception):
    """Raised for a catalog document that does not match the schema."""


@dataclass(frozen=True)
class CatalogPattern:
    """One catalog entry: imports to report and what to use instead.

    ``also_imports`` is the module named by an ``also-imports:`` condition:
    the entry fires only when the codebase imports it somewhere. ``condition_fn``
    names a ``CONDITION_FNS`` predicate the matched files must satisfy. An
    entry's ``description`` documents it in the TOML and is validated, but
    nothing prints it, so it is not kept here.
    """

    name: str
    imports: tuple[str, ...]
    suggest: str
    exclude_imports: tuple[str, ...]
    also_imports: str | None
    condition_fn: str | None


@dataclass(frozen=True)
class PairSide:
    """One library of a substitutable pair: its name in messages, and its modules.

    A module covers its submodules, as a pattern's ``imports`` do.
    ``no_substitute`` names what the other side has no replacement for
    (``os.path.normpath``: pathlib has no lexical normalization); a use of it
    is not a use of the side, since it cannot move.
    """

    label: str
    imports: tuple[str, ...]
    no_substitute: tuple[str, ...] = ()

    def covers(self, dotted: str) -> bool:
        """Whether a use of ``dotted`` is a use of this side that could move."""
        return any(covers(module, dotted) for module in self.imports) and not any(
            covers(name, dotted) for name in self.no_substitute
        )


@dataclass(frozen=True)
class CatalogPair:
    """Two libraries that do the same job (``requests`` and ``httpx``).

    ``dual-stack-dependency`` reports the pair when production code uses both.
    The two ``sides`` are unordered: which one is the minority is measured,
    not declared. ``description`` names the job in the finding.
    """

    name: str
    description: str
    sides: tuple[PairSide, PairSide]


@dataclass(frozen=True)
class Catalog:
    """A validated catalog document: its patterns and its pairs, in order."""

    patterns: tuple[CatalogPattern, ...] = ()
    pairs: tuple[CatalogPair, ...] = ()


# A run with no project catalog entries.
EMPTY_CATALOG = Catalog()


def _count_method_calls(tree: ast.Module, method_name: str) -> int:
    """Count calls to a specific method name (e.g. parser.add_argument)."""
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method_name
        ):
            count += 1
    return count


def _has_method_call(tree: ast.Module, method_name: str) -> bool:
    """Check if a method name is called anywhere in the tree."""
    return _count_method_calls(tree, method_name) > 0


# An argparse parser with this many arguments is complex enough for click.
COMPLEX_ARGPARSE_ARGUMENTS = 5


def _is_complex_argparse(all_trees: dict[Path, ast.Module], matched_files: set[Path]) -> bool:
    """True if any matched file has subcommands or 5+ arguments."""
    for filepath in matched_files:
        tree = all_trees[filepath]
        if _has_method_call(tree, "add_subparsers"):
            return True
        if _has_method_call(tree, "add_mutually_exclusive_group"):
            return True
        if _count_method_calls(tree, "add_argument") >= COMPLEX_ARGPARSE_ARGUMENTS:
            return True
    return False


# INI files whose format another tool dictates. Code that reads one has no
# format to choose, so tomllib is no alternative for it.
TOOL_INI_FILES = frozenset(
    {
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        ".pytest.ini",
        "mypy.ini",
        ".flake8",
        ".coveragerc",
        ".pylintrc",
        "pylintrc",
        "alembic.ini",
    }
)


def _names_tool_ini_file(tree: ast.Module) -> bool:
    """Whether the module names a ``TOOL_INI_FILES`` file in a string constant."""
    return any(
        isinstance(node, ast.Constant) and node.value in TOOL_INI_FILES for node in ast.walk(tree)
    )


def _chooses_ini_format(all_trees: dict[Path, ast.Module], matched_files: set[Path]) -> bool:
    """True if any matched file reads INI other than a tool's own INI files."""
    return any(not _names_tool_ini_file(all_trees[filepath]) for filepath in matched_files)


# The predicates a catalog entry's ``condition_fn`` may name. Each receives
# (all_trees, matched_files) and returns True if the finding should fire.
CONDITION_FNS: dict[str, Callable[[dict[Path, ast.Module], set[Path]], bool]] = {
    "complex_argparse": _is_complex_argparse,
    "chooses_ini_format": _chooses_ini_format,
}

# The catalog document's keys, and each entry's fields.
DOCUMENT_KEYS = frozenset({"patterns", "pairs"})
REQUIRED_FIELDS = frozenset({"name", "description", "imports", "suggest"})
OPTIONAL_FIELDS = frozenset({"exclude_imports", "condition", "condition_fn"})
ENTRY_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
# A pair's fields, all required, and each of its two sides' fields.
PAIR_FIELDS = frozenset({"name", "description", "sides"})
SIDE_REQUIRED_FIELDS = frozenset({"label", "imports"})
SIDE_FIELDS = SIDE_REQUIRED_FIELDS | {"no_substitute"}
# A pair is two libraries: a third makes "the minority" ambiguous.
PAIR_SIDES = 2


def _nonempty_string(value: object, where: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{where}: '{field}' must be a non-empty string, got {value!r}"
        raise CatalogError(msg)
    return value


def _string_list(value: object, where: str, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        msg = f"{where}: '{field}' must be a non-empty list of non-empty strings, got {value!r}"
        raise CatalogError(msg)
    return tuple(value)


def _also_imports(condition: object, where: str) -> str:
    """The module an ``also-imports:<module>`` condition names."""
    text = _nonempty_string(condition, where, "condition")
    module = text.removeprefix(ALSO_IMPORTS)
    if module == text or not module:
        msg = f"{where}: 'condition' must be '{ALSO_IMPORTS}<module>', got {condition!r}"
        raise CatalogError(msg)
    return module


def _condition_fn(name: object, where: str) -> str:
    text = _nonempty_string(name, where, "condition_fn")
    if text not in CONDITION_FNS:
        msg = (
            f"{where}: unknown condition_fn {text!r}. "
            f"Valid values: {', '.join(sorted(CONDITION_FNS))}"
        )
        raise CatalogError(msg)
    return text


def _check_fields(table: dict, where: str, valid: frozenset[str], required: frozenset[str]) -> None:
    unknown = set(table) - valid
    if unknown:
        msg = (
            f"{where}: unknown field(s): {', '.join(sorted(unknown))}. "
            f"Valid fields: {', '.join(sorted(valid))}"
        )
        raise CatalogError(msg)
    missing = required - set(table)
    if missing:
        msg = f"{where}: missing required field(s): {', '.join(sorted(missing))}"
        raise CatalogError(msg)


def _parse_entry(entry: object, where: str) -> CatalogPattern:
    if not isinstance(entry, dict):
        msg = f"{where}: must be a table, got {type(entry).__name__}"
        raise CatalogError(msg)
    _check_fields(entry, where, ENTRY_FIELDS, REQUIRED_FIELDS)
    _nonempty_string(entry["description"], where, "description")
    return CatalogPattern(
        name=_nonempty_string(entry["name"], where, "name"),
        imports=_string_list(entry["imports"], where, "imports"),
        suggest=_nonempty_string(entry["suggest"], where, "suggest"),
        exclude_imports=(
            _string_list(entry["exclude_imports"], where, "exclude_imports")
            if "exclude_imports" in entry
            else ()
        ),
        also_imports=_also_imports(entry["condition"], where) if "condition" in entry else None,
        condition_fn=(
            _condition_fn(entry["condition_fn"], where) if "condition_fn" in entry else None
        ),
    )


def _parse_side(side: object, where: str) -> PairSide:
    if not isinstance(side, dict):
        msg = f"{where}: must be a table, got {type(side).__name__}"
        raise CatalogError(msg)
    _check_fields(side, where, SIDE_FIELDS, SIDE_REQUIRED_FIELDS)
    imports = _string_list(side["imports"], where, "imports")
    no_substitute = (
        _string_list(side["no_substitute"], where, "no_substitute")
        if "no_substitute" in side
        else ()
    )
    for name in no_substitute:
        if not any(covers(module, name) and module != name for module in imports):
            msg = (
                f"{where}: no_substitute {name!r} is not a name under the side's "
                f"imports ({', '.join(imports)})"
            )
            raise CatalogError(msg)
    return PairSide(
        label=_nonempty_string(side["label"], where, "label"),
        imports=imports,
        no_substitute=no_substitute,
    )


def covers(module: str, other: str) -> bool:
    """Whether ``other`` is ``module`` or a name under it."""
    return other == module or other.startswith(module + ".")


def _parse_pair(entry: object, where: str) -> CatalogPair:
    if not isinstance(entry, dict):
        msg = f"{where}: must be a table, got {type(entry).__name__}"
        raise CatalogError(msg)
    _check_fields(entry, where, PAIR_FIELDS, PAIR_FIELDS)
    sides = entry["sides"]
    if not isinstance(sides, list) or len(sides) != PAIR_SIDES:
        msg = f"{where}: 'sides' must be an array of exactly two tables, got {sides!r}"
        raise CatalogError(msg)
    first, second = (_parse_side(side, f"{where} sides[{i}]") for i, side in enumerate(sides))
    if first.label == second.label:
        msg = f"{where}: both sides have the label {first.label!r}"
        raise CatalogError(msg)
    for module in first.imports:
        for other in second.imports:
            if covers(module, other) or covers(other, module):
                msg = (
                    f"{where}: sides overlap ({module!r} and {other!r}); "
                    f"a use must belong to one side only"
                )
                raise CatalogError(msg)
    return CatalogPair(
        name=_nonempty_string(entry["name"], where, "name"),
        description=_nonempty_string(entry["description"], where, "description"),
        sides=(first, second),
    )


def _parse_array[T](
    document: dict, key: str, source: str, parse: Callable[[object, str], T]
) -> tuple[T, ...]:
    """The array ``document[key]`` parsed entry by entry, or () when it is absent."""
    if key not in document:
        return ()
    entries = document[key]
    if not isinstance(entries, list):
        msg = f"{source}: '{key}' must be an array of tables, got {type(entries).__name__}"
        raise CatalogError(msg)
    return tuple(parse(entry, f"{source} {key}[{i}]") for i, entry in enumerate(entries))


def _refuse_duplicate_names(names: list[str], kind: str, source: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            msg = f"{source}: duplicate {kind} name {name!r}"
            raise CatalogError(msg)
        seen.add(name)


def _parse_catalog(document: object, source: str) -> Catalog:
    """Validate a catalog document and return its patterns and pairs, in order.

    ``document`` is a table holding a ``patterns`` array, a ``pairs`` array,
    or both: the whole of ``catalog.toml``, or a config file's ``catalog``
    table. ``source`` names it in error messages.

    Raises:
        CatalogError: on an unknown key or field, a missing or mistyped field,
            an unknown condition, a pair whose sides are not two distinct and
            disjoint libraries, two entries of one kind sharing a name, or a
            document with neither array.
    """
    if not isinstance(document, dict):
        msg = f"{source}: must be a table, got {type(document).__name__}"
        raise CatalogError(msg)
    unknown = set(document) - DOCUMENT_KEYS
    if unknown:
        msg = (
            f"{source}: unknown key(s): {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(DOCUMENT_KEYS))}"
        )
        raise CatalogError(msg)
    if not DOCUMENT_KEYS & set(document):
        msg = f"{source}: needs a 'patterns' or a 'pairs' array"
        raise CatalogError(msg)
    catalog = Catalog(
        patterns=_parse_array(document, "patterns", source, _parse_entry),
        pairs=_parse_array(document, "pairs", source, _parse_pair),
    )
    _refuse_duplicate_names([p.name for p in catalog.patterns], "pattern", source)
    _refuse_duplicate_names([p.name for p in catalog.pairs], "pair", source)
    return catalog


@functools.cache
def shipped_catalog() -> Catalog:
    """The patterns and pairs pysmelly ships in ``catalog.toml``, validated."""
    text = files("pysmelly").joinpath("catalog.toml").read_text(encoding="utf-8")
    return _parse_catalog(tomllib.loads(text), SHIPPED_SOURCE)


def _side_modules(pair: CatalogPair) -> frozenset[frozenset[str]]:
    """The pair's two sides as module sets, so their order does not matter."""
    return frozenset(frozenset(side.imports) for side in pair.sides)


def parse_user_catalog(document: object, source: str) -> Catalog:
    """Validate a project's catalog entries; they add to the shipped ones.

    Raises:
        CatalogError: as ``_parse_catalog`` does, and when an entry shadows a
            shipped one: a pattern or pair taking a shipped name, or a pair
            whose two sides are a shipped pair's. A project adds entries; it
            never replaces or overrides pysmelly's own.
    """
    catalog = _parse_catalog(document, source)
    shipped = shipped_catalog()
    shipped_names = {pattern.name for pattern in shipped.patterns}
    for pattern in catalog.patterns:
        if pattern.name in shipped_names:
            msg = (
                f"{source}: pattern name {pattern.name!r} is already a shipped catalog "
                f"entry. Project entries add patterns and cannot shadow shipped ones; "
                f"choose another name"
            )
            raise CatalogError(msg)
    shipped_pairs = {pair.name: pair for pair in shipped.pairs}
    shipped_sides = {_side_modules(pair): pair.name for pair in shipped.pairs}
    for pair in catalog.pairs:
        if pair.name in shipped_pairs:
            msg = (
                f"{source}: pair name {pair.name!r} is already a shipped catalog pair. "
                f"Project entries add pairs and cannot shadow shipped ones; "
                f"choose another name"
            )
            raise CatalogError(msg)
        if _side_modules(pair) in shipped_sides:
            msg = (
                f"{source}: pair {pair.name!r} has the same sides as the shipped pair "
                f"{shipped_sides[_side_modules(pair)]!r}, so it would shadow it. "
                f"Project pairs add new pairs only"
            )
            raise CatalogError(msg)
    return catalog
