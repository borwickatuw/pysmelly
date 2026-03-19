"""File discovery — find Python files to analyze."""

import ast
import re
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


def get_git_root(cwd: Path) -> Path | None:
    """Get the git repository root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_changed_lines(ref: str, cwd: Path) -> dict[str, set[int]]:
    """Get changed lines from git diff, keyed by repo-root-relative paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", ref, "--"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}

    changed: dict[str, set[int]] = {}
    current_file = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file is not None:
            # Parse @@ -old_start[,old_count] +new_start[,new_count] @@
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if count > 0:
                    changed.setdefault(current_file, set()).update(range(start, start + count))
    return changed
