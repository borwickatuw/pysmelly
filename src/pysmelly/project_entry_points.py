"""The entry points a project declares in its ``pyproject.toml``, read statically.

``[project.scripts]``, ``[project.gui-scripts]`` and every
``[project.entry-points.<group>]`` table name an object by reference,
``importable.module:object.attr``, which an installer turns into a script or
a plugin registration. This module reads those references from the target
directory's ``pyproject.toml`` (the file pysmelly's own config may live in;
nothing above it is read) and parses each into its module and attribute.
Nothing is imported: the entry-point index (``checks/entry_points.py``)
resolves the names against the analyzed files.

A reference that does not parse is an error, never skipped. An installer
refuses the same table (setuptools validates it against the PyPA entry-point
grammar), so the project cannot be built as declared, and skipping the entry
would leave a declared root out of the index silently. This module imports
only the leaf ``pysmelly.pyproject``, so ``AnalysisContext`` can hold what it
returns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pysmelly.pyproject import PYPROJECT, read_pyproject

# A dotted identifier: the module part and the object part of a reference.
_DOTTED = r"[^\W\d]\w*(?:\.[^\W\d]\w*)*"

# The PyPA object-reference grammar: ``module`` or ``module:object.attr``, with
# whitespace allowed around the colon and an optional, deprecated ``[extras]``.
_REFERENCE_RE = re.compile(
    rf"(?P<module>{_DOTTED})(?:\s*:\s*(?P<attr>{_DOTTED}))?(?:\s*\[[^\[\]]*\])?"
)


class ProjectEntryPointError(Exception):
    """``pyproject.toml`` declares an entry point that cannot be read."""


class DeclaredKind(StrEnum):
    """Which ``[project]`` table declared an entry point."""

    CONSOLE_SCRIPT = "console script"
    GUI_SCRIPT = "gui script"
    ENTRY_POINT = "entry point"


@dataclass(frozen=True)
class DeclaredEntryPoint:
    """One entry point the project's ``pyproject.toml`` declares.

    ``table`` is the TOML table it sits in (``project.scripts``,
    ``project.entry-points.pytest11``), ``name`` its key and ``value`` the
    reference as written. ``module`` is the importable module the reference
    names, and ``attr`` the dotted object inside it, or None when the
    reference names the whole module (``pkg.plugin``), which only an
    ``entry-points`` group may do.
    """

    kind: DeclaredKind
    table: str
    name: str
    value: str
    module: str
    attr: str | None

    @property
    def reason(self) -> str:
        """Where the declaration is, as the ``-v`` listing quotes it."""
        return f'[{self.table}] {self.name} = "{self.value}"'


def _parse_reference(
    kind: DeclaredKind, table: str, name: str, value: object, source: Path
) -> DeclaredEntryPoint:
    if not isinstance(value, str):
        msg = f"{source}: [{table}] {name} must be a string, got {type(value).__name__}"
        raise ProjectEntryPointError(msg)
    match = _REFERENCE_RE.fullmatch(value.strip())
    if match is None:
        msg = (
            f'{source}: [{table}] {name} = "{value}" is not an entry-point reference '
            "(expected importable.module or importable.module:object)"
        )
        raise ProjectEntryPointError(msg)
    attr = match["attr"]
    if attr is None and kind is not DeclaredKind.ENTRY_POINT:
        msg = (
            f'{source}: [{table}] {name} = "{value}" names no function '
            "(a script needs importable.module:function)"
        )
        raise ProjectEntryPointError(msg)
    return DeclaredEntryPoint(kind, table, name, value, match["module"], attr)


def _table(container: dict, key: str, label: str, source: Path) -> dict:
    value = container.get(key, {})
    if not isinstance(value, dict):
        msg = f"{source}: [{label}] must be a table, got {type(value).__name__}"
        raise ProjectEntryPointError(msg)
    return value


def load_project_entry_points(rootdir: Path) -> tuple[DeclaredEntryPoint, ...]:
    """Every entry point ``rootdir``'s ``pyproject.toml`` declares, in file order.

    A directory with no ``pyproject.toml``, or one with no ``[project]``
    entry-point tables, declares none.

    Raises:
        ProjectEntryPointError: ``pyproject.toml`` cannot be parsed, a table
            is not a table, or a value is not a string in the entry-point
            reference grammar (a script's must name a function).
    """
    data = read_pyproject(rootdir, ProjectEntryPointError)
    pyproject = rootdir / PYPROJECT
    project = _table(data, "project", "project", pyproject)
    tables = [
        (kind, f"project.{key}", _table(project, key, f"project.{key}", pyproject))
        for kind, key in (
            (DeclaredKind.CONSOLE_SCRIPT, "scripts"),
            (DeclaredKind.GUI_SCRIPT, "gui-scripts"),
        )
    ]
    groups = _table(project, "entry-points", "project.entry-points", pyproject)
    for group, entries in groups.items():
        label = f"project.entry-points.{group}"
        if not isinstance(entries, dict):
            msg = f"{pyproject}: [{label}] must be a table, got {type(entries).__name__}"
            raise ProjectEntryPointError(msg)
        tables.append((DeclaredKind.ENTRY_POINT, label, entries))
    return tuple(
        _parse_reference(kind, table, name, value, pyproject)
        for kind, table, entries in tables
        for name, value in entries.items()
    )
