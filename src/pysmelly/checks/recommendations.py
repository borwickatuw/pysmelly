"""Stdlib alternatives check — detect stdlib usage where well-known libraries are better."""

from __future__ import annotations

import ast
from functools import partial
from pathlib import Path

from pysmelly.catalog import CONDITION_FNS, covers, shipped_catalog
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding


def _collect_imports(
    all_trees: dict[Path, ast.Module],
) -> dict[Path, list[tuple[str, int]]]:
    """Collect all imports per file as (module_path, line_number) tuples."""
    result: dict[Path, list[tuple[str, int]]] = {}

    for filepath, tree in all_trees.items():
        imports: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((alias.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # Record the module itself
                imports.append((module, node.lineno))
                # "from os import path" also means "os.path" is used
                for alias in node.names:
                    imports.append((f"{module}.{alias.name}", node.lineno))
        if imports:
            result[filepath] = imports

    return result


def _codebase_imports_module(all_imports: dict[Path, list[tuple[str, int]]], module: str) -> bool:
    """Check if any file in the codebase imports the given module."""
    for file_imports in all_imports.values():
        for recorded, _ in file_imports:
            if covers(module, recorded):
                return True
    return False


# How many file:line locations a stdlib-alternatives message shows.
_MAX_SHOWN_LOCATIONS = 3


def _format_locations(sites: list[Site]) -> str:
    """Format file:line locations for display."""
    parts = [f"{site.file}:{site.line}" for site in sites[:_MAX_SHOWN_LOCATIONS]]
    if len(sites) > _MAX_SHOWN_LOCATIONS:
        parts.append("...")
    return ", ".join(parts)


def _stdlib_message(
    import_name: str, suggest: str, sites: list[Site], indices: tuple[int, ...]
) -> str:
    """The finding's message over the sites at ``indices``."""
    shown = [sites[index] for index in indices]
    loc = _format_locations(shown)
    if len(shown) == 1:
        return f"{import_name} imported ({loc}) — alternative: {suggest}"
    return f"{import_name} used in {len(shown)} files ({loc}) — alternative: {suggest}"


@check(
    "stdlib-alternatives",
    severity=Severity.LOW,
    description="stdlib modules where well-known third-party libraries are better",
    advice=Advice(
        fix="Move every site listed to the alternative named, then drop the old import.",
        dismiss=(
            "Dismiss only if the project must not add the dependency (a stdlib-only tool or"
            " a constrained runtime), as its packaging declares."
        ),
    ),
)
def check_stdlib_alternatives(ctx: AnalysisContext) -> list[Finding]:
    """Detect stdlib usage patterns where well-known libraries are better."""
    all_imports = _collect_imports(ctx.all_trees)
    findings = []

    for pattern in (*shipped_catalog().patterns, *ctx.user_catalog.patterns):
        excludes = pattern.exclude_imports

        # Collect all (file, line) matches for this pattern's imports
        matches: list[tuple[Path, int]] = []
        for filepath in sorted(all_imports):
            for recorded, lineno in all_imports[filepath]:
                # Skip excluded imports (e.g., unittest.mock is not unittest)
                if any(covers(exc, recorded) for exc in excludes):
                    continue
                for catalog_import in pattern.imports:
                    if covers(catalog_import, recorded):
                        matches.append((filepath, lineno))
                        break  # one match per import line is enough

        if not matches:
            continue

        # An also-imports: condition — the better thing is already in use
        if pattern.also_imports is not None and not _codebase_imports_module(
            all_imports, pattern.also_imports
        ):
            continue

        if pattern.condition_fn is not None:
            matched_files = {filepath for filepath, _ in matches}
            if not CONDITION_FNS[pattern.condition_fn](ctx.all_trees, matched_files):
                continue

        # Deduplicate by file — keep first occurrence per file
        seen_files: set[Path] = set()
        sites: list[Site] = []
        for filepath, lineno in matches:
            if filepath not in seen_files:
                seen_files.add(filepath)
                sites.append(Site(str(filepath), lineno))

        # One finding per pattern, anchored on the first file alphabetically;
        # an ignore on one file's import removes only that file's site.
        findings.append(
            multi_site_finding(
                "stdlib-alternatives",
                Severity.LOW,
                sites,
                partial(_stdlib_message, pattern.imports[0], pattern.suggest, sites),
            )
        )

    return findings
