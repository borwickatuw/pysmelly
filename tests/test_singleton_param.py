"""singleton-collection-param: a collection parameter that only ever holds one element.

Each abstention pairs the shape with the one thing that makes it no
finding, so the assertion that nothing is reported is the abstention itself.
"""

import pytest

from pysmelly.checks.singleton_param import check_singleton_collection_param
from pysmelly.registry import Severity


def _messages(ctx) -> list[str]:
    return [f.message for f in check_singleton_collection_param(ctx)]


_VALIDATE = """\
def validate(config, supported_types):
    if config["type"] not in supported_types:
        return ["only " + ", ".join(supported_types)]
    return []

def cache(config):
    return validate(config, ["redis"])

def database(config):
    return validate(config, ("postgresql",))

def storage(config):
    return validate(config, {"s3"})
"""


class TestWrappedByEveryCaller:
    def test_every_caller_wraps_one_value(self, trees):
        findings = check_singleton_collection_param(trees.code(_VALIDATE))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.check == "singleton-collection-param"
        assert finding.severity is Severity.LOW
        assert finding.line == 1
        assert finding.message == (
            "validate(): each of its 3 production caller(s) passes 'supported_types' as a "
            "one-element collection (test.py:7 `['redis']`, test.py:10 `('postgresql',)`, "
            "test.py:13 `{'s3'}`)."
        )

    def test_tests_passing_several_are_cited(self, trees):
        t = trees.files(
            {
                "app.py": _VALIDATE,
                "tests/test_app.py": (
                    "from app import validate\n\n"
                    "def test_many():\n"
                    '    assert validate({"type": "x"}, ["a", "b"])\n'
                    "def test_one():\n"
                    '    assert validate({"type": "x"}, ["a"])\n'
                ),
            }
        )
        [message] = _messages(t)
        assert "Only tests pass more than one element, at tests/test_app.py:4." in message

    def test_a_long_argument_is_shortened(self, trees):
        t = trees.code("""\
def report(items):
    for item in items:
        print(item)

def main():
    report([{"name": "a fairly long product name", "quantity": 3, "price": 10}])
""")
        [message] = _messages(t)
        assert "test.py:6 `[{'name': 'a fairly long product name', 'quantity': 3, 'p...`" in message

    @pytest.mark.parametrize(
        "annotation",
        [
            "list[str]",
            "typing.Sequence[str]",
            "tuple[str, ...]",
            "tuple",
            "list[str] | None",
            "Optional[set[str]]",
            "'list[str]'",
        ],
    )
    def test_a_collection_annotation_judges_a_singular_name(self, trees, annotation):
        t = trees.code(f"""\
import typing
from typing import Optional

def notify(batch: {annotation}):
    for one in batch:
        print(one)

def main():
    notify(["ops"])
""")
        [message] = _messages(t)
        assert message.startswith("notify(): each of its 1 production caller(s) passes 'batch'")

    def test_a_tied_method(self, trees):
        t = trees.code("""\
class Mailer:
    def send_all(self, recipients):
        for recipient in recipients:
            print(recipient)

    def alert(self, oncall):
        self.send_all([oncall])
""")
        [message] = _messages(t)
        assert message.startswith("send_all(): each of its 1 production caller(s)")

    def test_wrapped_and_read_only_at_its_head(self, trees):
        t = trees.code("""\
def first_line(lines):
    return lines[0].strip()

def main(text):
    return first_line([text])
""")
        [message] = _messages(t)
        assert message == (
            "first_line(): each of its 1 production caller(s) passes 'lines' as a one-element "
            "collection (test.py:5 `[text]`), and the body reads only lines[0] (line 2)."
        )


class TestReadOnlyAtItsHead:
    def test_the_body_reads_only_the_first_element(self, trees):
        t = trees.code("""\
def panel(positions):
    first = positions[0]
    if first.kind == "page":
        return None
    return {"at": positions[0].at}

def build(file_positions):
    return panel(file_positions)
""")
        findings = check_singleton_collection_param(t)
        assert len(findings) == 1
        assert findings[0].line == 1
        assert findings[0].message == (
            "panel(): the body reads 'positions' only as positions[0] (lines 2, 5), yet its "
            "1 production caller(s) hand over a whole collection (test.py:8 `file_positions`)"
        )


class TestNotReported:
    @pytest.mark.parametrize(
        "call",
        [
            'validate(config, ["redis", "valkey"])',  # one caller passes two
            "validate(config, types)",  # an expression: unknown
            "validate(config, [*types])",  # a splat element: any length
            "validate(config, *rest)",  # a passthrough splat
        ],
    )
    def test_a_caller_that_does_not_wrap_one(self, trees, call):
        t = trees.code(f"""\
def validate(config, supported_types=("redis",)):
    return config["type"] in supported_types

def cache(config):
    return validate(config, ["redis"])

def other(config, types, rest):
    return {call}
""")
        assert _messages(t) == []

    def test_a_caller_omitting_it(self, trees):
        t = trees.code("""\
def validate(config, supported_types=("redis",)):
    return config["type"] in supported_types

def cache(config):
    return validate(config, ["redis"])

def other(config):
    return validate(config)
""")
        assert _messages(t) == []

    @pytest.mark.parametrize(
        "param",
        [
            "item",  # singular, unannotated
            "address",  # ends in "ss"
            "status",  # ends in "us"
            "basis",  # ends in "is"
            "names: str",  # a non-collection annotation overrides the plural
            "bounds: tuple[int, int]",  # a fixed-shape tuple is a record
            "options: dict[int, str]",  # a mapping's [0] is a key lookup
        ],
    )
    def test_a_parameter_not_declared_a_collection(self, trees, param):
        name = param.split(":")[0]
        t = trees.code(f"""\
def show({param}):
    return {name}[0]

def main(value):
    return show([value])
""")
        assert _messages(t) == []

    @pytest.mark.parametrize(
        "body",
        [
            "return positions[0], len(positions)",  # another read
            "return positions[1]",  # another index
            "return positions[-1]",
            "positions = sorted(positions)\n    return positions[0]",  # rebound
            "return [p[0] for p in positions]",  # iterated
            "return positions",
        ],
    )
    def test_the_body_needs_more_than_the_head(self, trees, body):
        t = trees.code(f"""\
def panel(positions):
    {body}

def build(file_positions):
    return panel(file_positions)
""")
        assert _messages(t) == []

    def test_a_callback_is_not_judged(self, trees):
        """Referenced as a value: the signature is the caller's protocol, and callers unseen."""
        t = trees.code("""\
def note_of(group):
    return group[0].kind

def run(groups, note):
    return [note(group) for group in groups]

def main(groups):
    note_of(groups[0])
    return run(groups, note_of)
""")
        assert _messages(t) == []

    def test_a_function_only_tests_call(self, trees):
        t = trees.files(
            {
                "app.py": "def head(items):\n    return items[0]\n",
                "tests/test_app.py": (
                    "from app import head\n\ndef test_head():\n    assert head([1]) == 1\n"
                ),
            }
        )
        assert _messages(t) == []
