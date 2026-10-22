"""Reading the target directory's ``pyproject.toml``.

pysmelly's own config, the pytest config and the project's entry points can
all live in the one file; each reader parses it here and reports a file that
is not valid TOML as its own error, naming the file. This module imports
nothing from pysmelly, so any module can use it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = "pyproject.toml"


def read_pyproject(rootdir: Path, error: type[Exception]) -> dict:
    """``rootdir``'s ``pyproject.toml``, parsed; an empty table when there is no such file.

    No reader distinguishes an absent file from an empty one: each declares
    nothing.

    Raises:
        error: the file is not valid TOML. The message names the file. A
            caller passes its own error type, so an unreadable pyproject is
            reported where that caller's errors are, never read as absent.
    """
    path = rootdir / PYPROJECT
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        msg = f"{path}: {e}"
        raise error(msg) from e
