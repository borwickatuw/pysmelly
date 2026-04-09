"""Framework-specific suppression logic.

Centralizes Django/DRF/Celery/pluggy detection functions and constants
used by multiple check modules to suppress false positives from
framework-conventional patterns.
"""

from __future__ import annotations

import ast
from pathlib import Path


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


def is_manage_py(filepath: Path) -> bool:
    """Check if a file is Django's manage.py."""
    return filepath.name == "manage.py"


def has_framework_dispatch_decorator(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Check if a function is decorated with a framework dispatch decorator.

    Framework dispatch decorators (e.g. Django @receiver, Celery @task)
    indicate that all parameters are required by the framework, not by
    the function's own logic.
    """
    for deco in func_node.decorator_list:
        name = None
        if isinstance(deco, ast.Name):
            name = deco.id
        elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
            name = deco.func.id
        elif isinstance(deco, ast.Attribute):
            name = deco.attr
        elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
            name = deco.func.attr
        if name in FRAMEWORK_DISPATCH_DECORATORS:
            return True
    return False


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
