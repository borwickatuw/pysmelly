# pysmelly — Plan

## Backlog

### UX: --exclude-tests convenience flag

`duplicate-blocks` output for test files can be massive and drown out
production-code findings. Users can already `--exclude test_*` but a
dedicated `--exclude-tests` shorthand would be more discoverable. Should
match `test_*.py`, `*_test.py`, `conftest.py`, and `tests/`/`test/` dirs.

## Active

### 3rd-party opportunity detection + configuration file

Detect stdlib usage patterns where a well-known library would be better,
and provide a configuration mechanism so users can add their own patterns.

#### Goals

1. Ship a built-in catalog of 10-15 high-consensus patterns (requests
   over urllib, pathlib over os.path, etc.)
2. User-extensible pattern catalog via a `.pysmelly.toml` config file
3. Config file also serves as the future home for other project-level
   settings (exclude patterns, severity overrides, etc.)

#### Design constraints

- Zero dependencies — pysmelly reads TOML with `tomllib` (stdlib 3.11+)
- Patterns are AST-based, not string matching
- The check should be opt-in or LOW severity (recommendations, not smells)
- Config file is optional — pysmelly works without one

#### Built-in pattern catalog (starter set)

These are cases with near-universal consensus:

| Signal | Suggestion |
|--------|-----------|
| `urllib.request` or `http.client` usage | requests, httpx |
| `argparse.ArgumentParser` in 3+ files | Click, Typer |
| `os.path` manipulation when `pathlib` also used | pick one (prefer pathlib) |
| `datetime.strptime` / manual timezone handling | python-dateutil, arrow |
| `json.loads()` + manual key extraction into typed objects | Pydantic |
| `subprocess.run()` + stdout parsing | sh, plumbum |
| `re.compile()` same pattern in multiple files | shared utility or regex lib |
| dataclass `@classmethod` + `data.get("key", default)` | dacite, cattrs, Pydantic |

#### Configuration file design

`.pysmelly.toml` in project root:

```toml
# Project-level settings
[settings]
exclude = ["migrations/", "vendor/"]
min-severity = "medium"

# Custom pattern recommendations
[[recommendations]]
name = "boto3-error-handling"
description = "boto3.client() with try/except ClientError repeated"
signal = "boto3.client"      # import or call to detect
min-occurrences = 3          # threshold before flagging
suggest = ["shared client factory", "aws-error-utils"]
severity = "low"

[[recommendations]]
name = "manual-csv"
description = "csv module usage across many files"
signal = "csv.reader"
min-occurrences = 5
suggest = ["pandas for analysis", "shared CSV utility"]
severity = "low"
```

#### Implementation phases

**Phase 1: Config file infrastructure**
- Add `.pysmelly.toml` loading (tomllib)
- Wire `[settings]` section into CLI (exclude, min-severity)
- Existing CLI flags override config file values

**Phase 2: Built-in pattern detection**
- New check `stdlib-alternatives` in `checks/recommendations.py`
- Hardcoded catalog of patterns with AST-based detection
- LOW severity, included by default

**Phase 3: User-defined recommendations**
- `[[recommendations]]` section in `.pysmelly.toml`
- Simple signal matching (import detection, call counting)
- Users define name, signal, threshold, suggestion

#### Open questions

- ~~tomllib compatibility~~ — requires-python >= 3.12, so tomllib is
  available. No fallback needed.
- Should built-in patterns be overridable/disableable per-project?
- Signal format: simple string matching on imports/calls, or something
  more expressive? Start simple, extend later.
- Should the config file also support `known_used` symbols to suppress
  dead-code false positives (the other half of the Django feedback)?
