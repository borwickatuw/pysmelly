"""Tests for the entry-point index (``ctx.entry_points``) and the pyproject reader behind it."""

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

from pysmelly.checks.entry_points import EntryPointIndex, RootKind
from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.discovery import SHEBANG
from pysmelly.project_entry_points import (
    DeclaredEntryPoint,
    DeclaredKind,
    ProjectEntryPointError,
    load_project_entry_points,
)


def _index(files: dict[str, str], declared: tuple[DeclaredEntryPoint, ...] = ()) -> EntryPointIndex:
    sources = {Path(name): textwrap.dedent(code) for name, code in files.items()}
    trees = {path: ast.parse(source) for path, source in sources.items()}
    shebangs = frozenset(path for path, source in sources.items() if source.startswith(SHEBANG))
    ctx = AnalysisContext(trees, verbose=False, declared_entry_points=declared)
    ctx.shebang_files = shebangs
    return ctx.entry_points


def _roots(index: EntryPointIndex) -> list[tuple[RootKind, str | None, str | None]]:
    return [
        (root.kind, None if root.file is None else str(root.file), root.qualname)
        for root in index.roots
    ]


def _declared(tmp_path: Path, pyproject: str) -> tuple[DeclaredEntryPoint, ...]:
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent(pyproject), encoding="utf-8")
    return load_project_entry_points(tmp_path)


CLI_MODULE = """\
def main():
    run()


def run():
    pass
"""


class TestDeclaredInPyproject:
    def test_console_script_resolves_through_a_source_root(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "pkg.cli:main"\n')
        index = _index({"src/pkg/__init__.py": "", "src/pkg/cli.py": CLI_MODULE}, declared)
        assert _roots(index) == [(RootKind.CONSOLE_SCRIPT, "src/pkg/cli.py", "main")]
        assert index.roots[0].reason == '[project.scripts] tool = "pkg.cli:main"'
        assert index.roots[0].line == 1

    def test_gui_script(self, tmp_path):
        declared = _declared(tmp_path, '[project.gui-scripts]\nviewer = "pkg.cli:main"\n')
        index = _index({"pkg/__init__.py": "", "pkg/cli.py": CLI_MODULE}, declared)
        assert _roots(index) == [(RootKind.GUI_SCRIPT, "pkg/cli.py", "main")]

    def test_entry_point_group_naming_an_object_or_a_whole_module(self, tmp_path):
        declared = _declared(
            tmp_path,
            """\
            [project.entry-points.pytest11]
            plug = "pkg.plugin"

            [project.entry-points."flake8.extension"]
            X = "pkg.plugin:Checker.run"
            """,
        )
        files = {"pkg/__init__.py": "", "pkg/plugin.py": "class Checker:\n    def run(self): ..."}
        index = _index(files, declared)
        assert _roots(index) == [
            (RootKind.ENTRY_POINT, "pkg/plugin.py", None),
            (RootKind.ENTRY_POINT, "pkg/plugin.py", "Checker.run"),
        ]
        assert index.roots[0].describe() == (
            'pkg/plugin.py (module): entry point: [project.entry-points.pytest11] plug = "pkg.plugin"'
        )

    def test_reference_followed_through_a_package_reexport(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "pkg:main"\n')
        files = {"pkg/__init__.py": "from .cli import main\n", "pkg/cli.py": CLI_MODULE}
        assert _roots(_index(files, declared)) == [(RootKind.CONSOLE_SCRIPT, "pkg/cli.py", "main")]

    def test_script_naming_an_assigned_object(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "pkg.cli:app"\n')
        files = {"pkg/__init__.py": "", "pkg/cli.py": "import typer\napp = typer.Typer()\n"}
        index = _index(files, declared)
        assert _roots(index) == [(RootKind.CONSOLE_SCRIPT, "pkg/cli.py", "app")]
        assert isinstance(index.roots[0].node, ast.Assign)

    def test_declared_root_no_analyzed_file_defines_is_kept_unresolved(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "elsewhere.cli:main"\n')
        index = _index({"pkg/cli.py": CLI_MODULE}, declared)
        assert _roots(index) == [(RootKind.CONSOLE_SCRIPT, None, None)]
        assert index.unresolved == index.roots
        assert index.roots[0].describe() == (
            "(not analyzed) elsewhere.cli.main: console script: "
            '[project.scripts] tool = "elsewhere.cli:main"'
        )

    def test_no_pyproject_declares_nothing(self, tmp_path):
        assert load_project_entry_points(tmp_path) == ()

    def test_reference_with_extras_and_spaces(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "pkg.cli : main [fast]"\n')
        assert [(d.kind, d.module, d.attr) for d in declared] == [
            (DeclaredKind.CONSOLE_SCRIPT, "pkg.cli", "main")
        ]


class TestMalformedPyprojectFailsFast:
    """An installer refuses these tables too, so pysmelly says so rather than skip a root."""

    @pytest.mark.parametrize(
        ("pyproject", "message"),
        [
            ('[project.scripts]\ntool = "pkg.cli:"\n', "is not an entry-point reference"),
            ('[project.scripts]\ntool = "pkg/cli.py:main"\n', "is not an entry-point reference"),
            ('[project.scripts]\ntool = "pkg.cli"\n', "names no function"),
            ("[project.scripts]\ntool = 3\n", "tool must be a string, got int"),
            ('[project]\nscripts = "pkg.cli:main"\n', r"\[project.scripts\] must be a table"),
            (
                '[project]\nentry-points = {x = "m"}\n',
                r"\[project.entry-points.x\] must be a table",
            ),
            ("[project\n", "pyproject.toml"),
        ],
    )
    def test_loader_raises(self, tmp_path, pyproject, message):
        with pytest.raises(ProjectEntryPointError, match=message):
            _declared(tmp_path, pyproject)

    def test_cli_exits_1_naming_the_entry(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\ntool = "pkg.cli"\n', encoding="utf-8"
        )
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main([str(tmp_path), "--no-context"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert '[project.scripts] tool = "pkg.cli" names no function' in err


class TestMainBlocks:
    def test_functions_called_in_a_main_guard(self):
        code = """\
        import sys
        from pkg import tools

        def main():
            pass

        def unused():
            pass

        if __name__ == "__main__":
            tools.setup()
            sys.exit(main())
        """
        files = {
            "pkg/__init__.py": "",
            "pkg/tools.py": "def setup(): ...",
            "script.py": code,
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.MAIN, "pkg/tools.py", "setup"),
            (RootKind.MAIN, "script.py", None),
            (RootKind.MAIN, "script.py", "main"),
        ]
        assert index.roots[2].reason == "called in the __main__ block at line 10"

    def test_dunder_main_module_calls_at_module_level(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/cli.py": CLI_MODULE,
            "pkg/__main__.py": "from pkg.cli import main\n\nmain()\n",
        }
        assert _roots(_index(files)) == [
            (RootKind.MAIN, "pkg/__main__.py", None),
            (RootKind.MAIN, "pkg/cli.py", "main"),
        ]

    def test_main_guard_in_a_test_file_is_not_a_root(self):
        code = 'import unittest\n\ndef helper(): ...\n\nif __name__ == "__main__":\n    helper()\n'
        assert _roots(_index({"tests/test_x.py": code})) == []


class TestClick:
    def test_commands_and_groups_resolved_through_imports(self):
        files = {
            "pkg/__init__.py": "",
            "pkg/cli.py": """\
            import click

            @click.group()
            def cli():
                pass

            @cli.group()
            def sub():
                pass
            """,
            "pkg/commands.py": """\
            from pkg.cli import cli, sub

            @cli.command()
            def first():
                pass

            @sub.command
            def second():
                pass
            """,
        }
        assert _roots(_index(files)) == [
            (RootKind.CLICK, "pkg/cli.py", "cli"),
            (RootKind.CLICK, "pkg/cli.py", "sub"),
            (RootKind.CLICK, "pkg/commands.py", "first"),
            (RootKind.CLICK, "pkg/commands.py", "second"),
        ]

    def test_aliased_imports(self):
        code = """\
        import click as c
        from click import command as cmd

        @c.command()
        def one():
            pass

        @cmd
        def two():
            pass
        """
        index = _index({"tool.py": code})
        assert _roots(index) == [
            (RootKind.CLICK, "tool.py", "one"),
            (RootKind.CLICK, "tool.py", "two"),
        ]
        assert [root.reason for root in index.roots] == [
            "@c.command (click.command)",
            "@cmd (click.command)",
        ]

    def test_group_object_and_unrelated_command_decorator(self):
        code = """\
        import click
        from discord.ext import commands

        cli = click.Group()
        bot = commands.Bot()

        @cli.command()
        def real():
            pass

        @bot.command()
        def not_click():
            pass
        """
        assert _roots(_index({"tool.py": code})) == [(RootKind.CLICK, "tool.py", "real")]


class TestTyper:
    def test_commands_and_callback_on_a_typer_app(self):
        code = """\
        import typer

        app = typer.Typer()

        @app.callback()
        def root():
            pass

        @app.command()
        def hello():
            pass
        """
        assert _roots(_index({"tool.py": code})) == [
            (RootKind.TYPER, "tool.py", "root"),
            (RootKind.TYPER, "tool.py", "hello"),
        ]


class TestDjangoUrls:
    def test_views_named_by_urlpatterns(self):
        files = {
            "app/__init__.py": "",
            "app/views.py": """\
            def index(request): ...
            def legacy(request): ...
            class Detail: ...
            class ThingViewSet: ...
            def unrouted(request): ...
            """,
            "app/urls.py": """\
            from django.contrib.auth import views as auth_views
            from django.urls import include, path, re_path
            from rest_framework import routers

            from app import views

            router = routers.DefaultRouter()
            router.register(r"things", views.ThingViewSet)

            urlpatterns = [
                path("", views.index),
                re_path(r"^d/$", view=views.Detail.as_view()),
                path("old/", "app.views.legacy"),
                path("login/", auth_views.LoginView.as_view()),
                path("api/", include(router.urls)),
            ]
            """,
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.DJANGO_URLCONF, "app/urls.py", None),
            (RootKind.DJANGO_URL, "app/views.py", "index"),
            (RootKind.DJANGO_URL, "app/views.py", "legacy"),
            (RootKind.DJANGO_URL, "app/views.py", "Detail"),
            (RootKind.DJANGO_URL, "app/views.py", "ThingViewSet"),
        ]
        assert index.roots[0].reason == "assigns urlpatterns, a URLconf Django loads by name"
        assert index.roots[1].reason == "view in app/urls.py:11"
        assert index.roots[4].reason == "viewset registered on router in app/urls.py:8"

    def test_management_command_class(self):
        files = {
            "app/management/__init__.py": "",
            "app/management/commands/__init__.py": "",
            "app/management/commands/reindex.py": "class Command:\n    def handle(self): ...\n",
            "app/management/commands/helpers.py": "def shared(): ...\n",
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.DJANGO_COMMAND, "app/management/commands/reindex.py", "Command")
        ]
        assert index.roots[0].reason == "`manage.py reindex`, a management command"

    def test_route_calls_outside_a_urlconf_are_not_roots(self):
        code = "from django.urls import path\n\ndef index(r): ...\n\nroutes = [path('', index)]\n"
        assert _roots(_index({"app/routes.py": code})) == []


class TestCelery:
    def test_shared_task_and_app_task_with_aliases(self):
        files = {
            "proj/__init__.py": "",
            "proj/celery.py": "from celery import Celery as C\n\napp = C('proj')\n",
            "proj/tasks.py": """\
            from celery import shared_task as st
            from proj.celery import app as capp

            @st
            def first():
                pass

            @capp.task(bind=True)
            def second(self):
                pass
            """,
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.CELERY_TASK, "proj/tasks.py", "first"),
            (RootKind.CELERY_TASK, "proj/tasks.py", "second"),
        ]
        assert index.roots[0].reason == "@st (celery.shared_task)"
        assert index.roots[1].reason == "@capp.task, Celery app proj.celery.app at proj/celery.py:3"

    def test_task_on_an_app_built_elsewhere_is_a_dispatch_root(self):
        code = "from factory import make\n\napp = make()\n\n@app.task\ndef work():\n    pass\n"
        assert _roots(_index({"tasks.py": code})) == [
            (RootKind.DISPATCH_DECORATOR, "tasks.py", "work")
        ]


class TestDispatchDecorators:
    def test_framework_registered_functions_and_methods(self):
        code = """\
        from django.dispatch import receiver
        import pluggy

        hookimpl = pluggy.HookimplMarker("x")

        @receiver(post_save)
        def on_save(sender, **kwargs):
            pass

        class Plugin:
            @hookimpl
            def configure(self):
                pass

        def plain():
            pass
        """
        index = _index({"signals.py": code})
        assert _roots(index) == [
            (RootKind.DISPATCH_DECORATOR, "signals.py", "on_save"),
            (RootKind.DISPATCH_DECORATOR, "signals.py", "Plugin.configure"),
        ]
        assert index.roots[0].reason == "@receiver"


class TestDjangoFrameworkRoots:
    """What Django builds or loads by name from settings and app conventions (Phase 26-2)."""

    def test_admin_classes_by_base_through_a_project_base_and_by_register(self):
        code = """\
        from django.contrib import admin
        from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

        class BaseAdmin(admin.ModelAdmin):
            pass

        class BookAdmin(BaseAdmin):
            pass

        class LineInline(admin.TabularInline):
            pass

        class CustomUserAdmin(BaseUserAdmin):
            pass

        @admin.register(Author)
        class AuthorAdmin:
            pass

        class Unrelated:
            pass
        """
        index = _index({"app/__init__.py": "", "app/admin.py": code})
        assert _roots(index) == [
            (RootKind.DJANGO_ADMIN, "app/admin.py", "BaseAdmin"),
            (RootKind.DJANGO_ADMIN, "app/admin.py", "BookAdmin"),
            (RootKind.DJANGO_ADMIN, "app/admin.py", "LineInline"),
            (RootKind.DJANGO_ADMIN, "app/admin.py", "CustomUserAdmin"),
            (RootKind.DJANGO_ADMIN, "app/admin.py", "AuthorAdmin"),
        ]
        assert index.roots[0].reason == "subclass of django.contrib.admin.ModelAdmin"
        assert index.roots[1].reason == "subclass of app.admin.BaseAdmin"
        assert index.roots[4].reason == "@admin.register (django.contrib.admin.register)"

    def test_app_config_subclass(self):
        code = (
            "from django.apps import AppConfig\n\nclass BooksConfig(AppConfig):\n    name = 'b'\n"
        )
        index = _index({"books/__init__.py": "", "books/apps.py": code})
        assert _roots(index) == [(RootKind.DJANGO_APP_CONFIG, "books/apps.py", "BooksConfig")]

    def test_wsgi_and_asgi_application(self):
        wsgi = "from django.core.wsgi import get_wsgi_application\napplication = get_wsgi_application()\n"
        files = {
            "proj/__init__.py": "",
            "proj/wsgi.py": wsgi,
            "proj/asgi.py": "application = make()\n",
            "proj/other.py": "application = make()\n",
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.SERVER_APPLICATION, "proj/asgi.py", "application"),
            (RootKind.SERVER_APPLICATION, "proj/wsgi.py", "application"),
        ]
        assert index.roots[1].reason == "`application` in wsgi.py, which the WSGI server loads"

    def test_signal_handlers_connected_without_receiver(self):
        code = """\
        from django.db.models.signals import post_save
        from django.dispatch import Signal

        order_placed = Signal()

        def on_save(sender, **kwargs): ...
        def on_order(sender, **kwargs): ...
        def on_other(sender, **kwargs): ...

        post_save.connect(on_save)
        order_placed.connect(receiver=on_order)
        button.connect(on_other)
        """
        index = _index({"app/__init__.py": "", "app/signals.py": code})
        assert _roots(index) == [
            (RootKind.SIGNAL_HANDLER, "app/signals.py", "on_save"),
            (RootKind.SIGNAL_HANDLER, "app/signals.py", "on_order"),
        ]
        assert index.roots[0].reason == (
            "connected to django.db.models.signals.post_save at app/signals.py:10"
        )

    def test_settings_and_urlconf_modules(self):
        files = {
            "proj/__init__.py": "",
            "proj/settings.py": "INSTALLED_APPS = ['app']\n",
            "proj/urls.py": "urlpatterns = []\n",
        }
        assert _roots(_index(files)) == [
            (RootKind.DJANGO_SETTINGS, "proj/settings.py", None),
            (RootKind.DJANGO_URLCONF, "proj/urls.py", None),
        ]

    def test_a_settings_variant_star_importing_settings_is_a_settings_root(self):
        """``DJANGO_SETTINGS_MODULE=config.build_settings`` is set where pysmelly cannot see."""
        files = {
            "config/__init__.py": "",
            "config/base_settings.py": "INSTALLED_APPS = ['app']\n",
            "config/build_settings.py": "from .base_settings import *\n\nSTORAGES = {}\n",
            "config/ci_settings.py": "from config.build_settings import *\n\nDEBUG = False\n",
            "config/helpers.py": "from os.path import *\n\nLIMIT = 3\n",
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.DJANGO_SETTINGS, "config/base_settings.py", None),
            (RootKind.DJANGO_SETTINGS, "config/build_settings.py", None),
            (RootKind.DJANGO_SETTINGS, "config/ci_settings.py", None),
        ]
        assert index.roots[1].reason == (
            "star-imports config.base_settings, a settings module: a variant Django reads "
            "by name (DJANGO_SETTINGS_MODULE)"
        )

    def test_templatetags_module_without_a_library_roots_nothing(self):
        code = """\
        def shout(value):
            return value.upper()
        """
        files = {
            "app/__init__.py": "",
            "app/templatetags/__init__.py": "",
            "app/templatetags/shout.py": code,
        }
        assert _roots(_index(files)) == []

    def test_functions_registered_on_a_template_library_are_roots(self):
        code = """\
        from django import template as tpl
        from django.template import Library as Lib

        register = tpl.Library()
        extra = Lib()

        @register.filter
        def shout(value):
            return _upper(value)

        @register.simple_tag(takes_context=True)
        def greet(context): ...

        @extra.inclusion_tag("card.html")
        def card(obj): ...

        @register.tag(name="block")
        def do_block(parser, token): ...

        def plain(value): ...
        def compile_raw(parser, token): ...
        def _upper(value): ...

        register.filter("plain", plain)
        register.tag(name="raw", compile_function=compile_raw)
        """
        files = {
            "app/__init__.py": "",
            "app/templatetags/__init__.py": "",
            "app/templatetags/shout.py": code,
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "shout"),
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "greet"),
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "card"),
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "do_block"),
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "plain"),
            (RootKind.DJANGO_TEMPLATE_LIBRARY, "app/templatetags/shout.py", "compile_raw"),
        ]
        assert index.roots[0].reason == (
            "@register.filter, template library app.templatetags.shout.register "
            "at app/templatetags/shout.py:4"
        )
        assert index.roots[4].reason == (
            "registered by register.filter at app/templatetags/shout.py:24, "
            "template library app.templatetags.shout.register at app/templatetags/shout.py:4"
        )

    def test_a_same_named_register_that_is_not_a_django_library_roots_nothing(self):
        code = """\
        from myproject.registry import Library

        register = Library()

        @register.filter
        def shout(value): ...

        register.filter("plain", shout)
        """
        files = {
            "app/__init__.py": "",
            "app/templatetags/__init__.py": "",
            "app/templatetags/shout.py": code,
        }
        assert _roots(_index(files)) == []

    def test_middleware_by_signature_and_by_mixin(self):
        code = """\
        from django.utils.deprecation import MiddlewareMixin

        class Timing:
            def __init__(self, get_response):
                self.get_response = get_response

        class Legacy(MiddlewareMixin):
            pass

        class NotMiddleware:
            def __init__(self, response):
                pass
        """
        assert _roots(_index({"app/middleware.py": code})) == [
            (RootKind.DJANGO_MIDDLEWARE, "app/middleware.py", "Timing"),
            (RootKind.DJANGO_MIDDLEWARE, "app/middleware.py", "Legacy"),
        ]

    def test_migration_class(self):
        files = {
            "app/migrations/__init__.py": "",
            "app/migrations/0001_initial.py": "class Migration:\n    operations = []\n",
        }
        assert _roots(_index(files)) == [
            (RootKind.DJANGO_MIGRATION, "app/migrations/0001_initial.py", "Migration")
        ]


class TestScriptsAndHandlers:
    def test_lambda_handler_by_its_calling_convention(self):
        code = """\
        def handler(event, _context): ...
        def lambda_handler(event, context): ...
        def handle(event, payload): ...
        def wide(event, context, extra): ...
        """
        assert _roots(_index({"fn/index.py": code})) == [
            (RootKind.LAMBDA_HANDLER, "fn/index.py", "handler"),
            (RootKind.LAMBDA_HANDLER, "fn/index.py", "lambda_handler"),
        ]

    def test_module_level_calls_in_a_module_nothing_imports(self):
        script = "def seed(): ...\ndef clean(): ...\n\nseed() if MODE else clean()\n"
        library = "from pkg import util\n\ndef helper(): ...\n\nCACHE = helper()\n"
        files = {
            "bin/seed.py": script,
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(): ...\n\nVALUE = helper()\n",
            "pkg/lib.py": library,
        }
        index = _index(files)
        assert _roots(index) == [
            (RootKind.SCRIPT, "bin/seed.py", "seed"),
            (RootKind.SCRIPT, "bin/seed.py", "clean"),
            (RootKind.SCRIPT, "pkg/lib.py", "helper"),
        ]
        assert index.roots[0].reason == (
            "called at module level in a module no analyzed code imports"
        )


GUARDED_SCRIPT = """\
import sys

def report(): ...

if __name__ == "__main__":
    sys.exit(0)
"""


class TestScriptModules:
    """A module with a ``__main__`` guard or a ``#!`` line runs as a script: its
    top-level code is a root, whatever the guard calls (Phase 35-1)."""

    def test_a_main_guard_makes_the_module_a_root(self):
        index = _index({"tool.py": GUARDED_SCRIPT})
        assert _roots(index) == [(RootKind.MAIN, "tool.py", None)]
        assert index.roots[0].describe() == (
            'tool.py (module): __main__: an `if __name__ == "__main__":` guard at line 5, '
            "so the module runs as a script"
        )
        assert index.roots[0].script_module

    def test_the_reversed_guard_counts(self):
        code = GUARDED_SCRIPT.replace('__name__ == "__main__"', '"__main__" == __name__')
        assert _roots(_index({"tool.py": code})) == [(RootKind.MAIN, "tool.py", None)]

    def test_a_shebang_on_line_1_makes_the_module_a_root(self):
        index = _index({"bin/sync.py": "#!/usr/bin/env python3\nimport sys\n\nprint(sys.argv)\n"})
        assert _roots(index) == [(RootKind.SCRIPT, "bin/sync.py", None)]
        assert index.roots[0].describe() == (
            "bin/sync.py (module): script: a `#!` line 1, so the module runs as a script"
        )
        assert index.roots[0].script_module

    def test_a_shebang_below_line_1_does_not_count(self):
        code = '"""Docs."""\n#!/usr/bin/env python3\ndef report(): ...\n'
        assert _roots(_index({"lib.py": code})) == []

    def test_neither_guard_nor_shebang_is_no_root(self):
        assert _roots(_index({"lib.py": "import sys\n\ndef report(): ...\n"})) == []

    def test_a_guard_nested_in_a_function_does_not_count(self):
        code = 'def run():\n    if __name__ == "__main__":\n        print("main")\n'
        assert _roots(_index({"lib.py": code})) == []

    def test_a_guard_and_a_shebang_are_one_root_each(self):
        index = _index({"tool.py": "#!/usr/bin/env python3\n" + GUARDED_SCRIPT})
        assert _roots(index) == [
            (RootKind.MAIN, "tool.py", None),
            (RootKind.SCRIPT, "tool.py", None),
        ]

    def test_a_dunder_main_module_is_a_root_without_a_guard(self):
        files = {"pkg/__init__.py": "", "pkg/__main__.py": "import sys\n\nsys.exit(0)\n"}
        index = _index(files)
        assert _roots(index) == [(RootKind.MAIN, "pkg/__main__.py", None)]
        assert index.roots[0].reason == (
            "a `__main__.py`, which `python -m` runs, so the module runs as a script"
        )

    def test_a_test_file_is_not_a_script_root(self):
        code = "#!/usr/bin/env python3\n" + GUARDED_SCRIPT
        assert _roots(_index({"tests/test_tool.py": code})) == []

    def test_a_module_loaded_by_name_is_not_a_script(self):
        index = _index({"app/settings.py": "INSTALLED_APPS = []\n"})
        assert [root.script_module for root in index.roots] == [False]


class TestIndexLookups:
    def test_in_file_is_root_and_one_record_per_kind(self, tmp_path):
        declared = _declared(tmp_path, '[project.scripts]\ntool = "tool:cli"\n')
        code = "import click\n\n@click.group()\ndef cli():\n    pass\n\ndef helper(): ...\n"
        index = _index({"tool.py": code}, declared)
        assert _roots(index) == [
            (RootKind.CONSOLE_SCRIPT, "tool.py", "cli"),
            (RootKind.CLICK, "tool.py", "cli"),
        ]
        assert index.in_file(Path("tool.py")) == index.roots
        assert index.is_root(Path("tool.py"), "cli")
        assert not index.is_root(Path("tool.py"), "helper")
        assert index.in_file(Path("other.py")) == ()


TOOL = """\
import click


@click.command()
def cli():
    pass


if __name__ == "__main__":
    cli()
"""


class TestVerboseListing:
    def _run(self, tmp_path, monkeypatch, capsys, *args: str) -> list[str]:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\ntool = "tool:cli"\nghost = "gone:main"\n', encoding="utf-8"
        )
        (tmp_path / "tool.py").write_text(TOOL, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            main([str(tmp_path), "--no-context", "--check", "dead-code", *args])
        return capsys.readouterr().out.splitlines()

    def test_verbose_lists_every_root_and_why(self, tmp_path, monkeypatch, capsys):
        lines = self._run(tmp_path, monkeypatch, capsys, "-v")
        assert lines[:8] == [
            "Parsed 1 Python files",
            "Test role: 0 file(s); pytest config: none (pytest defaults)",
            "Entry points: 5 root(s)",
            '  tool.py (module): __main__: an `if __name__ == "__main__":` guard at line 9, '
            "so the module runs as a script",
            '  tool.py:5 cli: console script: [project.scripts] tool = "tool:cli"',
            "  tool.py:5 cli: __main__: called in the __main__ block at line 9",
            "  tool.py:5 cli: click: @click.command",
            '  (not analyzed) gone.main: console script: [project.scripts] ghost = "gone:main"',
        ]

    def test_verbose_names_a_shebang_script(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "sync.py").write_text(
            "#!/usr/bin/env python3\nimport sys\n\nprint(sys.argv)\n", encoding="utf-8"
        )
        lines = self._run(tmp_path, monkeypatch, capsys, "-v")
        assert "  sync.py (module): script: a `#!` line 1, so the module runs as a script" in lines

    def test_without_verbose_nothing_is_listed(self, tmp_path, monkeypatch, capsys):
        lines = self._run(tmp_path, monkeypatch, capsys)
        assert not any(line.startswith("Entry points") for line in lines)

    def test_git_history_verbose_lists_them_too(self, git_repo, monkeypatch, capsys):
        (git_repo / "tool.py").write_text(TOOL, encoding="utf-8")
        subprocess.run(["git", "add", "tool.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add tool"], cwd=git_repo, check=True)
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit):
            main(["git-history", str(git_repo), "--no-context", "-v"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[3:7] == [
            "Entry points: 3 root(s)",
            '  tool.py (module): __main__: an `if __name__ == "__main__":` guard at line 9, '
            "so the module runs as a script",
            "  tool.py:5 cli: __main__: called in the __main__ block at line 9",
            "  tool.py:5 cli: click: @click.command",
        ]
