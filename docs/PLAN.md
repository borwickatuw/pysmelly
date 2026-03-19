# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work (Phases 1-8), [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Phase 9: Architectural smells

More complex analysis, higher noise risk. Benefits from real-world tuning
learned in phases 7-8.

### inconsistent-error-handling

The same function is called from multiple sites, but some catch specific
exceptions while others catch broad `Exception` or don't handle errors at all.
Suggests the error contract is unclear or has drifted.

**AST approach:** For each function with 3+ callers, find the enclosing `Try`
node at each call site. Compare exception types caught. Flag when callers
diverge significantly (some catch specific, some catch broad, some don't catch).

### shared-mutable-module-state

Module-level mutable variables (lists, dicts, sets) that are both read and
written from multiple files. "Spooky action at a distance."

**AST approach:** Find module-level assignments to mutable types. Track the
variable name. Search other files for imports of that module + attribute writes
(`module.var.append(...)`, `module.var[key] = ...`, etc.). Flag variables with
cross-module mutation.

### circular-imports

Modules that import each other, creating circular dependency pressure. Often
masked by lazy imports or import-inside-function workarounds. The presence of
these workarounds is itself a smell.

**AST approach:** Build directed import graph, find strongly connected
components. Additionally detect `TYPE_CHECKING`-guarded imports and
function-level imports as circumstantial evidence. Note: pylint R0401 and
pycycle partially cover this, but the "detect workarounds as architectural
pressure" angle is novel.
