"""pytest's own configuration, read the way pytest reads it.

The test role's first layer (``roles``) asks pytest's config which files
are tests. This module finds that config in the target directory and reads
the two keys that decide it, ``testpaths`` and ``python_files``. It imports
nothing from pysmelly, so ``AnalysisContext`` can hold a ``PytestConfig``
without importing the checks package.
"""

from __future__ import annotations

import configparser
import os
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePath

from pysmelly.pyproject import PYPROJECT, read_pyproject

# The file name pytest loads as a local plugin wherever it collects.
CONFTEST = "conftest.py"


def is_conftest(path: PurePath) -> bool:
    """Whether pytest loads the module at ``path`` as a ``conftest.py`` plugin."""
    return path.name == CONFTEST


# pytest's own default, used when the config leaves python_files unset.
PYTEST_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")

# pytest's default ``python_functions``: in a test module, the functions it
# collects and runs are the ones whose names start with this.
PYTEST_DEFAULT_PYTHON_FUNCTIONS = "test"


def is_collected_test_name(name: str) -> bool:
    """Whether pytest collects a function of this name when it sits in a test module.

    Only meaningful for a definition in a test-role file: pytest never
    looks anywhere else, so elsewhere a ``test*`` name is just a name.
    """
    return name.startswith(PYTEST_DEFAULT_PYTHON_FUNCTIONS)


class PytestConfigError(Exception):
    """A pytest config file exists but cannot be read."""


@dataclass(frozen=True)
class PytestConfig:
    """The pytest settings that decide which files are tests.

    ``rootdir`` is the directory the config was read from, and ``base`` the
    directory the analyzed paths are relative to; a file's place relative to
    ``testpaths`` is its path from ``rootdir``. ``source`` names the config
    file and section, or is None when the directory has no pytest config.
    ``python_files_default`` says ``python_files`` was not set and pytest's
    default applies. ``pythonpath`` holds the directories pytest puts on
    ``sys.path``, relative to ``rootdir`` (pytest's default is none), which
    is where a test's ``import gallery`` finds ``tools/gallery.py``.
    ``entry_point_plugins`` holds the modules the project registers as
    pytest plugins through ``pytest11`` entry points in its
    ``pyproject.toml``; pytest calls the hooks they define.
    """

    rootdir: Path
    base: Path
    source: str | None
    testpaths: tuple[str, ...]
    python_files: tuple[str, ...]
    python_files_default: bool
    pythonpath: tuple[str, ...] = ()
    entry_point_plugins: tuple[str, ...] = ()

    def import_roots(self) -> tuple[Path, ...]:
        """The ``pythonpath`` directories relative to ``base``, as the analyzed paths are.

        An entry outside ``base`` holds no analyzed file and is left out.
        """
        roots = []
        for entry in self.pythonpath:
            # normpath, so that "../x" leaves base rather than staying inside it lexically.
            path = Path(os.path.normpath(self.rootdir / entry))
            if path.is_relative_to(self.base):
                roots.append(path.relative_to(self.base))
        return tuple(roots)


# No pytest config: pytest's defaults, rooted where the analyzed paths are.
NO_PYTEST_CONFIG = PytestConfig(
    rootdir=Path(),
    base=Path(),
    source=None,
    testpaths=(),
    python_files=PYTEST_DEFAULT_PYTHON_FILES,
    python_files_default=True,
)


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    except configparser.Error as e:
        msg = f"{path}: {e}"
        raise PytestConfigError(msg) from e
    return parser


def _ini_section(path: Path, section: str) -> dict[str, str] | None:
    parser = _read_ini(path)
    if parser.has_section(section):
        return dict(parser.items(section))
    return None


def _pyproject_section(data: dict, path: Path) -> dict | None:
    options = data.get("tool", {}).get("pytest", {}).get("ini_options")
    if options is None:
        return None
    if not isinstance(options, dict):
        msg = f"{path}: [tool.pytest.ini_options] must be a table"
        raise PytestConfigError(msg)
    return options


def locate_pytest_config(rootdir: Path) -> tuple[Path, str, dict] | None:
    """The pytest config in ``rootdir``, in pytest's precedence order.

    pytest takes the first of ``pytest.ini`` (even with no ``[pytest]``
    section), ``.pytest.ini``, ``pyproject.toml`` with
    ``[tool.pytest.ini_options]``, ``tox.ini`` with ``[pytest]``, and
    ``setup.cfg`` with ``[tool:pytest]``. A file that does not match is
    passed over, unread beyond telling whether it matches, exactly as pytest
    passes it over. Like pysmelly's own config, nothing above ``rootdir`` is
    consulted.

    Returns:
        ``(path, section, options)``, or None when the directory has no
        pytest config.

    Raises:
        PytestConfigError: a candidate file reached in that order cannot be
            parsed. Treating it as absent would silently fall back to pytest's
            defaults and give every ``test_*.py`` the test role.
    """
    for name in ("pytest.ini", ".pytest.ini"):
        path = rootdir / name
        if path.is_file():
            return path, "pytest", _ini_section(path, "pytest") or {}
    pyproject = rootdir / PYPROJECT
    options = _pyproject_section(read_pyproject(rootdir, PytestConfigError), pyproject)
    if options is not None:
        return pyproject, "tool.pytest.ini_options", options
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        path = rootdir / name
        if path.is_file():
            ini_options = _ini_section(path, section)
            if ini_options is not None:
                return path, section, ini_options
    return None


def _args_value(value: object, key: str, source: str) -> tuple[str, ...]:
    """A pytest ``args``-type value: a list of strings, or a shell-split string."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError as e:
            msg = f"{source}: '{key}' cannot be split: {e}"
            raise PytestConfigError(msg) from e
    msg = f"{source}: '{key}' must be a list of strings or a string, got {type(value).__name__}"
    raise PytestConfigError(msg)


def entry_point_plugins(rootdir: Path) -> tuple[str, ...]:
    """The modules ``rootdir``'s ``pyproject.toml`` registers as ``pytest11`` entry points.

    pytest loads each ``pytest11`` entry point at startup and registers the
    object it names as a plugin. Only a reference to a whole module
    (``pkg.plugin``) registers that module's functions as hooks; one naming
    an attribute (``pkg.plugin:Plugin``) registers that object instead, so
    it names no module here.

    Raises:
        PytestConfigError: ``pyproject.toml`` cannot be parsed, or its
            ``pytest11`` entry points are not a table of strings.
    """
    data = read_pyproject(rootdir, PytestConfigError)
    pyproject = rootdir / PYPROJECT
    entry_points = data.get("project", {}).get("entry-points", {}).get("pytest11")
    if entry_points is None:
        return ()
    if not isinstance(entry_points, dict) or not all(
        isinstance(value, str) for value in entry_points.values()
    ):
        msg = f"{pyproject}: [project.entry-points.pytest11] must be a table of strings"
        raise PytestConfigError(msg)
    references = (value.strip() for value in entry_points.values())
    return tuple(reference for reference in references if ":" not in reference)


def load_pytest_config(rootdir: Path, base: Path) -> PytestConfig:
    """The pytest settings for a run whose config lives in ``rootdir``.

    pytest's default applies to each key the config leaves unset, as pytest
    applies it: ``python_files`` defaults to ``test_*.py *_test.py`` and an
    unset ``testpaths`` means files are collected anywhere under ``rootdir``.

    Raises:
        PytestConfigError: the config file cannot be parsed, or a key holds a
            value of the wrong type.
    """
    found = locate_pytest_config(rootdir)
    plugins = entry_point_plugins(rootdir)
    if found is None:
        return PytestConfig(
            rootdir=rootdir,
            base=base,
            source=None,
            testpaths=(),
            python_files=PYTEST_DEFAULT_PYTHON_FILES,
            python_files_default=True,
            entry_point_plugins=plugins,
        )
    path, section, options = found
    location = f"{path} [{section}]"
    return PytestConfig(
        rootdir=rootdir,
        base=base,
        source=f"{path.name} [{section}]",
        testpaths=(
            _args_value(options["testpaths"], "testpaths", location)
            if "testpaths" in options
            else ()
        ),
        python_files=(
            _args_value(options["python_files"], "python_files", location)
            if "python_files" in options
            else PYTEST_DEFAULT_PYTHON_FILES
        ),
        python_files_default="python_files" not in options,
        pythonpath=(
            _args_value(options["pythonpath"], "pythonpath", location)
            if "pythonpath" in options
            else ()
        ),
        entry_point_plugins=plugins,
    )
