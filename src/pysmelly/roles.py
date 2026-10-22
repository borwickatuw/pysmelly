"""Test role — which analyzed files and definitions are tests.

The role comes from two layers that never compete:

1. **The runner's own configuration decides files.** pytest's ``testpaths``
   and ``python_files`` are read from the target directory's pytest config,
   located the way pytest locates it (``pytest_config``). When
   ``testpaths`` is set, a module under one of them is a test file when
   its name matches ``python_files``, which is what pytest collects there,
   and a file a ``testpaths`` entry names outright is one too. ``testpaths``
   names where pytest *searches*, not what it finds, so a module under a
   test path that matches no pattern is not a test file by location alone:
   a project that points ``testpaths`` at its own package keeps the
   production modules its production code imports production. When
   ``testpaths`` is unset, a module
   is a test module when its name matches ``python_files``. A
   ``conftest.py`` is a test file only under a test path (under the
   rootdir when ``testpaths`` is unset, since pytest then collects from
   the rootdir).
2. **Definitions declare themselves.** A ``unittest.TestCase`` subclass, a
   ``@pytest.fixture``, a ``pytest.mark`` decorator, or a ``test_`` function
   in a module that imports pytest is a test definition, and its file takes
   the test role. This layer only adds the role; it never takes it away.

The import graph finishes the job for test support modules: the helpers,
fixture builders and ``__init__.py`` files pytest does not collect. With
``testpaths`` set it reads only modules under a test path, since pytest
looks nowhere else; with none it reads every module. Under a test path, a
module that only test-role modules import is test support: ``testpaths``
is the runner saying where tests live, so no further evidence is needed.
With no ``testpaths``, such a module takes the role only when it also
shows it is built on the test runner: it imports ``pytest`` or
``unittest`` or a test-role module, or every module importing it is test
support itself. Without ``testpaths``, being imported only by tests is not
enough on its own: a CLI entry point, a library's public module, or
``pkg/billing.py`` beside its ``pkg/test_billing.py`` is imported only by
its tests and is production. The cost of the testpaths rule: where
``testpaths`` names the package itself, such a production module is test
support, which hides its test-only surface (fewer findings, not false
ones). Either way this repeats until nothing changes, so a helper's helper
follows. A module any production module imports stays production. A module that a
test-role module's ``pytest_plugins`` names, and that no production module
imports, is test support too: pytest loads it as a plugin of the tests.

Neither a filename nor a location is evidence on its own:
``src/quiz/test_knowledge.py`` is production when ``testpaths`` does not
reach it and it declares nothing, and ``tests/helpers.py`` is production
when nothing imports it. A path that is not analyzed (git
history names deleted files) has no declarations or importers to read, so
only layer 1 applies to it (``TestRoles.is_test``).

``TestRoles`` holds the result for a run; ``AnalysisContext.roles`` is it.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pysmelly.checks.helpers import (
    PYTEST_FIXTURE,
    ModuleImport,
    ModuleResolver,
    build_plugin_graph,
    filepath_to_module,
)
from pysmelly.pytest_config import PytestConfig, is_conftest

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext

# Base classes that are ``unittest.TestCase`` or subclasses of it in
# libraries pysmelly never parses. A class reaching one of these through its
# bases, across analyzed modules and import aliases, is a test class.
TESTCASE_BASES = frozenset(
    {
        "unittest.TestCase",
        "unittest.IsolatedAsyncioTestCase",
        "unittest.case.TestCase",
        "unittest.async_case.IsolatedAsyncioTestCase",
        "django.test.TestCase",
        "django.test.SimpleTestCase",
        "django.test.TransactionTestCase",
        "django.test.LiveServerTestCase",
        "django.test.testcases.TestCase",
        "django.test.testcases.SimpleTestCase",
        "django.test.testcases.TransactionTestCase",
        "django.test.testcases.LiveServerTestCase",
        "rest_framework.test.APITestCase",
        "rest_framework.test.APISimpleTestCase",
        "rest_framework.test.APITransactionTestCase",
        "rest_framework.test.APILiveServerTestCase",
    }
)

PYTEST_MARK_PREFIX = "pytest.mark."
TEST_FUNCTION_PREFIX = "test_"

# How many importers an "imported only by tests" reason names before
# summarising the rest as a count.
_CITED_IMPORTERS = 2

# Top-level modules of the test runners: importing one is what a module
# built on the test machinery does, and production code does not.
TEST_RUNNER_MODULES = frozenset({"pytest", "_pytest", "unittest"})


# --- Layer 1: the runner's config decides files ---


def _glob_prefix(parts: tuple[str, ...], pattern: tuple[str, ...]) -> int | None:
    """How many leading ``parts`` the glob ``pattern`` matches, segment by segment.

    ``**`` matches any number of segments, including none; every other
    segment is an ``fnmatch`` pattern that never crosses a ``/``. Returns the
    shortest match length, or None when no prefix of ``parts`` matches.
    """
    if not pattern:
        return 0
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        for skip in range(len(parts) + 1):
            matched = _glob_prefix(parts[skip:], rest)
            if matched is not None:
                return skip + matched
        return None
    if not parts or not fnmatch.fnmatchcase(parts[0], head):
        return None
    matched = _glob_prefix(parts[1:], rest)
    return None if matched is None else matched + 1


def _testpath_for(rel: PurePosixPath, testpaths: tuple[str, ...]) -> str | None:
    """The first ``testpaths`` entry ``rel`` is, or sits under."""
    for testpath in testpaths:
        matched = _glob_prefix(rel.parts, PurePosixPath(testpath).parts)
        if matched is not None:
            return testpath
    return None


def _python_files_match(rel: PurePosixPath, patterns: tuple[str, ...]) -> str | None:
    """The first ``python_files`` pattern ``rel`` matches, as pytest matches it.

    A pattern without ``/`` is matched against the file name; one with ``/``
    against the path from the rootdir, at any depth.
    """
    posix = rel.as_posix()
    for pattern in patterns:
        if "/" not in pattern:
            if fnmatch.fnmatchcase(rel.name, pattern):
                return pattern
        elif fnmatch.fnmatchcase(posix, pattern) or fnmatch.fnmatchcase(posix, f"*/{pattern}"):
            return pattern
    return None


def _rootdir_relative(filepath: Path, config: PytestConfig) -> PurePosixPath | None:
    try:
        return PurePosixPath((config.base / filepath).relative_to(config.rootdir).as_posix())
    except ValueError:
        return None


def _config_role(filepath: Path, config: PytestConfig) -> str | None:
    """Why the pytest config makes ``filepath`` a test file, or None when it does not."""
    rel = _rootdir_relative(filepath, config)
    if rel is None:
        return None
    if config.testpaths:
        testpath = _testpath_for(rel, config.testpaths)
        if testpath is None:
            return None
        where = f"under testpaths '{testpath}'"
        if rel.as_posix() == PurePosixPath(testpath).as_posix():
            return f"named by testpaths '{testpath}'"
    else:
        where = "under the rootdir (no testpaths)"
    if is_conftest(rel):
        return f"conftest.py {where}"
    pattern = _python_files_match(rel, config.python_files)
    if pattern is None:
        # testpaths names where pytest searches; a module it does not
        # collect there is test support only on import evidence.
        return None
    default = " (pytest default)" if config.python_files_default else ""
    return f"python_files '{pattern}'{default} {where}"


# --- Layer 2: definitions declare themselves ---

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _iter_definitions(
    body: list[ast.stmt], prefix: str = ""
) -> Iterator[tuple[str, Definition, bool]]:
    """Every def and class with its dotted qualname and whether it is module- or class-level.

    Definitions nested inside a function are yielded too, flagged as not
    collectable, since pytest collects only module- and class-level tests.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}{node.name}"
            yield qualname, node, True
            for inner, inner_node, collectable in _iter_definitions(node.body, f"{qualname}."):
                in_function = not isinstance(node, ast.ClassDef)
                yield inner, inner_node, collectable and not in_function


def _decorator_role(node: Definition, resolver: ModuleResolver) -> str | None:
    named = (d.func if isinstance(d, ast.Call) else d for d in node.decorator_list)
    for target in resolver.resolve_all(named):
        if target == PYTEST_FIXTURE:
            return f"@pytest.fixture on `{node.name}` (line {node.lineno})"
        if target.startswith(PYTEST_MARK_PREFIX):
            return f"@{target} on `{node.name}` (line {node.lineno})"
    return None


def _testcase_classes(
    all_trees: dict[Path, ast.Module],
    resolvers: dict[Path, ModuleResolver],
) -> dict[tuple[Path, str], str]:
    """Every class reaching a ``TESTCASE_BASES`` class through its bases, with why.

    Bases are resolved through each module's imports, aliases included, and
    followed across analyzed modules until nothing new is found.
    """
    classes: list[tuple[Path, str, ast.ClassDef, list[str]]] = []
    for filepath, tree in all_trees.items():
        resolver = resolvers[filepath]
        for qualname, node, _ in _iter_definitions(tree.body):
            if isinstance(node, ast.ClassDef):
                classes.append((filepath, qualname, node, resolver.resolve_all(node.bases)))

    known: dict[str, str] = {base: base for base in TESTCASE_BASES}
    found: dict[tuple[Path, str], str] = {}
    changed = True
    while changed:
        changed = False
        for filepath, qualname, node, bases in classes:
            if (filepath, qualname) in found:
                continue
            base = next((b for b in bases if b in known), None)
            if base is None:
                continue
            root = known[base]
            via = "" if base == root else f" via {base}"
            found[(filepath, qualname)] = (
                f"`{qualname}` subclasses {root}{via} (line {node.lineno})"
            )
            module = resolvers[filepath].module
            known[f"{module}.{qualname}" if module else qualname] = root
            changed = True
    return found


def build_test_definitions(
    all_trees: dict[Path, ast.Module],
    module_imports: dict[Path, list[ModuleImport]],
    package_dirs: frozenset[tuple[str, ...]],
) -> dict[Path, dict[str, str]]:
    """Per file, every definition that declares itself a test, by qualname, with why.

    Definitions nested inside a declared one carry its reason, so a
    ``TestCase`` subclass's methods are test definitions too.
    """
    resolvers = {
        filepath: ModuleResolver(
            filepath_to_module(str(filepath), package_dirs), module_imports[filepath]
        )
        for filepath in all_trees
    }
    testcases = _testcase_classes(all_trees, resolvers)
    index: dict[Path, dict[str, str]] = {}
    for filepath, tree in all_trees.items():
        resolver = resolvers[filepath]
        declared: dict[str, str] = {}
        for qualname, node, collectable in _iter_definitions(tree.body):
            enclosing = qualname.rpartition(".")[0]
            reason = testcases.get((filepath, qualname)) or _decorator_role(node, resolver)
            if (
                reason is None
                and collectable
                and resolver.imports_pytest
                and not isinstance(node, ast.ClassDef)
                and node.name.startswith(TEST_FUNCTION_PREFIX)
            ):
                reason = (
                    f"`{node.name}` is a test_ function in a module importing pytest "
                    f"(line {node.lineno})"
                )
            if reason is None and enclosing in declared:
                reason = declared[enclosing]
            if reason is not None:
                declared[qualname] = reason
        if declared:
            index[filepath] = declared
    return index


def _cite(importers: Iterable[Path]) -> str:
    names = sorted(str(path) for path in importers)
    cited = ", ".join(names[:_CITED_IMPORTERS])
    rest = len(names) - _CITED_IMPORTERS
    return f"{cited} and {rest} more" if rest > 0 else cited


def _runner_import(imports: list[ModuleImport]) -> str | None:
    """The first test-runner module (``TEST_RUNNER_MODULES``) these imports reach."""
    for imp in imports:
        root = imp.dotted.partition(".")[0]
        if root in TEST_RUNNER_MODULES:
            return root
    return None


class _TestSupport:
    """Finds the test support modules (see the module docstring) and adds them to ``roles``."""

    def __init__(
        self,
        files: dict[Path, str],
        import_graph: dict[Path, frozenset[Path]],
        plugin_graph: dict[Path, frozenset[Path]],
        module_imports: dict[Path, list[ModuleImport]],
        config: PytestConfig,
    ) -> None:
        self.roles = files
        self.config = config
        self.import_graph = import_graph
        self.module_imports = module_imports
        self.support: set[Path] = set()
        self.importers: dict[Path, set[Path]] = {}
        for importer, targets in import_graph.items():
            for target in targets:
                self.importers.setdefault(target, set()).add(importer)
        self.declarers: dict[Path, set[Path]] = {}
        for declarer, targets in plugin_graph.items():
            for target in targets:
                self.declarers.setdefault(target, set()).add(declarer)

    def _testpath(self, target: Path) -> str | None:
        """The ``testpaths`` entry ``target`` sits under, or None (always None when unset)."""
        if not self.config.testpaths:
            return None
        rel = _rootdir_relative(target, self.config)
        return None if rel is None else _testpath_for(rel, self.config.testpaths)

    def _searched(self, target: Path) -> bool:
        """Whether pytest looks at ``target``: it is under a test path, or none is set."""
        return not self.config.testpaths or self._testpath(target) is not None

    def _evidence(self, target: Path, sources: set[Path]) -> str | None:
        """Why a module only test-role modules import is test support, or None when it is not.

        Under a test path, being imported only by tests is enough: ``testpaths``
        is the runner saying where tests live. Elsewhere the module must also
        show it is built on the test machinery.
        """
        runner = _runner_import(self.module_imports[target])
        if runner is not None:
            return f"imports {runner}"
        tests = sorted(str(path) for path in self.import_graph[target] if path in self.roles)
        if tests:
            return f"imports test module {tests[0]}"
        testpath = self._testpath(target)
        if testpath is not None:
            return f"under testpaths '{testpath}'"
        if sources <= self.support:
            return "every importer is test support"
        return None

    def _reason(self, target: Path) -> str | None:
        """Why ``target`` is test support, or None when it is not (yet)."""
        sources = self.importers.get(target, set())
        if not all(source in self.roles for source in sources):
            return None
        declarers = sorted(d for d in self.declarers.get(target, ()) if d in self.roles)
        if declarers:
            return f"named by pytest_plugins in {_cite(declarers)}"
        if not sources:
            return None
        evidence = self._evidence(target, sources)
        if evidence is None:
            return None
        return f"imported only by tests ({_cite(sources)}); {evidence}"

    def add_all(self) -> None:
        """Add every support module to the roles with why, repeating until a pass adds nothing.

        A module nothing imports and no test's ``pytest_plugins`` names stays
        as it is: no importer is no evidence. Each pass can only add the
        role, and a module qualifies on what its importers, declarers and
        imports already are, so the loop reaches the one smallest set that
        satisfies the rule, whatever order modules are visited in.
        """
        named = [t for t in self.declarers if t not in self.importers]
        candidates = [t for t in [*self.importers, *named] if self._searched(t)]
        changed = True
        while changed:
            changed = False
            for target in candidates:
                if target in self.roles:
                    continue
                reason = self._reason(target)
                if reason is None:
                    continue
                self.roles[target] = reason
                self.support.add(target)
                changed = True


def build_test_files(
    all_trees: dict[Path, ast.Module],
    config: PytestConfig,
    definitions: dict[Path, dict[str, str]],
    import_graph: dict[Path, frozenset[Path]],
    plugin_graph: dict[Path, frozenset[Path]],
    module_imports: dict[Path, list[ModuleImport]],
) -> dict[Path, str]:
    """Every test-role file with why.

    The reason is the pytest config's, else the file's first declaration,
    else, for a module pytest searches (under ``testpaths``, or anywhere
    when it is unset), the test-role modules that are its only importers
    and its evidence of being test support, or the test-role modules whose
    ``pytest_plugins`` name it.
    """
    files: dict[Path, str] = {}
    for filepath in all_trees:
        reason = _config_role(filepath, config)
        if reason is None and filepath in definitions:
            reason = next(iter(definitions[filepath].values()))
        if reason is not None:
            files[filepath] = reason
    _TestSupport(files, import_graph, plugin_graph, module_imports, config).add_all()
    return files


class TestRoles:
    """The test role of every analyzed file and definition, and of any other path.

    Built lazily from an ``AnalysisContext``, which holds it as
    ``ctx.roles``: the declarations and the files are computed on first use
    and kept, and a path outside the analyzed tree is judged once.
    """

    # A project class, not a pytest test class.
    __test__ = False

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self._definitions: dict[Path, dict[str, str]] | None = None
        self._files: dict[Path, str] | None = None
        # Roles of paths outside the analyzed tree, by layer 1 alone, with why.
        self._unanalyzed: dict[Path, str | None] = {}

    @property
    def definitions(self) -> dict[Path, dict[str, str]]:
        """Per file, every definition that declares itself a test, by qualname, with why."""
        if self._definitions is None:
            ctx = self.ctx
            self._definitions = build_test_definitions(
                ctx.all_trees, ctx.module_imports, ctx.package_dirs
            )
        return self._definitions

    @property
    def files(self) -> dict[Path, str]:
        """Every test-role file, with why: the pytest config key or the declaring definition."""
        if self._files is None:
            ctx = self.ctx
            self._files = build_test_files(
                ctx.all_trees,
                ctx.pytest_config,
                self.definitions,
                ctx.import_graph,
                build_plugin_graph(ctx.all_trees, ctx.module_graph.by_module),
                ctx.module_imports,
            )
        return self._files

    def is_test(self, filepath: Path | str) -> bool:
        """Whether the file at ``filepath`` has the test role.

        An analyzed file has it when it is in ``files``. Any other path,
        which git history names for files since deleted, excluded or
        unparseable, has no definitions to declare themselves and no place in
        the import graph, so it takes the role the pytest config alone gives
        it (``_config_role``): a deleted ``tests/test_x.py`` is a test, a
        deleted helper is production, since location is no evidence.
        """
        path = Path(filepath)
        if path in self.ctx.all_trees:
            return path in self.files
        if path not in self._unanalyzed:
            self._unanalyzed[path] = _config_role(path, self.ctx.pytest_config)
        return self._unanalyzed[path] is not None

    def of(self, filepath: Path, qualname: str) -> str | None:
        """Why the definition ``qualname`` in ``filepath`` is a test, or None when it is production.

        A definition is a test when it declares itself (or sits inside one
        that does), or when its file has the test role.
        """
        declared = self.definitions.get(filepath, {}).get(qualname)
        if declared is not None:
            return declared
        return self.files.get(filepath)
