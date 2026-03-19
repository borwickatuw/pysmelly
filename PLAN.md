# pysmelly — Plan

## Backlog

### UX: --exclude-tests convenience flag

`duplicate-blocks` output for test files can be massive and drown out
production-code findings. Users can already `--exclude test_*` but a
dedicated `--exclude-tests` shorthand would be more discoverable. Should
match `test_*.py`, `*_test.py`, `conftest.py`, and `tests/`/`test/` dirs.
