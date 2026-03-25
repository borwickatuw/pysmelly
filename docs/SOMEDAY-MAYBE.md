## User-extensible pattern catalog

Allow users to add their own stdlib-alternatives patterns via
`[tool.pysmelly]` in `pyproject.toml` or a `.pysmelly.toml` file.
The shipped `catalog.toml` covers common cases; user patterns would
handle project-specific recommendations (e.g., "use our shared client
factory instead of raw boto3.client").

## Better Output for LLMs

- **Suggestion field** — Each finding includes a concrete suggestion (e.g., "Remove the `= None` default and update callers at lines X, Y, Z").
- **SARIF output** — For IDE integration (VS Code, GitHub Advanced Security).

## Configuration

- **`pyproject.toml` support** — `[tool.pysmelly]` section for thresholds, exclusions, enabled checks.
- **Threshold overrides** — e.g., `--foo-equals-foo-threshold=5`.
- **Entry-point plugins** — Allow third-party packages to register checks via `[project.entry-points."pysmelly.checks"]`.

## cross-file-feature-envy

A function that accesses attributes/methods of objects from another module more
than it uses things from its own module. Classic Fowler smell but hard to
threshold — many functions legitimately work with objects from other modules.
Would need strong filtering (project-internal modules only, 5+ foreign accesses,
<2 own-module accesses).

## exception-wrapping-chains

Functions that catch an exception and re-raise a different type, creating
wrapping chains across call layers. When 3+ levels deep, the exception hierarchy
is likely over-engineered. Novel but complex AST work to trace the chains.

## import-only-for-isinstance

A module imports a class from another module only to use it in isinstance()
checks — never to instantiate or subclass. Suggests coupling by type-checking
rather than by interface. Interesting but high noise risk (many isinstance checks
are legitimate for error handling, serialization, type narrowing).

## hub-and-spoke modules

Modules with 8+ internal imports that aren't obvious entry points. High efferent
coupling suggests too many responsibilities. Metric-based with subjective
thresholds.

## init-re-export-drift

`__init__.py` files that re-export names that no longer exist in submodules, or
re-export names no consumer actually imports via the package path. Niche, and
re-exports might serve external consumers outside the scanned codebase.

## look for `dict-builder` function

  As for what pysmelly could detect: this is a "dict-builder function" smell — a function whose primary job is           
  conditionally assembling a dict through mutation. The signal would be: function has N if blocks that each do dict[key] 
  = ... or dict.update(...) on the same variable, with the dict passed to a single API call at the end. That's a pattern 
  that correlates strongly with "hard to read" and "should be decomposed."                                             
## Framework method override detection

Caller-aware checks (unused-defaults, constant-args, etc.) flag Django
ModelAdmin method overrides like `get_form(self, request, obj=None)` where
the signature is dictated by the framework and can't change. Could
auto-detect methods on classes inheriting from known framework bases
(ModelAdmin, View, Form, etc.) and suppress findings on overridden methods.
The challenge is knowing which base classes to recognize — a hardcoded
Django list would be fragile, and detecting "overrides a parent method"
requires resolving the MRO which pysmelly doesn't currently do.

## review trivial-wrappers value

After many rounds of suppression (decorated functions, subclass methods,
non-pure-forwarding calls, multi-caller wrappers), the check is narrow.
Remaining cases: dict lookups, attribute access, pure forwarding calls,
constant returns in non-subclasses. The pure forwarding case is the most
useful — the others are usually intentional naming abstractions. Consider
dropping dict/attribute/constant patterns and keeping only pure forwarding.

## Diff-level git history checks (Phase 10d)

Deferred from Phase 10. These require per-commit diff parsing, which is
significantly more expensive and complex than the file-level analysis
currently used by git-history checks.

- **`same-change-multiple-files`** — When a commit's diff contains
  structurally similar hunks across 3+ files (same parameter added,
  same error handling pattern), flag the DRY violation. Requires diff
  extraction, normalization, and similarity comparison.
- **`growing-signatures`** — Function parameter lists growing over time.
  Requires `git show <hash>:<path>` and AST-parsing historical versions
  to compare parameter counts. Could be lazy (only for functions with
  4+ current params).

## Expected-coupling annotations

blast-radius and change-coupling dominate on projects with intentional
subsystem cohesion (category files change together, handler files change
together). The `expected-coupling` config exists but isn't discoverable.
Options:
- A `--group` or module-level annotation so related files can be declared
  as a subsystem
- Auto-detecting directory-level cohesion and suppressing intra-directory
  coupling (files in the same directory changing together is often normal)

## Delta / trend tracking

After refactoring, pysmelly output is identical — same findings, no signal
that things improved. Options:
- `--compare` flag that diffs against a baseline
- Trend indicators: "file.py: flagged by 3 checks (down from 4 last run)"
- A `.pysmelly-baseline.json` that records finding counts per file

## blast-radius: show top co-changing files

blast-radius says "touches N other files per commit" but doesn't say which
files. Including the top 3-5 co-changing files in the message would let
the user see the coupling structure directly instead of cross-referencing
with change-coupling findings.

## fix-follows-feature classification refinement

The keyword classifier conflates cleanup with defect repair. A commit like
"Fix parameter ordering" is really a refactoring, not a bug fix. Options:
- Require the word "fix" to co-occur with bug-like words (crash, error,
  broken, regression) to count as a true fix
- Weight conventional commit prefix (`fix:`) higher than keyword matches
- Allow `refactor:` prefix to override `fix` keyword in the message body

## Project-level git health metrics

Aggregate metrics about the whole codebase rather than per-file findings:

- **Codebase sprawl** — more files being created than deleted over time
- **Growing commit scope** — average files-per-commit increasing (harder
  to make isolated changes)
- **Increasing fix ratio** — project-wide fix commit percentage trending
  upward (accumulating instability)
- **Decreasing commit frequency** — development slowing down

These are real signals but don't fit pysmelly's per-file finding model.
They'd need a different output format — maybe a "project health" summary
section in the git-history output, separate from per-file findings.
Would need to decide on thresholds and presentation before implementing.
