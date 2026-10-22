"""Config file support — .pysmelly.toml and pyproject.toml [tool.pysmelly]."""

from __future__ import annotations

import glob
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

from pysmelly.catalog import CatalogError, parse_user_catalog
from pysmelly.pyproject import PYPROJECT, read_pyproject

# Keys that accept list values
LIST_KEYS = frozenset({"exclude", "skip"})

# Keys that accept string values
STRING_KEYS = frozenset({"min-severity", "check", "git-window", "commit-messages"})

# Keys that accept boolean values
BOOL_KEYS: frozenset[str] = frozenset()

# File-pattern pairs whose co-change is by design (fnmatch patterns)
EXPECTED_COUPLING_KEY = "expected-coupling"

# Keys that accept list-of-pairs values (each item is a 2-element list of strings)
PAIR_LIST_KEYS = frozenset({EXPECTED_COUPLING_KEY})

# The key whose value is a table in catalog.toml's schema: stdlib-alternatives
# patterns and dual-stack-dependency pairs the project adds to the shipped ones
CATALOG_KEY = "catalog"

VALID_KEYS = LIST_KEYS | STRING_KEYS | BOOL_KEYS | PAIR_LIST_KEYS | {CATALOG_KEY}

VALID_SEVERITIES = frozenset({"low", "medium", "high"})

VALID_COMMIT_MESSAGES = frozenset({"auto", "structured", "unstructured"})

_WINDOW_RE = re.compile(r"^\d+[dmy]$")


# Characters a TOML basic string may not hold raw: the C0 controls and DEL
_TOML_ESCAPED_CONTROLS = frozenset(map(chr, [*range(0x20), 0x7F]))


class ConfigError(Exception):
    """Raised for invalid config file contents."""


def _toml_char(char: str) -> str:
    if char in '"\\':
        return "\\" + char
    if char in _TOML_ESCAPED_CONTROLS:
        return f"\\u{ord(char):04X}"
    return char


def _toml_string(text: str) -> str:
    """``text`` as a TOML basic string: quote, backslash and the control
    characters TOML forbids raw are escaped, everything else kept."""
    return '"' + "".join(_toml_char(char) for char in text) + '"'


def expected_coupling_line(pairs: Iterable[tuple[str, str]]) -> str:
    """The config line declaring each file pair in ``pairs`` expected coupling.

    Paths are fnmatch-escaped, so each pattern matches its own file and no
    other, then written as TOML strings. The line is a complete assignment,
    valid on its own in ``[tool.pysmelly]`` or ``.pysmelly.toml``; a project
    that already sets the key adds the pairs to its list instead.
    """
    items = ", ".join(
        f"[{_toml_string(glob.escape(file_a))}, {_toml_string(glob.escape(file_b))}]"
        for file_a, file_b in pairs
    )
    return f"{EXPECTED_COUPLING_KEY} = [{items}]"


def _find_config_file(target_dir: Path) -> Path | None:
    """Find config file in target directory. First match wins:

    1. .pysmelly.toml
    2. pyproject.toml (must contain [tool.pysmelly])

    Returns:
        The config file path, or None when the directory genuinely has no
        pysmelly config. None never means "there is config I could not read".

    Raises:
        ConfigError: if pyproject.toml exists but cannot be parsed. Reporting
            an unreadable pyproject as "no config" would silently run with
            default settings — no skips, no excludes — and look like a clean
            result.
    """
    dotfile = target_dir / ".pysmelly.toml"
    if dotfile.is_file():
        return dotfile

    data = read_pyproject(target_dir, ConfigError)
    if "tool" in data and "pysmelly" in data["tool"]:
        return target_dir / PYPROJECT
    return None


def _validate_config(config: dict, source: str, valid_check_names: set[str]) -> dict:
    """Validate config keys, types, and values.

    Returns:
        The config, with a ``catalog`` table replaced by the ``Catalog`` it
        parses to.

    Raises:
        ConfigError: on the first problem found.
    """
    unknown = set(config) - VALID_KEYS
    if unknown:
        raise ConfigError(
            f"{source}: unknown key(s): {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(VALID_KEYS))}"
        )

    for key in LIST_KEYS:
        if key in config and not isinstance(config[key], list):
            raise ConfigError(f"{source}: '{key}' must be a list, got {type(config[key]).__name__}")
        if key in config:
            for item in config[key]:
                if not isinstance(item, str):
                    raise ConfigError(
                        f"{source}: '{key}' items must be strings, got {type(item).__name__}"
                    )

    for key in STRING_KEYS:
        if key in config and not isinstance(config[key], str):
            raise ConfigError(
                f"{source}: '{key}' must be a string, got {type(config[key]).__name__}"
            )

    for key in BOOL_KEYS:
        if key in config and not isinstance(config[key], bool):
            raise ConfigError(
                f"{source}: '{key}' must be a boolean, got {type(config[key]).__name__}"
            )

    for key in PAIR_LIST_KEYS:
        if key in config:
            if not isinstance(config[key], list):
                raise ConfigError(
                    f"{source}: '{key}' must be a list of pairs, got {type(config[key]).__name__}"
                )
            pair_length = 2
            for i, item in enumerate(config[key]):
                if not isinstance(item, list) or len(item) != pair_length:
                    raise ConfigError(
                        f"{source}: '{key}' items must be 2-element lists, "
                        f"got {item!r} at index {i}"
                    )
                if not all(isinstance(s, str) for s in item):
                    raise ConfigError(
                        f"{source}: '{key}' items must contain strings, got {item!r} at index {i}"
                    )

    if "min-severity" in config and config["min-severity"] not in VALID_SEVERITIES:
        raise ConfigError(
            f"{source}: invalid min-severity '{config['min-severity']}'. "
            f"Valid values: {', '.join(sorted(VALID_SEVERITIES))}"
        )

    if "check" in config and config["check"] not in valid_check_names:
        raise ConfigError(
            f"{source}: unknown check '{config['check']}'. "
            f"Use --list-checks to see available checks"
        )

    if "skip" in config:
        bad = [s for s in config["skip"] if s not in valid_check_names]
        if bad:
            raise ConfigError(
                f"{source}: unknown check(s) in skip: {', '.join(bad)}. "
                f"Use --list-checks to see available checks"
            )

    if "commit-messages" in config and config["commit-messages"] not in VALID_COMMIT_MESSAGES:
        raise ConfigError(
            f"{source}: invalid commit-messages '{config['commit-messages']}'. "
            f"Valid values: {', '.join(sorted(VALID_COMMIT_MESSAGES))}"
        )

    if "git-window" in config and not _WINDOW_RE.match(config["git-window"]):
        raise ConfigError(
            f"{source}: invalid git-window '{config['git-window']}'. "
            f"Expected format like 6m, 1y, 90d (number + d/m/y)"
        )

    if CATALOG_KEY not in config:
        return config
    try:
        catalog = parse_user_catalog(config[CATALOG_KEY], f"{source} {CATALOG_KEY}")
    except CatalogError as e:
        raise ConfigError(str(e)) from e
    return {**config, CATALOG_KEY: catalog}


def _warn_parent_config(target_dir: Path) -> None:
    """Warn if a parent directory has pysmelly config that isn't being used.

    An unparseable pyproject.toml in an ancestor directory is skipped rather
    than raised: this warning is advisory and about a directory the run was
    not pointed at.
    """
    parent = target_dir.parent
    while parent != parent.parent:
        try:
            found = _find_config_file(parent) is not None
        except ConfigError:
            found = False
        if found:
            print(
                f"Warning: no config in {target_dir}, but {parent} has pysmelly config. "
                f"Run from {parent} (or pass it as the target) to use that config.",
                file=sys.stderr,
            )
            return
        parent = parent.parent


def load_config(target_dir: Path, valid_check_names: set[str]) -> dict:
    """Find and load config from .pysmelly.toml or pyproject.toml.

    Returns:
        A dict with validated config keys (``catalog`` parsed into a
        ``Catalog``), or an empty dict when the directory has no pysmelly
        config.

    Exits:
        Status 1, with the message on stderr, when a config file exists but is
        unparseable or invalid. Invalid config is never silently defaulted.
    """
    try:
        config_file = _find_config_file(target_dir)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if config_file is None:
        _warn_parent_config(target_dir)
        return {}

    try:
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        print(f"Error: {config_file}: {e}", file=sys.stderr)
        sys.exit(1)

    if config_file.name == PYPROJECT:
        config = data.get("tool", {}).get("pysmelly", {})
        source = f"{config_file} [tool.pysmelly]"
    else:
        config = data
        source = str(config_file)

    try:
        return _validate_config(config, source, valid_check_names)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
