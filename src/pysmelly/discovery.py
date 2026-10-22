"""File discovery — find Python files to analyze."""

import ast
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A first line starting ``#!`` makes a file runnable by path: the kernel reads
# only the first two bytes, so it counts on line 1 only.
SHEBANG = "#!"


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command with UTF-8 decoding and path quoting disabled.

    `core.quotepath=off` makes git print non-ASCII filenames as raw UTF-8
    instead of quoted octal escapes, which would otherwise name files that do
    not exist. Output is decoded as UTF-8 instead of the host locale, so
    filenames, author names and commit subjects read the same everywhere.

    The bytes are decoded as they are, with no newline translation: text
    mode would turn a lone ``\\r`` (in a commit subject, an author name or a
    source line) into ``\\n`` and split one record in two. Git ends its lines
    with ``\\n`` alone, so readers split on that and nothing else.

    Raises:
        subprocess.CalledProcessError: git exited non-zero; its ``stdout``
            and ``stderr`` are decoded text.
        UnicodeDecodeError: git printed bytes that are not UTF-8.
    """
    command = ["git", "-c", "core.quotepath=off", *args]
    result = subprocess.run(command, capture_output=True, cwd=cwd, check=False)
    stdout = result.stdout.decode("utf-8")
    stderr = result.stderr.decode("utf-8")
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, result.returncode, stdout, stderr)


# Directories the walk outside git skips: environments, caches, VCS data.
_SKIP_DIRS = frozenset(
    {
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        "node_modules",
        ".tox",
        ".mypy_cache",
    }
)

# The directory name Django's app loader and Flask/Jinja look templates up in.
TEMPLATE_DIR = "templates"


class GitListError(Exception):
    """git is installed and the root is in a repository, but `git ls-files` failed."""


def git_lists(root: Path) -> bool:
    """Whether git lists the files under ``root``, rather than discovery walking it.

    It does when git is installed and ``root`` or a parent holds a ``.git``
    entry (a repository's directory, or a worktree's file). A snapshot such
    as a ``git archive`` export has neither, so it is walked. The test reads
    the filesystem, not git's messages, so a repository git refuses to read
    is not mistaken for no repository.
    """
    resolved = root.resolve()
    return shutil.which("git") is not None and any(
        (d / ".git").exists() for d in (resolved, *resolved.parents)
    )


def walk_note(root: Path) -> str:
    """The one stderr line a run prints for a root git does not list (``git_lists``)."""
    if shutil.which("git") is None:
        where = f"git is not installed; the files under {root} are found"
    else:
        where = f"{root} is not in a git repository; its files are found"
    return (
        f"pysmelly: {where} by walking the tree, so .gitignore does not apply "
        "and no finding carries a provenance line"
    )


def _list_files(root: Path, pathspec: str, walk_glob: str) -> list[Path]:
    """The files under root matching a git pathspec, respecting .gitignore in a
    git repo; where git does not list them (``git_lists``), the files
    ``walk_glob`` matches, skipping environment and cache directories.

    Raises:
        GitListError: git is installed and root is in a repository, but
            ``git ls-files`` failed; walking would read ignored files.
    """
    if git_lists(root):
        try:
            result = run_git(
                ["ls-files", "--cached", "--others", "--exclude-standard", pathspec],
                root,
            )
        except subprocess.CalledProcessError as err:
            msg = f"git ls-files failed in {root}: {err.stderr.strip() or err}"
            raise GitListError(msg) from err
        files = [root / line for line in result.stdout.split("\n") if line]
        return sorted(f for f in files if f.is_file())
    return sorted(
        path
        for path in root.rglob(walk_glob)
        if path.is_file() and not any(part in _SKIP_DIRS for part in path.parts)
    )


def get_python_files(root: Path) -> list[Path]:
    """Get all Python files under root, respecting .gitignore if in a git repo."""
    return _list_files(root, "*.py", "*.py")


def get_template_files(root: Path) -> list[Path]:
    """Every file under a ``templates`` directory below root, respecting .gitignore.

    That is where Django's app directories loader and Flask's Jinja loader
    look templates up, so it is where a template engine reads a project's
    objects by attribute (``{{ cut.unread_pages }}``).

    A symlink whose target resolves outside root is left out: the file it
    names belongs to another tree, and reading it would let that tree's text
    keep this project's fields alive.
    """
    resolved_root = root.resolve()
    return [
        path
        for path in _list_files(root, f":(glob)**/{TEMPLATE_DIR}/**", "*")
        if TEMPLATE_DIR in path.relative_to(root).parts[:-1]
        and path.resolve().is_relative_to(resolved_root)
    ]


@dataclass(frozen=True)
class ParsedFile:
    """A parsed Python file: its tree, and whether line 1 is a ``#!`` line."""

    tree: ast.Module
    shebang: bool


def parse_file(path: Path) -> ParsedFile | None:
    """Parse a Python file, returning None when it is not valid Python."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    return ParsedFile(tree, source.startswith(SHEBANG))


class GitNotFoundError(Exception):
    """Raised when a git repository cannot be found."""


class GitDiffError(Exception):
    """Raised when `git diff` cannot be run or its ref cannot be resolved."""


def get_git_root(cwd: Path) -> Path:
    """Get the git repository root directory.

    Raises:
        GitNotFoundError: If cwd is not inside a git repository.
    """
    try:
        result = run_git(["rev-parse", "--show-toplevel"], cwd)
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        msg = f"not a git repository: {cwd}"
        raise GitNotFoundError(msg) from err


def get_changed_lines(ref: str, cwd: Path) -> dict[str, set[int]]:
    """Get changed lines from git diff, keyed by repo-root-relative paths.

    Returns:
        A mapping of path to changed line numbers. An empty mapping means the
        diff is genuinely empty — never that the diff could not be taken.

    Raises:
        GitDiffError: if git is unavailable or `ref` cannot be resolved. A
            failure is never reported as an empty diff: callers filter findings
            down to the changed lines, so an empty mapping silently reports a
            clean run.
    """
    try:
        result = run_git(["diff", "--unified=0", ref, "--"], cwd)
    except subprocess.CalledProcessError as err:
        msg = f"cannot diff against '{ref}': {err.stderr.strip() or err}"
        raise GitDiffError(msg) from err
    except FileNotFoundError as err:
        msg = f"git not found on PATH; cannot diff against '{ref}'"
        raise GitDiffError(msg) from err

    changed: dict[str, set[int]] = {}
    current_file = None
    for line in result.stdout.split("\n"):
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


class GitRefError(Exception):
    """Raised when a ref names no commit, or a worktree for it cannot be added or removed."""


def resolve_commit(ref: str, cwd: Path) -> str:
    """The full SHA of the commit `ref` names.

    Raises:
        GitRefError: `ref` names no commit in the repository at `cwd`.
    """
    try:
        result = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    except subprocess.CalledProcessError as err:
        msg = f"cannot compare against '{ref}': it does not name a commit in {cwd}"
        raise GitRefError(msg) from err
    return result.stdout.strip()


def git_worktree(args: list[str], cwd: Path) -> str:
    """Run `git worktree ARGS` in the repository at `cwd`, and return what it printed.

    Raises:
        GitRefError: git failed. The message names the subcommand and carries
            git's own, so a checkout that could not be made or removed is
            never silent.
    """
    try:
        return run_git(["worktree", *args], cwd).stdout
    except subprocess.CalledProcessError as err:
        msg = f"git worktree {args[0]} failed in {cwd}: {err.stderr.strip() or err}"
        raise GitRefError(msg) from err
