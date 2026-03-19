"""Stdlib alternatives check — detect stdlib usage where well-known libraries are better."""

import ast
import tomllib
from importlib.resources import files
from pathlib import Path

from pysmelly.registry import Finding, Severity, check


def _load_catalog() -> list[dict]:
    """Load the pattern catalog from the shipped TOML file."""
    catalog_path = files("pysmelly").joinpath("catalog.toml")
    data = tomllib.loads(catalog_path.read_text())
    return data["patterns"]


def _collect_imports(all_trees: dict[Path, ast.Module]) -> dict[Path, list[tuple[str, int]]]:
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


def _matches_catalog_import(recorded: str, catalog_import: str) -> bool:
    """Check if a recorded import matches a catalog entry.

    A catalog entry "urllib.request" matches any recorded module that equals it
    or starts with "urllib.request.".
    """
    return recorded == catalog_import or recorded.startswith(catalog_import + ".")


def _codebase_imports_module(all_imports: dict[Path, list[tuple[str, int]]], module: str) -> bool:
    """Check if any file in the codebase imports the given module."""
    for file_imports in all_imports.values():
        for recorded, _ in file_imports:
            if _matches_catalog_import(recorded, module):
                return True
    return False


def _format_locations(matches: list[tuple[Path, int]], max_shown: int = 3) -> str:
    """Format file:line locations for display."""
    parts = [f"{m[0]}:{m[1]}" for m in matches[:max_shown]]
    if len(matches) > max_shown:
        parts.append("...")
    return ", ".join(parts)


@check(
    "stdlib-alternatives",
    severity=Severity.LOW,
    description="stdlib modules where well-known third-party libraries are better",
)
def check_stdlib_alternatives(all_trees: dict[Path, ast.Module], verbose: bool) -> list[Finding]:
    """Detect stdlib usage patterns where well-known libraries are better."""
    catalog = _load_catalog()
    all_imports = _collect_imports(all_trees)
    findings = []

    for pattern in catalog:
        # Collect all (file, line) matches for this pattern's imports
        matches: list[tuple[Path, int]] = []
        for filepath in sorted(all_imports):
            for recorded, lineno in all_imports[filepath]:
                for catalog_import in pattern["imports"]:
                    if _matches_catalog_import(recorded, catalog_import):
                        matches.append((filepath, lineno))
                        break  # one match per import line is enough

        if not matches:
            continue

        # Check condition if present
        condition = pattern.get("condition")
        if condition and condition.startswith("also-imports:"):
            required_module = condition[len("also-imports:") :]
            if not _codebase_imports_module(all_imports, required_module):
                continue

        # Deduplicate by file — keep first occurrence per file
        seen_files: set[Path] = set()
        unique_matches: list[tuple[Path, int]] = []
        for filepath, lineno in matches:
            if filepath not in seen_files:
                seen_files.add(filepath)
                unique_matches.append((filepath, lineno))

        # Build message
        import_name = pattern["imports"][0]
        suggest = pattern["suggest"]
        file_count = len(unique_matches)

        if file_count == 1:
            loc = _format_locations(unique_matches)
            message = f"{import_name} imported ({loc}) — consider {suggest}"
        else:
            loc = _format_locations(unique_matches)
            message = f"{import_name} used in {file_count} files ({loc}) — consider {suggest}"

        # Attach finding to first file alphabetically
        first_file, first_line = unique_matches[0]
        findings.append(
            Finding(
                file=str(first_file),
                line=first_line,
                check="stdlib-alternatives",
                message=message,
                severity=Severity.LOW,
            )
        )

    return findings
