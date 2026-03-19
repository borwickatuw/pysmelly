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

## `parallel-implementations`

Functions with the same name/signature in different files, or if/else branches that both produce the same type. Hard to detect generically — needs a clearer scope before attempting.

## `boolean-parameter-smell`

Functions with boolean parameters where the first statement is `if flag:` — suggests the function should be two functions. Likely noisy in practice (many legitimate uses of boolean params).

## `stale-comments`

Comments referencing function/variable names that no longer exist in the codebase. Interesting idea but fragile — comments aren't structured, and name matching would produce false positives on partial matches, English words, etc.

## `remainder-flags`

Detect argparse patterns where REMAINDER is used alongside flags that will be swallowed. Very niche — only relevant to CLI-heavy codebases using argparse.REMAINDER.

## look for `dict-builder` function

  As for what pysmelly could detect: this is a "dict-builder function" smell — a function whose primary job is           
  conditionally assembling a dict through mutation. The signal would be: function has N if blocks that each do dict[key] 
  = ... or dict.update(...) on the same variable, with the dict passed to a single API call at the end. That's a pattern 
  that correlates strongly with "hard to read" and "should be decomposed."                                             
## review trivial-wrappers value

After many rounds of suppression (decorated functions, subclass methods,
non-pure-forwarding calls, multi-caller wrappers), the check is narrow.
Remaining cases: dict lookups, attribute access, pure forwarding calls,
constant returns in non-subclasses. The pure forwarding case is the most
useful — the others are usually intentional naming abstractions. Consider
dropping dict/attribute/constant patterns and keeping only pure forwarding.
