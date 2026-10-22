# pysmelly

AST-based Python code smell detector for AI-assisted code review.

pysmelly finds **vestigial code patterns** — code that outlived the design that created it. It performs cross-file call-graph analysis to detect smells that single-file linters miss, then reports findings as **investigation pointers** — signals that tell an AI reviewer (like [Claude Code](https://docs.anthropic.com/en/docs/claude-code)) or a human where to look, not what to do. Findings include cross-file context (caller counts, blast radius) so the reviewer can apply judgment.

How the pieces fit together — the two pipelines, the shared analysis context, the data structures a check works with — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

```bash
# Run directly (no install)
uvx --from git+https://github.com/borwickatuw/pysmelly pysmelly

# Or install as a tool
uv tool install git+https://github.com/borwickatuw/pysmelly

# Or pip
pip install git+https://github.com/borwickatuw/pysmelly
```

## Releases

No version is stored in the repo: `[tool.hatch.version] source = "vcs"` makes
hatch derive it from git tags at build time. **Cutting a release is tagging
`vX.Y.Z` on the default branch and pushing the tag** — nothing else to bump.
A build taken between tags reports the next patch level with a dev suffix and
the commit it came from; `pysmelly --version` says what is actually installed.

The install commands above are unpinned git installs, so they build from the
default branch at install time — a consumer picks up whatever has landed
since, the next time they run that command, and upgrading is re-running it.
Append `@<tag>` to the git URL to pin a repo to one release.

Consumer repos also carry a generated guidance file from `pysmelly init`.
It is stamped with a hash of the guidance the tool shipped, and every run
compares that stamp against the installed version's, so a release that
changes the guidance text makes each consumer repo's next run print
`run pysmelly init to update it`. Regenerating is that command, over the
same path — nobody has to remember which release changed the text. It
replaces only the generated block between the `pysmelly-guidance` start and
end markers, so a findings register kept below the end marker survives.

## Usage

```bash
# Analyze current directory
pysmelly

# Analyze another project (the target is a project root: its pyproject.toml supplies the config)
pysmelly ../other-project

# Run a single check
pysmelly --check unused-defaults

# Run several checks (the flag is repeatable; the selections are unioned)
pysmelly --check unused-defaults --check dead-code

# Skip specific checks
pysmelly --skip single-call-site --skip trivial-wrappers

# Only show findings in changed lines
pysmelly --diff HEAD

# What a refactoring resolved and introduced, against a ref
pysmelly --compare main

# Never look at vendored or generated code (tests are not excluded: see "Tests and the test role")
pysmelly --exclude 'vendor/' --exclude '*_pb2.py'

# Summary counts by check (no individual findings)
pysmelly --summary

# Verbose output: the test-role files and the entry points found, each with why
pysmelly -v

# Set up AI review guidance for a project
pysmelly init

# Git history analysis (separate subcommand)
pysmelly git-history

# Git history with options
pysmelly git-history --window 1y              # look back 1 year (default: 6m)
pysmelly git-history --check bug-magnet       # run a single check
pysmelly git-history --all                    # show all findings
pysmelly git-history --summary                # counts per check (no Project trends block)

# Acknowledge a finding after reviewing/fixing
pysmelly git-history reviewed path/to/file.py
pysmelly git-history reviewed src/models/     # review all .py files in directory

# Re-analyze ignoring prior acknowledgments
pysmelly git-history --ignore-reviewed
```

## Configuration

Config lives in one of two files, in the directory pysmelly is pointed at.
`.pysmelly.toml` wins if it exists; otherwise `pyproject.toml` is used if it
has a `[tool.pysmelly]` table. Keys are the same in both — in `.pysmelly.toml`
they sit at the top level.

```toml
[tool.pysmelly]
skip = ["isinstance-chain", "arrow-code"]   # check names never to run
exclude = ["vendor/", "*_pb2.py"]           # paths never to look at: vendored, generated
```

**Config is read from the target directory and pysmelly does not walk
upward.** Pointing it at a subdirectory therefore runs with no skip list and
no excludes — silently, apart from a warning on stderr that a parent
directory has config it is not using. Run it from the project root with `.`
as the target.

| Key                 | Type                 | What it does                                                                                                                                  |
| ------------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `skip`              | list of strings      | Check names to leave out of the run. Unknown names are an error.                                                                              |
| `exclude`           | list of strings      | Path patterns never to look at (vendored, generated). No `/` matches filenames; a trailing `/` matches everything under matching directories. |
| `check`             | string               | Run only this check, as `--check` does.                                                                                                       |
| `min-severity`      | string               | `low`, `medium` or `high` — the floor findings must reach to be reported.                                                                     |
| `git-window`        | string               | How far `git-history` looks back: a number plus `d`, `m` or `y` (`90d`, `1y`).                                                                |
| `commit-messages`   | string               | `auto`, `structured` or `unstructured` — override how commit-message quality is judged.                                                       |
| `expected-coupling` | list of string pairs | File-pattern pairs whose co-change is by design, so coupling checks stay quiet about them.                                                    |
| `catalog`           | table                | The project's own `stdlib-alternatives` patterns and `dual-stack-dependency` pairs, in `catalog.toml`'s schema (below).                       |

Config and command line combine rather than compete: `skip` and `exclude`
merge with any flags passed, and the single-value keys apply only when the
corresponding flag was left at its default. An unknown key, a wrong type or
an invalid value is an error that exits 1 — bad config is never silently
replaced with defaults.

### Declaring expected coupling

`change-coupling` reports two files that keep changing in the same commits
with no import between them, and `blast-radius` a file whose commits drag
many others along. Sometimes that is the design: a view and its template
tag, a client and the fixture that mirrors it. Each finding names the config line that declares its pair,
ready to paste, and its dismissal says when to use it:

```text
=== change-coupling (1 finding(s)) ===
  api/views.py:1: api/views.py and billing/invoice.py changed together in 8/10 commits (last 6m) with no import relationship — hidden coupling — the expected-coupling line for them: `expected-coupling = [["api/views.py", "billing/invoice.py"]]`. Fix: Make the dependency explicit: move what both files change for into one module they import, or have one import it from the other. Dismiss only if the files change together by design; then declare the pair with the expected-coupling line the message gives.
```

`blast-radius` counts only production files as dragged along (a test changing with its code is the change's test), names the files each flagged file drags along most often,
with the commits they shared, and gives the line for the first of them;
declare whichever partners belong with it the same way. Every pair goes in
one list, and a pattern may use fnmatch wildcards (`*` also matches `/`), so
one pair can cover every app in a project:

```toml
[tool.pysmelly]
expected-coupling = [
    ["api/views.py", "billing/invoice.py"],
    ["*/settings.py", "*/urls.py"],
]
```

A pair matches in either order. `change-coupling` and `fix-propagation` then
stay quiet about it, and `blast-radius` stops counting the partner as a file
the other one drags along. The printed line escapes `*`, `?` and `[` in a
path, so it declares that one pair and nothing else. If the key is already
set, add the pair to the existing list instead of pasting a second
`expected-coupling =` line, which TOML rejects.

Two files in the same directory are not presumed to belong together.
Whether they should change in lockstep is the question the finding asks, so
declare the pair once you have decided it, or give it a reasoned
`# pysmelly: ignore[change-coupling]` on line 1 of either file. Sharing a
base class does count: two files in one directory that each subclass the
same class, defined in that directory or below it (`handlers/audio.py` and
`handlers/video.py` both extending `handlers/base.py`'s `Handler`),
implement one contract and are not reported. Naming the other module in a
dotted string counts as an import, since that is how a framework loads it:
Django's `ROOT_URLCONF = "proj.urls"` explains its settings module changing
with `proj/urls.py`, as a Celery schedule's `"proj.tasks.sweep"` explains
`proj/tasks.py`. Importing a common module does not count on its own.

### Tests and the test role

Do not exclude tests. pysmelly parses every file and gives each one a role.
A test-role file is evidence: a call from it counts as a test caller, so a
function only tests call reads "no production callers; still referenced by
tests/test_x.py:12" instead of "zero callers". It is never the subject of a
finding, except for the checks about tests (`orphaned-test-helpers`,
`test-erosion`). `pysmelly -v` lists every test-role file and why it took
the role.

The role comes from two layers that never compete.

1. **The runner's configuration decides files.** pysmelly reads pytest's
   `testpaths` and `python_files` from the first of these in the target
   directory, in pytest's own order: `pytest.ini` (even without a `[pytest]`
   section), `.pytest.ini`, `pyproject.toml` with
   `[tool.pytest.ini_options]`, `tox.ini` with `[pytest]`, `setup.cfg` with
   `[tool:pytest]`. A file without its section is passed over; nothing above
   the target directory is read; a config file that cannot be parsed is an
   error that exits 1. A key left unset takes pytest's default:
   `python_files` is `test_*.py *_test.py`, and no `testpaths` means
   anywhere under the target. (`pythonpath` is read too, only to resolve a
   test's imports.)
   - With `testpaths` set, a module under a test path (an entry may be a
     glob, `**` included) is a test file when it matches `python_files`,
     since that is what pytest collects there. A file a `testpaths` entry
     names outright is a test file too. `testpaths` says where pytest
     *searches*, not what it finds, so a module under a test path that
     matches no pattern is not a test file by location alone: a project
     whose `testpaths` names its own package (tests living beside the code)
     keeps the modules its production code imports production. A module
     outside every test path is not a test file by configuration, whatever
     its name.

   - With no `testpaths`, a module matching `python_files` is a test file,
     since pytest collects it from anywhere.

   - The import graph then finds test support (helpers, fixture builders,
     `__init__.py` files and the like), among modules under a test path
     when `testpaths` is set and among all modules when it is not.

     - **Under a test path**, a module that test-role modules import, and
       nothing else, is test support. `testpaths` is the runner saying
       where tests live, so it needs no further evidence: a plain
       `tests/helpers.py` that only tests import takes the role.

     - **With no `testpaths`**, such a module takes the role only when it
       also shows one of

       - it imports `pytest`, `_pytest` or `unittest`, or any submodule of
         them (`unittest.mock` counts);
       - it imports a test-role module (a test file, a module whose
         definitions declare themselves, or support found earlier);
       - every module importing it is itself test support.

       Being imported only by tests is not enough on its own there: an
       entry point, or a module beside its own test, is production.

     This repeats until nothing changes, so a helper's helper follows. A
     module any production module imports, or that nothing imports, stays
     as it is. A module a test-role module's `pytest_plugins` names, and no
     production module imports, takes the role too: pytest loads it as a
     plugin of the tests.

     **The cost where `testpaths` names the package.** A production module
     that only tests import (a Django settings module or middleware loaded
     by dotted string, a library module only its tests exercise) reads as
     test support there, so its test-only surface goes unreported. This
     errs toward fewer findings, not false ones.

   - A `conftest.py` is a test file only under a test path; with no
     `testpaths`, that is anywhere under the target.
1. **Definitions declare themselves.** A `unittest.TestCase` subclass
   (Django's and DRF's test cases included, followed through imports and
   aliases), a `@pytest.fixture`, a `@pytest.mark.*` decorator, or a `test_`
   function at module or class level in a module that imports pytest is a
   test definition, and its file takes the role. This layer only adds the
   role, so a `TestCase` subclass shipped inside a production package is a
   test wherever it lives.

**The filename alone is never evidence.** pysmelly has no naming rule of its
own (no `test_*`, `tests/` or `conftest.py` heuristic); a name counts only
through `python_files`, the pattern pytest itself collects by. Nor is a
location on its own: a module under `testpaths` that nothing imports, or
that a production module imports, is production like any other.
`src/quiz/test_knowledge.py`, outside `testpaths` and declaring nothing, is
production. With no `testpaths`, pytest would collect it, so it is a test
there too: set `testpaths` to keep a production `test_*.py` production.

**Deleted paths.** git-history checks also name files that no longer exist,
and files that were excluded or failed to parse. Those have no definitions
or importers to read, so only the runner's configuration classifies them: a
deleted `tests/test_x.py` is a test when pytest would have collected it, and
a deleted helper is production, under `testpaths` or not, since its
location is no evidence and it has no imports left to read.

**`exclude` means "never look".** An excluded file is not parsed at all, so
it is not evidence either; excluding tests hides their calls and makes
test-only code read as dead. Use `exclude` for vendored and generated code.
Code shipped for consumers' tests (a `testing.py`) or a library API that
only its tests exercise is production; when a finding there is a false
positive, suppress it with a stated reason rather than excluding the file.

### Project catalog entries

`stdlib-alternatives` reports imports that have a better replacement, from
the entries in pysmelly's
[`catalog.toml`](src/pysmelly/catalog.toml). A project can add entries of its
own under `catalog`, for example to steer code toward its shared AWS client
factory rather than raw `boto3`:

```toml
[[tool.pysmelly.catalog.patterns]]
name = "boto3-client-factory"
description = "AWS clients"
imports = ["boto3"]
condition = "also-imports:acme.aws"
suggest = "acme.aws.client() (the shared factory)"
```

In `.pysmelly.toml` the header is `[[catalog.patterns]]`. Once any file
imports `acme.aws`, every file that imports `boto3` is reported as one
finding: `boto3 used in 3 files (...) — consider acme.aws.client() (the shared factory)`.

| Field             | Required | What it does                                                                                               |
| ----------------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `name`            | yes      | Identifies the entry. It must be unique, and it must not be a shipped entry's name.                        |
| `description`     | yes      | What the imports are for, such as "AWS clients".                                                           |
| `imports`         | yes      | Modules to report. An entry matches the module and its submodules (`boto3` matches `boto3.s3`).            |
| `suggest`         | yes      | The replacement, quoted in the finding.                                                                    |
| `exclude_imports` | no       | Submodules that do not count, such as `unittest.mock` under `unittest`.                                    |
| `condition`       | no       | `also-imports:<module>`: report only when the codebase also imports that module.                           |
| `condition_fn`    | no       | A named test of the matched files: `complex_argparse` or `chooses_ini_format`. Any other name is an error. |

Project entries add to the shipped ones and are applied the same way,
conditions included. Using a shipped entry's name is an error, not an
override: to disagree with a shipped entry, `skip` the check or leave a
reasoned `# pysmelly: ignore[stdlib-alternatives]`. Unknown fields, missing
or mistyped fields and duplicate names are errors that exit 1, the same as
any other bad config. pysmelly's own `catalog.toml` passes through the same
validator.

The factory module imports `boto3` itself, so it is always among the files
the finding lists. It is the one sanctioned site, so give its import a
reasoned `# pysmelly: ignore[stdlib-alternatives]`. That ignore removes only
the factory's site. The other files stay in the finding, which moves to the
first of them when the factory came first in path order, and its message
ends `(1 other site ignored)`.

#### Project pairs

`dual-stack-dependency` (a `git-history` check) reports two libraries that
do one job when production code uses both, from the `pairs` in
`catalog.toml`: `requests`/`httpx`, `os.path`/`pathlib`, `argparse`/`click`,
`attrs`/`dataclasses` and `json`/`orjson`. A project adds its own pairs the
same way it adds patterns, for example an internal client and the one
replacing it:

```toml
[[tool.pysmelly.catalog.pairs]]
name = "acme-http"
description = "internal HTTP"
sides = [
    { label = "acme.legacy_http", imports = ["acme.legacy_http"] },
    { label = "acme.http", imports = ["acme.http"] },
]
```

In `.pysmelly.toml` the header is `[[catalog.pairs]]`. A catalog table may
hold `patterns`, `pairs` or both, but not neither.

| Field                   | Required | What it does                                                                                                                                      |
| ----------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                  | yes      | Identifies the pair. It must be unique among pairs, and it must not be a shipped pair's name.                                                     |
| `description`           | yes      | The job both sides do, quoted in the finding ("both used for internal HTTP").                                                                     |
| `sides`                 | yes      | Exactly two tables. Which one is the minority is measured, not declared, so their order does not matter.                                          |
| `sides[].label`         | yes      | The side's name in the finding. The two labels must differ.                                                                                       |
| `sides[].imports`       | yes      | Modules whose uses count for the side, submodules and names included. The two sides may not overlap (`os` and `os.path` would count a use twice). |
| `sides[].no_substitute` | no       | Names under the side's imports that the other side cannot replace, so their uses do not count (`os.path.normpath`: pathlib has no equivalent).    |

A pair with a shipped pair's name, or with the same two sides as a shipped
pair under another name, is an error: project pairs add pairs and never
shadow shipped ones.

### Ignores in findings that list several sites

An inline `# pysmelly: ignore[check]` speaks for the line it sits on (or the
line below it) and nothing else. Some checks report one pattern once,
across every place it occurs: `stdlib-alternatives`, `duplicate-blocks`,
`duplicate-except-blocks`, `param-clumps`, `scattered-constants`,
`scattered-isinstance`, `shotgun-surgery`, `repeated-string-parsing` (the
same `.split()[N]` in many places), `getattr-strings` (one name across
files), `isinstance-chain` (the same ladder in many functions),
`shared-mutable-module-state`, `same-change-multiple-files`,
`dual-stack-dependency` (one site per file using the minority library),
`unreachable-from-entry-points` (one site per member of a cluster),
`change-coupling` (line 1 of both files in a pair, or of every file in a
package-level cluster), `fix-propagation` (line 1 of both files in a pair), `blast-radius` (line 1 of every file in a
package-level cluster), `bypassed-helper` and `bypassed-helper-history`
(each hand-written copy), `coalesced-optional-field` (each coalescing read),
`write-only-record-keys` (each unread key's write), `swallowed-kwargs` (each
production call passing the keyword) and `return-shape-unused` (each
production caller). An
ignore on one of the listed places removes that place from the finding, and
the rest stay reported. The finding goes when every place is ignored, or when the
places left fall below the check's own floor: one copy of a duplicated block
duplicates nothing, two files do not make a `scattered-constants` finding
that needs three, one file is not a `change-coupling` or `fix-propagation` pair, and constants
left without the function or class that used them are no unreachable
cluster. Where the
finding is attached to the definition it is about (the class, the module
variable, the attribute's one defining class, the bypassed helper, the
coalesced field, the function whose return is unused), an ignore on that
definition still suppresses the whole finding.

## Checks

### High severity — act on or justify

| Check                      | What it finds                                                                                                                                                                                                                                                               |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `unused-defaults`          | Parameter defaults to `None` but every caller always passes a value. The `Optional` is vestigial — make the param required.                                                                                                                                                 |
| `dead-code`                | Public functions with no production callers. Cross-references direct calls, imports, dict/list references and callbacks; test callers are named. Each finding lists what deleting the function also frees (helpers, classes and constants nothing else in production uses). |
| `dead-exceptions`          | Custom exception classes production code never raises, catches or names (one only tests use names those tests); or (MEDIUM) caught in production while nothing there raises them or a subclass.                                                                             |
| `compat-shims`             | `try/except ImportError` patterns left over from supporting older Python versions the project no longer targets.                                                                                                                                                            |
| `suspicious-fallbacks`     | `.get()` on module-level constant dicts with non-trivial defaults. If the key should always exist, use `[]` indexing and fail fast.                                                                                                                                         |
| `unreachable-after-return` | Code after `return`/`raise` or exhaustive `if/else` branches — dead tail code from refactoring.                                                                                                                                                                             |
| `plaintext-passwords`      | `==`/`!=` comparison on password/secret/token variables — use `hmac.compare_digest()` or hash comparison.                                                                                                                                                                   |
| `secrets-in-logs`          | Secret-shaped values (password, token, card number, CVV) flowing into logging calls — logs outlive requests.                                                                                                                                                                |
| `late-binding-closures`    | Lambda/closure in loop captures loop variable by reference — all closures see the final value.                                                                                                                                                                              |

### Medium severity — review each, fix what makes sense

| Check                           | What it finds                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `env-fallbacks`                 | `os.environ.get()` or `os.getenv()` with non-None defaults — check whether the fallback is intentional.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `dead-branch-by-callers`        | An `if` or conditional expression testing parameters (truthiness, `==`/`in`/`is None` against a constant, builtin `isinstance`) that every production caller settles the same way. Names the dead lines and each caller with its value; abstains when any caller passes something not a constant. Dismiss only for a defensive check at a genuine public boundary.                                                                                                                                                                                                                                                                                                                                              |
| `test-only-default`             | A default every production caller overrides while tests omit it (any default, not just `None`): the default exists for the tests. Make the parameter required and pass the value in the tests. Needs tests in the analysis: with tests excluded it cannot apply and reports nothing. Never reports a parameter unused-defaults, always-default-params or dead-branch-by-callers owns.                                                                                                                                                                                                                                                                                                                           |
| `literal-dispatch-param`        | An `if`/`elif` ladder or `match` on one parameter's literal values (two arms or more) whose cases are exactly the constants its production and test callers pass, and whose fallthrough raises or returns None: a table of the callers' answers, the is_even shape. Names each caller and value, each arm's line and the dead fallthrough; when numbers go in and out it says to compute the result instead. Abstains when any production caller passes something it cannot read.                                                                                                                                                                                                                               |
| `fixture-literal-in-production` | A distinctive literal (5+ characters, or a number other than 0, ±1, 10, 100, 1000, a power of two or an HTTP status) that a test feeds a function, or checks its result against, and that the function's own body compares, matches or keys a lookup table on, while no production code produces the value anywhere: the special case serves the test. A literal production names as a constant, passes or returns is its own vocabulary and is skipped. Needs tests in the analysis.                                                                                                                                                                                                                           |
| `constant-args`                 | Parameter always receives the same literal value from every caller. The value should be a default or constant.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `vestigial-params`              | Parameters declared but never referenced in the function body — with cross-file caller count showing blast radius.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `foo-equals-foo`                | Single-use locals gathered into an object — suggests bundling into a dataclass or building directly.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `duplicate-blocks`              | Structurally identical code blocks across functions (AST-normalized, so different variable names still match).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `near-duplicate-with-drift`     | Copies in different files that were once the same block and have since diverged: function bodies, statement lists or their heads and tails of 40+ normalized tokens at ≥0.8 similarity, not identical, whose erased local names and literals agree at ≥0.75 where they line up. Each cluster prints a unified diff of the drift, capped at 40 lines.                                                                                                                                                                                                                                                                                                                                                            |
| `temp-accumulators`             | `parts = []; parts.append(...); join(parts)` patterns replaceable with comprehensions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `constant-dispatch-dicts`       | Module-level `{"name": func, ...}` tables that can get out of sync — consider decorator registration.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `return-none-instead-of-raise`  | Functions returning `None` on error where callers all guard against `None`. The function should raise instead.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `pass-through-params`           | Parameters received by a function and only forwarded to another function. The intermediary's signature is vestigial.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `param-clumps`                  | Groups of 3+ parameters appearing together in 3+ function signatures — extract a dataclass.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `runtime-monkey-patch`          | Function assigned to attribute of external object at module scope.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `fossilized-toggles`            | UPPER_CASE boolean constants that make conditionals always-true/false (dead branches). `pysmelly git-history` raises one blamed unchanged for 180+ days as `frozen-feature-flag`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `dead-constants`                | UPPER_CASE module-level constants no production code references — e.g. event name constants nobody uses; one only tests use names those tests.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `dead-abstraction`              | ABCs with zero concrete implementations (MEDIUM), or exactly one (LOW) — speculative generality or single-implementer indirection.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `dead-dispatch-entries`         | Dispatch dict entries whose key strings appear nowhere else in the codebase.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `unreachable-from-entry-points` | Production code no entry point reaches (console scripts, `__main__`, click/typer, Django, Celery, Lambda, scripts; see `-v`), one finding per connected cluster with its members listed: functions that only call each other, or that only tests call. Test callers never reach. A repo with no roots reports nothing and says so on stderr; a module nothing analyzed refers to is left to `dead-code`, unless a `__main__` guard or a `#!` line 1 makes it a script, whose top-level code is then a root. Dismiss when a root pysmelly cannot see (a plugin entry point, a file you exclude) runs the cluster.                                                                                                |
| `middle-man`                    | Classes where 75%+ of methods just delegate to a single wrapped object.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `write-only-attributes`         | Record-class fields (`@dataclass`, pydantic, attrs, `NamedTuple`, `TypedDict`) production code writes and no production code reads (a test that reads one is named, not counted). Wholesale reads (`asdict`, `fields()`, `vars`, `model_dump`, `**`), `getattr(x, "name")` and template reads (`{{ x.name }}` under `templates/`) count; parse-only models are never written. `__all__`-exported: LOW.                                                                                                                                                                                                                                                                                                          |
| `write-only-record-keys`        | Dict-as-record keys production code writes and nothing that receives the dict reads, followed through assignments, calls and returns. Abstains when the dict escapes (json, a template, logging `extra=`, a DataFrame, any unresolved call).                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `swallowed-kwargs`              | A caller passes `foo=` into a `**kwargs` that never reads `"foo"` and never forwards it to anything that takes it, followed through resolved forwards up to 3 deep. Abstains when kwargs are iterated, passed on whole, or forwarded to a library.                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `return-shape-unused`           | Every production caller discards a function's return value. LOW: most callers never read one element of a returned tuple, or none reads a field of a returned record. Abstains on callbacks, handlers and fluent `self`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `write-only-globals`            | Module-level containers mutated from functions but never read anywhere — dead bookkeeping.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `refused-bequest`               | Subclass whose inherited-method overrides are mostly `pass`/`raise NotImplementedError` — wrong base class; prefer composition.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `duplicate-except-blocks`       | Identical except handlers across files, or 3+ handlers in one `try` with identical bodies (collapse into a tuple).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `return-mutable-constant`       | Function returning a module-level dict/list/set by reference — callers mutate the shared constant.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `dict-key-typo`                 | Subscript key that's written, never read, and edit-distance-1 from a defined dict key — silent-failure typo.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `isinstance-chain`              | Functions with 5+ `isinstance()` checks, or the same type-dispatch ladder repeated across 3+ functions — investigate for polymorphism or dispatch table.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `logging-config-hijack`         | `logging.basicConfig()` at import time in library modules, or unguarded `addHandler()` in function bodies.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `boolean-param-explosion`       | Functions with 4+ boolean parameters — accumulated flags suggesting decomposition.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `exception-flow-control`        | Custom exceptions raised and caught in the same `try/except` — used as goto, not error handling.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `inconsistent-error-handling`   | Callers handle one function's errors divergently (try, or a catching with). An `except` whose every path re-raises what it caught is cleanup, not handling: the exception passes through it to whatever encloses the call. A catching site inside an entry point (a declared script, a click or typer command, a view, a task, an argparse handler) is deliberate layering: the message counts it separately and it is not evidence of an unclear contract.                                                                                                                                                                                                                                                     |
| `shared-mutable-module-state`   | Module-level mutable containers mutated from other files at import time.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `orphaned-test-helpers`         | Test helper functions and unused fixtures with zero callers.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `shadowed-method`               | Diamond inheritance where multiple parents define the same method — MRO silently picks one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `broken-backends`               | Non-abstract classes where every method raises `NotImplementedError` — missing ABC base or broken backend.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `inconsistent-returns`          | Functions returning 3+ distinct types across return paths — consider narrowing the return type.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `getattr-strings`               | `getattr(obj, 'literal')` without default or `hasattr(obj, 'literal')` — stringly-typed attribute access.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `temporal-coupling`             | Methods reading `self.x` only set by another non-`__init__` method, methods dereferencing a `None`-initialized attribute unguarded, or a `None` module global set via `global` in one function and read unguarded in others — implicit call ordering.                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `feature-envy`                  | Methods accessing 3+ attributes of another parameter, more than `self` — logic belongs elsewhere.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `anemic-domain`                 | Classes with 5+ `__init__` attributes but zero non-dunder methods — data bag with no behavior.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `scattered-isinstance`          | `isinstance` checks for the same project-defined type scattered across 3+ files. Anchored at the class definition.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `shotgun-surgery`               | Same `obj.attr` read or written in 4+ files — changes to that attribute require updating many files. Anchored at the attribute's defining class when unambiguous. A call `obj.attr(...)` does not count when every project class defining `attr` annotates it with a plain value type (`str`, `Path`, `datetime`, ...), since the call is that value's own method.                                                                                                                                                                                                                                                                                                                                              |
| `dict-as-dataclass`             | Functions returning dict literals with 4+ string keys — consider a dataclass or NamedTuple.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `unnamed-constant-use`          | A literal spelled inline where a module constant of that value fills the same role (keyword, compared field, dict key, return of a function named for it) — use the constant.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `self-deprecated`               | A function or class that deprecates itself (`warnings.warn(..., DeprecationWarning)` or `PendingDeprecationWarning` as a statement of its body, `@deprecated`) and still has production callers, listed for migration.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `repeated-string-parsing`       | Same `.split(delim)[N]` in 3+ locations — ad-hoc serialization format needing a dataclass.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `bypassed-helper`               | A module-level helper of 1-3 statements whose body other production code that can import it still spells out by hand, the helper's parameters standing for whatever the copy passes (names resolved through imports; a copy of a heavy helper may differ in one small place, which the message prints). Call the helper, or make the differing place a parameter. `pysmelly git-history` dates it as `bypassed-helper-history`.                                                                                                                                                                                                                                                                                 |
| `coalesced-optional-field`      | A dataclass, pydantic or attrs field typed `X \| None` whose production reads mostly (3+ and over half) coalesce it with `or {}`, `or []` and the like — default it to the empty value. Reads are tied to the record where annotations allow.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `init-re-export-drift`          | An `__init__.py` re-export of a name the project no longer defines (`from .x import y` where `x` lost `y`, or `x` is gone; an `__all__` entry nothing binds), skipping imports guarded by `try/except ImportError`; and re-exports no analyzed module imports through the package path, grouped per `__init__.py` with each defining module. Dismiss the second kind for a published library whose external consumers import through that path. A Celery app re-exported from the project package is not reported. Files the config excludes are not read, so a re-export only excluded tests import is reported; whether those tests should count is provisional, pending the owner's call (see DECISIONS.md). |

### Low severity — informational

| Check                        | What it finds                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `single-call-site`           | Short public functions called exactly once — candidates for inlining.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `lone-dissenter`             | A defaulted parameter with 3+ production callers where all but one pass the same constant or omit it: one caller's special case pushed into shared code. Names the dissenting call and its value, and the majority's sites. Abstains on a passthrough splat or a non-constant majority, on a recursive call, and on a method whose calls cannot be tied to its class.                                                                                                                               |
| `always-default-params`      | A defaulted parameter no production caller ever passes (unused-defaults' opposite end). Delete it and use the default; the message carries the branches the default decides. Skips process entry points (`main(argv=None)` called from `if __name__ == "__main__":`) and parameters the body never reads (vestigial-params). A parameter only tests pass names those tests.                                                                                                                         |
| `singleton-collection-param` | A collection parameter (annotated `list`, `Sequence`, variadic `tuple` and the like, or an unannotated plural name) that only ever carries one element: every production caller wraps one value (`[x]`, `(x,)`, `{x}`), or the body reads it only as `p[0]`. Names each caller and what it passes, the `p[0]` lines, and the tests that alone pass more. Take the element itself. Skips fixed-shape tuples, mappings and callbacks.                                                                 |
| `internal-only`              | Public functions production only calls within their own file — candidates for `_private` naming. Test callers are named, not counted.                                                                                                                                                                                                                                                                                                                                                               |
| `trivial-wrappers`           | Functions whose body is a single return of a lookup (`d[k]`), an attribute (`obj.attr`) or a call that only forwards the parameters — candidates for inlining. A constant return is not reported: on the fleet it was always a protocol method or hook default.                                                                                                                                                                                                                                     |
| `stdlib-alternatives`        | Stdlib modules where well-known third-party libraries are better, deprecated stdlib/third-party modules, and mixed stdlib/modern usage in the same codebase, from `catalog.toml` plus the project's own patterns ("Project catalog entries").                                                                                                                                                                                                                                                       |
| `scattered-constants`        | The same literal value (string or number) in assignments, comparisons, parameter defaults or keyword arguments across 3+ files — consider a named constant. A literal passed by keyword to a call that resolves outside the analyzed code (`extra="allow"`, `metavar="FILE"`) fills a library's parameter and is not counted. A value bound to an UPPER_CASE name at module or class level is already named: it counts only with definitions of that same name (one constant copied into 3+ files). |
| `type-encoded-as-data`       | Within 3 statements of a call, the caller compares a name field of its result (`.tool.name`, `.kind`, `params["engine"]`) with another module's constant to learn which branch ran.                                                                                                                                                                                                                                                                                                                 |
| `large-class`                | Classes with 20+ methods — review for single responsibility.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `long-function`              | Functions spanning 100+ lines — review for decomposition.                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `long-elif-chain`            | 8+ branch if/elif chains comparing the same variable to literals — consider a dict or enum.                                                                                                                                                                                                                                                                                                                                                                                                         |
| `arrow-code`                 | Functions with nesting depth 5+ (if/for/while/try/with pyramid) — consider extracting inner blocks. An `elif` arm is a sibling of its `if`, not a level; `long-elif-chain` covers long chains.                                                                                                                                                                                                                                                                                                      |
| `hungarian-notation`         | Variables like `strName`, `intCount`, `lstItems` — use snake_case instead.                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `law-of-demeter`             | Attribute chains 4+ deep (`a.b.c.d`) — reaching through object internals.                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `numbered-variables`         | A run of 4+ same-stem numbered names (`item1`…`item10`) — use a list or dict.                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `reimplemented-stdlib`       | Loops that hand-roll `collections.Counter`, `defaultdict(list)`, or dict merge.                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `deep-inheritance`           | In-codebase inheritance chain 5+ classes deep — the yo-yo problem.                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `mi-method-collision`        | Multiple-inheritance class where 2+ incomparable bases define the same method and the subclass overrides none — MRO silently picks one.                                                                                                                                                                                                                                                                                                                                                             |
| `half-finished-migration`    | An old/new pair linked by name (`parse`/`parse_v2`, `ClientV2`, `fetch_legacy`, `old_`/`new_`, `parse2`) or by module (`client.py`/`client_v2.py`, `legacy/`), both still called in production. `pysmelly git-history` raises a stalled one as `stalled-migration`. A deliberate compatibility surface is the reason to keep one.                                                                                                                                                                   |

### What the caller-value checks cannot see

`unused-defaults`, `constant-args`, `dead-branch-by-callers`,
`always-default-params`, `test-only-default`, `lone-dissenter`,
`literal-dispatch-param`, `fixture-literal-in-production` and
`singleton-collection-param` judge a function by the call sites the analysis
resolves to it. Two known gaps, both recorded in docs/DECISIONS.md ("Phase 25
fleet measurement"):

- **A non-`None` default that every caller passes, tests included, is owned
  by no check.** `unused-defaults` reads only `None` defaults,
  `always-default-params` needs production to omit the parameter, and
  `test-only-default` needs a test to omit it. A `describe(context=())`
  that every caller fills in is the shape.
- **A Celery task called only through `.delay(...)` or `.apply_async(...)`
  has no callers the analysis can see,** and a decorated function is not
  judged at all, so a literal ladder in such a task is not reported (a
  `calculate_file_checksum(checksum_type)` task, say).
- **A project that excludes its tests hides every test use.** When no test
  file is analyzed, `always-default-params` and `init-re-export-drift` end
  their message by saying a test that passes or imports the name would not be
  seen; include the tests in the analysis to rule that out.

### Git history checks

Run via `pysmelly git-history`. These analyze commit patterns to detect evolutionary signals invisible to static analysis.

| Check                        | What it finds                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bug-magnet`                 | Files where a majority of commits are fixes — recurring problems suggesting structural issues.                                                                                                                                                                                                                                                                                                                                 |
| `blast-radius`               | Files whose changes drag many other files along — poor encapsulation. Co-changed test-role files do not count. Names the most frequent co-change partners and the `expected-coupling` line for the first. Collapses same-directory clusters into package-level findings.                                                                                                                                                       |
| `change-coupling`            | Files that always change together but have no import relationship — hidden coupling. An import of a name an `__init__.py` re-exports counts as importing the module that defines it. Two files in one directory that subclass the same class, defined in that directory or below it, count as related. Prints the `expected-coupling` line that declares the pair.                                                             |
| `fix-follows-feature`        | Features that reliably produce fix commits shortly after — insufficient testing or fragile design. Skips a Django settings module (one that assigns `INSTALLED_APPS`).                                                                                                                                                                                                                                                         |
| `yo-yo-code`                 | High gross churn — code being written, deleted, rewritten repeatedly. Abstractions haven't stabilized.                                                                                                                                                                                                                                                                                                                         |
| `hotspot-acceleration`       | Files whose change frequency is increasing over time — emerging hotspots.                                                                                                                                                                                                                                                                                                                                                      |
| `divergent-change`           | One file appearing in commits with very different purposes — too many responsibilities.                                                                                                                                                                                                                                                                                                                                        |
| `fix-propagation`            | Files that co-change in fix commits — fixing one tends to break the other.                                                                                                                                                                                                                                                                                                                                                     |
| `knowledge-silo`             | Files where one author dominates all changes — bus-factor risk. Skipped for projects with < 3 contributors.                                                                                                                                                                                                                                                                                                                    |
| `emergency-hotspots`         | Files that attract disproportionate emergency/hotfix activity.                                                                                                                                                                                                                                                                                                                                                                 |
| `no-refactoring`             | Files with heavy fix/feature activity but zero refactoring commits. Skips a Django URLconf (a module that assigns `urlpatterns`).                                                                                                                                                                                                                                                                                              |
| `growth-trajectory`          | Files growing rapidly within the time window.                                                                                                                                                                                                                                                                                                                                                                                  |
| `churn-without-growth`       | Many commits but stable/shrinking line count — wrong abstractions being reworked.                                                                                                                                                                                                                                                                                                                                              |
| `stabilization-failure`      | Files that repeatedly burst with activity, go quiet, then burst again.                                                                                                                                                                                                                                                                                                                                                         |
| `test-erosion`               | Source files changing much more often than their tests.                                                                                                                                                                                                                                                                                                                                                                        |
| `abandoned-code`             | Files on disk with no commits in the window while directory peers keep evolving.                                                                                                                                                                                                                                                                                                                                               |
| `conscious-debt`             | Commits that explicitly acknowledge technical debt (workaround, hack, TODO).                                                                                                                                                                                                                                                                                                                                                   |
| `frozen-feature-flag`        | A `fossilized-toggles` constant whose line blame shows it unchanged for 180+ days, with an `if`/`else` or ternary it decides still carrying both branches. MEDIUM, like `fossilized-toggles`, until a real finding earns it more; the message says how long it has been frozen. A kill switch kept for incidents is the reason to keep one.                                                                                    |
| `stalled-migration`          | A `half-finished-migration` pair whose new member blame shows 90+ days old, with no commit in the last 90 days removing a production call of the old one. MEDIUM, one level above `half-finished-migration`.                                                                                                                                                                                                                   |
| `fossil-temporary`           | `#` comments that promise their own removal (`TODO`, `FIXME`, `XXX` opening the comment or followed by `:`/`(`; hack; "for now"; "remove after"; "once … lands"; legacy or temporary only when naming code, as in "legacy shim", "temporary fix"), blamed older than 6 months. Docstrings are not read.                                                                                                                        |
| `bypassed-helper-history`    | `bypassed-helper`, dated by blame: the helper's creation commit, and which hand-written copies predate it (never migrated) or came after it. MEDIUM, like `bypassed-helper`: the dating adds context, not a stricter bar.                                                                                                                                                                                                      |
| `growing-signatures`         | Functions with 4+ parameters at HEAD whose signature 3+ commits in the window each lengthened. The message lists each step's counts with the commit's sha and subject. Independent keyword options of a public API are the reason to keep one.                                                                                                                                                                                 |
| `same-change-multiple-files` | One commit added the same 3+ significant lines (names and literals masked) to 3+ existing files, and the copies are still at HEAD. Imports, docstrings, comments, version bumps, reformats, consolidating edits and commits touching 20+ files are ignored. Copies kept independent on purpose are the reason to keep one.                                                                                                     |
| `fix-added-special-case`     | A function that already carries a `long-elif-chain`, `isinstance-chain` or `literal-dispatch-param` finding, to which 3+ fix commits in the window each added an early exit for one literal value (`if x == "N/A": return None`, no `else`) still there at HEAD. MEDIUM; lists each fix and the test it added. A distinct documented exception of an outside system is the reason to keep one.                                 |
| `growing-dispatch-ladder`    | A dispatch of 4+ branches on one subject (an `if`/`elif` chain, a run of `if`s that each exit, or a `match`; equality, identity, membership or `isinstance`) in a function with one of the same three findings, to which 3+ feature commits in the window each added a branch still there at HEAD. MEDIUM; lists each commit and the branch it added. Branches too different for one table to hold are the reason to keep one. |
| `dual-stack-dependency`      | Both libraries of a catalog pair (`requests`/`httpx`, `os.path`/`pathlib`, others, or a project pair) in production, one with under 30% of the use sites and imports older by blame (median per file). MEDIUM; lists the minority files. A site the other library cannot serve is the reason to keep one.                                                                                                                      |

When a file is flagged by 3+ different checks, pysmelly surfaces it as a **convergence hotspot** at the top of the output — the highest-confidence signal. Every finding counts, including the ones the top-10 cap hides. Hotspots rank by how many checks flag the file, then by path; the default view and `--summary` list the first ten (`=== convergence hotspots (32 file(s), 10 shown) ===`) and `--more-please` lists all.

The `reviewed` command acknowledges findings and resets the analysis window for that file. Every git-history check except those named below then analyzes only the file's post-review commits, so improvements are reflected immediately. A finding that still fires on those commits ends `— reviewed YYYY-MM-DD, pattern persists`, so the acknowledgment is visibly registered. `abandoned-code` counts the marker as a touch, so a reviewed file is not flagged while the marker is in the window. The pair checks judge two files, so a pair counts only the commits after the later of its two markers, and only when both files carry one; mark both files of a pair to acknowledge it. A marker on one file leaves the pair judged on the whole window. `frozen-feature-flag`, `fossil-temporary`, `dual-stack-dependency`, `bypassed-helper-history` and `stalled-migration` read the age of lines at HEAD, not the window's commits, and ignore it too: acknowledging one means changing the line. `--ignore-reviewed` analyzes the full history and adds no note. When the review commit ages out of the time window, the full history is analyzed again.

### Project trends

Above the findings, `git-history` prints a `Project trends` block that compares the first half of the window with the second:

```
Project trends (window halves split at 2026-07-21; informational, not findings):
  files created / deleted: 6 / 1 -> 36 / 2 (created +500%, deleted +100%)
  fix-commit ratio: 0% -> 11% (from zero)
  commits per week: 1.6 -> 5.6 (+248%)
```

The four metrics are files created and deleted, Python files per commit, the fix-commit ratio (the same fix label the bug checks use), and commits per week. They count the same commits the checks read: non-merge commits touching `*.py`. The halves are the calendar window cut at its midpoint, so a project whose history starts mid-window shows an empty first half. A metric prints only when it moves by 25% or more relative to the first half, and the block is left out when none does. Zero baselines: zero to zero is no move, and zero to anything else prints as `from zero`. Files per commit and the fix ratio are not compared when either half has no commits. A rename counts as neither a creation nor a deletion.

The block is informational. It has no severity, adds nothing to the finding count or the exit status, and `--summary` leaves it out. It describes the whole project, so `reviewed` markers and `--ignore-reviewed` do not affect it. The reasoning is in [docs/DECISIONS.md](docs/DECISIONS.md) under "Project trends".

## Output

Text output, grouped by check, one line per finding:

```
=== unused-defaults (2 finding(s)) ===
  src/myapp/deploy.py:45: deploy() param 'timeout' defaults to None but all 3 caller(s) always pass it (3 production caller(s), 0 test caller(s)). Fix: Make the parameter required: delete its `= None` default and the None handling in the body; every caller counted already passes it. Dismiss only if the function is published API that code outside the analyzed tree calls without the parameter.
    provenance of timeout: added in 4f1c2ab (deploy: optional timeout for the CLI)
  src/myapp/config.py:12: load_config() param 'env' defaults to None but all 5 caller(s) always pass it (3 production caller(s), 2 test caller(s) in tests/test_config.py:14, :30). Fix: ... Dismiss only ...
```

By default only the ten highest-confidence findings print, and a check the
cap cut short says so in its heading: `=== unused-defaults (7 finding(s), 2 shown) ===`. The run ends with `Showing top 10. Run with --more-please for all N.` when the cap left anything out. `--summary` counts every finding, so it
prints no such line.

The caller-counting checks (dead-code, unused-defaults, pass-through-params,
internal-only, single-call-site) print production and test callers
separately, and name every file they counted as a test with its call lines,
so a file the test role misclassified shows in the message. When production
is zero the clause reads `no production callers; still referenced by tests/test_x.py:12, :40`. A test caller never keeps a function alive or
public: dead-code and internal-only decide on production alone.
Code only tests use is *test-only surface*. dead-code,
unreachable-from-entry-points, dead-constants and dead-exceptions report a
function, cluster, constant or exception class that only test-role code
references, and always-default-params a parameter only tests pass; each
names the tests that use it, which go or are rewritten with it, as in
`Tests that use it, to delete or rewrite: tests/test_x.py (every test); tests/test_y.py: test_a(), TestB`. A file is marked `(every test)` when
every test pytest collects in it uses the code, a fixture that uses it
brings the tests requesting it, and a file lists five tests before counting
the rest. dead-code's list covers what the deletion frees too.
write-only-record-keys cites the tests that read a key (or the whole dict)
the same way; a key only tests read is still reported. swallowed-kwargs
counts the production callers that pass the keyword and names the test
files that pass it too; a keyword only tests pass is not reported.
return-shape-unused judges a return value by its production callers alone:
a test reading the value is named in the clause, and does not keep it.
dead-exceptions' caught-never-raised kind looks for production raises only,
and cites a test-only raise without letting it keep the handler.
write-only-attributes counts only production writes and production reads:
a field only tests read is reported with the tests that read it. Templates
are production: a name read after a dot in a file under a `templates`
directory (`{{ cut.unread_pages }}`) counts as a read, found by a static
scan of the text.

Output includes a guidance preamble for LLM consumers. Use `--no-context` to suppress it.

### Fix and dismissal sentences

Every message ends with two sentences its check registers: `Fix:`, the edit
the finding asks for (the message before it names the sites the edit
touches), and `Dismiss only ...`, the one condition under which the finding
is not worth fixing. A reviewer either makes the edit or shows that the
condition holds; "the code works" is not the condition, so it is not an
argument. Where a check cannot name its edit, the fix says what to look at.
`--list-checks` prints each check's dismissal under its description. Every
check must register both (`@check(..., advice=Advice(fix=..., dismiss=...))`),
and the dismissal must open with "Dismiss only ".

### Provenance

A finding about something vestigial (dead-code, dead-constants,
unused-defaults, vestigial-params and the caller-value checks) gets a line
under it telling how it got that way, read from `git log -S`:

```
    provenance of old_helper: added in a1b2c3d (feat: batch export), last production reader removed in d4e5f6a (refactor: stream the export)
```

The first half is the commit that added the name. The second half is the
commit that took away its last production reader, and is printed only when
the code at that commit and its parent proves it. When git cannot place the
name (no repository, a shallow clone, no commit adds it) there is no line
at all, never a blank one. Only displayed findings are traced, so
`--summary` and the findings the display cap leaves out cost no git call.

### Finding chains

One vestigial parameter surfaces as several findings: an unused default on
one layer, a pass-through on the next, a parameter no caller passes further
up. Deleting a dead function resolves the findings in everything its
removal frees. Findings that one edit resolves are printed once, together,
in a `finding chains` section after the convergence hotspots, under a title
naming the edit:

```
=== finding chains (1 work item(s), 3 finding(s)) ===
  parameter chain over 2 function(s): wait_for_drain(ecs_client, timeout) src/app/ecs.py:183, running_tasks(ecs_client) src/app/ecs.py:138
    src/app/ecs.py:138: [unused-defaults] running_tasks() param 'ecs_client' defaults to None but ... Fix: ... Dismiss only ...
    src/app/ecs.py:183: [always-default-params] wait_for_drain() never receives 'timeout' ... Fix: ... Dismiss only ...
    src/app/ecs.py:183: [pass-through-params] wait_for_drain() receives 'ecs_client' but only forwards it to running_tasks() ... Fix: ... Dismiss only ...
```

A chain's members are not printed again under their checks, so a check's
heading counts only its unchained findings. Every member still counts on its
own: the total, `--summary` and the exit status are the same as without
chains. Work a chain as one item, one edit across every touchpoint it lists.

### Comparing against a ref

`--compare REF` runs the same checks twice: on the working tree, and on REF
checked out into a temporary detached `git worktree`. It prints the totals,
then every check and file whose count changed, then each resolved and each new
finding by name:

```
Compared against main (3e620c3): 4 finding(s) then, 3 now (-1)

=== per check ===
  dead-code                      2 -> 1  (-1)

=== per file ===
  src/myapp/util.py: 0 finding(s), down from 1; fixed: dead-code x1

=== resolved since main (1) ===
  src/myapp/util.py: dead-code in old_helper (was line 40): old_helper() is never called ...
```

- Findings match on (check, file, enclosing definition), never on line
  number, so an edit that moves a finding's line is not a change. Where one
  definition has several findings of one check, a finding whose message is
  unchanged is paired first.
- Both runs use the config, check selection, excludes and severity floor of
  the current invocation, so a delta reflects the code and not a changed
  skip list. Inline `# pysmelly: ignore` comments are read from each tree's
  own source.
- The exit status is 1 when anything is new since REF and 0 otherwise, so
  `--compare main` can gate "no new smells". `--summary` keeps the counts
  and drops the two lists.
- It fails fast, exiting 1, outside a git repository, on a ref that names no
  commit, or when a target directory does not exist at REF. The temporary
  worktree is removed on every exit path, and nothing is written to the repo.
- `--compare` and `--diff` cannot be combined: one reports every finding
  resolved or introduced since REF, the other only those on changed lines.
- It covers the AST checks only. `pysmelly git-history` has no `--compare`,
  because history checks read the history up to now. Running them at REF
  would measure a different window, not a different tree.

## What pysmelly is NOT

pysmelly intentionally does **not** cover:

- **Formatting** — use [black](https://github.com/psf/black) and [isort](https://github.com/PyCQA/isort)
- **Single-file lint rules** — use [ruff](https://github.com/astral-sh/ruff) (reimplements 900+ flake8/pylint rules, extremely fast)
- **Type checking** — use [mypy](https://github.com/python/mypy) or [pyright](https://github.com/microsoft/pyright)
- **Security** — use [bandit](https://github.com/PyCQA/bandit)
- **Dead code by name-matching** — use [vulture](https://github.com/jendrikseipp/vulture) (faster, scope-agnostic approach that complements pysmelly's call-graph analysis)
- **Dependency vulnerabilities** — use `pip-audit`

pysmelly focuses on the gap: **cross-file call-graph analysis** and **design-level code smells** that require understanding how functions are actually used across a codebase.

## Using with Claude Code

pysmelly is designed as an **investigation dispatcher** for AI code review. Its findings are starting points — signals that tell Claude Code where to look and what to investigate, with enough cross-file context to act on. Claude Code reads the findings, examines the actual code, and applies judgment about what to fix.

### Setup

Run `pysmelly init` in your project to set up AI review guidance:

```bash
pysmelly init                    # creates PYSMELLY.md + adds reference to CLAUDE.md
pysmelly init docs/PYSMELLY.md   # custom path for the guidance file
```

This creates a guidance file that tells Claude Code what pysmelly is, how to run it, and — critically — how to interpret findings without dismissing them. The generated text sits between a `<!-- pysmelly-guidance HASH -->` start marker and a `<!-- /pysmelly-guidance -->` end marker; anything you add below the end marker (a findings register, repo-specific conventions) is yours.

Re-running `pysmelly init` replaces only that block — everything outside it is kept byte for byte — and never duplicates the CLAUDE.md reference. A file generated by a pysmelly older than the end marker gets one: the start marker's hash proves where the unedited generated text ends, and `init` says it added the marker there. An existing file whose block cannot be bounded that way (hand-written, or generated text edited since) is refused, exit 1, with nothing written; put `<!-- /pysmelly-guidance -->` on its own line where the generated text ends and re-run, or pass `--force` to replace the whole file.

### Review workflow

```bash
pysmelly .
```

Run it from the project root with `.` as the target: `[tool.pysmelly]` config is
read from the target directory's `pyproject.toml` and pysmelly does not walk
upward, so pointing it at a subdirectory silently drops your skip list and
excludes.

The guidance file `pysmelly init` writes tells Claude Code to **default to fixing findings**, not explaining why they're acceptable; the preamble in the text output says when that file is missing or stale. The three severity levels guide priority:

- **High**: Fix these — dead code, unused defaults, unreachable code
- **Medium**: Fix unless there's a specific reason not to (framework convention, public API). State the reason if skipping.
- **Low**: Review and fix where it makes sense — investigation pointers, not mandates

### What makes findings actionable for AI review

pysmelly findings include **cross-file context** that single-file linters can't provide:

- *"format_type is declared but never used in parse_body() — 12 callers still pass it"* — Claude Code can trace the vestigial parameter through the call chain and remove it everywhere
- *"deploy() param 'timeout' defaults to None but all 3 callers always pass it"* — Claude Code can make the parameter required and simplify the callers
- *"TASK_BEFORE_EXECUTE = 'task:before_execute' is never referenced anywhere"* — Claude Code can investigate whether the constant was superseded and delete it

The output is shaped so that "the code works" is not an answer to a finding:

- **`Fix:`** names the edit, and **`Dismiss only ...`** names the one condition that excuses not making it. The guidance tells the session to make the edit, or to suppress the finding with a rationale saying how that condition holds.
- **Provenance lines** tell how the code became vestigial: the commit that added the name and the one that removed its last production reader.
- **Finding chains** turn the several findings one vestigial parameter or one dead function produces into one work item, so the session makes one edit instead of arguing each finding apart.
- **`--compare main`** shows the session what its refactoring resolved, and that it introduced nothing new.

## Requirements

- Python 3.12+
- No external dependencies (stdlib only)
- Calls `git` (via subprocess, not shell) for `.gitignore`-aware file discovery, `--diff` mode, `--compare` (a temporary `git worktree`), and version detection. Falls back gracefully when `git` is not available.

## License

BSD-3-Clause
