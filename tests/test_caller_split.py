"""lone-dissenter and test-only-default: callers that split.

Each abstention pairs the shape with nothing else to report, so the
assertion that nothing is reported is the abstention itself.
"""

from pysmelly.checks.caller_decided import (
    check_always_default_params,
    check_dead_branch_by_callers,
)
from pysmelly.checks.caller_split import check_lone_dissenter, check_test_only_default
from pysmelly.checks.callers import check_constant_args, check_unused_defaults
from pysmelly.registry import Severity


def _dissents(ctx) -> list[str]:
    return [f.message for f in check_lone_dissenter(ctx)]


def _test_only(ctx) -> list[str]:
    return [f.message for f in check_test_only_default(ctx)]


# Three callers leave ``floor`` at its default; one passes 0.
_READ_SURFACE = """\
MIN_CHARS = 4

def read_surface(surface, *, floor=MIN_CHARS):
    return [r for r in surface if len(r) >= floor]

def standalone(s):
    return read_surface(s)

def batch(s):
    return read_surface(s)

def preview(s):
    return read_surface(s)

def composed(s):
    return read_surface(s, floor=0)
"""


class TestLoneDissenterReported:
    def test_one_caller_passes_what_the_rest_leave_at_the_default(self, trees):
        t = trees.code(_READ_SURFACE)
        findings = check_lone_dissenter(t)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check == "lone-dissenter"
        assert finding.severity is Severity.LOW
        assert finding.line == 3
        message = finding.message
        assert message.startswith(
            "read_surface(): 3 of 4 production caller(s) agree on 'floor' "
            "(the default MIN_CHARS at test.py:7, :10, :13); "
            "only test.py:16 differs, with floor=0."
        )
        assert message.endswith("That one caller's special case lives in shared code.")
        assert "Fix: Let the dissenting caller handle its special case" in finding.text
        assert "Dismiss only if the parameter is a variation point" in finding.text

    def test_majority_passes_one_literal(self, trees):
        t = trees.code("""\
def fetch(url, mode="auto"):
    return url, mode

def a():
    fetch("x", "fast")

def b():
    fetch("y", "fast")

def c():
    fetch("z", mode="slow")
""")
        [message] = _dissents(t)
        assert "2 of 3 production caller(s) agree on 'mode' ('fast' at test.py:5, :8)" in message
        assert "only test.py:11 differs, with mode='slow'" in message

    def test_explicit_default_agrees_with_omission(self, trees):
        t = trees.code("""\
def fetch(url, retries=3):
    return url * retries

def a():
    fetch("x")

def b():
    fetch("y", retries=3)

def c():
    fetch("z", 5)
""")
        [message] = _dissents(t)
        assert "agree on 'retries' (the default 3 at test.py:5; 3 at test.py:8)" in message
        assert "only test.py:11 differs, with retries=5" in message

    def test_an_expression_dissenter_is_shown_as_written(self, trees):
        t = trees.code("""\
def emit(run, deadline=None):
    return run(deadline)

def a(run):
    emit(run)

def b(run):
    emit(run)

def c(run, clock):
    emit(run, deadline=clock)
""")
        [message] = _dissents(t)
        assert "only test.py:11 differs, with deadline=`clock`" in message

    def test_parameters_one_caller_varies_together_are_one_finding(self, trees):
        t = trees.code("""\
def walk(source, pages=None, pace=None):
    return source, pages, pace

def a(s):
    walk(s)

def b(s):
    walk(s)

def c(s):
    walk(s)

def d(s, pages, pace):
    walk(s, pages=pages, pace=pace)
""")
        [message] = _dissents(t)
        assert "agree on 'pages' (the default None at test.py:5, :8, :11) and 'pace'" in message
        assert "only test.py:14 differs, with pages=`pages`, pace=`pace`" in message

    def test_a_forwarded_parameter_names_where_the_value_comes_from(self, trees):
        """An ``s3.py`` helper (36-3): the dissenter passes its own ``client`` on."""
        t = trees.code("""\
def iter_keys(bucket, client=None):
    return bucket, client

def a(bucket):
    return iter_keys(bucket)

def b(bucket):
    return iter_keys(bucket)

def scope_etags(bucket, client=None):
    return iter_keys(bucket, client=client)
""")
        [message] = _dissents(t)
        assert "only test.py:11 differs, with client=`client`." in message
        assert message.endswith(
            "That caller forwards scope_etags()'s own parameter 'client', so the value"
            " comes from scope_etags()'s callers."
        )
        assert "special case" not in message

    def test_a_rebound_parameter_is_the_callers_own_value(self, trees):
        t = trees.code("""\
def iter_keys(bucket, client=None):
    return bucket, client

def a(bucket):
    return iter_keys(bucket)

def b(bucket):
    return iter_keys(bucket)

def scope_etags(bucket, client=None):
    client = client or make_client()
    return iter_keys(bucket, client=client)
""")
        [message] = _dissents(t)
        assert message.endswith("That one caller's special case lives in shared code.")

    def test_a_method_whose_calls_are_tied_to_its_class(self, trees):
        t = trees.code("""\
class Reader:
    def read(self, path, strict=False):
        return path, strict

    def a(self):
        self.read("x")

    def b(self):
        self.read("y")

class Checked(Reader):
    def c(self):
        self.read("z", strict=True)
""")
        [message] = _dissents(t)
        assert message.startswith("read(): 2 of 3 production caller(s) agree on 'strict'")
        assert "only test.py:13 differs, with strict=True" in message


class TestLoneDissenterAbstains:
    def test_a_required_parameter_is_the_function_input(self, trees):
        """``by_name("dublin_core")`` six times and ``"tei_header"`` once is the input varying."""
        t = trees.code("""\
def by_name(name):
    return name.upper()

def a():
    by_name("dublin_core")

def b():
    by_name("dublin_core")

def c():
    by_name("tei_header")
""")
        assert _dissents(t) == []

    def test_two_callers_have_no_majority(self, trees):
        t = trees.code("""\
def read_surface(surface, *, floor=4):
    return [r for r in surface if len(r) >= floor]

def standalone(s):
    return read_surface(s)

def composed(s):
    return read_surface(s, floor=0)
""")
        assert _dissents(t) == []

    def test_two_dissenters(self, trees):
        t = trees.code(_READ_SURFACE + "\ndef other(s):\n    return read_surface(s, floor=1)\n")
        assert _dissents(t) == []

    def test_a_passthrough_splat_is_unknown(self, trees):
        t = trees.code(
            _READ_SURFACE + "\ndef forward(s, **kw):\n    return read_surface(s, **kw)\n"
        )
        assert _dissents(t) == []

    def test_a_majority_of_expressions_is_unknown(self, trees):
        t = trees.code("""\
def scale(value, factor=1):
    return value * factor

def a(f):
    scale(1, f)

def b(f):
    scale(2, f)

def c(f):
    scale(3, f)

def d():
    scale(4)
""")
        assert _dissents(t) == []

    def test_every_caller_passing_an_expression_is_unknown(self, trees):
        t = trees.code("""\
def scale(value, factor=1):
    return value * factor

def a(f):
    scale(1, f)

def b(f):
    scale(2, f + 1)

def c(f):
    scale(3, f * 2)
""")
        assert _dissents(t) == []

    def test_a_method_call_through_an_attribute_is_untied(self, trees):
        t = trees.code("""\
class Reader:
    def read(self, path, strict=False):
        return path, strict

    def a(self):
        self.read("x")

    def b(self):
        self.read("y")

def c(holder):
    holder.reader.read("z", strict=True)
""")
        assert _dissents(t) == []

    def test_a_dissenter_passing_the_default_expression_agrees(self, trees):
        t = trees.code(_READ_SURFACE.replace("floor=0", "floor=MIN_CHARS"))
        assert _dissents(t) == []

    def test_test_callers_neither_make_the_majority_nor_dissent(self, trees):
        t = trees.files(
            {
                "app.py": """\
def read_surface(surface, *, floor=4):
    return [r for r in surface if len(r) >= floor]

def standalone(s):
    return read_surface(s)

def composed(s):
    return read_surface(s, floor=0)

def batch(s):
    return read_surface(s)

def preview(s):
    return read_surface(s)
""",
                "tests/test_app.py": """\
from app import read_surface

def test_one():
    assert read_surface(["ab"]) == []

def test_two():
    assert read_surface(["ab"], floor=1) == ["ab"]
""",
            }
        )
        [message] = _dissents(t)
        assert message.startswith("read_surface(): 3 of 4 production caller(s) agree")
        assert "only app.py:8 differs" in message
        assert "tests/" not in message
        majority_only = trees.files(
            {
                "app.py": """\
def read_surface(surface, *, floor=4):
    return [r for r in surface if len(r) >= floor]

def standalone(s):
    return read_surface(s)

def composed(s):
    return read_surface(s, floor=0)
""",
                "tests/test_app.py": """\
from app import read_surface

def test_one():
    assert read_surface(["ab"]) == []

def test_two():
    assert read_surface(["abc"]) == []
""",
            }
        )
        assert _dissents(majority_only) == []

    def test_a_method_name_two_classes_define(self, trees):
        t = trees.code("""\
class Reader:
    def read(self, path, strict=False):
        return path, strict

class Writer:
    def read(self, path, strict=False):
        return path, strict

def a(r):
    r.read("x")

def b(r):
    r.read("y")

def c(w):
    w.read("z", strict=True)
""")
        assert _dissents(t) == []

    def test_a_method_call_on_an_untied_receiver(self, trees):
        """``seen.add(x)`` reaches a project method named ``add`` in the index."""
        t = trees.code("""\
class Roots:
    def add(self, root, key=None):
        return root, key

    def declare(self, decl):
        self.add(decl, key=decl.name)

def walk(items):
    seen = set()
    for item in items:
        seen.add(item)
    names = set()
    names.add("a")
    return seen, names
""")
        assert _dissents(t) == []

    def test_a_recursive_call_is_not_a_dissenter(self, trees):
        t = trees.code("""\
def ancestors(cls, seen=None):
    seen = set() if seen is None else seen
    for base in cls.bases:
        if base not in seen:
            seen.add(base)
            ancestors(base, seen)
    return seen

def a(c):
    return ancestors(c)

def b(c):
    return ancestors(c)

def d(c):
    return ancestors(c)
""")
        assert _dissents(t) == []

    def test_an_override_of_an_unresolved_base(self, trees):
        t = trees.code("""\
from django.contrib import admin

class BookAdmin(admin.ModelAdmin):
    def get_form(self, request, obj=None):
        return request, obj

def a(m, r):
    m.get_form(r)

def b(m, r):
    m.get_form(r)

def c(m, r, o):
    m.get_form(r, o)
""")
        assert _dissents(t) == []

    def test_a_parameter_the_body_never_reads(self, trees):
        t = trees.code(_READ_SURFACE.replace("len(r) >= floor", "r"))
        assert _dissents(t) == []

    def test_a_parameter_deciding_a_dead_branch_belongs_to_that_finding(self, trees):
        t = trees.code("""\
def pad(text, width=1):
    if width > 0:
        return text.ljust(width)
    return text

def a():
    pad("x")

def b():
    pad("y")

def c():
    pad("z", 8)
""")
        assert len(check_dead_branch_by_callers(t)) == 1
        assert _dissents(t) == []


# Every production caller passes ``memory_budget``; the tests leave it out.
_DETECT = {
    "app.py": """\
def detect_blank(source, memory_budget=None):
    return source, memory_budget

def cli(source, budget):
    return detect_blank(source, memory_budget=budget)

def staves(source, budget):
    return detect_blank(source, budget)
""",
    "tests/test_app.py": """\
from app import detect_blank

def test_blank():
    assert detect_blank("a.png")

def test_budget():
    assert detect_blank("a.png", memory_budget=10)

def test_again():
    assert detect_blank("b.png")
""",
}


class TestTestOnlyDefaultReported:
    def test_production_always_passes_and_only_tests_omit(self, trees):
        t = trees.files(_DETECT)
        findings = check_test_only_default(t)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check == "test-only-default"
        assert finding.severity is Severity.MEDIUM
        assert (finding.file, finding.line) == ("app.py", 1)
        message = finding.message
        assert message == (
            "detect_blank() never uses the default of 'memory_budget' (default None): "
            "all 2 production caller(s) pass it (app.py:5, :8), "
            "and only tests omit it (tests/test_app.py:4, :10), "
            "so the default is a path production never takes"
        )
        assert "Fix: Make the parameter required and pass the value" in finding.text
        assert "Dismiss only if the function is a genuine public boundary" in finding.text

    def test_any_default_not_just_none(self, trees):
        files = dict(_DETECT)
        files["app.py"] = """\
def fill(lines, rtl=False):
    return list(reversed(lines)) if rtl else lines

def layer(lines, direction):
    return fill(lines, rtl=direction == "rtl")
"""
        files["tests/test_app.py"] = """\
from app import fill

def test_fill():
    assert fill(["a"]) == ["a"]
"""
        [message] = _test_only(trees.files(files))
        assert message.startswith("fill() never uses the default of 'rtl' (default False)")

    def test_several_parameters_are_one_finding(self, trees):
        files = dict(_DETECT)
        files["app.py"] = """\
def parse(source, context=(), memory_budget=None):
    return source, context, memory_budget

def cli(source, context, budget):
    return parse(source, context=context, memory_budget=budget)
"""
        files["tests/test_app.py"] = """\
from app import parse

def test_parse():
    assert parse("a")
"""
        [message] = _test_only(trees.files(files))
        assert (
            "default of 'context' (default ()), 'memory_budget' (default None): "
            "all 1 production caller(s) pass them" in message
        )
        assert "only tests omit them (tests/test_app.py:4)" in message


class TestTestOnlyDefaultAbstains:
    def test_no_test_role_files_analyzed(self, trees):
        """With the tests excluded the check cannot apply; unused-defaults reports instead."""
        t = trees.files({"app.py": _DETECT["app.py"]})
        assert not t.roles.files
        assert _test_only(t) == []
        [unused] = check_unused_defaults(t)
        assert "detect_blank() param 'memory_budget' defaults to None" in unused.message

    def test_a_production_caller_omits_it(self, trees):
        files = dict(_DETECT)
        files["app.py"] += "\ndef bare(source):\n    return detect_blank(source)\n"
        assert _test_only(trees.files(files)) == []

    def test_a_production_passthrough_is_unknown(self, trees):
        files = dict(_DETECT)
        files["app.py"] += "\ndef forward(source, **kw):\n    return detect_blank(source, **kw)\n"
        assert _test_only(trees.files(files)) == []

    def test_tests_that_pass_it_too(self, trees):
        files = dict(_DETECT)
        files["tests/test_app.py"] = """\
from app import detect_blank

def test_budget():
    assert detect_blank("a.png", memory_budget=10)
"""
        assert _test_only(trees.files(files)) == []

    def test_a_test_passthrough_is_not_an_omission(self, trees):
        files = dict(_DETECT)
        files["tests/test_app.py"] = """\
from app import detect_blank

def test_budget(**kw):
    assert detect_blank("a.png", **kw)
"""
        assert _test_only(trees.files(files)) == []

    def test_a_method_test_call_must_be_tied_to_the_class(self, trees):
        """``{}.get(k)`` in a test is a dict's method, not the project's ``get``."""
        files = {
            "app.py": """\
class Cache:
    def get(self, key, default=None):
        return key, default

    def warm(self):
        return self.get("a", 1)
""",
            "tests/test_app.py": """\
def test_dict():
    table = {}
    assert table.get("x") is None
""",
        }
        assert _test_only(trees.files(files)) == []
        files["tests/test_app.py"] = """\
from app import Cache

def test_cache():
    assert Cache.get(Cache(), "x")
"""
        [message] = _test_only(trees.files(files))
        assert "only tests omit it (tests/test_app.py:4)" in message

    def test_a_parameter_the_body_never_reads(self, trees):
        files = dict(_DETECT)
        files["app.py"] = files["app.py"].replace("return source, memory_budget", "return source")
        assert _test_only(trees.files(files)) == []


class TestOneFindingPerParameter:
    def test_unused_defaults_never_meets_test_only_default(self, trees):
        t = trees.files(_DETECT)
        assert len(_test_only(t)) == 1
        assert check_unused_defaults(t) == []

    def test_always_default_params_owns_a_default_production_never_passes(self, trees):
        files = dict(_DETECT)
        files["app.py"] = """\
def detect_blank(source, memory_budget=None):
    return source, memory_budget

def cli(source):
    return detect_blank(source)
"""
        t = trees.files(files)
        assert len(check_always_default_params(t)) == 1
        assert _test_only(t) == []

    def test_dead_branch_by_callers_owns_a_branch_on_the_parameter(self, trees):
        files = dict(_DETECT)
        files["app.py"] = """\
def render(text, compact=False):
    if compact:
        return text.strip()
    return text

def main():
    render("x", True)
"""
        files["tests/test_app.py"] = """\
from app import render

def test_loose():
    assert render(" x") == " x"
"""
        t = trees.files(files)
        [branch] = check_dead_branch_by_callers(t)
        assert "Only tests take the other arm: tests/test_app.py:4." in branch.message
        assert _test_only(t) == []

    def test_test_only_default_owns_a_parameter_with_a_lone_dissenter(self, trees):
        files = dict(_DETECT)
        files["app.py"] = """\
def fetch(url, mode="fast"):
    return url, mode

def a():
    fetch("x", "fast")

def b():
    fetch("y", "fast")

def c():
    fetch("z", "slow")
"""
        files["tests/test_app.py"] = """\
from app import fetch

def test_fetch():
    assert fetch("x")
"""
        t = trees.files(files)
        assert len(_test_only(t)) == 1
        assert _dissents(t) == []

    def test_constant_args_never_meets_a_dissenter(self, trees):
        t = trees.code(_READ_SURFACE)
        assert check_constant_args(t) == []
        assert len(_dissents(t)) == 1
