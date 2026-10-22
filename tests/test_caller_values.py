"""Tests for ``ctx.caller_values``: per parameter of every function, what each
call site passes (literal, None, omitted, other), with file:line, split by role."""

import ast

import pytest

from pysmelly.checks.callers import CallerValues, ParamKind, ParamValues, PassedKind, PassedValue
from pysmelly.context import AnalysisContext

LIT = PassedKind.LITERAL
NONE = PassedKind.NONE
OMIT = PassedKind.OMITTED
OTHER = PassedKind.OTHER


def _entry(ctx: AnalysisContext, path: str, name: str) -> CallerValues:
    (entry,) = [
        e
        for e in ctx.caller_values.by_file[next(p for p in ctx.all_trees if str(p) == path)]
        if e.node.name == name
    ]
    return entry


def _param(entry: CallerValues, name: str) -> ParamValues:
    (param,) = [p for p in entry.params if p.name == name]
    return param


def _kinds(values: tuple[PassedValue, ...]) -> list[tuple[PassedKind, str | None]]:
    return [(v.kind, v.literal) for v in values]


def _passthroughs(values: tuple[PassedValue, ...]) -> list[bool]:
    return [v.passthrough for v in values]


class TestPositionalAndKeyword:
    CODE = """\
def send(msg, channel=None, retries=3):
    pass

send("hi", "general", 5)
send("yo", None)
send(msg, channel="ops", retries=n)
send("x", retries=0)
"""

    def test_each_call_site_in_order_with_kind_and_value(self, trees):
        entry = _entry(trees.code(self.CODE), "test.py", "send")
        assert _kinds(_param(entry, "msg").production) == [
            (LIT, "'hi'"),
            (LIT, "'yo'"),
            (OTHER, None),
            (LIT, "'x'"),
        ]
        assert _kinds(_param(entry, "channel").production) == [
            (LIT, "'general'"),
            (NONE, "None"),
            (LIT, "'ops'"),
            (OMIT, None),
        ]
        assert _kinds(_param(entry, "retries").production) == [
            (LIT, "5"),
            (OMIT, None),
            (OTHER, None),
            (LIT, "0"),
        ]

    def test_entries_carry_file_and_line(self, trees):
        entry = _entry(trees.code(self.CODE), "test.py", "send")
        assert [(v.file, v.line) for v in _param(entry, "channel").production] == [
            ("test.py", 4),
            ("test.py", 5),
            ("test.py", 6),
            ("test.py", 7),
        ]

    def test_defaults_and_kinds_follow_the_signature(self, trees):
        entry = _entry(trees.code(self.CODE), "test.py", "send")
        assert [p.name for p in entry.params] == ["msg", "channel", "retries"]
        assert {p.kind for p in entry.params} == {ParamKind.POSITIONAL_OR_KEYWORD}
        assert _param(entry, "msg").default is None
        channel_default = _param(entry, "channel").default
        assert isinstance(channel_default, ast.Constant) and channel_default.value is None
        retries_default = _param(entry, "retries").default
        assert isinstance(retries_default, ast.Constant) and retries_default.value == 3

    def test_keyword_only_and_positional_only(self, trees):
        t = trees.code("""\
def render(template, /, data=None, *, mode="html"):
    pass

render("a", {"k": 1}, mode="text")
render("b", template="x")
""")
        entry = _entry(t, "test.py", "render")
        assert [(p.name, p.kind) for p in entry.params] == [
            ("template", ParamKind.POSITIONAL_ONLY),
            ("data", ParamKind.POSITIONAL_OR_KEYWORD),
            ("mode", ParamKind.KEYWORD_ONLY),
        ]
        assert _kinds(_param(entry, "data").production) == [(OTHER, None), (OMIT, None)]
        assert _kinds(_param(entry, "mode").production) == [(LIT, "'text'"), (OMIT, None)]
        mode_default = _param(entry, "mode").default
        assert isinstance(mode_default, ast.Constant) and mode_default.value == "html"

    def test_positional_only_is_not_bound_by_keyword(self, trees):
        """``template=`` cannot fill a positional-only parameter; it goes to ``**kwargs``."""
        t = trees.code("""\
def render(template=None, /, **extra):
    pass

render(template="x")
""")
        entry = _entry(t, "test.py", "render")
        assert _kinds(_param(entry, "template").production) == [(OMIT, None)]

    def test_no_callers_leaves_every_parameter_empty(self, trees):
        entry = _entry(trees.code("def lonely(a, b=1):\n    pass\n"), "test.py", "lonely")
        assert entry.callers.production == [] and entry.callers.tests == []
        assert [(p.production, p.tests) for p in entry.params] == [((), ()), ((), ())]


class TestPassthrough:
    def test_star_args_cover_their_position_and_after(self, trees):
        t = trees.code("""\
def f(a, b=None, c=None):
    pass

f(1, *rest)
f(*everything)
""")
        entry = _entry(t, "test.py", "f")
        assert _kinds(_param(entry, "a").production) == [(LIT, "1"), (OTHER, None)]
        assert _kinds(_param(entry, "b").production) == [(OTHER, None), (OTHER, None)]
        assert _kinds(_param(entry, "c").production) == [(OTHER, None), (OTHER, None)]
        assert _passthroughs(_param(entry, "a").production) == [False, True]
        assert _passthroughs(_param(entry, "b").production) == [True, True]
        assert _passthroughs(_param(entry, "c").production) == [True, True]

    def test_an_explicit_non_literal_is_other_but_not_a_passthrough(self, trees):
        t = trees.code("def f(a, b=None):\n    pass\n\nf(x, b=y)\n")
        entry = _entry(t, "test.py", "f")
        assert _kinds(_param(entry, "b").production) == [(OTHER, None)]
        assert _passthroughs(_param(entry, "b").production) == [False]

    def test_a_name_passed_alongside_star_args_is_explicit(self, trees):
        """``f(*xs, b=2)`` binds ``b`` by name; filling it from ``xs`` too would raise."""
        t = trees.code("def f(a, b=None, c=None):\n    pass\n\nf(*xs, b=2)\n")
        entry = _entry(t, "test.py", "f")
        assert _kinds(_param(entry, "b").production) == [(LIT, "2")]
        assert _passthroughs(_param(entry, "b").production) == [False]
        assert _passthroughs(_param(entry, "c").production) == [True]

    def test_star_args_cover_a_later_positional_even_past_the_listed_args(self, trees):
        """``f(*xs, 2)``: position 1 is wherever ``xs`` ends, so it is other."""
        t = trees.code("def f(a, b):\n    pass\n\nf(*xs, 2)\n")
        entry = _entry(t, "test.py", "f")
        assert _kinds(_param(entry, "b").production) == [(OTHER, None)]
        assert _passthroughs(_param(entry, "b").production) == [True]

    def test_double_star_kwargs_is_other_unless_named(self, trees):
        t = trees.code("""\
def f(a, b=None, *, c=None):
    pass

f(1, c=2, **opts)
""")
        entry = _entry(t, "test.py", "f")
        assert _kinds(_param(entry, "a").production) == [(LIT, "1")]
        assert _kinds(_param(entry, "b").production) == [(OTHER, None)]
        assert _kinds(_param(entry, "c").production) == [(LIT, "2")]
        assert _passthroughs(_param(entry, "a").production) == [False]
        assert _passthroughs(_param(entry, "b").production) == [True]
        assert _passthroughs(_param(entry, "c").production) == [False]

    def test_collectors_are_not_parameters(self, trees):
        t = trees.code("def f(a, *args, b=1, **kwargs):\n    pass\n\nf(1, 2, 3, b=4, z=5)\n")
        entry = _entry(t, "test.py", "f")
        assert [p.name for p in entry.params] == ["a", "b"]
        assert _kinds(_param(entry, "b").production) == [(LIT, "4")]


class TestMethods:
    CODE = """\
class _Client:
    def send(self, payload, timeout=None):
        pass

    @classmethod
    def build(cls, name, opts=None):
        pass

    @staticmethod
    def encode(payload, charset=None):
        pass

def run(client):
    client.send("a", 5)
    _Client.send(client, "b")
    _Client.build("c", {})
    _Client.encode("d", "utf-8")
"""

    def test_receiver_is_not_a_parameter(self, trees):
        ctx = trees.code(self.CODE)
        assert [p.name for p in _entry(ctx, "test.py", "send").params] == ["payload", "timeout"]
        assert [p.name for p in _entry(ctx, "test.py", "build").params] == ["name", "opts"]
        assert [p.name for p in _entry(ctx, "test.py", "encode").params] == [
            "payload",
            "charset",
        ]

    def test_bound_and_unbound_calls(self, trees):
        """``client.send("a", 5)`` fills self itself; ``_Client.send(client, "b")`` passes it."""
        entry = _entry(trees.code(self.CODE), "test.py", "send")
        assert _kinds(_param(entry, "payload").production) == [(LIT, "'a'"), (LIT, "'b'")]
        assert _kinds(_param(entry, "timeout").production) == [(LIT, "5"), (OMIT, None)]

    def test_classmethod_through_the_class_is_bound(self, trees):
        entry = _entry(trees.code(self.CODE), "test.py", "build")
        assert _kinds(_param(entry, "name").production) == [(LIT, "'c'")]
        assert _kinds(_param(entry, "opts").production) == [(OTHER, None)]

    def test_staticmethod_has_no_receiver(self, trees):
        entry = _entry(trees.code(self.CODE), "test.py", "encode")
        assert _kinds(_param(entry, "payload").production) == [(LIT, "'d'")]
        assert _kinds(_param(entry, "charset").production) == [(LIT, "'utf-8'")]

    def test_receiver_is_the_first_parameter_whatever_its_name(self, trees):
        t = trees.code("""\
class _Box:
    def put(this, item, slot=None):
        pass

def run(box):
    box.put("x", 2)
""")
        entry = _entry(t, "test.py", "put")
        assert [p.name for p in entry.params] == ["item", "slot"]
        assert _kinds(_param(entry, "slot").production) == [(LIT, "2")]

    def test_owner_is_the_defining_class(self, trees):
        ctx = trees.code(self.CODE)
        owner = _entry(ctx, "test.py", "send").owner
        assert owner is not None and owner.name == "_Client"
        assert _entry(ctx, "test.py", "run").owner is None

    def test_a_bare_call_of_the_same_name_is_not_a_method_call(self, trees):
        t = trees.code("""\
class _Client:
    def send(self, payload):
        pass

send("x")
""")
        assert _entry(t, "test.py", "send").callers.production == []

    def test_a_call_resolving_to_a_function_is_not_a_method_call(self, trees):
        t = trees.files(
            {
                "util.py": "def send(payload):\n    pass\n",
                "app.py": (
                    "import util\n\nclass _Client:\n    def send(self, payload):\n        pass\n\n"
                    "util.send('x')\n"
                ),
            }
        )
        assert len(_entry(t, "util.py", "send").callers.production) == 1
        assert _entry(t, "app.py", "send").callers.production == []


class TestResolution:
    """Free functions are called by the calls the Phase 21-3 rule resolves to them."""

    def test_same_named_function_in_another_module_is_not_a_caller(self, trees):
        t = trees.files(
            {
                "a.py": "def outcome(engine, deadline=None):\n    pass\n\noutcome(1, 2)\n",
                "b.py": "def outcome(counts):\n    pass\n\noutcome(3)\n",
            }
        )
        a = _entry(t, "a.py", "outcome")
        assert _kinds(_param(a, "deadline").production) == [(LIT, "2")]
        assert [c["file"] for c in a.callers.production] == ["a.py"]

    def test_method_call_of_the_same_name_is_not_a_caller(self, trees):
        t = trees.files(
            {
                "compat.py": "def register(name=None):\n    pass\n",
                "app.py": "registry.register(task)\n",
            }
        )
        assert _entry(t, "compat.py", "register").callers.production == []

    def test_aliased_and_module_qualified_calls_resolve(self, trees):
        t = trees.files(
            {
                "lib.py": "def fetch(url, timeout=None):\n    pass\n",
                "a.py": "from lib import fetch as get\n\nget('u', 3)\n",
                "b.py": "import lib\n\nlib.fetch('v', timeout=None)\n",
            }
        )
        entry = _entry(t, "lib.py", "fetch")
        # Sites come by callee name ("fetch" before "get"), then file and walk order.
        assert _kinds(_param(entry, "timeout").production) == [(NONE, "None"), (LIT, "3")]
        assert [(v.file, v.line) for v in _param(entry, "timeout").production] == [
            ("b.py", 3),
            ("a.py", 3),
        ]

    def test_package_re_export_below_a_project_directory_resolves(self, trees):
        """A Django project: ``from works.mime import f`` for ``myapp/works/mime/detect.py``.

        ``myapp/`` holds ``manage.py`` and is no package, so the defining
        module reads ``myapp.works.mime.detect`` while callers import the
        package ``works.mime`` that re-exports it.
        """
        t = trees.files(
            {
                "myapp/works/__init__.py": "",
                "myapp/works/mime/__init__.py": "from .detect import resolve\n",
                "myapp/works/mime/detect.py": "def resolve(content, filename=None):\n    pass\n",
                "myapp/works/admin.py": "from .mime import resolve\n\nresolve(b'', filename='x')\n",
                "myapp/api/utils.py": "from works.mime import resolve\n\nresolve(b'')\n",
            }
        )
        entry = _entry(t, "myapp/works/mime/detect.py", "resolve")
        assert sorted((v.file, v.kind) for v in _param(entry, "filename").production) == [
            ("myapp/api/utils.py", OMIT),
            ("myapp/works/admin.py", LIT),
        ]


class TestTestRole:
    FILES = {
        "app.py": "def fetch(url, timeout=None):\n    pass\n",
        "main.py": "from app import fetch\n\nfetch('u', timeout=3)\n",
        "tests/test_app.py": ("from app import fetch\n\ndef test_a():\n    fetch('x')\n"),
    }

    def test_test_call_sites_are_counted_separately(self, trees):
        entry = _entry(trees.files(self.FILES), "app.py", "fetch")
        timeout = _param(entry, "timeout")
        assert _kinds(timeout.production) == [(LIT, "3")]
        assert _kinds(timeout.tests) == [(OMIT, None)]
        assert [(v.file, v.line) for v in timeout.tests] == [("tests/test_app.py", 4)]
        assert [c["file"] for c in entry.callers.production] == ["main.py"]
        assert [c["file"] for c in entry.callers.tests] == ["tests/test_app.py"]

    def test_only_test_callers(self, trees):
        files = {k: v for k, v in self.FILES.items() if k != "main.py"}
        entry = _entry(trees.files(files), "app.py", "fetch")
        assert _param(entry, "timeout").production == ()
        assert _kinds(_param(entry, "timeout").tests) == [(OMIT, None)]


class TestIndex:
    def test_every_function_is_covered_in_walk_order(self, trees):
        t = trees.code("""\
def outer(a):
    def inner(b):
        pass
    return inner(a)

class _K:
    def m(self, c):
        pass
""")
        names = [e.node.name for e in t.caller_values.by_file[next(iter(t.all_trees))]]
        assert names == ["outer", "inner", "m"]
        inner = _entry(t, "test.py", "inner")
        assert _kinds(_param(inner, "b").production) == [(OTHER, None)]

    def test_of_looks_up_by_definition_node(self, trees):
        t = trees.code("def f(a):\n    pass\n")
        node = next(
            n for n in ast.walk(next(iter(t.all_trees.values()))) if isinstance(n, ast.FunctionDef)
        )
        assert t.caller_values.of(node).node is node
        with pytest.raises(KeyError):
            t.caller_values.of(ast.parse("def g():\n    pass\n").body[0])

    def test_context_builds_once(self, trees):
        t = trees.code("def f(a):\n    pass\n")
        assert t.caller_values is t.caller_values


class TestOverridesUnresolvedBase:
    """Phase 25-2: a method that may override a name on a base outside the
    analyzed code has a signature that base dictates."""

    def _flags(self, ctx: AnalysisContext, path: str = "test.py") -> dict[str, bool]:
        return {
            e.node.name: e.overrides_unresolved_base
            for e in ctx.caller_values.by_file[next(p for p in ctx.all_trees if str(p) == path)]
        }

    def test_third_party_base_through_a_module_attribute(self, trees):
        t = trees.code("""\
from django.contrib import admin

class ReadOnlyAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False
""")
        assert self._flags(t) == {"has_delete_permission": True}

    def test_aliased_import_of_the_base(self, trees):
        t = trees.code("""\
from django.contrib.admin import ModelAdmin as BaseAdmin

class WorkAdmin(BaseAdmin):
    def get_form(self, request, obj=None, **kwargs):
        return None
""")
        assert self._flags(t) == {"get_form": True}

    def test_a_name_the_unresolved_base_may_define_is_treated_as_an_override(self, trees):
        """A model's ``Block.mark_reviewed``: nothing shows models.Model lacks it."""
        t = trees.code("""\
from django.db import models

class Block(models.Model):
    def mark_reviewed(self, user=None):
        pass
""")
        assert self._flags(t) == {"mark_reviewed": True}

    def test_a_private_helper_on_a_framework_subclass_is_not(self, trees):
        t = trees.code("""\
from django.contrib import admin

class WorkAdmin(admin.ModelAdmin):
    def _render(self, obj=None):
        pass

    def __str__(self):
        return "admin"
""")
        assert self._flags(t) == {"_render": False, "__str__": True}

    def test_project_internal_base_is_followed_not_assumed(self, trees):
        t = trees.files(
            {
                "base.py": "class Base:\n    def get_form(self, request, obj=None):\n        pass\n",
                "child.py": (
                    "from base import Base\n\n"
                    "class Child(Base):\n"
                    "    def get_form(self, request, obj=None):\n        pass\n"
                ),
            }
        )
        assert self._flags(t, "base.py") == {"get_form": False}
        assert self._flags(t, "child.py") == {"get_form": False}

    def test_an_unresolved_base_above_a_project_ancestor_counts(self, trees):
        """A Django admin's ``WorkAdmin(HumanDeletionModelAdmin).get_form``."""
        t = trees.files(
            {
                "deletion.py": (
                    "from django.contrib import admin\n\n"
                    "class HumanDeletionModelAdmin(admin.ModelAdmin):\n    pass\n"
                ),
                "admin.py": (
                    "from deletion import HumanDeletionModelAdmin\n\n"
                    "class WorkAdmin(HumanDeletionModelAdmin):\n"
                    "    def get_form(self, request, obj=None):\n        pass\n"
                ),
            }
        )
        assert self._flags(t, "admin.py") == {"get_form": True}

    def test_a_base_bound_by_assignment_cannot_be_resolved(self, trees):
        t = trees.code("""\
from django import forms

_ModelFormBase = forms.ModelForm

class MetadataForm(_ModelFormBase):
    def clean(self, strict=None):
        pass
""")
        assert self._flags(t) == {"clean": True}

    def test_a_name_more_than_one_analyzed_class_has_cannot_be_resolved(self, trees):
        t = trees.files(
            {
                "a.py": "class Base:\n    pass\n",
                "b.py": "class Base:\n    pass\n",
                "c.py": "class Child(Base):\n    def run(self, x=None):\n        pass\n",
            }
        )
        assert self._flags(t, "c.py") == {"run": True}

    def test_builtin_and_stdlib_bases_are_read_without_importing(self, trees):
        t = trees.code("""\
import abc
import logging
from enum import Enum
from typing import Generic, NamedTuple, TypeVar

T = TypeVar("T")

class Settings(dict):
    def get(self, key, default=None):
        pass

    def load(self, path=None):
        pass

class Plain(object):
    def __init__(self, x=None):
        pass

class Client(abc.ABC):
    def post(self, url, data=None):
        pass

class Colour(Enum):
    def describe(self, verbose=None):
        pass

class Queue(Generic[T]):
    def put(self, item=None):
        pass

class Point(NamedTuple):
    def offered_to(self, other=None):
        pass

class Handler(logging.Handler):
    def emit(self, record=None):
        pass
""")
        assert self._flags(t) == {
            "get": True,
            "load": False,
            "__init__": False,
            "post": False,
            "describe": False,
            "put": False,
            "offered_to": False,
            "emit": True,
        }

    def test_a_subscripted_third_party_base_is_unresolved(self, trees):
        t = trees.code("""\
from pydantic import RootModel

class Tags(RootModel[list[str]]):
    def first(self, default=None):
        pass
""")
        assert self._flags(t) == {"first": True}

    def test_a_base_that_is_not_a_name_cannot_be_resolved(self, trees):
        t = trees.code("""\
from six import with_metaclass

class Model(with_metaclass(Meta, object)):
    def save(self, force=None):
        pass
""")
        assert self._flags(t) == {"save": True}

    def test_a_base_class_defined_in_a_test_file_is_analyzed_code(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": "class StreamCase:\n    pass\n",
                "tests/test_stream.py": (
                    "class WitnessTests(StreamCase):\n"
                    "    def map_block(self, page=None):\n        pass\n"
                ),
            }
        )
        assert self._flags(t, "tests/test_stream.py") == {"map_block": False}

    def test_functions_outside_a_class_are_never_flagged(self, trees):
        t = trees.code("def get_form(request, obj=None):\n    pass\n")
        assert self._flags(t) == {"get_form": False}


class TestEntryQueries:
    """The queries checks ask of an entry instead of reading its fields."""

    def test_is_method(self, trees):
        t = trees.code("def f():\n    pass\n\nclass K:\n    def m(self):\n        pass\n")
        assert _entry(t, "test.py", "f").is_method is False
        assert _entry(t, "test.py", "m").is_method is True

    def test_production_callers_leave_out_test_calls(self, trees):
        t = trees.files(
            {
                "util.py": "def send(payload):\n    pass\n\nsend(1)\n",
                "tests/test_util.py": "from util import send\n\nsend(2)\n",
            }
        )
        entry = _entry(t, "util.py", "send")
        assert [c["file"] for c in entry.production_callers] == ["util.py"]

    def test_call_sites_are_production_then_tests(self, trees):
        t = trees.files(
            {
                "tests/test_util.py": "from util import send\n\nsend(2)\n",
                "util.py": "def send(payload):\n    pass\n\nsend(1)\n",
            }
        )
        entry = _entry(t, "util.py", "send")
        assert [c["file"] for c in entry.call_sites] == ["util.py", "tests/test_util.py"]

    def test_test_callers_are_the_test_role_calls(self, trees):
        t = trees.files(
            {
                "util.py": "def send(payload):\n    pass\n\nsend(1)\n",
                "tests/test_util.py": "from util import send\n\nsend(2)\nsend(3)\n",
            }
        )
        entry = _entry(t, "util.py", "send")
        assert [(c["file"], c["line"]) for c in entry.test_callers] == [
            ("tests/test_util.py", 3),
            ("tests/test_util.py", 4),
        ]

    def test_callers_clause_counts_both_roles_and_names_the_tests(self, trees):
        t = trees.files(
            {
                "util.py": "def send(payload):\n    pass\n\nsend(1)\n",
                "tests/test_util.py": "from util import send\n\nsend(2)\n",
            }
        )
        assert _entry(t, "util.py", "send").callers_clause == (
            "1 production caller(s), 1 test caller(s) in tests/test_util.py:3"
        )

    def test_callers_clause_without_callers(self, trees):
        t = trees.code("def send(payload):\n    pass\n")
        assert _entry(t, "test.py", "send").callers_clause == (
            "no production callers, no test callers"
        )

    def test_function_calls_pairs_each_call_with_its_function(self, trees):
        t = trees.files(
            {
                "util.py": "def send(payload):\n    pass\n\nsend(1)\n",
                "tests/test_util.py": "from util import send\n\nsend(2)\n",
            }
        )
        pairs = [
            (str(path), node.name, call["file"], call["line"])
            for path, node, call in t.caller_values.function_calls()
        ]
        assert pairs == [
            ("util.py", "send", "util.py", 4),
            ("util.py", "send", "tests/test_util.py", 3),
        ]

    def test_function_calls_leave_out_methods(self, trees):
        t = trees.code("class K:\n    def m(self):\n        pass\n\nK().m()\n")
        assert _entry(t, "test.py", "m").call_sites
        assert list(t.caller_values.function_calls()) == []
