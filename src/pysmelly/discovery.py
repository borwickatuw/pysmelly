"""File discovery — find Python files to analyze."""

import ast
import subprocess
from pathlib import Path


def get_python_files(root: Path) -> list[Path]:
    """Get all Python files under root, respecting .gitignore if in a git repo."""
    # Try git ls-files first (respects .gitignore)
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            capture_output=True,
            text=True,
            cwd=root,
            check=True,
        )
        files = [root / line for line in result.stdout.strip().splitlines() if line]
        return sorted(f for f in files if f.exists())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Fallback: walk the directory, skipping common non-source dirs
    skip_dirs = {".venv", "venv", ".git", "__pycache__", "node_modules", ".tox", ".mypy_cache"}
    files = []
    for path in root.rglob("*.py"):
        if not any(part in skip_dirs for part in path.parts):
            files.append(path)
    return sorted(files)


def parse_file(path: Path) -> ast.Module | None:
    """Parse a Python file, returning AST or None on error."""
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return None
