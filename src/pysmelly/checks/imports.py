"""Import-related checks: compatibility shims and ``__init__.py`` re-export drift."""

from __future__ import annotations

import ast
import functools
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pysmelly.checks.framework import celery_app_names, is_manage_py
from pysmelly.checks.helpers import INIT_FILE, TESTS_UNSEEN
from pysmelly.checks.module_graph import STAR, ModuleNode, OriginKind
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

INIT_RE_EXPORT_DRIFT = "init-re-export-drift"


@check(
    "compat-shims",
    severity=Severity.HIGH,
    description="try/except ImportError patterns from old Python support",
    advice=Advice(
        fix=(
            "Keep the import the supported Python versions provide and delete the try/except"
            " and the fallback."
        ),
        dismiss=(
            "Dismiss only if the fallback serves a Python version or optional dependency"
            " the project still declares in pyproject.toml; name it in the ignore."
        ),
    ),
)
def check_compat_shims(ctx: AnalysisContext) -> list[Finding]:
    """Find try/except ImportError patterns (compatibility shims).

    These are often left over from supporting older Python versions
    that the project no longer targets per requires-python.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        # manage.py has Django's auto-generated try/except ImportError boilerplate
        if is_manage_py(filepath):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not node.body:
                continue
            if not isinstance(node.body[0], (ast.Import, ast.ImportFrom)):
                continue

            for handler in node.handlers:
                if handler.type is None:
                    continue
                if isinstance(handler.type, ast.Name) and handler.type.id in {
                    "ImportError",
                    "ModuleNotFoundError",
                }:
                    imp = node.body[0]
                    if isinstance(imp, ast.Import):
                        mod_name = imp.names[0].name
                    elif isinstance(imp, ast.ImportFrom):
                        mod_name = imp.module or ""
                    else:
                        mod_name = "?"

                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="compat-shims",
                            message=(f"compatibility shim for '{mod_name}'"),
                            severity=Severity.HIGH,
                        )
                    )

    return findings


@dataclass(frozen=True)
class _ReExport:
    """One name an ``__init__.py`` binds by a module-scope from-import of a project module."""

    name: str
    line: int
    defined_in: str


def _names_module(imported: str, module: str) -> bool:
    """Whether the dotted path ``imported`` may name the analyzed module ``module``.

    Equal paths do, and so does a dotted suffix either way: a directory on
    ``sys.path`` names ``myapp/works/views/__init__.py`` (module
    ``myapp.works.views``) as ``works.views``.
    """
    return imported == module or module.endswith(f".{imported}") or imported.endswith(f".{module}")


class _PackagePathUses:
    """Every way an analyzed module can reach a name through a package's path.

    ``from pkg import X`` (or a star import of ``pkg`` that binds ``X``),
    ``pkg.X`` on a module that binds ``pkg`` itself, a dotted string ending
    in ``X`` (``mock.patch("pkg.X")``, a Django dotted path), and a
    ``pyproject.toml`` entry point ``pkg:X``.
    """

    def __init__(self, ctx: AnalysisContext) -> None:
        self._graph = ctx.module_graph
        self._trees = ctx.all_trees
        self._string_suffixes = ctx.references.dotted_string_suffixes
        self._declared = ctx.declared_entry_points
        self._from_imports = [
            (record.source, record.name, path)
            for path, node in self._graph.nodes.items()
            for record in node.bindings
            if record.source is not None and record.name is not None
        ]
        self._attributes: dict[Path, frozenset[str]] = {}

    def reached(self, package: ModuleNode) -> frozenset[str]:
        """The names analyzed modules other than ``package`` reach through its path."""
        module, own = package.module, package.path
        names = {
            name
            for source, name, path in self._from_imports
            if path != own and _names_module(source, module)
        }
        if STAR in names:
            names.update(name for name in package.bound_names if package.star_binds(name))
        names.update(
            point.attr.split(".")[0]
            for point in self._declared
            if point.attr is not None and _names_module(point.module, module)
        )
        for path in self._binding_module(module):
            if path != own:
                names.update(self._attribute_names(path))
        return frozenset(names | self._string_suffixes)

    def _binding_module(self, module: str) -> Iterator[Path]:
        """The files whose imports bind ``module`` itself: ``import pkg``, ``from . import pkg``."""
        for path, node in self._graph.nodes.items():
            if any(
                not imported.from_source and _names_module(imported.dotted, module)
                for imported in node.imports
            ):
                yield path

    def _attribute_names(self, path: Path) -> frozenset[str]:
        if path not in self._attributes:
            self._attributes[path] = frozenset(
                node.attr for node in ast.walk(self._trees[path]) if isinstance(node, ast.Attribute)
            )
        return self._attributes[path]


def _locally_read(tree: ast.Module) -> frozenset[str]:
    """The names an ``__init__.py`` reads itself, anywhere in its tree."""
    return frozenset(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )


def _left_out(base: Path, package: tuple[str, Path], module: str) -> bool:
    """Whether ``module``, under the analyzed ``package``, is a file the analysis left out.

    An excluded, untracked (a build-generated ``_version.py``) or
    unparseable module still exists on disk under ``base``, the directory
    the analyzed paths are relative to.
    """
    name, init = package
    location = base / init.parent / Path(*module[len(name) + 1 :].split("."))
    return location.with_name(f"{location.name}.py").exists() or location.is_dir()


def _stale_findings(ctx: AnalysisContext, node: ModuleNode) -> Iterator[Finding]:
    """Re-exports of a name the project no longer defines, and ``__all__`` entries nothing binds.

    A guarded import (``optional``: in a ``try`` that catches the failure)
    is expected to fail and is skipped. An import from a module the project
    no longer has is told from a library's by sitting under an analyzed
    package, and from a file the analysis left out by the disk.
    """
    graph = ctx.module_graph
    for binding in node.scope:
        record = binding.imported
        if (
            record is None
            or record.source is None
            or record.name is None
            or binding.name == STAR
            or binding.optional
        ):
            continue
        if record.source in graph.by_module:
            if graph.origin(record.source, record.name).kind is not OriginKind.MISSING:
                continue
            problem = f"`{record.source}` no longer defines it"
        elif (package := graph.enclosing_package(record.source)) is not None and not _left_out(
            ctx.pytest_config.base, package, record.source
        ):
            problem = f"`{record.source}` is no longer a module of the project"
        else:
            continue
        yield _stale_finding(
            node,
            binding.line,
            f"re-exports `{record.name}` from `{record.source}`, but {problem} — "
            f"importing `{node.module}` raises ImportError",
        )
    for name, line in (node.exports or {}).items():
        if graph.origin(node.module, name).kind is OriginKind.MISSING:
            yield _stale_finding(
                node,
                line,
                f"`__all__` lists `{name}`, which `{node.module}` no longer binds — "
                f"`from {node.module} import *` raises AttributeError",
            )


def _stale_finding(node: ModuleNode, line: int, message: str) -> Finding:
    return Finding(
        file=str(node.path),
        line=line,
        check=INIT_RE_EXPORT_DRIFT,
        message=message,
        severity=Severity.MEDIUM,
    )


def _re_exports(ctx: AnalysisContext, node: ModuleNode) -> Iterator[_ReExport]:
    """The names ``node``'s ``__init__.py`` binds only to pass them on.

    A module-scope from-import of a name some other analyzed module defines,
    when that binding is the one the package's name resolves to, the
    ``__init__.py`` never reads the name itself, and the name is not a
    dunder (``__version__``). A submodule binding (``from . import sub``)
    is not a re-export of a name: it runs the submodule. Nor is a Celery
    app (``framework.celery_app_names``), which the package imports so
    that the app exists whenever the package is loaded.
    """
    graph = ctx.module_graph
    read_here = _locally_read(ctx.all_trees[node.path])
    for binding in node.scope:
        record = binding.imported
        if (
            record is None
            or record.source is None
            or record.name is None
            or binding.name == STAR
            or binding.name in read_here
            or binding.name.startswith("__")
        ):
            continue
        origin = graph.origin(record.source, record.name)
        defining = origin.path
        if origin.kind is not OriginKind.DEFINED or defining is None or defining == node.path:
            continue
        if graph.origin(node.module, binding.name) != origin:
            continue
        if origin.name in celery_app_names(ctx.all_trees[defining], ctx.module_imports[defining]):
            continue
        yield _ReExport(binding.name, binding.line, origin.module)


def _relative_to(package: str, module: str) -> str:
    """``module`` as a relative path when it sits under ``package`` (``.impl``)."""
    return module[len(package) :] if module.startswith(f"{package}.") else module


def _unused_message(
    package: str, unused: list[_ReExport], indices: tuple[int, ...], *, tests_seen: bool
) -> str:
    by_module: dict[str, list[str]] = defaultdict(list)
    for i in indices:
        by_module[_relative_to(package, unused[i].defined_in)].append(f"`{unused[i].name}`")
    names = "; ".join(
        f"{', '.join(names)} (from `{module}`)" for module, names in by_module.items()
    )
    them = "it" if len(indices) == 1 else "them"
    message = (
        f"`{package}` re-exports {names}, but no analyzed module imports {them} through `{package}`"
    )
    if not tests_seen:
        message += "; " + TESTS_UNSEEN.format(use=f"imports {them}")
    return message


@check(
    INIT_RE_EXPORT_DRIFT,
    severity=Severity.MEDIUM,
    description="__init__.py re-exports of a name no longer defined, or that nothing imports",
    advice=Advice(
        fix=(
            "Drop the re-export (or the `__all__` entry) and import each name from the"
            " module that defines it now."
        ),
        dismiss=(
            "Dismiss only for a re-export nothing analyzed imports, when the package is a"
            " published library whose external consumers import the name through it."
        ),
    ),
)
def check_init_re_export_drift(ctx: AnalysisContext) -> list[Finding]:
    """Find ``__init__.py`` re-exports that drifted from the code behind them.

    A re-export (``from .x import y``, or an ``__all__`` entry) of a name
    the project no longer defines makes importing the package fail; a
    guarded import (inside a ``try`` that catches the ImportError) is
    expected to fail and is skipped. A re-export no analyzed module imports
    through the package's path (``_PackagePathUses``) is reported once per
    ``__init__.py``, naming each name and its defining module.
    """
    graph = ctx.module_graph
    uses = _PackagePathUses(ctx)
    findings: list[Finding] = []
    for path, node in graph.nodes.items():
        if path.name != INIT_FILE:
            continue
        findings.extend(_stale_findings(ctx, node))
        reached = uses.reached(node)
        unused = [reexport for reexport in _re_exports(ctx, node) if reexport.name not in reached]
        if unused:
            findings.append(
                multi_site_finding(
                    INIT_RE_EXPORT_DRIFT,
                    Severity.MEDIUM,
                    [Site(str(path), reexport.line) for reexport in unused],
                    functools.partial(
                        _unused_message,
                        node.module,
                        unused,
                        tests_seen=bool(ctx.roles.files),
                    ),
                )
            )
    return findings
