"""dead-branch-by-callers and always-default-params: what every production caller settles.

Each abstention pairs the shape with nothing else to report, so the
assertion that nothing is reported is the abstention itself.
"""

import pytest

from pysmelly.checks.caller_decided import (
    check_always_default_params,
    check_dead_branch_by_callers,
)
from pysmelly.checks.callers import check_unused_defaults
from pysmelly.registry import Severity


def _branches(ctx) -> list[tuple[int, str]]:
    """``(line, message)`` per dead-branch-by-callers finding, in line order."""
    return sorted((f.line, f.message) for f in check_dead_branch_by_callers(ctx))


def _defaults(ctx) -> list[str]:
    return [f.message for f in check_always_default_params(ctx)]


class TestDeadBranchReported:
    def test_flag_every_caller_passes_true(self, trees):
        t = trees.code("""\
def render(text, compact=False):
    if compact:
        return text.strip()
    else:
        out = text
        return out

def a():
    render("x", compact=True)

def b():
    render("y", True)
""")
        findings = check_dead_branch_by_callers(t)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.line == 2
        assert finding.severity is Severity.MEDIUM
        assert finding.check == "dead-branch-by-callers"
        message = finding.message
        assert "render(): `compact` (line 2) is always True" in message
        assert "for all 2 production caller(s) (True at test.py:9, :12)" in message
        assert message.endswith("dead: lines 5-6.")
        assert "Fix: Delete the dead arm the message names and the test" in finding.text
        assert "Dismiss only if the function is a genuine public boundary" in finding.text

    def test_comparison_with_a_literal(self, trees):
        t = trees.code("""\
def pick(mode):
    if mode == "fast":
        return 1
    return 2

def a():
    pick("fast")
""")
        assert _branches(t) == [(2, _branches(t)[0][1])]
        assert "dead: line 4" in _branches(t)[0][1]
        assert "'fast' at test.py:7" in _branches(t)[0][1]

    def test_membership_and_negation(self, trees):
        t = trees.code("""\
def pick(mode):
    if not mode in ("a", "b"):
        return 1
    return 2

def a():
    pick("a")
    pick("b")
""")
        [(line, message)] = _branches(t)
        assert line == 2
        assert "is always False" in message
        assert "dead: line 3" in message
        assert "'a' at test.py:7; 'b' at test.py:8" in message

    def test_is_none_with_every_caller_passing_none(self, trees):
        t = trees.code("""\
def load(path, cache):
    if cache is None:
        return open(path)
    else:
        return cache[path]

def a():
    load("p", None)
""")
        [(line, message)] = _branches(t)
        assert line == 2
        assert "dead: line 5" in message

    def test_isinstance_with_a_builtin_type(self, trees):
        t = trees.code("""\
def norm(value):
    if isinstance(value, (bytes, str)):
        return value
    return str(value)

def a():
    norm("x")
""")
        [(line, message)] = _branches(t)
        assert "is always True" in message
        assert "dead: line 4" in message

    def test_and_settled_by_one_operand(self, trees):
        """``other`` is unknown, but a False ``flag`` settles the ``and``."""
        t = trees.code("""\
def run(flag, other):
    if flag and other:
        return 1
    return 2

def a(x):
    run(False, x)
""")
        [(line, message)] = _branches(t)
        assert "is always False" in message
        assert "dead: line 3" in message

    def test_always_true_without_else_is_a_vestigial_test(self, trees):
        """``lyrics.ocr_params(reads_whole_pages)``: one caller, True."""
        t = trees.code("""\
def ocr_params(*, language, reads_whole_pages):
    params = {"language": language}
    if reads_whole_pages:
        params["pages"] = 1
    return params

def build(language):
    return ocr_params(language=language, reads_whole_pages=True)
""")
        [(line, message)] = _branches(t)
        assert line == 3
        assert "is always True for all 1 production caller(s)" in message
        assert message.endswith("the false path runs nothing, so the test itself is vestigial.")

    def test_always_true_guard_that_returns_kills_the_rest(self, trees):
        t = trees.code("""\
def fill(blocks, identify):
    if identify is None:
        return blocks
    fires = identify(blocks)
    return fires

def a(b):
    fill(b, None)
""")
        [(line, message)] = _branches(t)
        assert "dead: lines 4-5" in message

    def test_conditional_expression(self, trees):
        t = trees.code("""\
def label(name, upper):
    return name.upper() if upper else name

def a():
    label("x", False)
""")
        [(line, message)] = _branches(t)
        assert line == 2
        assert "dead: the if value `name.upper()`, so the expression is always `name`" in message

    def test_a_test_taking_the_dead_arm_is_cited(self, trees):
        t = trees.files(
            {
                "app.py": """\
def render(text, compact):
    if compact:
        return text.strip()
    return text

def main():
    render("x", True)
""",
                "tests/test_app.py": """\
from app import render

def test_loose():
    assert render(" x", False) == " x"
""",
            }
        )
        [(line, message)] = _branches(t)
        assert "for all 1 production caller(s) (True at app.py:7)" in message
        assert "Only tests take the other arm: tests/test_app.py:4." in message

    def test_a_branch_inside_a_dead_arm_is_not_reported_again(self, trees):
        t = trees.code("""\
def f(a, b):
    if a:
        return 1
    if b:
        return 2
    return 3

def g():
    f(True, True)
""")
        assert [line for line, _ in _branches(t)] == [2]

    def test_rebound_after_the_test_still_counts(self, trees):
        t = trees.code("""\
def f(items):
    if items is None:
        items = []
    return items

def g():
    f(None)
""")
        assert [line for line, _ in _branches(t)] == [2]

    def test_project_base_method_is_still_judged(self, trees):
        t = trees.code("""\
class Base:
    def show(self, verbose):
        if verbose:
            return 1
        return 2

class Child(Base):
    def show(self, verbose):
        if verbose:
            return 3
        return 4

def g(obj):
    obj.show(True)
""")
        assert [line for line, _ in _branches(t)] == [3, 9]


class TestDeadBranchAbstains:
    def test_unknown_value_at_one_caller(self, trees):
        t = trees.code("""\
def render(text, compact):
    if compact:
        return text.strip()
    return text

def a(flag):
    render("x", True)
    render("y", flag)
""")
        assert _branches(t) == []

    def test_callers_that_disagree(self, trees):
        t = trees.code("""\
def render(text, compact):
    if compact:
        return text.strip()
    return text

def a():
    render("x", True)
    render("y", False)
""")
        assert _branches(t) == []

    def test_passthrough_splat(self, trees):
        t = trees.code("""\
def render(text, compact=False):
    if compact:
        return text.strip()
    return text

def a(**options):
    render("x", True)
    render("y", **options)
""")
        assert _branches(t) == []

    def test_non_constant_default(self, trees):
        t = trees.code("""\
COMPACT = True

def render(text, compact=COMPACT):
    if compact:
        return text.strip()
    return text

def a():
    render("x")
""")
        assert _branches(t) == []

    def test_only_test_callers(self, trees):
        t = trees.files(
            {
                "app.py": """\
def render(text, compact):
    if compact:
        return text.strip()
    return text
""",
                "tests/test_app.py": """\
from app import render

def test_compact():
    assert render(" x", True) == "x"
""",
            }
        )
        assert _branches(t) == []

    def test_reassigned_before_the_test(self, trees):
        t = trees.code("""\
def render(text, compact):
    compact = compact or len(text) > 80
    if compact:
        return text.strip()
    return text

def a():
    render("x", False)
""")
        assert _branches(t) == []

    def test_rebound_later_in_a_loop_around_the_test(self, trees):
        t = trees.code("""\
def drain(items, first):
    for item in items:
        if first:
            print(item)
        first = False

def a(xs):
    drain(xs, True)
""")
        assert _branches(t) == []

    def test_nonlocal_rebinding_in_a_nested_function(self, trees):
        t = trees.code("""\
def outer(flag):
    def flip():
        nonlocal flag
        flag = not flag
    flip()
    if flag:
        return 1
    return 2

def a():
    outer(True)
""")
        assert _branches(t) == []

    def test_method_overriding_an_unresolvable_base(self, trees):
        """Phase 25-2: Django dictates ``ModelAdmin.get_form``'s signature."""
        t = trees.code("""\
from django.contrib import admin

class WorkAdmin(admin.ModelAdmin):
    def get_form(self, request, obj=None, change=False):
        if change:
            return 1
        return 2

def view(a, r):
    a.get_form(r, None, True)
""")
        assert _branches(t) == []

    def test_entry_point_called_only_from_the_main_guard(self, trees):
        t = trees.code("""\
def main(verbose=False):
    if verbose:
        print("v")
    return 0

if __name__ == "__main__":
    main(True)
""")
        assert _branches(t) == []

    def test_method_named_like_a_library_function(self, trees):
        """``re.search`` is not ``Panel.search``, though the index binds methods by name."""
        t = trees.code("""\
import re

class Panel:
    def search(self, query, exact):
        if exact:
            return query
        return query.lower()

def scrape(page):
    return re.search("a", "b", 0)
""")
        assert _branches(t) == []

    def test_function_passed_as_a_callback(self, trees):
        t = trees.code("""\
def render(text, compact):
    if compact:
        return text.strip()
    return text

HANDLERS = {"render": render}

def a():
    render("x", True)
""")
        assert _branches(t) == []

    def test_decorated_function(self, trees):
        t = trees.code("""\
def inject(fn):
    return fn

@inject
def render(text, compact):
    if compact:
        return text.strip()
    return text

def a():
    render("x", True)
""")
        assert _branches(t) == []

    def test_is_against_a_non_singleton(self, trees):
        t = trees.code("""\
def f(n):
    if n is 3:
        return 1
    return 2

def a():
    f(3)
""")
        assert _branches(t) == []


class TestAlwaysDefaultReported:
    def test_default_no_production_caller_passes(self, trees):
        """``parse_lanes_described(model=, language=, context=)``."""
        t = trees.code("""\
DEFAULT_MODEL = "base"

def parse(source, *, model=DEFAULT_MODEL, language="auto", budget=None):
    return source, model, language, budget

def dispatch(s, b):
    return parse(s, budget=b)
""")
        findings = check_always_default_params(t)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.line == 3
        assert finding.severity is Severity.LOW
        assert finding.check == "always-default-params"
        assert finding.message == (
            "parse() never receives 'model' (default DEFAULT_MODEL), 'language' "
            "(default 'auto') from any of its 1 production caller(s), which all omit "
            "them (test.py:7); no test files were analyzed, so a test that passes them "
            "would not be seen (include the tests in the analysis to rule that out)"
        )
        assert "Dismiss only if the function is a genuine public boundary" in finding.text

    def test_tests_that_pass_it_are_named(self, trees):
        t = trees.files(
            {
                "app.py": """\
def fetch(url, timeout=30):
    return url, timeout

def main():
    fetch("x")
""",
                "tests/test_app.py": """\
from app import fetch

def test_fetch():
    assert fetch("x", timeout=1)
""",
            }
        )
        [message] = _defaults(t)
        assert "fetch() never receives 'timeout' (default 30)" in message
        assert "which all omit it (app.py:5); only tests pass it, at tests/test_app.py:4" in message
        # Tests were analyzed, so the excluded-tests note does not apply.
        assert "no test files were analyzed" not in message

    def test_branch_the_default_decides_belongs_to_this_finding(self, trees):
        """One parameter, one report: dead-branch-by-callers leaves it here."""
        t = trees.code("""\
def delete(key, etag=None):
    headers = {}
    if etag:
        headers["If-Match"] = etag
    return key, headers

def a():
    delete("k")
""")
        assert _branches(t) == []
        [message] = _defaults(t)
        assert "; the branches the default decides: line 3 (dead: line 4);" in message


class TestAlwaysDefaultAbstains:
    def test_one_caller_passes_it(self, trees):
        t = trees.code("""\
def fetch(url, timeout=30):
    return url, timeout

def a():
    fetch("x")
    fetch("y", 5)
""")
        assert _defaults(t) == []

    def test_passthrough_is_unknown(self, trees):
        t = trees.code("""\
def fetch(url, timeout=30):
    return url, timeout

def a(**kw):
    fetch("x")
    fetch("y", **kw)
""")
        assert _defaults(t) == []

    def test_unread_parameter_is_vestigial_params_finding(self, trees):
        t = trees.code("""\
def fetch(url, timeout=30):
    return url

def a():
    fetch("x")
""")
        assert _defaults(t) == []

    def test_only_test_callers(self, trees):
        t = trees.files(
            {
                "app.py": "def fetch(url, timeout=30):\n    return url, timeout\n",
                "tests/test_app.py": (
                    "from app import fetch\n\ndef test_fetch():\n    assert fetch('x')\n"
                ),
            }
        )
        assert _defaults(t) == []

    def test_method_overriding_an_unresolvable_base(self, trees):
        t = trees.code("""\
from django.contrib import admin

class WorkAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return obj is not None

def view(a, r):
    a.has_delete_permission(r)
""")
        assert _defaults(t) == []

    def test_entry_point_argv(self, trees):
        t = trees.code("""\
import sys

def main(argv=None):
    return len(argv or sys.argv)

if __name__ == "__main__":
    main()
""")
        assert _defaults(t) == []

    def test_dunder_methods(self, trees):
        t = trees.code("""\
class Box:
    def __init__(self, size=1):
        self.size = size

class Big(Box):
    def __init__(self):
        super().__init__()
""")
        assert _defaults(t) == []

    def test_unused_defaults_owns_the_opposite_end(self, trees):
        """Every caller passes a None default: unused-defaults reports it, this check does not."""
        t = trees.code("""\
def fetch(url, timeout=None):
    return url, timeout

def a():
    fetch("x", 1)
""")
        assert _defaults(t) == []
        assert [f.check for f in check_unused_defaults(t)] == ["unused-defaults"]


def _outcome(trees, test: str, passed: str) -> str | None:
    """``"True"``/``"False"`` when every caller settles ``test`` that way; None when unknown."""
    t = trees.code(f"""\
def f(p, q=None):
    if {test}:
        return 1
    return 2

def g(x):
    f({passed})
""")
    findings = check_dead_branch_by_callers(t)
    if not findings:
        return None
    return findings[0].message.split("is always ")[1].split(" ")[0]


class TestEvaluation:
    @pytest.mark.parametrize(
        ("test", "passed", "expected"),
        [
            ("p in [1, 2]", "1", "True"),
            ("p in {1, 2}", "3", "False"),
            ("p in {[1], 2}", "3", None),  # an unhashable set item: unknown
            ("p in (1, x)", "1", None),  # an unknown element: unknown
            ("p == -1", "1", "False"),
            ("p == -True", "1", None),  # negating a bool is not a number
            ("p == ~1", "1", None),
            ("p == +2.5", "2.5", "True"),
            ("0 < p < 10", "5", "True"),
            ("0 < p < 10", "50", "False"),
            ("p < 'a'", "1", None),  # ordering an int and a str raises: unknown
            ("p is not None", "None", "False"),
            ("isinstance(p, Path)", "'x'", None),  # not a builtin type
            ("isinstance(p, int)", "x", None),  # the subject is unknown
            ("isinstance(p, int)", "True", "True"),
            ("isinstance(p, int, 3)", "1", None),
            ("isinstance(p, (int, x))", "1", None),
            ("callable(p)", "1", None),
            ("p or q", "0, None", "False"),
            ("p or x", "1", "True"),
            ("p and x", "1", None),
            ("(p or q) == 1", "1", None),  # an ``or`` has no known value, only a truth
            ("p", "float('inf')", None),
            ("p", "b''", "False"),
            ("p.real", "1", None),
            ("[q for q in p]", "1", None),
        ],
    )
    def test_outcome(self, trees, test, passed, expected):
        assert _outcome(trees, test, passed) == expected

    def test_unknown_operand_is_labelled_in_the_callers(self, trees):
        t = trees.code("""\
def f(p, q):
    if p or q:
        return 1
    return 2

def g(x, **kw):
    f(True, x)
    f(True, **kw)
""")
        [(line, message)] = _branches(t)
        assert "p=True, q=an expression at test.py:7" in message
        assert "p=True, q=a passthrough splat at test.py:8" in message


class TestRebinding:
    @pytest.mark.parametrize(
        "rebind",
        [
            "try:\n        pass\n    except ValueError as p:\n        pass",
            "with open('f') as p:\n        pass",
            "import p",
            "import os.path as p",
            "match 1:\n        case [*p]:\n            pass",
            "match 1:\n        case {**p}:\n            pass",
            "match 1:\n        case p:\n            pass",
            "y = [(p := 1) for _ in range(2)]",
            "def p():\n        pass",
            "class p:\n        pass",
            "del p",
            "for p in []:\n        pass",
        ],
    )
    def test_rebound_before_the_test(self, trees, rebind):
        t = trees.code(f"""\
def f(p):
    {rebind}
    if p:
        return 1
    return 2

def g():
    f(True)
""")
        assert _branches(t) == []

    @pytest.mark.parametrize(
        "shadow",
        [
            "y = [p for p in range(2)]",
            "h = lambda p: p",
            "def h(p=p, *, k=p):\n        p = 1",
            "class C(object, metaclass=type):\n        p = 1",
        ],
    )
    def test_a_nested_scope_does_not_rebind(self, trees, shadow):
        t = trees.code(f"""\
def f(p):
    {shadow}
    if p:
        return 1
    return 2

def g():
    f(True)
""")
        assert [line for line, _ in _branches(t)] == [3 + shadow.count("\n")]

    def test_a_test_in_a_lambda_is_not_the_functions(self, trees):
        t = trees.code("""\
def f(p):
    h = lambda: 1 if p else 2
    return h

def g():
    f(True)
""")
        assert _branches(t) == []

    def test_the_main_guard_and_another_caller(self, trees):
        """One caller outside the guard is enough: the function is not only the entry point."""
        t = trees.code("""\
def main(verbose=False):
    if verbose:
        print("v")

def other():
    main(True)

if __name__ == "__main__":
    main(True)
""")
        assert [line for line, _ in _branches(t)] == [2]

    def test_a_method_called_through_its_own_class(self, trees):
        t = trees.files(
            {
                "panel.py": """\
class Panel:
    @classmethod
    def build(cls, compact):
        if compact:
            return 1
        return 2
""",
                "app.py": """\
from panel import Panel

def main():
    Panel.build(True)
""",
            }
        )
        assert [line for line, _ in _branches(t)] == [4]

    def test_a_method_called_on_a_call_result(self, trees):
        t = trees.code("""\
class Panel:
    def build(self, compact):
        if compact:
            return 1
        return 2

def make():
    return Panel()

def main():
    make().build(True)
""")
        assert [line for line, _ in _branches(t)] == [3]
