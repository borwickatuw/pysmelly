# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work (Phases 1-8), [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Phase 9: Architectural smells

Two checks that analyze cross-file patterns at the architectural level.
Both are grounded in real patterns found in production codebases.

### Check 1: `inconsistent-error-handling` (MEDIUM severity)

The same function is called from 3+ sites with divergent error handling —
some catch specific exceptions, some catch broad `Exception`, some don't
catch at all. Suggests the error contract is unclear or has drifted.

#### Algorithm

1. Build function index (reuse `build_function_index` from helpers)
2. For each function with 3+ call sites (via `find_calls_to_function`):
   a. For each call site, walk up the AST to find the enclosing `Try` node
      (if any) that covers the call
   b. Classify each call site into one of:
      - **unhandled** — no enclosing try/except
      - **specific** — catches named exception types (ValueError, KeyError, etc.)
      - **broad** — catches `Exception` or bare `except:`
   c. Flag when callers diverge: at least one specific AND at least one
      broad or unhandled. Pure unhandled-vs-broad is lower signal (both
      are lazy).
3. One finding per function, anchored at the function definition

#### Message format

`deploy() has 5 callers with inconsistent error handling: 2 catch specific
exceptions (TimeoutError, ConnectionError), 2 catch broad Exception, 1
unhandled — error contract is unclear`

#### Edge cases

- **Nested try/except**: Use the innermost enclosing try that covers the call
  line range. If the call is in a nested try, use that one.
- **Multiple except clauses**: If a try has both specific and broad handlers
  (`except ValueError: ... except Exception: ...`), classify as "specific"
  since the caller is at least attempting granularity.
- **Async functions**: Handle both `FunctionDef` and `AsyncFunctionDef`.
- **Method calls**: `obj.method()` — match on the method name. A method
  called via attribute access is still a call site.

#### Noise suppression

- **Minimum divergence**: Require at least 1 specific + 1 (broad or unhandled).
  All-broad or all-unhandled isn't interesting — it's consistently lazy.
- **Skip test files**: Test callers often have different error handling needs.
- **Skip functions with only 2 callers**: Need 3+ for the pattern to matter.
- **Don't flag "broad + unhandled" only**: This is the weakest signal. The
  interesting case is when *someone* bothered to catch a specific exception,
  proving there IS a known failure mode that other callers ignore.

#### Files

- New check in `src/pysmelly/checks/callers.py` (it's a caller-aware check)
- Add to `CALLER_AWARE_CHECKS` in cli.py
- Tests in `tests/test_callers.py`

#### Test cases (~12)

- finds divergent handling (1 specific, 1 broad, 1 unhandled)
- ignores consistent specific handling across all callers
- ignores consistent broad handling across all callers
- ignores all-unhandled callers (no error handling anywhere)
- ignores functions with only 2 callers
- ignores test file callers
- nested try uses innermost enclosing
- multiple except clauses classified as specific
- method calls counted as call sites
- broad-only + unhandled-only not flagged (weakest signal)
- message includes exception type names from specific callers
- severity is MEDIUM

---

### Check 2: `shared-mutable-module-state` (HIGH severity)

Module-level mutable variables (lists, dicts, sets) that are mutated from
other files at module scope. "Spooky action at a distance." Real-world
examples: Django settings files that `import *` then `.append()` to
MIDDLEWARE and INSTALLED_APPS; registries populated by cross-module
mutations at import time.

#### Algorithm

1. **Collect mutable module-level assignments**: Walk each file's top-level
   statements. Record variables assigned to `[]`, `{}`, `set()`, or
   `defaultdict(...)`.
2. **Collect cross-file mutations**: For each file, find:
   a. `import module` or `from package import module` — then look for
      `module.var.append(...)`, `module.var[key] = ...`,
      `module.var.extend(...)`, `module.var.update(...)`,
      `module.var.insert(...)`, `module.var.add(...)`
   b. `from module import *` — then look for bare mutations of known
      variable names from that module at module scope
3. **Filter**: Only flag mutations that happen at module scope (not inside
   functions/methods). Module-scope mutation means it runs at import time —
   that's the smell.
4. **Group**: One finding per mutable variable, listing the files that
   mutate it.

#### Message format

`MIDDLEWARE (defined in config/base_settings.py:12) is mutated at module
scope from 2 other files (config/settings.py:53, config/settings.py:261)
— consider consolidating mutations or using an immutable pattern`

#### Edge cases

- **`from base import *` pattern**: Django settings use `from .base_settings
  import *` then mutate the imported lists. Need to track star imports and
  resolve which names they bring in (by reading the source module's
  top-level assignments).
- **Star import ambiguity**: If we can't resolve a star import (circular,
  missing file), skip it rather than false-positive.
- **Mutation methods**: Cover `.append()`, `.extend()`, `.insert()`,
  `.update()`, `.add()`, `.setdefault()`, `[key] = val`, `.pop()`,
  `.remove()`, `.clear()`. Also `+=` augmented assignment on lists.
- **Nested attribute access**: `settings.MIDDLEWARE.append(...)` where
  `settings` is an imported module.

#### Noise suppression

- **Only module-scope mutations**: Mutations inside functions are runtime
  behavior, not import-time side effects. Skip them.
- **Skip test files**: Test setup commonly mutates module state.
- **Skip `__init__.py` calling `.register()`**: Decorator-based registries
  (like pysmelly's own `@check` decorator) are intentional. Suppress when
  the mutation method is `.register()` or the variable is an instance of a
  class with a `.register()` method.
- **Skip known framework patterns**: Optionally suppress Django
  `INSTALLED_APPS`/`MIDDLEWARE` mutations if we want to avoid noise on
  every Django project. Or let the finding stand — it IS a smell, just a
  well-established one.

#### Files

- New module `src/pysmelly/checks/architecture.py` (distinct from callers
  or patterns — these are architectural-level checks)
- Add to `CALLER_AWARE_CHECKS` in cli.py
- Tests in `tests/test_architecture.py`

#### Test cases (~14)

- finds mutation via `module.var.append()` at module scope
- finds mutation via `module.var[key] = val` at module scope
- finds star-import followed by bare `VAR.append()` at module scope
- finds `+=` augmented assignment on imported list
- ignores mutation inside a function body
- ignores mutation inside a class body
- ignores non-mutable module-level assignments (str, int, tuple)
- ignores mutation of locally-defined variables (not imported)
- ignores test files
- ignores variables only mutated within the defining file
- multiple mutation sites grouped into one finding
- finding anchored at variable definition
- message lists mutating files
- severity is HIGH

---

### ~~circular-imports~~

Dropped — see [DECISIONS.md](DECISIONS.md). Pylint R0401 and pycycle cover this.

## Implementation sequence

1. `inconsistent-error-handling` first — builds on existing caller
   infrastructure (`build_function_index`, `find_calls_to_function`),
   lower implementation risk.
2. `shared-mutable-module-state` second — needs new infrastructure for
   import resolution and star-import tracking.
