"""The entry-point index: every root the analyzed code is run from, and why.

A root is a definition something outside the analyzed code calls: an
installer's script, a plugin host, the interpreter running a module, a CLI
framework, a URL router, a task queue. ``AnalysisContext.entry_points``
builds an :class:`EntryPointIndex` once per run; reachability (Phase 26),
finding chains (29), module layering (30) and entry-point error handling (33)
read it. The kinds, and what makes each a root:

- **console script**, **gui script**, **entry point**: ``[project.scripts]``,
  ``[project.gui-scripts]`` and ``[project.entry-points.<group>]`` in the
  target directory's ``pyproject.toml`` (``project_entry_points``). The
  reference is resolved through the analyzed modules, following a package's
  re-exports; a group entry naming a whole module roots that module. A
  declared root no analyzed file defines is kept, unresolved, since the
  project says it exists.
- **__main__**: what a top-level ``if __name__ == "__main__":`` block calls
  (``helpers.main_guard_blocks``, which ``_find_main_entry_points`` also
  reads), and what a ``__main__.py`` calls at module level. The module
  holding such a guard, or a ``__main__.py``, is also a root itself, a
  *script module* (Phase 35-1): its top-level code runs, so what that code
  reaches is live, but its other definitions are not (unlike a whole module
  a host loads by name).
- **click**: ``@click.command``/``@click.group`` and ``@<group>.command``/
  ``@<group>.group``, where the group is a click group defined in analyzed
  code (decorated, or ``click.Group(...)``), however it was imported.
- **typer**: ``@<app>.command``/``@<app>.callback`` on a ``typer.Typer()``.
- **django url**: the view each ``path``/``re_path``/``url`` in a URLconf
  names (a function, ``View.as_view()``, or a dotted string), and each
  viewset registered on a DRF router there.
- **django command**: the ``Command`` class of each
  ``<app>/management/commands/<name>.py``, which ``manage.py <name>`` runs.
- **celery task**: ``@shared_task``, and ``@<app>.task`` on a ``Celery()``.
- **dispatch decorator**: any function carrying one of
  ``framework.FRAMEWORK_DISPATCH_DECORATORS`` that is not already a celery
  task, such as ``@receiver`` and ``@hookimpl``, or ``@app.task`` on an app
  this module cannot see being built.
- **django admin**: a class whose base is a ``django.contrib`` admin class
  (``ModelAdmin``, an inline, ``AdminSite``, ``auth.admin.UserAdmin``), or a
  project class that is one, or that ``@admin.register(...)`` decorates.
- **django app config**: a subclass of ``django.apps.AppConfig``.
- **django template library**: each function registered on a module-level
  ``django.template.Library()`` instance, by decorator
  (``@register.filter``, ``@register.simple_tag(...)``, ``@register.tag``,
  ``@register.inclusion_tag(...)``, ``@register.simple_block_tag``) or by
  call (``register.filter("name", fn)``). The template engine calls those;
  the rest of the module is reached only through them, so a dead helper in
  a ``templatetags`` module is still reported, and a module there with no
  ``Library()`` roots nothing.
- **django settings**, **django urlconf**: whole modules Django loads by
  name: a module assigning ``INSTALLED_APPS`` or star-importing such a
  module to override it (``test_settings``, a build-time settings file a
  Dockerfile names), a module assigning ``urlpatterns``.
- **django middleware**: a class whose ``__init__`` takes ``get_response``,
  or a ``MiddlewareMixin`` subclass.
- **django migration**: the ``Migration`` class of a ``migrations`` package.
- **wsgi/asgi application**: the module-level ``application`` of a
  ``wsgi.py`` or ``asgi.py``.
- **signal handler**: what ``<signal>.connect(handler)`` registers, for a
  Django built-in signal or a ``django.dispatch.Signal()`` defined in
  analyzed code.
- **lambda handler**: a top-level ``handler(event, context)`` (either name
  may carry leading underscores), the AWS Lambda calling convention.
- **script**: what the top-level code of a module no production module
  imports calls (outside a ``__main__`` guard, which is **__main__**), since
  such a module only runs as a script; and, as a script module, a module
  whose line 1 is a ``#!`` line (``ctx.shebang_files``, read with the file,
  since the AST keeps no comments).

Every name is resolved with ``helpers.ModuleResolver`` through the module's
imports, so aliases count and a same-named library decorator does not.
Nothing is imported or executed. The AST kinds read production files only:
a ``__main__`` block or a dispatch decorator in a test-role file starts a
test, not the program, and code reached only from tests is the test-only
surface the reachability check exists to find. Apart from a declared root,
a reference that resolves to no analyzed definition (a library's view, a
call to ``sys.exit``) is not a root and is not listed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pysmelly.checks.framework import (
    CELERY_APP_CLASSES,
    framework_dispatch_decorators,
    is_django_settings_module,
    is_django_urlconf,
)
from pysmelly.checks.helpers import (
    INIT_FILE,
    ModuleResolver,
    decorator_target,
    filepath_to_module,
    import_bindings,
    main_guard_blocks,
)
from pysmelly.project_entry_points import DeclaredEntryPoint, DeclaredKind

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext


class RootKind(StrEnum):
    """Why a definition is a root, by what calls it."""

    CONSOLE_SCRIPT = "console script"
    GUI_SCRIPT = "gui script"
    ENTRY_POINT = "entry point"
    MAIN = "__main__"
    CLICK = "click"
    TYPER = "typer"
    DJANGO_URL = "django url"
    DJANGO_COMMAND = "django command"
    CELERY_TASK = "celery task"
    DISPATCH_DECORATOR = "dispatch decorator"
    DJANGO_ADMIN = "django admin"
    DJANGO_TEMPLATE_LIBRARY = "django template library"
    DJANGO_SETTINGS = "django settings"
    DJANGO_URLCONF = "django urlconf"
    DJANGO_MIDDLEWARE = "django middleware"
    DJANGO_MIGRATION = "django migration"
    LAMBDA_HANDLER = "lambda handler"
    SCRIPT = "script"
    DJANGO_APP_CONFIG = "django app config"
    SERVER_APPLICATION = "wsgi/asgi application"
    SIGNAL_HANDLER = "signal handler"


# The kinds a module run as a script is a root of: one with an
# ``if __name__ == "__main__":`` guard or a ``__main__.py`` (``__main__``), and
# one whose line 1 is a ``#!`` line (``script``).
_SCRIPT_KINDS = frozenset({RootKind.MAIN, RootKind.SCRIPT})
_SCRIPT_REASON = "so the module runs as a script"
_DUNDER_MAIN_FILE = "__main__.py"

_DECLARED_KINDS = {
    DeclaredKind.CONSOLE_SCRIPT: RootKind.CONSOLE_SCRIPT,
    DeclaredKind.GUI_SCRIPT: RootKind.GUI_SCRIPT,
    DeclaredKind.ENTRY_POINT: RootKind.ENTRY_POINT,
}

_CLICK_DECORATORS = frozenset({"click.command", "click.group"})
_CLICK_GROUP_DECORATORS = frozenset({"click.group"})
_CLICK_GROUP_CLASSES = frozenset({"click.Group", "click.CommandCollection"})
_TYPER_APP_CLASSES = frozenset({"typer.Typer"})
_CELERY_TASK_DECORATORS = frozenset({"celery.shared_task", "celery.task"})
_DJANGO_ROUTES = frozenset({"django.urls.path", "django.urls.re_path", "django.conf.urls.url"})
_DRF_ROUTERS_MODULE = "rest_framework.routers."
# Django runs ``manage.py NAME`` from the ``Command`` class in
# ``<app>/management/commands/NAME.py``.
_DJANGO_COMMANDS_DIR = ("management", "commands")
# Django's admin autodiscovers each app's ``admin.py`` and instantiates the
# ModelAdmin and inline classes registered there: a class whose base is a
# ``*Admin``, ``*Inline`` or ``*AdminSite`` class in a ``django.contrib``
# admin module (``django.contrib.admin``, ``django.contrib.auth.admin``,
# ``django.contrib.gis.admin``), or one ``@admin.register(...)`` decorates.
# The app registry builds each ``AppConfig`` named by ``INSTALLED_APPS``. A
# subclass of a project class that is one of these counts too.
_DJANGO_CONTRIB = "django.contrib."
_DJANGO_ADMIN_MODULE = "admin"
_DJANGO_ADMIN_SUFFIXES = ("Admin", "Inline", "AdminSite")
_DJANGO_ADMIN_REGISTER = frozenset(
    {"django.contrib.admin.register", "django.contrib.admin.decorators.register"}
)
_DJANGO_APP_CONFIG_BASES = frozenset({"django.apps.AppConfig", "django.apps.config.AppConfig"})
# ``{% load name %}`` imports an app's ``templatetags/name.py`` and reads its
# ``register``, a ``Library`` instance; the engine calls what is registered
# on it. Each registrar takes the function as a decorator, as a positional
# argument (``filter(name, fn)``) or by one of these keywords.
_DJANGO_TEMPLATE_LIBRARY_CLASSES = frozenset(
    {"django.template.Library", "django.template.library.Library", "django.template.base.Library"}
)
_DJANGO_TEMPLATE_REGISTRARS = frozenset(
    {"filter", "simple_tag", "simple_block_tag", "inclusion_tag", "tag"}
)
_DJANGO_TEMPLATE_FUNCTION_KEYWORDS = frozenset({"filter_func", "compile_function", "func"})
# Django runs each ``Migration`` class in an app's ``migrations`` package.
_DJANGO_MIGRATIONS_DIR = "migrations"
_DJANGO_MIGRATION_CLASS = "Migration"
# Django builds each class in ``MIDDLEWARE`` with ``get_response``; an old-style
# one subclasses ``MiddlewareMixin``.
_DJANGO_MIDDLEWARE_PARAM = "get_response"
_DJANGO_MIDDLEWARE_BASES = frozenset({"django.utils.deprecation.MiddlewareMixin"})
# AWS Lambda calls a handler named in its deployment config as
# ``handler(event, context)``; the parameter names are the convention, the
# second often underscored as unused.
_LAMBDA_PARAMS = ("event", "context")
# A WSGI or ASGI server loads the module-level ``application`` of these files
# (Django's ``WSGI_APPLICATION``/``ASGI_APPLICATION`` and ``startproject``).
_SERVER_MODULES = {"wsgi.py": "WSGI", "asgi.py": "ASGI"}
_SERVER_APPLICATION = "application"
# ``<signal>.connect(handler)`` registers ``handler`` with the dispatcher:
# Django's built-in signals live under these modules, and a project's own
# signals are ``django.dispatch.Signal()`` instances.
_DJANGO_SIGNAL_MODULES = (
    "django.db.models.signals.",
    "django.core.signals.",
    "django.test.signals.",
    "django.contrib.auth.signals.",
    "django.db.backends.signals.",
)
_DJANGO_SIGNAL_CLASSES = frozenset({"django.dispatch.Signal", "django.dispatch.dispatcher.Signal"})

# How many re-exports (``from .cli import main`` in ``__init__.py``) a
# declared reference is followed through before it counts as unresolved.
MAX_REEXPORT_HOPS = 3

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_BLOCK_NODES = (ast.stmt, ast.excepthandler, ast.match_case)


@dataclass(frozen=True)
class EntryPointRoot:
    """One root and the reason it is one.

    ``target`` is the dotted path the root names. ``file`` and ``qualname``
    locate its definition (``qualname`` is dotted through enclosing classes
    and functions), and ``node`` is that definition: a function, a class,
    or the module-level assignment that binds the name (a console script
    naming a ``typer.Typer()`` instance). A declared entry point naming a
    whole module has a ``file`` but no ``qualname``, ``node`` or ``line``;
    one no analyzed file defines has none of the four.
    """

    kind: RootKind
    reason: str
    target: str
    file: Path | None
    qualname: str | None
    line: int | None
    node: ast.stmt | None

    @property
    def script_module(self) -> bool:
        """Whether this root is a module run as a script: the interpreter runs its
        top-level code, which reaches what it uses. Any other module root is one
        a host loads by name, reading every definition it holds."""
        return self.file is not None and self.qualname is None and self.kind in _SCRIPT_KINDS

    def describe(self) -> str:
        """The root as the ``-v`` listing prints it."""
        if self.file is None:
            where = f"(not analyzed) {self.target}"
        elif self.qualname is None:
            where = f"{self.file} (module)"
        else:
            where = f"{self.file}:{self.line} {self.qualname}"
        return f"{where}: {self.kind}: {self.reason}"


class EntryPointIndex:
    """Every root found, in file and line order, with lookups by definition.

    ``roots`` holds one record per root and kind: a function that is both
    a console script and a click command appears twice. Unresolved
    declared roots sort last.
    """

    def __init__(self, roots: Iterable[EntryPointRoot]) -> None:
        self.roots = tuple(sorted(roots, key=_root_order))
        self._by_file: dict[Path, list[EntryPointRoot]] = defaultdict(list)
        for root in self.roots:
            if root.file is not None:
                self._by_file[root.file].append(root)

    @property
    def unresolved(self) -> tuple[EntryPointRoot, ...]:
        """Declared roots no analyzed file defines."""
        return tuple(root for root in self.roots if root.file is None)

    def in_file(self, path: Path) -> tuple[EntryPointRoot, ...]:
        """The roots defined in ``path``, module roots included."""
        return tuple(self._by_file.get(path, ()))

    def is_root(self, path: Path, qualname: str) -> bool:
        """Whether the definition ``qualname`` in ``path`` is a root of any kind."""
        return any(root.qualname == qualname for root in self._by_file.get(path, ()))

    def describe(self) -> list[str]:
        """The ``-v`` listing: a count line, then one indented line per root."""
        return [
            f"Entry points: {len(self.roots)} root(s)",
            *(f"  {root.describe()}" for root in self.roots),
        ]


_KIND_ORDER = {kind: i for i, kind in enumerate(RootKind)}


def _root_order(root: EntryPointRoot) -> tuple:
    return (
        root.file is None,
        str(root.file or ""),
        root.line or 0,
        _KIND_ORDER[root.kind],
        root.qualname or "",
        root.target,
    )


def _collect_definitions(nodes: Iterable[ast.AST], prefix: str, out: dict[str, ast.stmt]) -> None:
    for node in nodes:
        if isinstance(node, _DEF_NODES):
            qualname = f"{prefix}{node.name}"
            out[qualname] = node
            _collect_definitions(node.body, f"{qualname}.", out)
        elif isinstance(node, _BLOCK_NODES):
            children = (c for c in ast.iter_child_nodes(node) if isinstance(c, _BLOCK_NODES))
            _collect_definitions(children, prefix, out)


def _module_assignments(tree: ast.Module) -> Iterator[tuple[str, ast.stmt, ast.expr]]:
    """Each top-level ``NAME = value`` (or annotated) assignment: name, statement, value."""
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    yield target.id, stmt, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name):
                yield stmt.target.id, stmt, stmt.value


def _definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    """Every function and class in ``tree`` by qualname, and the module's assigned names."""
    defs: dict[str, ast.stmt] = {}
    for name, stmt, _value in _module_assignments(tree):
        defs.setdefault(name, stmt)
    _collect_definitions(tree.body, "", defs)
    return defs


_Decorated = tuple[Path, str, ast.FunctionDef | ast.AsyncFunctionDef, list[tuple[str, str]]]


@dataclass(frozen=True)
class _Found:
    """An analyzed definition a name resolved to."""

    file: Path
    qualname: str
    node: ast.stmt
    target: str

    def key(self, kind: RootKind) -> tuple:
        """What makes this definition one root of ``kind``: one record per kind."""
        return (kind, self.file, self.qualname)

    def root(self, kind: RootKind, reason: str) -> EntryPointRoot:
        """This definition as a root of ``kind``."""
        return EntryPointRoot(
            kind, reason, self.target, self.file, self.qualname, self.node.lineno, self.node
        )


class DefinitionResolver:
    """Resolves names in one analyzed module, and dotted paths to analyzed definitions."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.by_module = ctx.module_graph.by_module
        self.modules_of: dict[Path, list[str]] = defaultdict(list)
        for module, paths in sorted(self.by_module.items()):
            for path in paths:
                self.modules_of[path].append(module)
        self.defs = {path: _definitions(tree) for path, tree in ctx.all_trees.items()}
        self.bindings = {path: import_bindings(imps) for path, imps in ctx.module_imports.items()}
        self.names = {
            path: ModuleResolver(filepath_to_module(str(path), ctx.package_dirs), imps)
            for path, imps in ctx.module_imports.items()
        }
        # Per module, the modules its ``from m import *`` statements copy.
        self.star_sources = {
            path: [imp.source for imp in imps if imp.name == "*" and imp.source]
            for path, imps in ctx.module_imports.items()
        }

    def name(self, path: Path, expr: ast.expr) -> str | None:
        """The dotted path ``expr`` names in ``path``, or None when it is not a name chain."""
        resolved = self.names[path].resolve_all([expr])
        return resolved[0] if resolved else None

    def full_names(self, path: Path, qualname: str) -> list[str]:
        """Every dotted path an import can name the definition ``qualname`` in ``path`` by."""
        return [f"{module}.{qualname}" for module in self.modules_of[path]]

    def found(self, path: Path, qualname: str, node: ast.stmt) -> _Found:
        """The definition ``qualname`` in ``path``, named by its first full name."""
        names = self.full_names(path, qualname)
        return _Found(path, qualname, node, names[0] if names else qualname)

    def module_name(self, path: Path) -> str:
        """The module ``path`` by its first dotted name, else its file stem."""
        modules = self.modules_of[path]
        return modules[0] if modules else path.stem

    def module_files(self, module: str) -> list[Path]:
        return sorted(self.by_module.get(module, ()))

    def definition(self, dotted: str, hops: int = 0) -> _Found | None:
        """The analyzed definition ``dotted`` names, following re-exports, or None."""
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            module, qualname = ".".join(parts[:split]), ".".join(parts[split:])
            for path in self.module_files(module):
                if qualname in self.defs[path]:
                    return self.found(path, qualname, self.defs[path][qualname])
                head, _, rest = qualname.partition(".")
                imported = self.bindings[path].get(head)
                if imported is not None and hops < MAX_REEXPORT_HOPS:
                    found = self.definition(f"{imported}.{rest}" if rest else imported, hops + 1)
                    if found is not None:
                        return found
        return None

    def definition_of(self, path: Path, expr: ast.expr) -> _Found | None:
        dotted = self.name(path, expr)
        return None if dotted is None else self.definition(dotted)


class _ConventionRoots:
    """Roots a framework finds by convention rather than by a decorator or a
    declaration: Django's admin classes, app configs, settings, URLconfs,
    middleware and migrations, a WSGI or ASGI
    ``application``, and an AWS Lambda handler."""

    def __init__(
        self,
        resolver: DefinitionResolver,
        production: list[tuple[Path, ast.Module]],
        roots: dict[tuple, EntryPointRoot],
        add: Callable[[RootKind, str, _Found], None],
    ) -> None:
        self.resolver = resolver
        self.production = production
        self.roots = roots
        self.add = add

    def find(self) -> None:
        self._django_subclasses(RootKind.DJANGO_ADMIN, _is_django_admin_base)
        self._django_subclasses(RootKind.DJANGO_APP_CONFIG, _DJANGO_APP_CONFIG_BASES.__contains__)
        self._django_admin_registered()
        self._django_modules()
        self._django_middleware()
        self._lambda_handlers()
        self._server_applications()

    def _classes(self) -> Iterator[tuple[Path, str, ast.ClassDef]]:
        for path, _tree in self.production:
            for qualname, node in self.resolver.defs[path].items():
                if isinstance(node, ast.ClassDef):
                    yield path, qualname, node

    def _django_subclasses(self, kind: RootKind, is_base: Callable[[str], bool]) -> None:
        """Every production class with a base ``is_base`` accepts, or a project
        class already found, repeated to a fixed point."""
        classes = [
            (path, qualname, node, [self.resolver.name(path, base) for base in node.bases])
            for path, qualname, node in self._classes()
        ]
        known: set[str] = set()
        changed = True
        while changed:
            changed = False
            for path, qualname, node, resolved in classes:
                base = next(
                    (d for d in resolved if d is not None and (d in known or is_base(d))), None
                )
                if base is None or (kind, path, qualname) in self.roots:
                    continue
                self.add(kind, f"subclass of {base}", self.resolver.found(path, qualname, node))
                known.update(self.resolver.full_names(path, qualname))
                changed = True

    def _django_admin_registered(self) -> None:
        for path, qualname, node in self._classes():
            for decorator in node.decorator_list:
                expr = decorator_target(decorator)
                dotted = self.resolver.name(path, expr)
                if dotted in _DJANGO_ADMIN_REGISTER:
                    reason = _decorator_reason(ast.unparse(expr), dotted)
                    self.add(
                        RootKind.DJANGO_ADMIN, reason, self.resolver.found(path, qualname, node)
                    )

    def _module_root(self, kind: RootKind, path: Path, reason: str) -> None:
        """``path`` as a whole-module root: a host loads it by name."""
        target = self.resolver.module_name(path)
        self.roots[(kind, path)] = EntryPointRoot(kind, reason, target, path, None, None, None)

    def _django_modules(self) -> None:
        """Settings and URLconf modules, which Django loads by the dotted name a
        setting or ``include()`` gives; and each app's migrations."""
        settings = {path for path, tree in self.production if is_django_settings_module(tree)}
        for path in sorted(settings):
            reason = "assigns INSTALLED_APPS, a settings module Django reads by name"
            self._module_root(RootKind.DJANGO_SETTINGS, path, reason)
        for path, base in self._settings_variants(settings):
            reason = (
                f"star-imports {base}, a settings module: a variant Django reads by name "
                "(DJANGO_SETTINGS_MODULE)"
            )
            self._module_root(RootKind.DJANGO_SETTINGS, path, reason)
        for path, tree in self.production:
            if is_django_urlconf(tree):
                reason = "assigns urlpatterns, a URLconf Django loads by name"
                self._module_root(RootKind.DJANGO_URLCONF, path, reason)
            migration = self.resolver.defs[path].get(_DJANGO_MIGRATION_CLASS)
            if path.parent.name == _DJANGO_MIGRATIONS_DIR and isinstance(migration, ast.ClassDef):
                reason = f"`{_DJANGO_MIGRATION_CLASS}` in a migrations package, which migrate runs"
                found = self.resolver.found(path, _DJANGO_MIGRATION_CLASS, migration)
                self.add(RootKind.DJANGO_MIGRATION, reason, found)

    def _settings_variants(self, settings: set[Path]) -> list[tuple[Path, str]]:
        """Modules that copy a settings module with ``from base import *`` and override
        some of it (``test_settings``, a build-time settings file), with the module they
        copy; a variant of a variant counts, to a fixed point."""
        found: list[tuple[Path, str]] = []
        known = set(settings)
        changed = True
        while changed:
            changed = False
            for path, _tree in self.production:
                if path in known:
                    continue
                base = next(
                    (
                        source
                        for source in self.resolver.star_sources.get(path, ())
                        if known.intersection(self.resolver.module_files(source))
                    ),
                    None,
                )
                if base is not None:
                    known.add(path)
                    found.append((path, base))
                    changed = True
        return found

    def _django_middleware(self) -> None:
        for path, qualname, node in self._classes():
            init = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, _FUNCTION_NODES) and item.name == "__init__"
                ),
                None,
            )
            params = [] if init is None else [arg.arg for arg in init.args.args]
            bases = {dotted for base in node.bases if (dotted := self.resolver.name(path, base))}
            if params[1:2] == [_DJANGO_MIDDLEWARE_PARAM]:
                reason = f"__init__(self, {_DJANGO_MIDDLEWARE_PARAM}), a middleware Django builds"
            elif bases & _DJANGO_MIDDLEWARE_BASES:
                reason = f"subclass of {min(bases & _DJANGO_MIDDLEWARE_BASES)}"
            else:
                continue
            self.add(RootKind.DJANGO_MIDDLEWARE, reason, self.resolver.found(path, qualname, node))

    def _lambda_handlers(self) -> None:
        for path, tree in self.production:
            for node in tree.body:
                if not isinstance(node, _FUNCTION_NODES):
                    continue
                args = node.args
                names = tuple(arg.arg.lstrip("_") for arg in args.posonlyargs + args.args)
                if names == _LAMBDA_PARAMS and not (args.vararg or args.kwonlyargs or args.kwarg):
                    reason = "handler(event, context), the AWS Lambda calling convention"
                    found = self.resolver.found(path, node.name, node)
                    self.add(RootKind.LAMBDA_HANDLER, reason, found)

    def _server_applications(self) -> None:
        for path, _tree in self.production:
            server = _SERVER_MODULES.get(path.name)
            stmt = self.resolver.defs[path].get(_SERVER_APPLICATION)
            if server is None or not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            reason = f"`{_SERVER_APPLICATION}` in {path.name}, which the {server} server loads"
            self.add(
                RootKind.SERVER_APPLICATION,
                reason,
                self.resolver.found(path, _SERVER_APPLICATION, stmt),
            )


class _Builder:
    def __init__(self, ctx: AnalysisContext, declared: Iterable[DeclaredEntryPoint]) -> None:
        self.resolver = DefinitionResolver(ctx)
        self.declared = tuple(declared)
        self.production = [
            (path, tree) for path, tree in ctx.all_trees.items() if path not in ctx.roles.files
        ]
        self.import_graph = ctx.import_graph
        self.shebang_files = ctx.shebang_files
        self.roots: dict[tuple, EntryPointRoot] = {}

    def add(self, kind: RootKind, reason: str, found: _Found, key: tuple | None = None) -> None:
        """Record ``found`` as a root, once per kind unless ``key`` says otherwise."""
        key = key or found.key(kind)
        if key in self.roots:
            return
        self.roots[key] = found.root(kind, reason)

    def build(self) -> EntryPointIndex:
        self._declared_roots()
        self._script_modules()
        self._main_roots()
        self._decorated_roots()
        self._django_roots()
        self._django_commands()
        _ConventionRoots(self.resolver, self.production, self.roots, self.add).find()
        self._script_roots()
        self._signal_handlers()
        self._template_registrations()
        return EntryPointIndex(self.roots.values())

    def _script_modules(self) -> None:
        """Each production module that runs as a script, as a root of its top-level
        code: one with a top-level ``__main__`` guard, a ``__main__.py``
        (``python -m``), or one whose line 1 is a ``#!`` line."""
        for path, tree in self.production:
            target = self.resolver.module_name(path)
            for kind, reason in _script_evidence(path, tree, shebang=path in self.shebang_files):
                self.roots[(kind, path)] = EntryPointRoot(
                    kind, reason, target, path, None, None, None
                )

    def _script_roots(self) -> None:
        """What the top-level code of a module no production module imports calls:
        such a module runs only as a script (``python x.py``, ``shell < x.py``)."""
        imported = {
            dep
            for path, _tree in self.production
            for dep in self.import_graph.get(path, ())
            if dep != path
        }
        for path, tree in self.production:
            if path in imported or path.name == _DUNDER_MAIN_FILE:
                continue
            guards = set(map(id, main_guard_blocks(tree)))
            top = [
                stmt
                for stmt in tree.body
                if not isinstance(stmt, (*_DEF_NODES, ast.Import, ast.ImportFrom))
                and id(stmt) not in guards
            ]
            reason = "called at module level in a module no analyzed code imports"
            self._called(RootKind.SCRIPT, path, top, reason)

    def _signal_handlers(self) -> None:
        signals = self._app_instances(_DJANGO_SIGNAL_CLASSES)
        for path, tree in self.production:
            for call in ast.walk(tree):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "connect"
                ):
                    continue
                signal = self.resolver.name(path, call.func.value)
                if signal is None or not (
                    signal.startswith(_DJANGO_SIGNAL_MODULES) or signal in signals
                ):
                    continue
                handler = _argument(call, 0, "receiver")
                found = None if handler is None else self.resolver.definition_of(path, handler)
                if found is not None:
                    reason = f"connected to {signal} at {path}:{call.lineno}"
                    self.add(RootKind.SIGNAL_HANDLER, reason, found)

    def _template_registrations(self) -> None:
        """What a call registers on a template ``Library``: ``register.filter("x", fn)``,
        ``register.tag(compile_function=fn)``, ``register.inclusion_tag("t.html")(fn)``."""
        libraries = self._app_instances(_DJANGO_TEMPLATE_LIBRARY_CLASSES)
        for path, tree in self.production:
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                registrar = call.func.func if isinstance(call.func, ast.Call) else call.func
                if not (
                    isinstance(registrar, ast.Attribute)
                    and registrar.attr in _DJANGO_TEMPLATE_REGISTRARS
                ):
                    continue
                library = self.resolver.name(path, registrar.value)
                if library not in libraries:
                    continue
                functions = [
                    *call.args,
                    *(
                        kw.value
                        for kw in call.keywords
                        if kw.arg in _DJANGO_TEMPLATE_FUNCTION_KEYWORDS
                    ),
                ]
                reason = (
                    f"registered by {ast.unparse(registrar)} at {path}:{call.lineno}, "
                    f"template library {libraries[library]}"
                )
                for function in functions:
                    found = self.resolver.definition_of(path, function)
                    if found is not None:
                        self.add(RootKind.DJANGO_TEMPLATE_LIBRARY, reason, found)

    def _django_commands(self) -> None:
        for path, _tree in self.production:
            if path.parent.parts[-2:] != _DJANGO_COMMANDS_DIR or path.name == INIT_FILE:
                continue
            command = self.resolver.defs[path].get("Command")
            if isinstance(command, ast.ClassDef):
                reason = f"`manage.py {path.stem}`, a management command"
                found = self.resolver.found(path, "Command", command)
                self.add(RootKind.DJANGO_COMMAND, reason, found)

    def _declared_roots(self) -> None:
        for decl in self.declared:
            kind = _DECLARED_KINDS[decl.kind]
            if decl.attr is None:
                files = self.resolver.module_files(decl.module)
                file = files[0] if files else None
                self.roots[(kind, decl.table, decl.name)] = EntryPointRoot(
                    kind, decl.reason, decl.module, file, None, None, None
                )
                continue
            target = f"{decl.module}.{decl.attr}"
            found = self.resolver.definition(target)
            if found is None:
                self.roots[(kind, decl.table, decl.name)] = EntryPointRoot(
                    kind, decl.reason, target, None, None, None, None
                )
            else:
                self.add(kind, decl.reason, found, key=(kind, decl.table, decl.name))

    def _called(self, kind: RootKind, path: Path, nodes: Iterable[ast.AST], reason: str) -> None:
        """Record, as a ``kind`` root, each function a call in ``nodes`` resolves to."""
        for node in nodes:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    found = self.resolver.definition_of(path, call.func)
                    if found is not None:
                        self.add(kind, reason, found)

    def _main_roots(self) -> None:
        for path, tree in self.production:
            for block in main_guard_blocks(tree):
                reason = f"called in the __main__ block at line {block.lineno}"
                self._called(RootKind.MAIN, path, [block], reason)
            if path.name == _DUNDER_MAIN_FILE:
                module_level = [s for s in tree.body if not isinstance(s, _DEF_NODES)]
                reason = f"called at module level in {_DUNDER_MAIN_FILE}"
                self._called(RootKind.MAIN, path, module_level, reason)

    def _app_instances(self, classes: frozenset[str]) -> dict[str, str]:
        """Each module-level ``x = Cls(...)`` of ``classes``, by every dotted name, to its site."""
        found: dict[str, str] = {}
        for path, tree in self.production:
            for name, stmt, value in _module_assignments(tree):
                if not isinstance(value, ast.Call):
                    continue
                if self.resolver.name(path, value.func) not in classes:
                    continue
                for full in self.resolver.full_names(path, name):
                    found[full] = f"{full} at {path}:{stmt.lineno}"
        return found

    def _decorated(self) -> Iterator[_Decorated]:
        """Each production function with decorators: path, qualname, node, and per
        decorator its source text and the dotted path it resolves to."""
        for path, _tree in self.production:
            for qualname, node in self.resolver.defs[path].items():
                if not isinstance(node, _FUNCTION_NODES) or not node.decorator_list:
                    continue
                decorators = []
                for decorator in node.decorator_list:
                    expr = decorator_target(decorator)
                    dotted = self.resolver.name(path, expr)
                    if dotted is not None:
                        decorators.append((ast.unparse(expr), dotted))
                yield path, qualname, node, decorators

    def _click_groups(self, decorated: list[_Decorated]) -> dict[str, str]:
        """Every click group by each dotted name it is known by, to its site; nested
        groups (``@cli.group()``) found by repeating to a fixed point."""
        groups = self._app_instances(_CLICK_GROUP_CLASSES)
        changed = True
        while changed:
            changed = False
            for path, qualname, node, decorators in decorated:
                if not any(_is_group_decorator(dotted, groups) for _src, dotted in decorators):
                    continue
                for full in self.resolver.full_names(path, qualname):
                    if full not in groups:
                        groups[full] = f"{full} at {path}:{node.lineno}"
                        changed = True
        return groups

    def _decorated_roots(self) -> None:
        decorated = list(self._decorated())
        click_groups = self._click_groups(decorated)
        typer_apps = self._app_instances(_TYPER_APP_CLASSES)
        celery_apps = self._app_instances(CELERY_APP_CLASSES)
        template_libraries = self._app_instances(_DJANGO_TEMPLATE_LIBRARY_CLASSES)
        for path, qualname, node, decorators in decorated:
            found = self.resolver.found(path, qualname, node)
            for src, dotted in decorators:
                owner, _, attr = dotted.rpartition(".")
                if dotted in _CLICK_DECORATORS:
                    self.add(RootKind.CLICK, _decorator_reason(src, dotted), found)
                elif attr in {"command", "group"} and owner in click_groups:
                    self.add(RootKind.CLICK, f"@{src}, click group {click_groups[owner]}", found)
                elif attr in {"command", "callback"} and owner in typer_apps:
                    self.add(RootKind.TYPER, f"@{src}, typer app {typer_apps[owner]}", found)
                elif dotted in _CELERY_TASK_DECORATORS:
                    self.add(RootKind.CELERY_TASK, _decorator_reason(src, dotted), found)
                elif attr in {"task", "periodic_task"} and owner in celery_apps:
                    self.add(
                        RootKind.CELERY_TASK, f"@{src}, Celery app {celery_apps[owner]}", found
                    )
                elif attr in _DJANGO_TEMPLATE_REGISTRARS and owner in template_libraries:
                    reason = f"@{src}, template library {template_libraries[owner]}"
                    self.add(RootKind.DJANGO_TEMPLATE_LIBRARY, reason, found)
            if (RootKind.CELERY_TASK, path, qualname) in self.roots:
                continue
            dispatch = framework_dispatch_decorators(node)
            if dispatch:
                self.add(RootKind.DISPATCH_DECORATOR, f"@{dispatch[0]}", found)

    def _django_roots(self) -> None:
        for path, tree in self.production:
            if not is_django_urlconf(tree):
                continue
            routers = self._routers(path, tree)
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call):
                    continue
                if self.resolver.name(path, call.func) in _DJANGO_ROUTES:
                    view = _argument(call, 1, "view")
                    reason = f"view in {path}:{call.lineno}"
                elif (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "register"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in routers
                ):
                    view = _argument(call, 1, "viewset")
                    reason = f"viewset registered on {call.func.value.id} in {path}:{call.lineno}"
                else:
                    continue
                found = None if view is None else self._view(path, view)
                if found is not None:
                    self.add(RootKind.DJANGO_URL, reason, found)

    def _routers(self, path: Path, tree: ast.Module) -> set[str]:
        """The module-level names in a URLconf bound to a DRF router."""
        return {
            name
            for name, _stmt, value in _module_assignments(tree)
            if isinstance(value, ast.Call)
            and (self.resolver.name(path, value.func) or "").startswith(_DRF_ROUTERS_MODULE)
        }

    def _view(self, path: Path, view: ast.expr) -> _Found | None:
        """The analyzed view a route names: a function, a class's ``as_view()``, or a
        dotted string; None for ``include(...)`` and anything outside the project."""
        if isinstance(view, ast.Constant) and isinstance(view.value, str):
            return self.resolver.definition(view.value)
        if isinstance(view, ast.Call):
            return self._as_view_class(path, view.func)
        return self.resolver.definition_of(path, view)

    def _as_view_class(self, path: Path, callee: ast.expr) -> _Found | None:
        """The class a ``Cls.as_view()`` route names; None for ``include(...)`` and
        any other call."""
        if not isinstance(callee, ast.Attribute) or callee.attr != "as_view":
            return None
        return self.resolver.definition_of(path, callee.value)


def _script_evidence(
    path: Path, tree: ast.Module, *, shebang: bool
) -> Iterator[tuple[RootKind, str]]:
    """Why the module ``path`` runs as a script, one reason per kind: a top-level
    ``__main__`` guard (else being a ``__main__.py``), and a ``#!`` line 1."""
    guard = next(main_guard_blocks(tree), None)
    if guard is not None:
        yield (
            RootKind.MAIN,
            f'an `if __name__ == "__main__":` guard at line {guard.lineno}, {_SCRIPT_REASON}',
        )
    elif path.name == _DUNDER_MAIN_FILE:
        yield RootKind.MAIN, f"a `{_DUNDER_MAIN_FILE}`, which `python -m` runs, {_SCRIPT_REASON}"
    if shebang:
        yield RootKind.SCRIPT, f"a `#!` line 1, {_SCRIPT_REASON}"


def _is_group_decorator(dotted: str, groups: dict[str, str]) -> bool:
    owner, _, attr = dotted.rpartition(".")
    return dotted in _CLICK_GROUP_DECORATORS or (attr == "group" and owner in groups)


def _is_django_admin_base(dotted: str) -> bool:
    """A ``django.contrib`` admin class: ``ModelAdmin``, an inline, an ``AdminSite``,
    or a contrib app's own admin (``django.contrib.auth.admin.UserAdmin``)."""
    module, _, name = dotted.rpartition(".")
    return (
        dotted.startswith(_DJANGO_CONTRIB)
        and _DJANGO_ADMIN_MODULE in module.split(".")
        and name.endswith(_DJANGO_ADMIN_SUFFIXES)
    )


def _decorator_reason(src: str, dotted: str) -> str:
    return f"@{src}" if src == dotted else f"@{src} ({dotted})"


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def build_entry_point_index(
    ctx: AnalysisContext, declared: Iterable[DeclaredEntryPoint]
) -> EntryPointIndex:
    """Every root of the analyzed code: ``declared`` from ``pyproject.toml``, and
    what the production files' ``__main__`` code, decorators and URLconfs root."""
    return _Builder(ctx, declared).build()
