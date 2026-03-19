# pysmelly — Plan

## Backlog

### dead-code: framework-aware suppression for Django/pytest

Django views, context processors, validators, and admin methods are referenced
by string dotted paths in settings/urlpatterns, not by direct calls or imports.
These appear as false positives in `dead-code`.

Options:
- Detect dotted-path string literals (e.g., `"myapp.context_processors.site_url"`)
  and treat the final component as a reference
- Let users provide a config file listing known-used symbols
- For now, `# pysmelly: ignore` is the workaround

### UX: --exclude-tests convenience flag

`duplicate-blocks` output for test files can be massive and drown out
production-code findings. Users can already `--exclude test_*` but a
dedicated `--exclude-tests` shorthand would be more discoverable. Should
match `test_*.py`, `*_test.py`, `conftest.py`, and `tests/`/`test/` dirs.
