"""Shared analysis context — caches expensive indices across checks."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pysmelly.checks.callers import CallerValueIndex
    from pysmelly.checks.entry_points import EntryPointIndex
    from pysmelly.checks.helpers import ModuleImport, NameRebinding, RaiseSite, ReferenceIndices
    from pysmelly.checks.module_graph import ModuleGraph
    from pysmelly.checks.near_duplicates import NearDuplicatePair
    from pysmelly.checks.reachability import UnitGraph
    from pysmelly.git_history import GitHistory
    from pysmelly.roles import TestRoles

from pysmelly.catalog import EMPTY_CATALOG, Catalog
from pysmelly.project_entry_points import DeclaredEntryPoint
from pysmelly.pytest_config import NO_PYTEST_CONFIG, PytestConfig

DEFAULT_COMMIT_MESSAGES = "auto"


@dataclass(frozen=True)
class GitSettings:
    """What a git-history run reads history with: the repository and how to read it."""

    root: Path
    window: str = "6m"
    commit_messages: str = DEFAULT_COMMIT_MESSAGES
    expected_coupling: list[list[str]] = field(default_factory=list)


class AnalysisContext:
    """Holds parsed trees and lazily-computed indices shared across checks.

    Expensive indices (function_index, call_index, reference indices) are
    computed once on first access instead of being rebuilt per-check.
    """

    def __init__(
        self,
        all_trees: dict[Path, ast.Module],
        verbose: bool,
        git: GitSettings | None = None,
        pytest_config: PytestConfig = NO_PYTEST_CONFIG,
        user_catalog: Catalog = EMPTY_CATALOG,
        declared_entry_points: tuple[DeclaredEntryPoint, ...] = (),
    ) -> None:
        self.all_trees = all_trees
        self.verbose = verbose
        self.git = git
        self.expected_coupling = git.expected_coupling if git is not None else []
        self.pytest_config = pytest_config
        # The project's own catalog patterns and pairs, from its config's
        # ``catalog`` table; they run after the shipped ones.
        self.user_catalog = user_catalog
        # What the target directory's pyproject.toml declares in [project.scripts],
        # [project.gui-scripts] and [project.entry-points.*].
        self.declared_entry_points = declared_entry_points
        # Every name the project's templates read after a dot
        # (``{{ cut.unread_pages }}``; ``pysmelly.templates``). A template is
        # production code the AST never sees. The CLI sets it after reading
        # the files; a context built from trees alone has no templates.
        self.template_attributes: frozenset[str] = frozenset()
        # The analyzed files whose line 1 is a ``#!`` line, which the AST does
        # not keep. The CLI sets it from ``discovery.parse_file``; a context
        # built from trees alone has none.
        self.shebang_files: frozenset[Path] = frozenset()
        self._entry_points: EntryPointIndex | None = None
        self._unit_graph: UnitGraph | None = None
        self._function_index: dict[str, list[dict]] | None = None
        self._call_index: dict[str, list[dict]] | None = None
        self._references: ReferenceIndices | None = None
        self._package_dirs: frozenset[tuple[str, ...]] | None = None
        self._module_rebindings: dict[Path, dict[str, list[NameRebinding]]] | None = None
        self._module_imports: dict[Path, list[ModuleImport]] | None = None
        self._subscript_string_keys: dict[Path, frozenset[str]] | None = None
        self._raise_sites: dict[str, list[RaiseSite]] | None = None
        self._caller_values: CallerValueIndex | None = None
        self._roles: TestRoles | None = None
        self._module_graph: ModuleGraph | None = None
        self._near_duplicate_pairs: list[NearDuplicatePair] | None = None
        self._parent_maps: dict[int, dict[ast.AST, ast.AST]] = {}
        self._git_history: GitHistory | None = None
        self._git_history_computed: bool = False

    @property
    def function_index(self) -> dict[str, list[dict]]:
        if self._function_index is None:
            from pysmelly.checks.helpers import build_function_index

            self._function_index = build_function_index(self.all_trees, self.roles.files)
        return self._function_index

    @property
    def call_index(self) -> dict[str, list[dict]]:
        if self._call_index is None:
            from pysmelly.checks.helpers import build_call_index

            self._call_index = build_call_index(self.all_trees)
        return self._call_index

    @property
    def references(self) -> ReferenceIndices:
        """The import, value, dotted-string and decorator views, built in one pass."""
        if self._references is None:
            from pysmelly.checks.helpers import build_reference_indices

            self._references = build_reference_indices(self.all_trees)
        return self._references

    @property
    def package_dirs(self) -> frozenset[tuple[str, ...]]:
        """Directories, as split path parts, that hold an analyzed ``__init__.py``."""
        if self._package_dirs is None:
            from pysmelly.checks.helpers import package_dirs

            self._package_dirs = package_dirs(self.all_trees)
        return self._package_dirs

    @property
    def module_rebindings(self) -> dict[Path, dict[str, list[NameRebinding]]]:
        """Per file, every site that rebinds or deletes a module-level name, by name."""
        if self._module_rebindings is None:
            from pysmelly.checks.helpers import build_module_rebindings

            self._module_rebindings = build_module_rebindings(self.all_trees)
        return self._module_rebindings

    @property
    def module_imports(self) -> dict[Path, list[ModuleImport]]:
        """Per file, every name its import statements bind, in walk order."""
        if self._module_imports is None:
            from pysmelly.checks.helpers import build_module_imports

            self._module_imports = build_module_imports(self.all_trees, self.package_dirs)
        return self._module_imports

    @property
    def subscript_string_keys(self) -> dict[Path, frozenset[str]]:
        """Per file, every string constant read as a subscript key (``x["key"]``)."""
        if self._subscript_string_keys is None:
            from pysmelly.checks.helpers import build_subscript_string_keys

            self._subscript_string_keys = build_subscript_string_keys(self.all_trees)
        return self._subscript_string_keys

    @property
    def near_duplicate_pairs(self) -> list[NearDuplicatePair]:
        """Similar-but-not-identical code across files, with aligned line ranges."""
        if self._near_duplicate_pairs is None:
            from pysmelly.checks.near_duplicates import find_near_duplicate_pairs

            self._near_duplicate_pairs = find_near_duplicate_pairs(self)
        return self._near_duplicate_pairs

    @property
    def raise_sites(self) -> dict[str, list[RaiseSite]]:
        """Every ``raise`` site across all files, keyed by the name it raises."""
        if self._raise_sites is None:
            from pysmelly.checks.helpers import build_raise_index

            self._raise_sites = build_raise_index(self.all_trees)
        return self._raise_sites

    @property
    def caller_values(self) -> CallerValueIndex:
        """Every function's call sites and what each passes per parameter, split by role."""
        if self._caller_values is None:
            from pysmelly.checks.callers import build_caller_values

            self._caller_values = build_caller_values(self)
        return self._caller_values

    @property
    def module_graph(self) -> ModuleGraph:
        """Each module's imports, bound names and re-export origins (``checks/module_graph.py``)."""
        if self._module_graph is None:
            from pysmelly.checks.module_graph import build_module_graph

            self._module_graph = build_module_graph(
                self.all_trees,
                self.package_dirs,
                self.pytest_config.import_roots(),
                self.module_imports,
            )
        return self._module_graph

    @property
    def import_graph(self) -> dict[Path, frozenset[Path]]:
        """Per file, the other analyzed files its imports load (``module_graph.loads``)."""
        return self.module_graph.loads

    @property
    def entry_points(self) -> EntryPointIndex:
        """Every root the analyzed code is run from, with why (``checks/entry_points.py``)."""
        if self._entry_points is None:
            from pysmelly.checks.entry_points import build_entry_point_index

            self._entry_points = build_entry_point_index(self, self.declared_entry_points)
        return self._entry_points

    @property
    def unit_graph(self) -> UnitGraph:
        """Production top-level units and what each references (``checks/reachability.py``)."""
        if self._unit_graph is None:
            from pysmelly.checks.reachability import UnitGraph

            self._unit_graph = UnitGraph(self)
        return self._unit_graph

    @property
    def roles(self) -> TestRoles:
        """The test role of every file and definition, and of any other path (``roles.py``)."""
        if self._roles is None:
            from pysmelly.roles import TestRoles

            self._roles = TestRoles(self)
        return self._roles

    def parent_map(self, tree: ast.Module) -> dict[ast.AST, ast.AST]:
        key = id(tree)
        if key not in self._parent_maps:
            from pysmelly.checks.helpers import build_parent_map

            self._parent_maps[key] = build_parent_map(tree)
        return self._parent_maps[key]

    @property
    def git_history(self) -> GitHistory | None:
        """Lazy-built GitHistory, or None when the run has no git settings."""
        if not self._git_history_computed:
            if self.git is not None:
                from pysmelly.git_history import GitHistory

                git = self.git
                self._git_history = GitHistory(git.root, git.window, git.commit_messages)
            self._git_history_computed = True
        return self._git_history
