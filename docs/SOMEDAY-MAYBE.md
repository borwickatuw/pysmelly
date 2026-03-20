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
