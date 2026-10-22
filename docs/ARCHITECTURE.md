# Architecture

The high-level map: why pysmelly exists, what the parts are, and how a run
moves through them. Read this before changing the pipeline; the per-check
inventory is `uv run pysmelly --list-checks`, and the per-finding wording is
each check's own module.

## Purpose

pysmelly finds **vestigial code patterns** — code that outlived the design
that created it — and reports them as **investigation pointers** for an AI
reviewer or a human, not as mandates.

The reason it exists as a separate tool is the thing single-file linters
cannot do: **cross-file call-graph analysis**. Whether a parameter's `None`
default is vestigial depends on what every caller passes; whether a public
function is dead depends on every import, dict reference and callback in the
tree. So a finding carries cross-file context — caller counts, blast radius —
and the reviewer applies judgment with that context in hand. Formatting,
single-file lint rules, typing and security are deliberately out of scope
(see README's "What pysmelly is NOT").

**Security property: pysmelly never imports code named by its input.**
Analyzed source is only ever parsed with `ast.parse`; nothing it names is
imported, executed or looked up on `sys.path`, whatever the interpreter's
current directory. Where a check needs to know what a standard-library class
defines, `checks/stdlib_classes.py` reads it statically (below, "Framework
overrides"). DECISIONS.md, "pysmelly never imports code named by its input",
has the rule and its limits; GOVERNANCE.md has the grep that re-verifies it.

Three severities encode how much benefit of the doubt a finding gets:
`HIGH` — act on it or justify keeping it; `MEDIUM` — review each, fix what
makes sense; `LOW` — informational.

## Components

| Module                    | Responsibility                                                                                                                         |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `cli.py`                  | Click entry point; both pipelines; target/flag/config merging; inline suppression; the `init` and `reviewed` commands                  |
| `config.py`               | Finds and validates the config file for the target directory                                                                           |
| `pyproject.py`            | Parses the target directory's `pyproject.toml` for the readers that share it (config, pytest config, entry points)                     |
| `catalog.py`              | The import catalog: schema validator, named conditions, shipped patterns and pairs, and the project's own entries                      |
| `discovery.py`            | Git-aware file discovery (Python files, and the files under `templates` directories), AST parsing, git root and `--diff` changed lines |
| `templates.py`            | Static scan of a Django/Jinja template's text for the names it reads after a dot (`{{ cut.unread_pages }}`)                            |
| `context.py`              | `AnalysisContext` — the parsed trees plus the lazily-built indices every check shares                                                  |
| `pytest_config.py`        | Finds and reads the target directory's pytest config (`testpaths`, `python_files`), as pytest would                                    |
| `project_entry_points.py` | Reads the target directory's `pyproject.toml` entry points (`[project.scripts]`, `gui-scripts`, `entry-points`)                        |
| `roles.py`                | The test role: which files and definitions are tests, from the pytest config and from what definitions declare                         |
| `registry.py`             | The `@check` decorator, the registry dicts, `Finding` and its `Site`s, `Severity`                                                      |
| `checks/`                 | Every registered check, one theme per module, plus the shared AST/history/framework helpers                                            |
| `git_history.py`          | `GitHistory` — one `git log` parse, indexed by file, with lazy numstat and time slices; `GitQueries`/`LineHistory` git queries         |
| `provenance.py`           | The provenance lines under displayed findings: each finding's `Subject`s traced through `git log -S`                                   |
| `trends.py`               | The git-history `Project trends` block: the window's two halves compared, informational only                                           |
| `output.py`               | Text formatting, confidence ranking, convergence hotspots and finding chains                                                           |
| `chains.py`               | Finding chains: the findings one edit resolves (a shared parameter, a dead-code removal), joined into one work item                    |
| `compare.py`              | `--compare REF`: the checks run again on REF in a temporary detached `git worktree`, and the findings matched to report the delta      |

Five modules under `checks/` register nothing and exist to be imported by
the ones that do: `helpers.py` (shared AST utilities and the index builders),
`stdlib_classes.py` (what a builtin or standard-library class defines, read
statically without importing anything),
`framework.py` (Django/DRF/Celery/pluggy suppression predicates),
`history_helpers.py` (shared git-history gates and thresholds), and
`records.py` (record classes — dataclass, pydantic, attrs, NamedTuple,
TypedDict — recognised through `module_imports`, the sites that write a
field, and the sites that read a record wholesale, for write-only-attributes).
`module_graph.py` registers nothing either: it builds the module graph
(below, "The module graph").
`entry_points.py` registers nothing either: it builds the entry-point index
(below, "The entry-point index"), and neither does `test_surface.py`, which
names the tests that use test-only code (below, "The test-only surface").
`checks/__init__.py` imports every check module for its side effect — the
`@check` decorator runs at import time and populates the registry, so a new
check module is invisible until it is listed there. `catalog.toml` is data,
not code. Its `patterns` drive the `stdlib-alternatives` check, and its
`pairs` (two libraries that do one job) drive `dual-stack-dependency`.
`catalog._parse_catalog` is its one validator, returning a `Catalog` of
both. It reads the shipped file and a project config's `catalog` table
alike, and `parse_user_catalog` adds that a project entry may not shadow a
shipped one: no shipped pattern or pair name, and no pair with a shipped
pair's two sides. The project's entries reach both pipelines as
`AnalysisContext.user_catalog` and run after the shipped ones.
`checks/dual_stack.py` counts each pair's production use sites from the
AST (a maximal `name.attr...` load resolved through the module's imports)
and, being a git-history check, blames the import lines those uses go
through to date each side.

## Data flow — the AST pipeline

`pysmelly [TARGET...]` with no subcommand:

1. **Config merge** (`cli._load_and_merge_config` → `config.load_config`).
   Targets are resolved to absolute roots; config is read from the single
   root, or from the current directory when several targets are given.
   Config-file `exclude` and `skip` are prepended to the CLI's, `min-severity`
   applies only when the flag was left at its default, and a config `check`
   applies only when `--check` was not passed. Invalid config is an error and
   exits 1 — never a silent default.
1. **Discovery and parse** (`cli._discover_and_parse` → `discovery`). Each
   root must be a directory. `base` is the single root, or the common path of
   several. Files come from `git ls-files` (cached and untracked-not-ignored),
   falling back to an `rglob` that skips `.venv`, `__pycache__` and friends
   when git is not installed or no `.git` sits at or above the root (a
   `git archive` snapshot); that prints one stderr note per root
   (`discovery.git_lists`, `walk_note`). A root in a repository whose `git ls-files`
   fails exits 1 with git's error (`GitListError`) rather than walking, which
   would read ignored files. Paths are made relative to `base`, matched against
   the exclude patterns, and parsed; a file with a syntax error is dropped
   rather than fatal. The result is `all_trees: dict[Path, ast.Module]`, keyed
   by the `base`-relative path that every `Finding.file` later quotes.
   The files under a `templates` directory are found the same way
   (`discovery.get_template_files`), excludes applied, and read as text:
   `cli._template_attributes` collects every name they read after a dot
   (`templates.attribute_names`), skipping a file that is not UTF-8. The AST
   pipeline sets the result on the context as `template_attributes`; a
   template is production code the AST never sees, and write-only-attributes
   counts its reads.
1. **pytest config** (`cli._load_pytest_config` → `pytest_config`). Read
   from the same directory as pysmelly's own config; see "The test role"
   below. A config file that cannot be parsed, or a key of the wrong type,
   is an error and exits 1.
1. **Context** (`context.AnalysisContext`). Wraps `all_trees`, the verbose
   flag and the pytest config. Nothing is indexed yet.
1. **Check selection**. Either the union of every check named by a
   (repeatable) `--check`, or every registered check whose category is
   `ast` and whose name is not in the merged skip list.
1. **Run** (`registry.run_check`). Each check is a plain function of
   `AnalysisContext` returning `list[Finding]`; the results are
   concatenated. A finding anchored in a test-role file is dropped here,
   unless the check is about tests (see "The test role" below). Checks never
   read files or call git themselves — everything they need is on the
   context.
1. **Suppress** (`cli._apply_suppression`). Source lines of the files that
   have findings or sites are re-read and `# pysmelly: ignore` /
   `# pysmelly: ignore[check-name]` on the finding's line, or the line
   above, drops it. An ignore speaks for its own line only, so on a site of
   a multi-site finding (see `Finding` below) it removes that site: the
   finding is re-rendered from the sites left, re-anchored on the first one
   left when its anchor was the site removed (`registry.may_anchor` applies,
   as in **Run**), and dropped when none are left or when `render` returns
   None because the sites left fall below the check's own floor.
1. **Filter** (`cli._apply_filters`). The severity floor, then `--diff`:
   findings are kept only where the line appears in `git diff` against the
   given ref. A diff that cannot be taken is an error, not an empty diff.
   It runs after suppression because suppression can move an anchor.
1. **Chain** (`chains.chain_findings`). The findings left that one edit
   resolves are joined into a chain, set on each as `Finding.chain` (below,
   "Finding chains"). It runs last, so a chain holds only findings that
   are reported; it adds and drops none.
1. **Output** (`output.format_text`). Convergence hotspots first — a file
   flagged by three or more different checks is the highest-confidence signal,
   counted over every finding and ranked by check count then path, the first
   ten listed under the cap and in `--summary`
   — then findings grouped by check. Unless `--more-please` is passed the
   display is capped at the highest-confidence findings, ranked by severity
   and then by how rarely the check fired; a section heading still gives its
   check's count and says how many the cap shows. Each displayed finding that names
   `subjects` gets a provenance line per subject git can place (below,
   "Provenance"). Exit status is 1 when anything was
   found, 0 otherwise. Under `-v` the header under the file count lists the
   test-role files and why each took the role, then the entry-point roots
   and why each is one (`cli._verbose_header`), in both pipelines.

**`--compare REF`** (`compare.run_comparison`, Phase 29-4) replaces the
Output step. Steps 1 to 9 run twice over the same merged config and check
selection: on the target roots, and on the same roots inside a checkout of
REF that `compare.ref_worktree` adds as a temporary detached worktree and
removes on every exit, including a failed `git worktree remove` (the
directory is deleted anyway) and an exception out of `add` itself (git is
asked whether it registered the path, and the registration is removed).
`match_findings` pairs the two runs' findings on (check, file, innermost
enclosing definition, `<module>` outside any), never on line, preferring
pairs whose message is unchanged; what is left over is resolved (only at
REF) or new (only now). `format_comparison` prints the totals, the
per-check and per-file deltas, then each resolved and new finding. Exit 1
when anything is new. It cannot be combined with `--diff`, covers the AST
pipeline only, and writes no state.

Guidance is prepended unless `--no-context` is passed. It is one hint, not a
lecture: `cli._check_guidance_status` hashes the guidance templates and looks
for that marker in the project's guidance file, so the preamble says "run
`pysmelly init`" exactly when the file is missing, unreadable, or stale.
The guidance file is the one CLAUDE.md's reference names
(`cli.GUIDANCE_REFERENCE_RE`: `Read [..](PATH)` followed in the same
paragraph by `before running` … `pysmelly`, line breaks allowed), else
`./PYSMELLY.md`. A CLAUDE.md that links a `PYSMELLY.md` in no shape the
regex reads gets its own hint naming the sentence to use; pysmelly never
guesses a second location.

## Data flow — the git-history pipeline

`pysmelly git-history [TARGET...]` is a second pipeline over the same parts.
Config merge, discovery and parse are shared verbatim (the config load also
picks up `git-window` and `commit-messages`), and so is everything from
running the checks onward — the same filter, suppression and output path.
History checks therefore see the parsed AST as well as the history.

What differs is the middle. The target must be inside a git repository
(`discovery.get_git_root`, exiting 1 otherwise), the context is built with
a `context.GitSettings` (the git root, window, commit-message mode and
expected coupling), and the
checks that run are exactly those registered with `category="git-history"`.
The first check to touch `ctx.git_history` triggers the single `git log`
parse; everything after that is index lookups.

After the checks, `trends.project_trends` builds the `Project trends` block
(nothing for an empty window). `trends.split_window` cuts the window at its midpoint
(from `GitHistory.window_start`, the cutoff git itself applies, to the moment
of the run) and counts each half; `GitHistory.lifecycle_by_commit` supplies
the created and deleted files from one more `git log --name-status` run.
`trends.format_trends` keeps the metrics that moved by 25% or more and
`output.format_text` prints them under the window header. The block is not a
`Finding`: it carries no severity, never enters the counts or the exit
status, and `--summary` leaves it out. A git query behind it that fails raises
`GitHistoryQueryError`, which the CLI reports and exits 1 on, rather than
printing a block built from missing data.

Two commands support the loop rather than the pipeline:
`pysmelly git-history reviewed PATH...` writes an empty acknowledgment commit
whose message carries `pysmelly: reviewed <path>` markers, which the parser
reads back as a per-file review date so subsequent runs analyze only
post-review commits; `--ignore-reviewed` discards those dates for one run.
The checks that narrow to post-review commits carry
`history_helpers.notes_persisting_review`, which appends
`reviewed YYYY-MM-DD, pattern persists` to a finding on a reviewed file.
The pair checks (change-coupling, fix-propagation) count co-changes through
`history_helpers.coupled_pairs`, which narrows a pair to the commits
after the later of its two markers only when both files carry one, and carry
`notes_persisting_pair_review`, which notes a finding only when every file it
is sited on is reviewed.
The blame-age checks (`frozen-feature-flag` and `fossil-temporary` in
`history_age.py`, `dual-stack-dependency`, `bypassed-helper-history` and
`stalled-migration`) read the age of lines at HEAD, not the window's
commits, so they ignore the marker. `abandoned-code` counts the marker as a touch, so
it cannot fire on a reviewed file and carries no note.
`tests/test_history_review.py` pins these lists against the registry.
`pysmelly init` writes the guidance file whose staleness the preamble above
reports, and adds a reference to it in the project's `CLAUDE.md`. The
generated text is a block from the hash-stamped start marker to
`cli.GUIDANCE_END_MARKER`; on an existing file `init` replaces that block
only (`cli._generated_block_span`), so whatever the repo keeps outside it —
the findings register PYSMELLY-REVIEW Practice 8a puts there — survives
byte for byte. A marker counts only alone on its line (registers quote it in
prose). A file with one start marker and no end marker predates the end
marker: `init` then wrote the start marker line and the template whose
`sha256[:12]` it carries (mdformat may have added one blank line between
them), so `cli._hash_proven_block_end` takes the one line end at which the
text after the marker hashes to that value as the block's end, splices there,
and says it added the end marker. No such line end (the block was edited since
generation), more than one, or any other file without exactly one start
marker followed by one end marker is refused before anything is written,
`CLAUDE.md` included, unless `--force`; the staleness hint says so, rather
than sending the reader to a command that will refuse.

## Key data structures

**`Finding`** (`registry.py`) — what every check returns: `file` (the
`base`-relative path as a string), `line`, `check` (the registered name),
`message`, `severity`. There is no other finding type; the output formatter
knows nothing else. The output prints `Finding.text`, not `message`: the
message, then `Fix:` and the check's fix sentence, then its dismissal
sentence. Both sentences come from the check's registration
(`@check(..., advice=Advice(fix=..., dismiss=...))`, Phase 29-1), held in
`CHECK_FIXES` and `CHECK_DISMISSALS`. `registry._sentences` refuses a
registration whose fix is not a capitalized sentence or whose dismissal
does not open with `DISMISSAL_OPENING` ("Dismiss only "), so a check
without both does not register. The fix names the edit and the message
names its sites; the dismissal names the one condition under which the
finding is not worth fixing. `--list-checks` prints the dismissal under
each check. A finding that reports one pattern across several places
(`stdlib-alternatives`, `duplicate-blocks`, `duplicate-except-blocks`,
`param-clumps`, `scattered-constants`, `scattered-isinstance`,
`shotgun-surgery`, the same-index form of `repeated-string-parsing`, the
cross-file form of `getattr-strings`, the recurring-ladder form of
`isinstance-chain`, `shared-mutable-module-state`,
`same-change-multiple-files`, `dual-stack-dependency`, a `change-coupling`
pair or cluster, a `blast-radius` cluster, `bypassed-helper` and
`bypassed-helper-history`, `coalesced-optional-field`,
`write-only-record-keys`, `swallowed-kwargs`, `return-shape-unused`, an
`unreachable-from-entry-points` cluster) also carries `sites` (`Site`:
file and line) and `render`, which rebuilds the message from any subset of
them; checks build it with `registry.multi_site_finding`. It anchors on its
first site, or on the subject's definition when there is one (the class
`scattered-isinstance` names, the variable `shared-mutable-module-state`
names, the one class that defines a `shotgun-surgery` attribute, the
directory of a coupling cluster, the helper a `bypassed-helper` copy spells
out, the field `coalesced-optional-field` names, the function whose return
`return-shape-unused` judges). An ignore
on a definition anchor speaks for the definition and suppresses the whole
finding; on a site it removes that site.

A finding about a definition, a parameter or a literal names it in
`subjects`, the one record of what a finding is about: each a `Subject` of
`name`, the defining `file`, a `scope` (None for a module-level function or
constant, the enclosing function's lines for a parameter or a literal), a
`parameter_of` (the function's name for a parameter, None otherwise) and
`vestigial`, whether the finding says it is vestigial. That flag is what
decides provenance: only vestigial subjects are traced, because a
parameter that is still read (only forwarded, or always given one value)
has no "last production reader removed" to find. Chains ignore the flag
and join on any parameter. dead-code,
dead-constants, unused-defaults, vestigial-params and the caller-value
checks of Phase 25 name vestigial subjects; constant-args and
pass-through-params name the parameter they judge as not vestigial, since
it is still read. `registry.parameter_subject` builds a parameter,
`callers.param_subjects` several of one function's, and
`callers.literal_subjects` a function's literals. `subjects`, like `sites`,
never takes part in equality, and site narrowing keeps it.

**Provenance** (`provenance.py`, Phase 29-2). Before formatting, the CLI
asks `output.printed_findings` which findings `format_text` will print one
by one (none under `--summary`; those the cap keeps, and every member of a
finding chain one of them belongs to), hands just those to a
`ProvenanceReader` over the analyzed root, which traces their vestigial
subjects only, and puts its lines on them
as `Finding.notes` (`provenance.with_notes`), which `format_text` prints
under each. A `--summary` run and the findings the cap leaves out cost no
git call. When some
displayed finding names a subject, the reader finds the git root (none: no
lines), checks that HEAD exists and the clone is not shallow
(`GitQueries.has_complete_history`; otherwise no lines), and runs one
`GitQueries.token_commits` per distinct subject, side by side. A
module-level name is looked for in every Python file under the analyzed
root, and only production files count; a scoped name in its own file
through renames (`--follow`), and only when every whole-word occurrence of
it there sits inside its scope, since otherwise the file's history of the
text is not the subject's. `trace_addition` walks the commits oldest
first, counting whole-word occurrences in their hunks: `added` is the
commit that last took the count from none to some. A falling count is not
a removed reader (a docstring mention goes, two reads become one), so the
removal is read from the code: a reader is a load of the name in the
parsed file (any load, bare or as an attribute, for a module-level name;
a bare load in its function's body for a parameter; a literal has none).
The removal clause needs HEAD to hold no production reader, and the
newest commit since `added` that changed the reader count to have taken
its parent's readers down and left none anywhere (`GitQueries.blob` at the
commit and its parent, `files_with_word` and `lines_at` for the whole
tree). The line is `provenance of NAME: added in <sha> (<subject>), last production reader removed in <sha> (<subject>)`,
the second half only when that is proven, and nothing at all when no
commit added the name. Subjects pass through `display_text`, which drops
control, format and separator characters and truncates. A git failure is an
error (exit 1), never a line left out.

**`Severity`** (`registry.py`) — `HIGH`/`MEDIUM`/`LOW`, used both as the
filter floor and as the primary sort key in output.

**The registry** (`registry.py`) — four parallel dicts keyed by check name:
`CHECKS` (the function), `CHECK_SEVERITY`, `CHECK_DESCRIPTIONS`,
`CHECK_CATEGORIES`. The `@check(name, severity=, description=, category=)`
decorator writes all four. Category is what separates the two pipelines.
`@check(..., about_tests=True)` also puts the name in
`TEST_SUBJECT_CHECKS`, the checks whose findings may sit in test-role files.

**`AnalysisContext`** (`context.py`) — the one object a check receives. It
holds `all_trees`, `verbose`, the git settings, the pytest config and `template_attributes` (the names the templates read; empty unless the CLI set it), and exposes expensive
indices as lazy properties so they are computed once per run rather than once
per check:

| Property                                                                        | What it holds                                                        |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `function_index`                                                                | Top-level public function definitions by name — file, line, AST node |
| `call_index`                                                                    | Every call site by callee name — file, line, AST node, receiver kind |
| `import_index`, `value_references`, `dotted_string_suffixes`, `decorator_names` | Via `references`: four views built in one pass over every tree       |
| `package_dirs`                                                                  | Directories, as path parts, that hold an analyzed `__init__.py`      |
| `module_rebindings`                                                             | Per file, every site that rebinds or deletes a module-level name     |
| `module_imports`                                                                | Per file, every name its import statements bind, in walk order       |
| `subscript_string_keys`                                                         | Per file, every string constant read as a subscript key (`x["k"]`)   |
| `raise_sites`                                                                   | Every `raise` site by the class it raises (variables resolved)       |
| `caller_values`                                                                 | Per function, its call sites and what each passes per parameter      |
| `entry_points`                                                                  | Every root the code is run from, with why (`EntryPointIndex`)        |
| `unit_graph`                                                                    | Production top-level units and their references (`UnitGraph`)        |
| `roles`                                                                         | The test role of files, definitions and any path (`TestRoles`)       |
| `module_graph`                                                                  | Each module's imports, bound names and re-export origins (below)     |
| `import_graph`                                                                  | Per file, the other analyzed files its imports load (`module_graph`) |
| `parent_map(tree)`                                                              | Child-to-parent AST map, memoized per tree                           |
| `git_history`                                                                   | `GitHistory`, or `None` when the run has no git root                 |

The four reference indices share a builder (`helpers.build_reference_indices`)
and one property, `references`, holding the `ReferenceIndices` it returns,
because they are four answers to one walk: "where else does this name appear
— as an import, as a value, inside a dotted string, as a decorator?" That is
what makes a check able to ask whether a function nobody calls is nevertheless
referenced. `function_index` is deliberately narrow — top-level, public,
undecorated, non-method, and not a function pytest collects (a `test*`
function in a test-role file) — because the checks built on it reason
about free functions; `call_index` is deliberately wide, recording every
call and tagging its receiver so those checks can discard same-named method
calls.

`module_rebindings`, `module_imports`, `subscript_string_keys` and
`raise_sites` (Phase 23) replaced walks of every tree once per candidate.
dead-constants and
fossilized-toggles read `module_rebindings` to ask whether a constant is
ever rebound (`helpers.is_constant_reassigned` is now a lookup in it).
return-none and the context-manager resolver resolve a call site's callee
from `module_imports`, read through each module's `module_graph` node,
finding the candidate sites through `call_index`.
dict-as-dataclass reads `subscript_string_keys`, and dead-exceptions asks
`raise_sites` whether a candidate class is raised anywhere and, for its
caught-never-raised kind (Phase 27-5), whether it or a subclass is raised in
production code. `raise err` is keyed by what the nearest scope that assigns
`err` builds (`err = E(...)` gives `E`), so the index stays the one answer
to "who raises this"; a handler's `except E as err: raise err` re-raises and
stays keyed `err`. Each is built by
one `helpers.build_*` function in one pass per tree, keyed by file when the
question is per module and by name when it spans the codebase, so a check
that asks about N candidates costs one walk, not N. A new index follows the
same shape: a `helpers.build_*` builder, a lazy property here, and a per-file
key when the question is asked per module. DECISIONS.md ("Index the
per-candidate tree walks (Phase 23)") has the measurements.

`checks/constant_roles.py` (Phase 28-3) builds its `ConstantIndex` from
the same indices: `module_rebindings` decides which UPPER_CASE names are
constants, `module_imports` resolves `NAME`, `mod.NAME` and
`pkg.mod.NAME` to the module that defines them, and `import_graph` decides
which files a constant is offered to. unnamed-constant-use and
type-encoded-as-data share it; neither walks a tree per candidate.

`checks/migrations.py` (Phase 28-4) pairs definitions by name markers
(`_v2`, `V2`, `_legacy`, `old_`, `new_`, a bare digit) and by marked
module paths (`client_v2.py`, `legacy/`), then resolves each member's
callers through `callers._resolving_calls`, so a pair counts only when
production calls both. `find_migration_pairs` is shared by the AST check
half-finished-migration and its git-history counterpart
stalled-migration, which ages the pairs. self-deprecated reads the same
caller resolution for definitions that deprecate themselves.

`checks/bypassed_helpers.py` (Phase 28-2) turns a small helper's body into a
shape (node types, with its parameters as wildcards, its locals as
consistently renamed names, and every other name resolved through
`module_imports` to a dotted path) and buckets every production expression
and statement by a cheap key (node type, and a call's resolved callee), so a
helper is compared with its bucket only. bypassed-helper reports the copies;
its git-history twin bypassed-helper-history dates them through
`LineHistory.files_line_commits`. `checks/optional_fields.py` (Phase 28-2,
coalesced-optional-field) reads optional record fields from
`records.RecordIndex` and their reads from `records.attribute_reads`, which
ties each `x.attr` receiver to record classes as write-only-attributes ties a
wholesale read.

`checks/record_keys.py` (Phase 27-1, write-only-record-keys) is the one
check that follows a value rather than a name. From each dict display a
production function builds, it classifies every use: a key read or write, a
wholesale read, a hand-off it follows (a local name bound once, a call that
`records.RecordIndex` resolves to a project function, a `return` to the
call sites `callers._resolving_calls` finds, a list the dict is appended
to), or an escape. Any escape in production, or more than four hops of
call and return, and the dict reports nothing. The module docstring lists
the escape set.

`checks/swallowed_kwargs.py` (Phase 27-3, swallowed-kwargs) works the other
way round from the caller-value index: from each call that passes an
explicit keyword, to the one project function it reaches. A module-level or
nested function's calls come from `caller_values` (calls resolved by the
21-3 rule); a constructor, `Cls.m()`, `self.m()` for a method only one
class defines, and `super().m()` in a single-base class resolve through the
module's imports (`records._ModuleNames`) and single-base project
ancestors. A keyword no named parameter binds lands in `**kwargs`; the
callee's uses of that name decide whether it is read, may be read (any use
that is not a constant key), or forwarded, and forwards are resolved the
same way for up to `MAX_FORWARD_HOPS` (3). Any unresolved forward abstains.

`checks/return_shape.py` (Phase 27-4, return-shape-unused) reads
`caller_values` from the callee's side: for each module-level function, and
each method whose name one analyzed class defines, it classifies what every
production call site does with the value (discard, read in parts, use
whole), following a name bound to the result through the enclosing
function. A method's calls count only when the receiver ties them to the
class (`Cls.m()`, or `self.m()` in the class or a project subclass), since
the index gives a method every attribute call of its name. Returned records
are recognised with `records.RecordIndex`.

**The module graph** (`module_graph`, Phase 30-1) is the one resolved import
graph of the analyzed modules (`checks/module_graph.py`). Its `nodes` hold,
per file, a `ModuleNode`: the dotted `module` and `package`, `imports` (every
dotted path an import statement anywhere in the file names, as
`ImportedPath`s, marking a from-import's source and whether the path is
absolute), `bindings` (the file's `module_imports` entry), `scope` (each
name the module scope binds, in source order, with its line, the import
record when an import binds it, and whether it sits in a `try` body that
catches a failed import, `optional`, or in an `except` handler, `fallback`)
and `exports` (a static `__all__`, each name with its line). The graph also
holds `by_module` (each dotted module name
with its files, under the analyzed root and under pytest's `pythonpath`
roots) and `loads` (per file, the analyzed files its imports load), which
`ctx.import_graph` exposes. `origin(module, name)` follows a name through
re-exports, star imports and submodules to where it is defined, and
`reexports(path)` gives every from-import binding of a file with its origin;
an origin is `DEFINED`, `MODULE`, `EXTERNAL`, `MISSING` or `UNKNOWN` (a module
`__getattr__`, a dynamic `__all__`, a star import from outside the analysis).
An `except` fallback yields to the import its `try` guards (Phase 30-2), so
`try: from .opt import f` / `except ImportError: f = None` resolves `f` to
`opt`. `defining_files(path)` follows every from-import in a file (stars
excepted) to the files that define what it binds, and
`enclosing_package(dotted)` names the deepest analyzed package above a
module path, which tells a deleted project module from a library's.
`resolve(module, dotted)` follows a dotted name read in a module, each
attribute looked up on the analyzed module reached so far. Each node's
`class_bases` lists the dotted name every base of a module-level class
spells, and `base_classes(path)` resolves them to the analyzed classes they
name (Phase 30-3). return-none's caller resolution
(`_CallTarget.defined_in`, `_CallMatcher.in_module`), change-coupling's
import test (`history_coupling._imports_module` over `ModuleNode.imports`,
plus `defining_files` for the re-export credit, `by_module` for a dotted
string naming the other module (`history_coupling._string_loads`) and
`base_classes` for the shared-parent credit) and `init-re-export-drift`
(`checks/imports.py`) read it.

**The caller-value index** (`caller_values`, Phase 25-1) answers "what does
each caller pass for this parameter?" for every function in the analyzed
code. `callers.build_caller_values` builds it from `call_index` and
`module_imports`; it lives beside the resolution in `callers.py` rather than
in `helpers.py`, and it is a `CallerValueIndex`:

- `by_file[path]` lists that file's functions (`ast.walk` order) as
  `CallerValues`; `of(node)` finds one by its definition node and raises
  `KeyError` for any other node. Each carries `file`, `node`, `owner` (the
  class whose body defines a method, else None), `callers` (a
  `callers._Callers`, the same production/test split `_callers_clause`
  prints) and `params`. Checks outside `callers.py` read an entry through
  its queries rather than `node`, `owner` and `callers`: `name`, `line`
  (the `def`), `is_method`, `class_name`, `production_callers`,
  `test_callers` (in the order of each `ParamValues.tests`), `call_sites`
  (production, then tests) and `encloses(file, line)` (a call inside the
  definition, so a recursive one). `function_calls()` yields every call of
  a function outside a class body.
- `params` follows the signature, leaving out a method's receiver and the
  `*args`/`**kwargs` collectors. Each `ParamValues` has `name`, `kind`
  (`ParamKind`: positional-only, positional-or-keyword, keyword-only),
  `default` (the expression, None when there is none) and two tuples,
  `production` and `tests`, with one `PassedValue` per call site in the
  order of `callers.production` and `callers.tests`.
- A `PassedValue` is `kind`, `literal`, `passthrough`, `file`, `line` and
  `argument` (the expression passed; None when the parameter is omitted or
  a passthrough).
  `kind` is a `PassedKind`: `LITERAL` (an `ast.Constant`, with its `repr`
  in `literal`), `NONE` (`literal` is `"None"`), `OMITTED` (the default is
  used) or `OTHER` (anything else, including `-1`, which is a unary
  operation, and every passthrough). A parameter no argument names, with a
  `*args` at or before its position or a `**kwargs` in the call, is a
  passthrough: `OTHER` with `passthrough` set, because the splat may fill it
  or leave the default. Any other `OTHER` is an explicit argument, so the
  parameter is passed. A passthrough is unknown and never supports a
  finding: unused-defaults does not count it as passing the parameter, and
  constant-args does not count it as a literal.

Which calls reach a function:

- **Functions outside a class body** (module-level or nested) are called by
  the calls that resolve to them under the Phase 21-3 rule (`_CallMatcher`,
  through `callers._CallResolver`, which `_resolving_calls` also uses). A
  same-named function in another module, a method call of the same name and
  a library's function are not callers.
- **Methods** are called by every attribute call of their name
  (`obj.m()`, `self.m()`, `Cls.m()`, `mod.Cls.m()`) that does not resolve to
  a function, since the 21-3 rule binds by module and import, not by
  receiver. Two classes defining the same method name both receive the
  same calls; a check that needs one owner per call requires the name to be
  unique, as `migrations.py` does. A class's constructor calls (`Cls(...)`)
  are not attributed to `__init__`.
- **Binding.** Every function in a class body takes its first positional
  parameter as the receiver unless it is a `staticmethod`. A bound call
  (`obj.m(x)`, or `Cls.m(x)` for a `classmethod`) fills the receiver, so
  its positions start one later. An unbound call through the defining class
  (`Cls.m(obj, x)`) passes the receiver as its first argument. A
  positional-only parameter is never bound by keyword.
- **Framework overrides** (Phase 25-2). `CallerValues.overrides_unresolved_base`
  is set on a method that may override a name defined on a base the
  analysis cannot resolve, and every caller-value check that judges
  methods skips it: that base dictates the signature.
  `inheritance.OverrideResolver` decides it from the hierarchy index the
  inheritance checks share (`_index_classes`, `_project_base_defs`,
  `_ancestors`), built over every analyzed file. An analyzed base is
  followed to its own bases. A builtin or standard-library base, its path
  read through `helpers.ModuleResolver`, is read by
  `stdlib_classes.StdlibClasses` without importing anything: a bare name
  from the already-loaded `builtins`, a compiled-in module
  (`sys.builtin_module_names`, such as `_typing`) only from the object
  already in `sys.modules`, and any other standard-library module by
  parsing its source, located under `sysconfig.get_paths()["stdlib"]` by
  path arithmetic, following the class's bases through that file and
  other standard-library files to a bounded number of hops (parsed files
  are cached per run). The method overrides the base only if that class
  (or one it inherits, `object` aside) defines the name. A class the
  static reading cannot follow (a C extension with no source, a binding
  it does not model) is unresolved. Any other base (third-party,
  assigned, ambiguous, or not a name) is unresolved, and every method name
  but a `_`-prefixed non-dunder counts as a possible override.
  DECISIONS.md ("Framework overrides skip the caller-value checks") has
  the measurement.

`checks/caller_decided.py` (Phase 25-3) reads it for dead-branch-by-callers
and always-default-params. A function is judged only when the index sees
all its callers: at least one production caller, not decorated beyond
`staticmethod`/`classmethod`/`override`, not a dunder, a stub or a dictated
signature, not referenced as a value, decorator or dotted string, not
`overrides_unresolved_base`, not called only from an
`if __name__ == "__main__":` guard (the process entry point), and, for a
method, no production call through an imported name other than its class
(`re.search` is not `Panel.search`). `_Body` walks the function's own scope
for `if` statements and conditional expressions and for every rebinding of
a parameter (assignment, `del`, handler, import, `match` capture, walrus,
nested `def`/`class`, `nonlocal`); a rebinding before a test, or in a loop
around it, makes the parameter unknown there. Each test is evaluated per
production site with three-valued logic over the constants the index
records (an omitted parameter takes its default when that is a constant).
A branch decided by a parameter that always-default-params reports is cited
in that finding and not reported again (DECISIONS.md, Phase 25-3).

`checks/caller_split.py` (Phase 25-4) reads the same judged functions
(`caller_decided._functions`) for lone-dissenter and test-only-default.
lone-dissenter keys each production site's value for a defaulted parameter
(a constant, or the default when omitted; anything else is no key, and a
passthrough anywhere abstains) and reports the one site that differs when
at least three agree on one key and only it does, unless it is a recursive
call (`encloses`) or passes the default's own expression. Findings group
the parameters one dissenting call varies. test-only-default reports
defaulted parameters every production site passes explicitly and some
test-role site omits, and returns nothing when no test-role file was
analyzed. A method is judged by either only when its name is unique among
analyzed classes and every production call's receiver ties it to the
class (`Cls.m()`, `self.m()`/`cls.m()` in the class or a project
subclass, matched by class name); a test call that is not tied is left
out. A parameter deciding a dead-branch-by-callers branch belongs to that
finding, and one test-only-default reports is not also a lone dissent
(DECISIONS.md, Phase 25-4).

`checks/literal_dispatch.py` (Phase 25-5) reads the same subjects
(`caller_split._subjects`, so methods are tied the same way) for two
is_even checks. literal-dispatch-param finds, in the function's own scope,
an `if`/`elif` chain or `match` of two arms or more on one parameter whose
every test compares it with constants, with a fallthrough (the `else`, the
wildcard case, or, for a top-level chain whose arms all leave the function,
the statements after it) that only raises or returns None. It reports when
every production site's value is a known constant
(`caller_decided._known_production`) and those values plus the tied test
sites' known values equal the handled set; a rebound parameter or one
deciding a dead-branch-by-callers branch is not judged.
fixture-literal-in-production first classifies every distinctive literal
in production files: deciding (an `==`/`!=`/`in`/`not in` operand, a
`case` value, or a key of a dict display looked up by a computed key in
the same module, directly or through a bare name it is bound to) or
producing (any other use; a bare string statement is neither). A literal
with any producing use is dropped. A tied test call feeds the literals
among its arguments, and those its enclosing `assert` compares with; one
the called function's own body decides on (`encloses`) is reported, one
finding per function.

`checks/singleton_param.py` (Phase 25-6) reads the same subjects for
singleton-collection-param. A parameter is judged when the signature
declares it a collection: an annotation naming `list`, `set`, `Sequence`,
`Iterable` and the like or a variadic `tuple` (any arm of a union or
`Optional`, string annotations parsed), or, unannotated, a plural name; a
fixed-shape `tuple[A, B]`, a mapping or any other annotation is not. Every
production site must pass it explicitly
(`caller_decided._explicit_production`; an omission or a passthrough splat
abstains). It reports when every site passes a one-element list, tuple or
set display, or when the body mentions the parameter only as `p[0]` (any
other load, store or index abstains), one finding per parameter, citing
the tied test sites that pass a display of two elements or more
(DECISIONS.md, Phase 25-6).

unused-defaults and constant-args read it. unused-defaults judges the
positional parameters with a literal `None` default and counts every caller,
tests included. constant-args reads the production values of the
positional-or-keyword parameters of a `function_index` function. Both
leave out the parameters they never judged (keyword-only ones, and for
constant-args positional-only ones too); widening either is a behaviour
change for its own sub-phase. Phase 25's later checks read the same index;
28-2's bypassed-helper does not, since a copy of a helper's body is not a
call and passes no arguments (DECISIONS.md, Phase 28-2).

**The entry-point index** (`entry_points`, Phase 26-1) answers "what runs
this code from outside?" for reachability (26), finding chains (29), module
layering (30) and entry-point error handling (33).
`checks/entry_points.build_entry_point_index` builds it from the
`[project.scripts]`, `[project.gui-scripts]` and
`[project.entry-points.<group>]` references that
`project_entry_points.load_project_entry_points` reads from the target
directory's `pyproject.toml` (the directory config is read from; nothing
above it), passed to the context as `declared_entry_points`, and from the
production files' ASTs. It is an `EntryPointIndex`:

- `roots` is a tuple of `EntryPointRoot`, sorted by file and line, one per
  root and kind (a function that is a console script and a click command
  is two). Each has `kind` (`RootKind`), `reason` (the config key, the
  decorator as written and what it resolves to, or the call site), `target`
  (the dotted path), and `file`, `qualname` (dotted through enclosing
  classes and functions), `line` and `node` locating the definition.
  `in_file(path)`, `is_root(path, qualname)` and `unresolved` are the
  lookups; `describe()` is the `-v` listing.
- The kinds: console script, gui script and entry point (declared;
  resolved through the analyzed modules and a package's re-exports, and kept
  with no `file` when nothing analyzed defines them, or with no `qualname`
  when a group entry names a whole module); `__main__` (what a top-level
  `if __name__ == "__main__":` block calls, through
  `helpers.main_guard_blocks`, and what a `__main__.py` calls at module
  level; from Phase 35-1 also the guarded module or `__main__.py` itself,
  as a script module); click (`@click.command`/`@click.group`, and `.command`/`.group` on
  a click group defined in analyzed code); typer (`.command`/`.callback` on
  a `typer.Typer()`); django url (the views a URLconf's
  `path`/`re_path`/`url` name, and viewsets registered on a DRF router);
  django command (the `Command` class in `management/commands/`); celery
  task (`@shared_task`, `.task` on a `Celery()`); dispatch decorator
  (`framework.framework_dispatch_decorators`: `@receiver`, `@hookimpl`, a
  `.task` on an app built where the index cannot see); and, from Phase 26-2,
  what Django builds or loads by name (django admin: a `django.contrib`
  admin subclass or `@admin.register`; django app config; django template
  library, each function registered on a `django.template.Library()`
  instance by decorator or `register.filter(name, fn)`; django settings (a
  module assigning `INSTALLED_APPS` or, from Phase 26-4, one that
  star-imports a settings module to override it) and django urlconf, whole
  modules; django middleware; django migration), a
  `wsgi.py`/`asgi.py` `application`, a
  signal handler `<signal>.connect(...)` registers, a lambda handler
  (`handler(event, context)`), and script (what the top-level code of a
  module no production module imports calls, and from Phase 35-1 a module
  whose line 1 is a `#!` line, as a script module). The shebang is read
  with the file (`discovery.parse_file`), since the AST keeps no comments,
  and reaches the index as `ctx.shebang_files`.
- Every name resolves through the module's imports (`helpers.ModuleResolver`),
  so aliases count and a library's same-named decorator does not; nothing
  is imported. The AST kinds read production files only, because code
  reached only from tests is what reachability exists to find. Apart from
  declared roots, a reference that resolves to no analyzed definition (a
  library view, `sys.exit`) is not a root.
- A declared reference that does not parse as an entry-point reference (or
  a script's that names no function, or a table that is not a table) exits
  1 naming the key, as a malformed pytest config does: the installer refuses
  it too, and skipping it would drop a declared root silently.
- inconsistent-error-handling (Phase 33-2) reads the function and class
  roots as `callers._EntryPointScopes`, adding the argparse handlers a
  `.set_defaults(...)` keyword names (resolved by the 21-3 rule), and sets
  aside a catching call site any of them encloses.

**Reading the index from a later check** (Phases 29 and 33). The index
is the one answer to "what runs this code"; a check reads it rather than
finding roots of its own. It is built lazily on first access, once per run,
from production files only, and nothing in it is imported or executed. A
root has one of four shapes, told apart by its fields:

| shape                    | `file` | `qualname` | `node`                    | from                                                                            |
| ------------------------ | ------ | ---------- | ------------------------- | ------------------------------------------------------------------------------- |
| a definition             | set    | set        | function, class or assign | every AST kind, and a declared reference naming an object                       |
| a whole module           | set    | `None`     | `None`                    | django settings and urlconf, an entry-point group's module                      |
| a script module          | set    | `None`     | `None`                    | `__main__` (a guard or `__main__.py`) or script (`#!`); `script_module` is true |
| declared, never analyzed | `None` | `None`     | `None`                    | a `[project.scripts]` target outside the analyzed tree                          |

A whole module is loaded by name, so every definition it holds is live; a
script module is run, so only its top-level code is, and what that code
reaches. `EntryPointRoot.script_module` tells the two apart.

Which lookup each consumer wants:

- *Is this definition run from outside?* `is_root(path, qualname)`, with
  `qualname` dotted through enclosing classes. A method of a root class is
  not a root itself: the class is (a Django `Command`, a `ModelAdmin`).
- *Does this module hold an entry point?* `in_file(path)`, whole-module
  roots included. A module-level check that should leave entry-point
  modules out asks this. Filter the roots by `kind` when only some kinds
  should count: a template-tag library is a root, but it is not a
  program's front door.
- *Which code is an entry point's own body?* The `node` of each function
  and class root, as `callers._EntryPointScopes` reads them for Phase 33's
  inconsistent-error-handling. That class adds argparse handlers, which the
  index does not hold; a third consumer that needs them should move that
  kind into the index rather than copy the class.
- *What does deleting this free, and what can nothing reach?* Do not walk
  the roots again. `ctx.unit_graph.roots` is the index mapped to units, and
  `cascade.removal_cascade` never frees a root, so Phase 29's finding chains
  read the cascade and inherit the rule.
- *Is a declared root missing?* `unresolved` lists the declared roots no
  analyzed file defines. A check that needs every root resolved abstains
  when it is non-empty, as reachability abstains when there is no root.

This shape is the public contract. `RootKind` values print in `-v` and in
DECISIONS.md, so a new kind goes at the end of the enum, with a `reason`
naming the config key, decorator or call site, and a test. A new kind can
only shrink unreachable clusters and dead-code cascades and widen what
inconsistent-error-handling sets aside; measure those three on the fleet
when one lands. Two older readers still find `__main__` callees by name
with `helpers._find_main_entry_points`, a stand-in from before the index:
`helpers.build_function_index` (so dead-code, among others, never judges a
function named like one) and return-shape-unused (DECISIONS.md, Phase
27-4).

**Reachability** (`checks/reachability.py`, Phase 26-2,
unreachable-from-entry-points) walks forward from those roots over a graph
of units: each top-level function, class and simple assignment of a
production file, and each module's remaining top-level code, with methods
and nested functions folded into their top-level unit. Edges are the calls
`caller_values` resolves, every name or attribute chain that resolves to
an analyzed definition (through `DefinitionResolver`, the index's own
resolver, or a module whose name ends with the imported one), import
statements, dotted strings, path-loaded modules, `getattr` literals,
`__all__`, `globals()` and registering decorators; a module loaded by name
reaches every definition it holds. Test-role files contribute edges that
are never followed but that join the clusters one test holds alive. A
repo with no resolved root abstains with a stderr note; a module nothing
analyzed refers to is left out, unless it is a script module (Phase 35-1),
whose top-level code is a root. The module docstring has the full rule and
DECISIONS.md (Phase 26-2) the measurement. The graph is a `UnitGraph`, built
once per run as `ctx.unit_graph`. A `Unit` is `(file, name)` for a top-level
definition and `(file, None)` for a module's remaining top-level code.
`members` maps each definition unit to a `Member` (file, name, line,
`MemberKind`), `edges` holds the production references,
`test_edges` the test-role ones, `test_sites` the `(file, line)` of each
test-role reference per target unit, `roots` the rooted units and
`referrers` the reverse of `edges`.

**The removal cascade** (`checks/cascade.py`, Phase 26-3) answers "what else
dies when this goes?" over the same graph. `removal_cascade(ctx, unit)`
returns a `Cascade`: `removed` holds the unit given, and `freed` lists the
definitions (functions, classes, module-level assignments) left with no
production referrer, as `Member`s sorted by file and line. The candidates
are what the removed unit reaches through other definitions. A candidate
that something else refers to is kept, and so is everything a kept
one reaches. Cycles are freed whole, module top-level code and entry-point
roots are never freed, and test-role references keep nothing alive.
`Cascade.describe(subject, reported)` renders `Removing f() also frees N: ...`, marks the members in `reported` and lists at most
`MAX_CASCADE_LISTED`. dead-code appends it to every finding, with the other
dead-code subjects as `reported`. An unreachable-from-entry-points cluster
is closed under the cascade, so that check prints none. The finding chains
(below) call `removal_cascade` with the unit each dead-code finding removes
and read `freed`. DECISIONS.md (Phase 26-3) has the rule's alternatives and
the measurement; it takes one unit since Phase 25-6, and chains need no
more, since each dead-code finding removes one.

**Finding chains** (`chains.py`, Phase 29-3) answer "which findings does one
edit resolve?", so the output prints them once as one work item. A chain
holds two findings or more, and a finding is in one chain at most.
Cascade chains come first: a dead-code finding removes its subject's unit
(the one dead-code cascades from; a function inside a module-level
`if`/`try` is no unit and roots no chain) and what
`removal_cascade` frees, and every finding whose anchor and sites all lie in
top-level statements made only of removed units joins it (an unreachable
cluster, a trivial wrapper, the fields of a freed class). Parameter chains
come from the findings left. Their nodes are the parameter subjects
(a `Subject` with a `parameter_of`), vestigial or not, keyed on file, scope
and name, which unused-defaults, constant-args, pass-through-params,
vestigial-params, always-default-params, dead-branch-by-callers,
lone-dissenter, test-only-default, literal-dispatch-param and
singleton-collection-param name. Findings on the same parameter
join, and so do findings on two parameters when the first's function is a
conduit for it: every read of it there is a bare-name argument
(`CallerValues.names_passed`) filling a flagged parameter at a production
call, and a method's call counts only as `self.m(...)`/`cls.m(...)` from
its own class, because `caller_values` matches method calls by name.
`output.format_text` prints a section `finding chains` after the convergence
hotspots, one title per chain a displayed finding belongs to (each
function named once, its parameters together) and every member under it
with its provenance `notes` (the cap's leftovers too), and leaves the
members out of their check sections. `--summary`, the totals and the exit
status read findings, so chains change none of them, and `Finding.chain`
takes no part in equality.

**The test-only surface** (`checks/test_surface.py`, Phase 26-4) answers
"which tests use it?" for code only test-role code uses: the tests that go
or are rewritten when it goes. A
`TestSurface` lists, per test file, the top-level test units (the
`UnitGraph`'s units, read from `statements`, which holds every analyzed
file's top-level statements, test-role ones included) that reference the
targets, and marks a file `(every test)` when every test pytest collects in
it (a top-level `test*` function, or a `test*` method of a top-level class)
uses them. Everything starts from `(file, line)` sites:
`surface_of_units(ctx, units, files)` reads `UnitGraph.test_sites` (per
production unit, every test-role reference into it), and `files` adds
the test files a check's own name-based evidence counted, named even when
no unit in them is tied to a target; `surface_of_sites` takes the sites a
check cites (write-only-attributes keys its test reads by the first line of
each test function, method, or class remainder). A site belongs to its
top-level unit and, in a class, to the method spanning it; a method naming
a using method (`self.helper`, a fixture parameter) uses it too, and a site
in the class body or in a method nothing names (`setUp`) covers the whole
class. A top-level `@pytest.fixture` takes the tests and methods requesting
it by parameter (in its file, or under a `conftest.py`'s directory), to a
fixed point. `describe()` renders
`tests/test_x.py (every test); tests/test_y.py: test_a(), TestB.test_c()` (five
tests a file, then a count), and `listing()` the same without the
every-test mark, for tests that change
rather than go. dead-code (over the removed function and what it frees),
unreachable-from-entry-points (over the cluster), dead-constants and
dead-exceptions end their findings with `Tests that use it, to delete or rewrite: ...`;
always-default-params names the tests that pass the parameter, and
write-only-attributes the tests that read the field.

**The test role** (`roles.py`, Phase 24). Whether code is a test comes from
two layers that never compete, and never from a filename alone.

1. The runner's config decides files. `pytest_config.locate_pytest_config`
   takes the first of `pytest.ini` (even with no `[pytest]` section),
   `.pytest.ini`, `pyproject.toml` with `[tool.pytest.ini_options]`,
   `tox.ini` with `[pytest]` and `setup.cfg` with `[tool:pytest]` in the
   target directory, pytest's own order; nothing above it is read. A file
   that does not match is passed over unread beyond telling so, as pytest
   passes it over. From the match it reads `testpaths` and `python_files`
   (a TOML list, or a shell-split string); pytest's default applies to a
   key left unset (`test_*.py *_test.py`; no `testpaths` means anywhere
   under the rootdir). A module is a test when it matches `python_files`
   and, if `testpaths` is set, sits under one of them (glob segments and
   `**` allowed) or is named by one. `testpaths` names where pytest
   searches, so location alone gives no role: a module under a test path
   that matches no pattern is test support only on the import evidence
   below, and a project whose `testpaths` names its own package keeps the
   modules its production code imports production (Phase 34). A `conftest.py` is a test
   only under a test path.
1. Definitions declare themselves, and this layer only adds the role. A
   class whose bases reach `unittest.TestCase` (or a known library
   subclass of it, `roles.TESTCASE_BASES`) through imports, aliases and
   analyzed modules; a `@pytest.fixture`; a `@pytest.mark.*`; a `test_`
   function at module or class level in a module importing pytest. A
   definition nested in a declared one carries its reason, and a file
   holding any declaration takes the role, citing the first one.

The import graph (`ctx.import_graph`, the module graph's `loads`) finds test support outside
the layers, among modules under a test path when `testpaths` is set and
among all modules when it is not. Under a test path, a module that only
test-role modules import takes the role on that alone (`testpaths` is the
runner saying where tests live). With no `testpaths`, it also has to import
`pytest` or `unittest` (`roles.TEST_RUNNER_MODULES`) or a test-role module,
or every module importing it has to have taken the role this way: there,
an entry point, a library's public module, or a module beside its own test
is imported only by its tests and is production. Either way this repeats
to a fixed point. The cost: where `testpaths` names the package itself, a
production module only tests import is read as test support, so its
test-only surface is not reported (DECISIONS.md, Phase 34-1). The graph knows a module by its path from
the analyzed root less any source root, and by its path from each pytest
`pythonpath` entry, and tries an unresolved top-level name beside the
importing file (pytest's default import mode). A module named by a
test-role module's `pytest_plugins` (`helpers.build_plugin_graph`) and imported by no production module takes the role
as well. See DECISIONS.md, "The test role replaces filename test-ness
(Phase 24-2)".

`ctx.roles` (`roles.TestRoles`) holds the answer for the run: `files` and
`definitions`, each with why; `of(path, qualname)`, why one definition is a
test (its own declaration, its enclosing one's, or its file's); and
`is_test(path)` for any path, analyzed or not.

`ctx.roles.files` gives the config reason first, then the declaration,
then the import evidence. Under `-v` both pipelines list them.

Every check reads the role. Nothing decides test-ness from a filename: the
filename rules (`test_*`, `*_test.py`, `conftest.py`, a `tests/` or `test/`
directory) are gone, and a `test*` function name means "collected by
pytest" only in a test-role file (`pytest_config.is_collected_test_name`).
How the role is used:

- **Subject.** Test-role files are never the subject of a finding.
  `helpers.source_trees(ctx)` and `history_helpers.source_files(ctx)`, the
  walks most checks start from, leave them out. `registry.run_check` drops
  any finding still anchored in one, unless the check is about tests
  (`TEST_SUBJECT_CHECKS`: `orphaned-test-helpers`, `test-erosion`).
- **Evidence.** Test-role files stay in the call graph. single-call-site
  and inconsistent-error-handling split callers by role, and so do the
  `helpers.*_in_production` reference helpers: a test-role caller is a
  test caller, which never justifies production code.
  change-coupling and fix-propagation skip pairs with one test-role file,
  and blast-radius counts no test-role file among the files a change drags
  along (36-3).
  write-only-attributes counts no write or read made in a test-role file,
  and dead-constants and dead-exceptions no reference: code only tests use
  is reported as test-only surface, naming those tests (Phase 26-4).
  write-only-record-keys counts reads and writes made once the dict has
  been returned to a test-role caller as the test's, and cites them;
  production handing the dict to a test-role helper stays production.
  `test-erosion` takes a module's tests to be the test-role files that
  import it directly, and counts their commits once each.
- **Messages** (Phase 24-3). The caller-counting checks (dead-code,
  unused-defaults, pass-through-params, internal-only, single-call-site)
  split their call sites with `callers._split_callers` and print one
  clause, `callers._callers_clause`: `N production caller(s), M test caller(s) in tests/test_x.py:12, :40`, or `no production callers; still referenced by ...` when production is zero. Every file counted as a test
  is named, so a misclassified file is visible in the finding. dead-code
  and internal-only decide on production alone, as single-call-site does:
  a test caller never keeps a function alive or public. unused-defaults
  and pass-through-params decide on every caller and only report the split.
- **Tests parsed or not.** orphaned-test-helpers judges fixtures only when
  some test-role file holds a collected test (a module-level `test*`
  function or a `test*` method of a module-level class). When the tests
  were excluded and only conftest or helpers are parsed, no fixture is
  reported (DECISIONS.md, "Skip fixture findings in
  `orphaned-test-helpers` when test files are excluded"). What pytest
  calls without a request is never reported: an `autouse=True` fixture,
  and a `pytest_` hook in a module pytest registers as a plugin (a
  `conftest.py`, a module `pytest_plugins` names, a `pytest11` entry point
  in `pyproject.toml`, read into `PytestConfig.entry_point_plugins`).
- **Paths outside the analyzed tree.** Git history names files that are
  deleted, excluded or unparseable. `ctx.roles.is_test` gives such a path
  the role from layer 1 alone (`roles._config_role`), since there are no
  definitions or imports to read. A deleted `tests/test_x.py` is a test,
  and a deleted helper is production, under `testpaths` or not.

**`ctx.near_duplicate_pairs`** (built by `checks/near_duplicates.py`) —
similar-but-not-identical code units (function bodies, nested statement
lists, their heads and tails, single statements) paired by normalized-token
similarity and by agreement of the names and literals where the shapes line
up, each pair with its two `CodeSpan`s and the aligned line ranges between
them. It is a lazy property like the other indices, so every caller in a
run shares one pairing.
`near-duplicate-with-drift` reports its clusters; a check that needs to know
"where else does this block live" reads the same pairs.

**`GitHistory`** (`git_history.py`) — the history-side equivalent of
`AnalysisContext`. Construction runs one `git log` over the window and builds
reverse indices: `commits_for_file`, `last_modified`, `authors_for_file`, and
`reviewed_at` from the review markers. Line-level stats (`file_stats`) come
from a second `git log --numstat` run deferred until a check asks, as are
`time_slices`. Derived properties — `message_quality`, `distinct_authors`,
`median_commit_size`, `commits_per_slice`, `is_coarse_grained` — let checks
gate themselves on whether the repository's history can support the question
they ask.

**`GitQueries`** (`git_history.py`) — the git queries that read the whole
history rather than a window: `token_commits(token, pathspecs, follow=)`,
one `git log -S` with its hunks and each commit's abbreviated sha and
subject (`CommitRef`), newest first in topological order;
`blob(rev, path)`, `files_with_word(rev, token, pathspecs)` and
`lines_at(rev, paths)` (several files through one `git grep`), which read
a revision's code; and `has_complete_history()`. The provenance lines are
its reader. It holds the one `_query` runner, so it and `LineHistory` share
one cache and one error path. `discovery.run_git` decodes git's bytes as
UTF-8 with no newline translation, and every reader splits on `\n` alone:
a lone `\r` in a subject, an author name or a source line is text, not a
line break.

**`LineHistory`** (`git_history.py`) — the line-level queries, a
`GitQueries` that adds the window, reached as
`GitHistory.line_history` and so present only inside a git root. Building it
runs nothing; each query runs on first use, for candidates a check has
already found, because none is affordable over a whole repository. One
method, `_query`, runs git for all of them (through `discovery.run_git`,
list args and `core.quotepath=off`), caches the output by its exact argument
list, and raises `GitHistoryQueryError` when git fails rather than answering
"no history". Paths are git-root-relative and line numbers are the file at
HEAD, not the working tree: `head_source(path)` and `head_tree(path)` give
that file (None when HEAD has no such path), so a check finds the lines it
blames there rather than in the working tree. `commit_hunks(sha)` parses `git show -U0` into
`Hunk`s; `function_history(path, qualname)` finds the definition in the HEAD
blob's AST (decorators included) and runs `git log -L` over its lines in the
window, returning `FunctionRevision`s newest first — the range follows
renames of the file but not a move into another file; `line_ages(path, lines)` runs one `git blame -w -M -C` and gives each line's author time,
so a reformat or a move does not make a line young; `line_commits(path, lines)` reads the
same blame into `BlamedLine`s (sha, author time, subject), the commit behind each line. Its first readers are the
blame-age checks: `frozen-feature-flag` takes `patterns_misc.find_fossilized_toggles`'
candidates (the AST run has no history, so the raised toggle is a
git-history check of its own) and `fossil-temporary` tokenizes each file's
HEAD text for marked comments. A check with many candidates asks in batches,
because one git call per candidate is what makes line history unaffordable:
`ranges_history(ranges)` tracks many HEAD line ranges, in any number of files,
through one `git log -L` (`function_history` is its one-range case), and
`edit_hunks(shas, paths)` gives many commits' hunks to existing files from
one `git show -U0`. `changes_matching(pattern, since)` runs one `git log -G`
for every name a check asks about and gives each matching commit's hunks
(stalled-migration's "was a caller removed" question).
`lines_at("HEAD", paths)` reads several HEAD files through one
`git grep`, and `head_changed_paths()` names the files whose working tree
differs from HEAD, so a check can take HEAD line numbers from the analyzed
tree where they agree; `head_paths()` lists every path HEAD holds, so an
untracked file is known to have nothing to blame. The commit-shape checks in `history_shape.py` are
their readers: `growing-signatures` follows every 4+-parameter signature in
one `ranges_history` call, mapping older file names through
`GitHistory.renames` (read from the same `git log --name-status` run as
`lifecycle_by_commit`), and `same-change-multiple-files` compares the
normalized hunks of every commit that edited 3+ files. The function ratchets
in `history_ratchets.py` (`fix-added-special-case`, `growing-dispatch-ladder`)
read each candidate function's guards and ladders from its HEAD AST, follow
every candidate in one `ranges_history` call shared by both checks, and tie
each added `if`/`elif`/`case` line to its function by the `def` above it in
the hunk and to a HEAD branch by its `ast.unparse`d test. Their gate (a
`long-elif-chain`, `isinstance-chain` or `literal-dispatch-param` finding
on the function) is applied last: the candidates are narrowed by each gate
check's per-function condition (`structure.count_elif_chain`,
`patterns_misc.isinstance_call_count`, `function_dispatch_type_set`,
`literal_dispatch.literal_ladders`), and the gate checks themselves run only
when a function's history reaches a bar. A new kind of query
is one more method on `_query`, as `GitQueries.token_commits` (Phase 29's
`git log -S`) is.

**`Hunk`** (`git_history.py`) — `old_file`, `new_file` (None for
`/dev/null`), git's `old_start`/`old_count`/`new_start`/`new_count`, and the
prefixed `body`; `added`, `removed`, `new_side`, `old_side`, `old_lines`, `new_lines` derive from
them. **`FunctionRevision`** — a `sha` and its hunks over the function's
range; `added` is what the commit added to it, `source` the function as the
commit left it.

**`CommitInfo`** (`git_history.py`) — `hash`, `date`, `message`, `author`,
`files`. `classify_commit` labels a message (fix, feature, refactor, debt,
emergency) from conventional-commit prefixes, emoji and keywords; the labels
are what the bug, coupling and growth checks count.

**`FileStats`** (`git_history.py`) — `total_insertions`, `total_deletions`,
`commit_count` for one file, whole-window or post-review.

**`TimeSlice`** (`git_history.py`) — one uniform period of the window:
`start`, `end`, `commits`, `files_touched`, `files_by_category`. Sequence
checks (acceleration, stabilization, fix-follows-feature) read slices rather
than raw commits.

## Adding a check

Write a function taking `AnalysisContext` and returning `list[Finding]` in
the `checks/` module whose theme it matches, decorate it with `@check(...)`
including its `advice=Advice(fix=..., dismiss=...)`, add its README row
under its severity (`tests/test_docs.py` checks the tables), and — only if
the module is new — add it to `checks/__init__.py`. A finding about a
vestigial name lists it in `subjects` with `vestigial=True` so it gets a
provenance line. Shared AST
work belongs in `helpers.py`, framework-shaped false-positive suppression in
`framework.py`. A check that needs a new cross-file index adds it as a lazy
property on `AnalysisContext` rather than walking every tree itself.
