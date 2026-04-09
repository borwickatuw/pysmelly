"""Config file support — .pysmelly.toml and pyproject.toml [tool.pysmelly]."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Keys that accept list values
LIST_KEYS = frozenset({"exclude", "skip"})

# Keys that accept string values
STRING_KEYS = frozenset({"min-severity", "check", "git-window", "commit-messages"})

# Keys that accept boolean values
BOOL_KEYS: frozenset[str] = frozenset()

# Keys that accept list-of-pairs values (each item is a 2-element list of strings)
PAIR_LIST_KEYS = frozenset({"expected-coupling"})

VALID_KEYS = LIST_KEYS | STRING_KEYS | BOOL_KEYS | PAIR_LIST_KEYS

VALID_SEVERITIES = frozenset({"low", "medium", "high"})

VALID_COMMIT_MESSAGES = frozenset({"auto", "structured", "unstructured"})

_WINDOW_RE = __import__("re").compile(r"^\d+[dmy]$")


class ConfigError(Exception):
    """Raised for invalid config file contents."""


def _find_config_file(target_dir: Path) -> Path | None:
    """Find config file in target directory. First match wins:

    1. .pysmelly.toml
    2. pyproject.toml (must contain [tool.pysmelly])
    """
    dotfile = target_dir / ".pysmelly.toml"
    if dotfile.is_file():
        return dotfile

    pyproject = target_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except tomllib.TOMLDecodeError:
            return None
        if "tool" in data and "pysmelly" in data["tool"]:
            return pyproject
    return None


def _validate_config(config: dict, source: str, valid_check_names: set[str]) -> None:
    """Validate config keys, types, and values. Raises ConfigError on problems."""
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


def _warn_parent_config(target_dir: Path) -> None:
    """Warn if a parent directory has pysmelly config that isn't being used."""
    parent = target_dir.parent
    while parent != parent.parent:
        if _find_config_file(parent) is not None:
            print(
                f"Warning: no config in {target_dir}, but {parent} has pysmelly config. "
                f"Run from {parent} (or pass it as the target) to use that config.",
                file=sys.stderr,
            )
            return
        parent = parent.parent


def load_config(target_dir: Path, valid_check_names: set[str]) -> dict:
    """Find and load config from .pysmelly.toml or pyproject.toml.

    Returns a dict with validated config keys, or empty dict if no config found.
    Raises ConfigError on invalid config (prints message and exits).
    """
    config_file = _find_config_file(target_dir)
    if config_file is None:
        _warn_parent_config(target_dir)
        return {}

    try:
        data = tomllib.loads(config_file.read_text())
    except tomllib.TOMLDecodeError as e:
        print(f"Error: {config_file}: {e}", file=sys.stderr)
        sys.exit(1)

    if config_file.name == "pyproject.toml":
        config = data.get("tool", {}).get("pysmelly", {})
        source = f"{config_file} [tool.pysmelly]"
    else:
        config = data
        source = str(config_file)

    try:
        _validate_config(config, source, valid_check_names)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    return config
