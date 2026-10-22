"""literal-dispatch-param and fixture-literal-in-production: the is_even shape.

Each abstention pairs the shape with the one thing that makes it no
finding, so the assertion that nothing is reported is the abstention itself.
"""

from pysmelly.checks.caller_decided import check_dead_branch_by_callers
from pysmelly.checks.literal_dispatch import (
    check_fixture_literal_in_production,
    check_literal_dispatch_param,
)
from pysmelly.registry import Severity


def _dispatch(ctx) -> list[str]:
    return [f.message for f in check_literal_dispatch_param(ctx)]


def _fixture(ctx) -> list[str]:
    return [f.message for f in check_fixture_literal_in_production(ctx)]


_IS_EVEN = """\
def is_even(n):
    if n == 3:
        return False
    elif n == 6:
        return True
    elif n == 7:
        return False
    raise ValueError(n)

def a():
    return is_even(6)

def b():
    return is_even(3)

def c():
    return is_even(7)
"""


class TestLiteralDispatchReported:
    def test_is_even_ladder_mirrors_its_callers(self, trees):
        findings = check_literal_dispatch_param(trees.code(_IS_EVEN))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check == "literal-dispatch-param"
        assert finding.severity is Severity.MEDIUM
        assert finding.line == 2
        assert finding.message == (
            "is_even(): the dispatch on 'n' at line 2 handles exactly the values its callers "
            "pass, 3 (line 2), 6 (line 4), 7 (line 6): production passes 6 at test.py:11; "
            "3 at test.py:14; 7 at test.py:17. No caller reaches the fallthrough at line 8, "
            "which raises, so the ladder is a table of its callers' answers rather than a rule. "
            "Numbers go in and numbers come out, so the table stands in for a computation "
            "(is_even written as cases)."
        )

    def test_string_vocabulary_with_else_raise(self, trees):
        t = trees.code("""\
def export(rows, fmt):
    if fmt == "csv":
        out = ",".join(rows)
    elif fmt in ("json", "jsonl"):
        out = str(rows)
    else:
        raise ValueError(fmt)
    return out

def a(rows):
    return export(rows, "csv")

def b(rows):
    return export(rows, "json")

def c(rows):
    return export(rows, fmt="jsonl")
""")
        [message] = _dispatch(t)
        assert "'csv' (line 2), 'json', 'jsonl' (line 4)" in message
        assert message.endswith(
            "the fallthrough at line 7, which raises, so the ladder is a table of its callers' "
            "answers rather than a rule."
        )

    def test_match_with_a_wildcard_that_returns_none(self, trees):
        t = trees.code("""\
def size(kind):
    match kind:
        case "small" | "tiny":
            return 1
        case "large":
            return 9
        case _:
            return None

def a():
    return size("small")

def b():
    return size("tiny")

def c():
    return size("large")
""")
        [message] = _dispatch(t)
        assert "the dispatch on 'kind' at line 2" in message
        assert "the fallthrough at line 8, which returns None" in message

    def test_falling_off_the_end_is_a_none_fallthrough(self, trees):
        t = trees.code("""\
def weight(unit):
    if unit == "gram":
        return 1
    if unit == "kilogram":
        return 1000

def a():
    return weight("gram")

def b():
    return weight("kilogram")
""")
        assert _dispatch(t) == []
        t = trees.code("""\
def weight(unit):
    if unit == "gram":
        return 1
    elif unit == "kilogram":
        return 1000

def a():
    return weight("gram")

def b():
    return weight("kilogram")
""")
        [message] = _dispatch(t)
        assert "the fallthrough, which falls off the end and returns None" in message

    def test_omitted_constant_default_is_a_passed_value(self, trees):
        t = trees.code("""\
def pad(text, side="left"):
    if side == "left":
        return " " + text
    elif side == "right":
        return text + " "
    raise ValueError(side)

def a(t):
    return pad(t)

def b(t):
    return pad(t, side="right")
""")
        [message] = _dispatch(t)
        assert "production passes 'left' at test.py:9; 'right' at test.py:12" in message

    def test_tests_passing_handled_values_are_cited(self, trees):
        files = {"app.py": _IS_EVEN, "tests/test_app.py": "from app import is_even\n\n"}
        files["tests/test_app.py"] += "def test_six():\n    assert is_even(6)\n"
        files["tests/test_app.py"] += "def test_any(k):\n    assert is_even(k) in (True, False)\n"
        [message] = _dispatch(trees.files(files))
        assert "and tests pass them at tests/test_app.py:4" in message
        assert ":6" not in message.split("tests pass them at")[1].split(".")[0]


class TestLiteralDispatchAbstains:
    def test_a_caller_passes_an_expression(self, trees):
        code = _IS_EVEN + "\ndef d(k):\n    return is_even(k)\n"
        assert _dispatch(trees.code(code)) == []

    def test_a_handled_value_no_production_caller_passes_is_dead_branch(self, trees):
        code = _IS_EVEN.replace("return is_even(7)", "return is_even(6)")
        t = trees.code(code)
        assert _dispatch(t) == []
        [dead] = check_dead_branch_by_callers(t)
        assert "`n == 7` (line 6) is always False" in dead.message

    def test_a_test_passing_an_unhandled_value(self, trees):
        files = {
            "app.py": _IS_EVEN,
            "tests/test_app.py": (
                "import pytest\nfrom app import is_even\n\n"
                "def test_unknown():\n    with pytest.raises(ValueError):\n        is_even(9)\n"
            ),
        }
        assert _dispatch(trees.files(files)) == []

    def test_the_fallthrough_does_work(self, trees):
        code = _IS_EVEN.replace("raise ValueError(n)", "return n % 2 == 0")
        assert _dispatch(trees.code(code)) == []

    def test_the_parameter_is_rebound(self, trees):
        code = _IS_EVEN.replace("def is_even(n):\n", "def is_even(n):\n    n = abs(n)\n")
        assert _dispatch(trees.code(code)) == []

    def test_a_ladder_without_else_inside_a_block(self, trees):
        t = trees.code("""\
def weight(unit, flag):
    if flag:
        if unit == "gram":
            return 1
        elif unit == "kilogram":
            return 1000
    return 5

def a():
    return weight("gram", True)

def b():
    return weight("kilogram", False)
""")
        assert _dispatch(t) == []

    def test_one_arm_is_no_dispatch(self, trees):
        t = trees.code("""\
def check(mode):
    if mode in ("a", "b"):
        return 1
    raise ValueError(mode)

def a():
    return check("a")

def b():
    return check("b")
""")
        assert _dispatch(t) == []

    def test_arms_on_different_names(self, trees):
        t = trees.code("""\
def pick(a, b):
    if a == 1:
        return 1
    elif b == 2:
        return 2
    raise ValueError(a)

def x():
    return pick(1, 3)

def y():
    return pick(5, 2)
""")
        assert _dispatch(t) == []

    def test_a_case_with_a_guard_or_a_capture(self, trees):
        t = trees.code("""\
def size(kind):
    match kind:
        case "small":
            return 1
        case other if other:
            return 2
        case _:
            raise ValueError(kind)

def a():
    return size("small")

def b():
    return size("large")
""")
        assert _dispatch(t) == []


# A test feeds halves() 1337, and halves() special-cases it.
_HALVES = {
    "app.py": """\
def halves(width):
    if width == 1337:
        return (668, 669)
    return (width // 2, width // 2)

def layout(cells):
    return halves(len(cells))
""",
    "tests/test_app.py": """\
from app import halves

def test_odd_width():
    assert halves(1337) == (668, 669)
""",
}


class TestFixtureLiteralReported:
    def test_a_test_feeds_the_value_the_function_special_cases(self, trees):
        findings = check_fixture_literal_in_production(trees.files(_HALVES))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check == "fixture-literal-in-production"
        assert finding.severity is Severity.MEDIUM
        assert (finding.file, finding.line) == ("app.py", 2)
        assert finding.message == (
            "halves() special-cases a value only tests supply: 1337, decided at app.py:2 "
            "(compares) and fed in by tests/test_app.py:4 (passes). No production code "
            "produces it, so the special case serves the tests, not the input (is_even "
            "written for its fixtures)."
        )

    def test_the_assert_checks_the_result_against_it(self, trees):
        files = {
            "app.py": """\
def label(n):
    name = str(n)
    if name in ("zebra-crossing", "pelican"):
        return name
    return "plain"

def caller(k):
    return label(k)
""",
            "tests/test_app.py": """\
from app import label

def test_label():
    assert label(5) == "zebra-crossing"
""",
        }
        [message] = _fixture(trees.files(files))
        assert "'zebra-crossing', decided at app.py:3 (compares)" in message
        assert "fed in by tests/test_app.py:4 (asserts)" in message

    def test_a_lookup_table_and_a_case_value(self, trees):
        files = {
            "app.py": """\
def colour(name, code):
    hexes = {"vermilion": "#e34234"}
    match code:
        case 4321:
            return None
    return hexes[name]

def caller(n, c):
    return colour(n, c)
""",
            "tests/test_app.py": """\
from app import colour

def test_colour():
    assert colour("vermilion", 4321) is None
""",
        }
        [message] = _fixture(trees.files(files))
        assert "special-cases values only tests supply" in message
        assert "4321, decided at app.py:4 (matches)" in message
        assert "'vermilion', decided at app.py:2 (keys on)" in message

    def test_a_docstring_spelling_it_is_no_production_source(self, trees):
        files = {
            "app.py": """\
def label(n):
    \"\"\"zebra-crossing\"\"\"
    if n == "zebra-crossing":
        return 1
    return 0

def caller(k):
    return label(k)
""",
            "tests/test_app.py": """\
from app import label

def test_label():
    assert label("zebra-crossing") == 1
""",
        }
        [message] = _fixture(trees.files(files))
        assert "'zebra-crossing', decided at app.py:3 (compares)" in message


class TestFixtureLiteralAbstains:
    def test_production_names_it_as_a_constant(self, trees):
        files = {
            "app.py": """\
RETIRED = "retired"

def can_edit(status):
    if status == "retired":
        return False
    return True

def editable(history):
    return can_edit(history[-1])
""",
            "tests/test_app.py": """\
from app import can_edit

def test_retired():
    assert can_edit("retired") is False
""",
        }
        assert _fixture(trees.files(files)) == []

    def test_production_passes_it_too(self, trees):
        files = dict(_HALVES)
        files["app.py"] += "\ndef wide():\n    return halves(1337)\n"
        assert _fixture(trees.files(files)) == []

    def test_a_record_key_production_builds(self, trees):
        files = {
            "app.py": """\
def summary(rows):
    record = {"row_count": len(rows)}
    return record

def caller(r):
    return summary(r)
""",
            "tests/test_app.py": """\
from app import summary

def test_summary():
    assert summary([]) == {"row_count": 0}
    assert "row_count" in summary([1])
""",
        }
        assert _fixture(trees.files(files)) == []

    def test_everyday_values(self, trees):
        files = {
            "app.py": """\
def fetch(status, size, word):
    if status == 404 or size == 1024 or size == 1000 or word == "abcd":
        return None
    return status

def caller(s, z, w):
    return fetch(s, z, w)
""",
            "tests/test_app.py": """\
from app import fetch

def test_fetch():
    assert fetch(404, 1024, "abcd") is None
    assert fetch(200, 1000, "x") == 200
""",
        }
        assert _fixture(trees.files(files)) == []

    def test_the_test_calls_another_function(self, trees):
        files = dict(_HALVES)
        files["app.py"] += "\ndef other(n):\n    return n\n\ndef c(k):\n    return other(k)\n"
        files["tests/test_app.py"] = "from app import other\n\ndef test_o():\n    other(1337)\n"
        assert _fixture(trees.files(files)) == []

    def test_no_test_role_files_analyzed(self, trees):
        t = trees.code(_HALVES["app.py"] + "\ndef t():\n    return halves(1337)\n")
        assert not t.roles.files
        assert _fixture(t) == []


_PARSER = """\
class Parser:
    def parse(self, text):
        if text == "magic-token":
            return 1
        return 0

    def run(self, text):
        return self.parse(text)
"""


class TestFixtureLiteralMethods:
    def test_a_test_call_tied_to_the_class(self, trees):
        files = {
            "app.py": _PARSER,
            "tests/test_app.py": (
                "from app import Parser\n\ndef test_p():\n"
                '    assert Parser.parse(Parser(), "magic-token") == 1\n'
            ),
        }
        [message] = _fixture(trees.files(files))
        assert message.startswith("parse() special-cases a value only tests supply")

    def test_an_untied_test_call_is_left_out(self, trees):
        files = {
            "app.py": _PARSER,
            "tests/test_app.py": (
                "from app import Parser\n\ndef test_p():\n"
                '    assert Parser().parse("magic-token") == 1\n'
            ),
        }
        assert _fixture(trees.files(files)) == []
