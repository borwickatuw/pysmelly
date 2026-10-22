"""Framework-specific suppression logic.

Centralizes Django/DRF/Celery/pluggy detection functions and constants
used by multiple check modules to suppress false positives from
framework-conventional patterns.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.helpers import (
    ModuleImport,
    ModuleResolver,
    decorator_target,
    is_click_callback_signature,
    own_scope_nodes,
    referenced_name,
)

# The classes whose instance is a Celery application.
CELERY_APP_CLASSES = frozenset({"celery.Celery"})


def is_migration_file(filepath: Path | str) -> bool:
    """Check if a file is a Django migration (migrations/0001_*.py pattern).

    Looks for a 'migrations' directory component followed by a filename
    that starts with a digit (the Django auto-numbering convention).
    """
    parts = Path(filepath).parts
    for i, part in enumerate(parts):
        if part == "migrations" and i + 1 < len(parts):
            return parts[i + 1][:1].isdigit()
    return False


def is_settings_file(filepath: Path) -> bool:
    """Check if a file looks like a Django/framework settings file.

    Settings files contain UPPER_CASE constants read by the framework via
    getattr() — they appear unused in static analysis but are required.
    """
    if filepath.name == "settings.py":
        return True
    if "settings" in filepath.parts:
        return True
    return False


def _module_binds(tree: ast.Module, name: str) -> bool:
    """Whether the module itself assigns *name*, at any depth outside a def or class.

    ``urlpatterns += [...]`` under ``if settings.DEBUG:`` binds the module
    attribute just as a top-level assignment does, so compound statements are
    searched; a function or class body binds a different scope and is not.
    """
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
        for node in own_scope_nodes(tree.body)
    )


def celery_app_names(tree: ast.Module, imports: list[ModuleImport]) -> frozenset[str]:
    """The names the module binds to a Celery application (``app = Celery(...)``).

    Celery's Django integration re-exports the app from the project
    package's ``__init__.py`` (``from .celery import app as celery_app``) so
    that importing the package creates it and ``@shared_task`` binds to it.
    That import is load-bearing even when nothing imports the name.
    ``imports`` is the module's ``ctx.module_imports`` entry.
    """
    resolver = ModuleResolver("", imports)
    return frozenset(
        target.id
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and isinstance(stmt.value, ast.Call)
        and set(resolver.resolve_all([stmt.value.func])) & CELERY_APP_CLASSES
        for target in stmt.targets
        if isinstance(target, ast.Name)
    )


def is_django_urlconf(tree: ast.Module) -> bool:
    """Whether a module is a Django URLconf: it assigns ``urlpatterns``.

    Recognised by content, not by the name ``urls.py``: Django loads whatever
    module ``ROOT_URLCONF`` or ``include()`` names, and a ``urls.py`` without
    ``urlpatterns`` is ordinary code.
    """
    return _module_binds(tree, "urlpatterns")


def is_django_settings_module(tree: ast.Module) -> bool:
    """Whether a module is a Django settings module: it assigns ``INSTALLED_APPS``.

    Unlike :func:`is_settings_file`, which guesses from the path, this reads
    the content, so ``config/base.py`` counts and a ``settings.py`` that only
    holds helper code does not.
    """
    return _module_binds(tree, "INSTALLED_APPS")


def is_manage_py(filepath: Path) -> bool:
    """Check if a file is Django's manage.py."""
    return filepath.name == "manage.py"


def framework_dispatch_decorators(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """The function's framework dispatch decorators, by last name, in order.

    ``receiver`` for ``@receiver(...)``, ``task`` for ``@app.task``; empty
    when none is one. Framework dispatch decorators (e.g. Django @receiver,
    Celery @task) mean the framework calls the function: all its parameters
    are required by the framework, not by its own logic, and the
    entry-point index takes it as a root.
    """
    names = (referenced_name(decorator_target(deco)) for deco in func_node.decorator_list)
    return [name for name in names if name in FRAMEWORK_DISPATCH_DECORATORS]


# Decorators that indicate framework dispatch — all params are
# required by the framework, not by the function's own logic.
FRAMEWORK_DISPATCH_DECORATORS = frozenset(
    {
        "receiver",  # Django signals
        "task",  # Celery
        "shared_task",  # Celery
        "periodic_task",  # Celery
        "hookimpl",  # pluggy
    }
)


# Framework hook methods where accessing params more than self is expected.
# These have signatures dictated by the framework, so patterns like
# feature-envy (accessing another param more than self) are normal.
FRAMEWORK_HOOK_METHODS = frozenset(
    {
        # Django admin
        "formfield_for_foreignkey",
        "formfield_for_manytomany",
        "formfield_for_dbfield",
        "formfield_for_choice_field",
        "has_add_permission",
        "has_change_permission",
        "has_delete_permission",
        # Django views/forms
        "get_context_data",
        "get_queryset",
        "get_form_kwargs",
        "get_form_class",
        "form_valid",
        "form_invalid",
        # Django management commands
        "add_arguments",
        # Django middleware
        "process_request",
        "process_response",
        "process_view",
        "process_exception",
        # Django REST framework
        "get_serializer_class",
        "get_permissions",
        "perform_create",
        "perform_update",
    }
)


# Parameter names injected by web frameworks (request/response objects).
# Methods receiving these params inherently operate on them, so accessing
# their attributes more than self is expected.
FRAMEWORK_PARAM_NAMES = frozenset({"request", "response"})


# Python protocol dunder methods whose signatures are dictated by the language.
# The author cannot remove these params even if unused — the interpreter passes
# them unconditionally (e.g. context manager calls __exit__(exc_type, exc_val,
# exc_tb) on every exit). Treating their params as "vestigial" is wrong.
PROTOCOL_DUNDER_METHODS = frozenset(
    {
        # Context manager protocol
        "__exit__",
        "__aexit__",
        # Descriptor protocol
        "__get__",
        "__set__",
        "__delete__",
        "__set_name__",
        # Subclass / generic hooks
        "__init_subclass__",
        "__class_getitem__",
    }
)


def has_dictated_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function's parameter list is dictated rather than chosen.

    A protocol dunder (``__exit__(exc_type, exc_val, exc_tb)``) and a Click
    ``callback=`` function (``(ctx, param, value)``) both take exactly the
    parameters the language or the framework passes. Neither an unused one
    nor a clump repeated across such functions says anything about the
    author's design, so parameter-shape checks skip them.
    """
    return node.name in PROTOCOL_DUNDER_METHODS or is_click_callback_signature(node)
