# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work, [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Candidates for new checks

Cross-file patterns that no existing tool catches well. All require
analyzing multiple files together — single-file linters can't do these.

### dead-exceptions

Custom exception classes defined but never raised or caught anywhere in the
codebase. These accumulate after error handling refactors — the exception class
stays but nothing uses it.

**AST approach:** Find all `ClassDef` nodes whose bases include Exception/Error.
Scan all `Raise` and `ExceptHandler` nodes across files. Flag classes with zero
raise + zero catch. Low false-positive risk.

### dead-dispatch-entries

Entries in dispatch dicts or registry mappings whose keys are never looked up
anywhere in the codebase. Natural extension of the existing
`constant-dispatch-dicts` check.

**AST approach:** Find dispatch dicts (string keys -> function values). For each
key, search all files for that string constant being used in a lookup. Flag
entries whose key never appears elsewhere. Conservative — only flag when
lookups use traceable constant strings (keys from external input won't match,
which is the right default).

### inconsistent-error-handling

The same function is called from multiple sites, but some catch specific
exceptions while others catch broad `Exception` or don't handle errors at all.
Suggests the error contract is unclear or has drifted.

**AST approach:** For each function with 3+ callers, find the enclosing `Try`
node at each call site. Compare exception types caught. Flag when callers
diverge significantly (some catch specific, some catch broad, some don't catch).

### scattered-constants

The same literal value (string, number) appears in 3+ files as a bare constant.
This is the cross-file version of "magic number" — ruff PLR2004 only does
single-file.

**AST approach:** Collect all `ast.Constant` values across files, group by
value, flag values appearing in 3+ distinct files. Filter trivially common
values (0, 1, True, False, None, empty string, short strings). Restrict to
assignments and comparisons (not log messages or format strings).

### scattered-isinstance

The same `isinstance(x, SomeType)` check appears across 3+ files for a
project-defined class. Suggests the type differentiation should be pushed into
the type hierarchy (method dispatch) rather than scattered across consumers.

**AST approach:** Find all `isinstance()` calls, group by the type argument.
Flag project-defined types (those appearing as `ClassDef` in the scanned files)
checked in 3+ distinct files. Ignore stdlib types.

### shared-mutable-module-state

Module-level mutable variables (lists, dicts, sets) that are both read and
written from multiple files. "Spooky action at a distance."

**AST approach:** Find module-level assignments to mutable types. Track the
variable name. Search other files for imports of that module + attribute writes
(`module.var.append(...)`, `module.var[key] = ...`, etc.). Flag variables with
cross-module mutation.

### orphaned-test-helpers

Utility functions in test files (conftest.py, test helpers) with zero callers.
Test codebases accumulate dead helpers faster than production code. Vulture
catches some via name-matching but is imprecise with pytest fixtures.

**AST approach:** Build function index for test files. For non-fixture
functions, check for callers. For `@pytest.fixture` functions, check if the
fixture name appears as a parameter in any test function across the test suite.

### circular-imports

Modules that import each other, creating circular dependency pressure. Often
masked by lazy imports or import-inside-function workarounds. The presence of
these workarounds is itself a smell.

**AST approach:** Build directed import graph, find strongly connected
components. Additionally detect `TYPE_CHECKING`-guarded imports and
function-level imports as circumstantial evidence. Note: pylint R0401 and
pycycle partially cover this, but the "detect workarounds as architectural
pressure" angle is novel.
