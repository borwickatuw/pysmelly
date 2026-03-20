"""Shared analysis context — caches expensive indices across checks."""

from __future__ import annotations

import ast
from pathlib import Path


class AnalysisContext:
    """Holds parsed trees and lazily-computed indices shared across checks.

    Expensive indices (function_index, call_index, reference indices) are
    computed once on first access instead of being rebuilt per-check.
    """

    def __init__(self, all_trees: dict[Path, ast.Module], verbose: bool) -> None:
        self.all_trees = all_trees
        self.verbose = verbose
        self._function_index: dict[str, list[dict]] | None = None
        self._call_index: dict[str, list[dict]] | None = None
        self._import_index: dict[str, set[str]] | None = None
        self._value_references: set[str] | None = None
        self._dotted_string_suffixes: set[str] | None = None
        self._decorator_names: set[str] | None = None
        self._parent_maps: dict[int, dict[ast.AST, ast.AST]] = {}

    @property
    def function_index(self) -> dict[str, list[dict]]:
        if self._function_index is None:
            from pysmelly.checks.helpers import build_function_index

            self._function_index = build_function_index(self.all_trees)
        return self._function_index

    @property
    def call_index(self) -> dict[str, list[dict]]:
        if self._call_index is None:
            from pysmelly.checks.helpers import build_call_index

            self._call_index = build_call_index(self.all_trees)
        return self._call_index

    def _build_reference_indices(self) -> None:
        """Single-pass builder for import, value, dotted-string, and decorator indices."""
        from pysmelly.checks.helpers import build_reference_indices

        indices = build_reference_indices(self.all_trees)
        self._import_index = indices.import_index
        self._value_references = indices.value_references
        self._dotted_string_suffixes = indices.dotted_string_suffixes
        self._decorator_names = indices.decorator_names

    @property
    def import_index(self) -> dict[str, set[str]]:
        if self._import_index is None:
            self._build_reference_indices()
        return self._import_index  # type: ignore[return-value]

    @property
    def value_references(self) -> set[str]:
        if self._value_references is None:
            self._build_reference_indices()
        return self._value_references  # type: ignore[return-value]

    @property
    def dotted_string_suffixes(self) -> set[str]:
        if self._dotted_string_suffixes is None:
            self._build_reference_indices()
        return self._dotted_string_suffixes  # type: ignore[return-value]

    @property
    def decorator_names(self) -> set[str]:
        if self._decorator_names is None:
            self._build_reference_indices()
        return self._decorator_names  # type: ignore[return-value]

    def parent_map(self, tree: ast.Module) -> dict[ast.AST, ast.AST]:
        key = id(tree)
        if key not in self._parent_maps:
            from pysmelly.checks.helpers import build_parent_map

            self._parent_maps[key] = build_parent_map(tree)
        return self._parent_maps[key]
