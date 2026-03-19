# pysmelly — Plan

## Backlog

### UX: --exclude-tests convenience flag

`duplicate-blocks` output for test files can be massive and drown out
production-code findings. Users can already `--exclude test_*` but a
dedicated `--exclude-tests` shorthand would be more discoverable. Should
match `test_*.py`, `*_test.py`, `conftest.py`, and `tests/`/`test/` dirs.

### User-extensible pattern catalog

Allow users to add their own stdlib-alternatives patterns via
`[tool.pysmelly]` in `pyproject.toml` or a `.pysmelly.toml` file.
The shipped `catalog.toml` covers common cases; user patterns would
handle project-specific recommendations (e.g., "use our shared client
factory instead of raw boto3.client").

## Completed

### stdlib-alternatives check (Phase 6)

Shipped TOML catalog with 22 patterns across four categories:
unconditional, conditional (cross-file "already using the better thing"),
deprecated stdlib, deprecated third-party. Includes `condition_fn`
support for AST-level complexity checks (argparse only flagged for
complex CLIs). See `docs/PLAN-ARCHIVE.md` for details.
