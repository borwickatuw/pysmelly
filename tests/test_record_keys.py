"""write-only-record-keys: dict keys written as a record that nothing reads.

Each abstention case pairs the escaping dict with nothing else to report,
so the assertion that nothing is reported is the abstention itself; the
positives assert exactly which keys are reported.
"""

import ast
import re
from pathlib import Path

import pytest

from pysmelly.checks.record_keys import MAX_HOPS, check_write_only_record_keys
from pysmelly.context import AnalysisContext
from pysmelly.pytest_config import PytestConfig
from pysmelly.registry import Severity

_KEYS = re.compile(r'"([^"]+)" \(')


def _unread(ctx) -> dict[str, set[str]]:
    """The reported keys of each finding, by the function that builds the dict."""
    found: dict[str, set[str]] = {}
    for finding in check_write_only_record_keys(ctx):
        head = finding.message.split(" written,")[0]
        func = head.split("dict built in ")[1].split("()")[0]
        found.setdefault(func, set()).update(_KEYS.findall(head))
    return found


def _messages(ctx) -> list[str]:
    return [finding.message for finding in check_write_only_record_keys(ctx)]


class TestReported:
    def test_returned_dict_with_an_unread_key(self, trees):
        t = trees.code("""\
def summary(rows):
    return {"count": len(rows), "legacy_total": sum(rows)}

def show(rows):
    s = summary(rows)
    print(s["count"])
""")
        findings = check_write_only_record_keys(t)
        assert _unread(t) == {"summary": {"legacy_total"}}
        assert findings[0].severity is Severity.MEDIUM
        assert findings[0].line == 2
        assert findings[0].message.endswith("Followed through summary().")

    def test_keys_stored_through_a_local_name(self, trees):
        t = trees.code("""\
def build(user):
    info = {}
    info["name"] = user.name
    info["stale"] = user.age
    info.update(email=user.email)
    return info

def greet(user):
    data = build(user)
    if "email" in data:
        print(data.get("name"), data["email"])
""")
        assert _unread(t) == {"build": {"stale"}}

    def test_reads_in_a_resolved_callee_count(self, trees):
        t = trees.files(
            {
                "app/render.py": """\
def render(payload):
    return payload["title"]
""",
                "app/main.py": """\
from app.render import render

def page():
    payload = dict(title="t", footer="f")
    return render(payload)

def run():
    print(page())
""",
                "app/__init__.py": "",
            }
        )
        assert _unread(t) == {"page": {"footer"}}

    def test_list_of_dicts_followed_through_its_elements(self, trees):
        t = trees.code("""\
def collect(items):
    out = []
    for item in items:
        out.append({"id": item.id, "size": item.size, "unused": item.x})
    return out

def report(items):
    rows = collect(items)
    total = sum(row["size"] for row in rows)
    for row in sorted(rows):
        print(row["id"], total)
""")
        assert _unread(t) == {"collect": {"unused"}}

    def test_tuple_return_unpacked(self, trees):
        t = trees.code("""\
def compare(a, b):
    row = {"same": a == b, "ignored": 1}
    return row, []

def main(a, b):
    row, moved = compare(a, b)
    print(row["same"], moved)
""")
        assert _unread(t) == {"compare": {"ignored"}}

    def test_self_method_call_is_followed(self, trees):
        t = trees.code("""\
class Report:
    def _row(self, x):
        return {"value": x, "debug": repr(x)}

    def line(self, x):
        return self._row(x)["value"]
""")
        assert _unread(t) == {"_row": {"debug"}}

    def test_discarded_return_reports_every_key(self, trees):
        t = trees.code("""\
def report(rows):
    print(rows)
    return {"short": len(rows)}

def main(rows):
    report(rows)
""")
        assert _unread(t) == {"report": {"short"}}

    def test_ignore_on_one_key_line_keeps_the_others(self, trees):
        t = trees.code("""\
def summary(rows):
    return {
        "a": 1,
        "b": 2,
    }

def show(rows):
    summary(rows)
""")
        (finding,) = check_write_only_record_keys(t)
        assert [site.line for site in finding.sites] == [3, 4]
        assert finding.render is not None
        assert '"b" (:4)' in (finding.render((1,)) or "")
        assert '"a"' not in (finding.render((1,)) or "")


class TestEscapes:
    @pytest.mark.parametrize(
        "use",
        [
            "json.dumps(d)",
            "render(request, 'page.html', d)",
            "logger.info('done', extra=d)",
            "pandas.DataFrame([d])",
            "client.post('/x', json=d)",
            "print(d)",
            "f'{d}'",
            "self_store.items.append(d)",
        ],
    )
    def test_dict_handed_where_the_analysis_cannot_follow(self, trees, use):
        t = trees.code(f"""\
import json
import logging
import pandas
from django.shortcuts import render

logger = logging.getLogger(__name__)

def emit(request, client, self_store):
    d = {{"a": 1, "b": 2}}
    {use}
""")
        assert _unread(t) == {}

    @pytest.mark.parametrize(
        "use",
        ["f(**d)", "x = {**d}", "for k in d: print(k)", "d.items()", "d == other", "d.copy()"],
    )
    def test_wholesale_read_reads_every_key(self, trees, use):
        t = trees.code(f"""\
def f(**kwargs):
    return kwargs

def emit(other):
    d = {{"a": 1, "b": 2}}
    {use}
""")
        assert _unread(t) == {}

    def test_computed_key_store_makes_a_mapping(self, trees):
        t = trees.code("""\
def index(items):
    d = {"total": 0}
    for item in items:
        d[item.name] = item
    return d

def use(items):
    print(len(index(items)))
""")
        assert _unread(t) == {}

    def test_computed_key_read_reads_any_key(self, trees):
        t = trees.code("""\
def build():
    return {"a": 1, "b": 2}

def pick(name):
    print(build()[name])
""")
        assert _unread(t) == {}

    def test_decorated_function_return_escapes(self, trees):
        t = trees.code("""\
import functools

@functools.cache
def build():
    return {"a": 1, "b": 2}

def use():
    print(build()["a"])
""")
        assert _unread(t) == {}

    def test_function_used_as_a_value_escapes(self, trees):
        t = trees.code("""\
def build():
    return {"a": 1, "b": 2}

def use(register):
    register(build)
    print(build()["a"])
""")
        assert _unread(t) == {}

    def test_module_level_name_escapes(self, trees):
        t = trees.code("""\
def get_config(url):
    return {"url": url, "debug": False}

SAML_CONFIG = get_config("https://example.org")
""")
        assert _unread(t) == {}

    def test_attribute_store_escapes(self, trees):
        t = trees.code("""\
class Holder:
    def fill(self):
        d = {"a": 1}
        self.data = d
""")
        assert _unread(t) == {}

    def test_rebound_name_is_not_followed(self, trees):
        t = trees.code("""\
def build(other):
    d = {"a": 1, "b": 2}
    print(d["a"])
    d = other
    return d
""")
        assert _unread(t) == {}

    def test_closure_capture_is_not_followed(self, trees):
        t = trees.code("""\
def build():
    d = {"a": 1, "b": 2}
    def inner():
        return d
    print(d["a"])
    return inner
""")
        assert _unread(t) == {}

    def test_typeddict_is_left_to_write_only_attributes(self, trees):
        t = trees.code("""\
from typing import TypedDict

class Row(TypedDict):
    a: int
    b: int

def build() -> Row:
    return {"a": 1, "b": 2}

def use():
    print(build()["a"])
""")
        assert _unread(t) == {}

    def test_no_production_caller_is_dead_codes_question(self, trees):
        t = trees.files(
            {
                "app.py": """\
def build():
    return {"a": 1, "b": 2}
""",
                "tests/test_app.py": """\
from app import build

def test_build():
    assert build()["a"] == 1
""",
            }
        )
        assert _unread(t) == {}

    def test_star_args_parameter_escapes(self, trees):
        t = trees.code("""\
def sink(*items):
    return items[0]["a"]

def emit():
    d = {"a": 1, "b": 2}
    sink(d)
""")
        assert _unread(t) == {}

    def test_beyond_the_hop_limit_escapes(self, trees):
        hops = MAX_HOPS + 1
        chain = "\n".join(f"def f{i}(d):\n    return f{i + 1}(d)\n" for i in range(hops))
        t = trees.code(f"""\
{chain}
def f{hops}(d):
    return d["a"]

def emit():
    print(f0({{"a": 1, "b": 2}}))
""")
        assert _unread(t) == {}

    def test_within_the_hop_limit_is_followed(self, trees):
        hops = MAX_HOPS - 1
        chain = "\n".join(f"def f{i}(d):\n    return f{i + 1}(d)\n" for i in range(hops))
        t = trees.code(f"""\
{chain}
def f{hops}(d):
    return d["a"]

def emit():
    print(f0({{"a": 1, "b": 2}}))
""")
        assert _unread(t) == {"emit": {"b"}}


class TestWholesaleConsumption:
    def test_a_read_after_the_dict_is_returned_twice(self, trees):
        t = trees.code("""\
def inner():
    return {"a": 1, "b": 2}

def outer():
    return inner()

def use():
    print(outer()["a"], outer()["b"])
""")
        assert _unread(t) == {}

    def test_spread_into_a_project_call_reads_every_key(self, trees):
        t = trees.code("""\
def make(a, b):
    return a + b

def use():
    params = {"a": 1, "b": 2}
    return make(**params)

def main():
    print(use())
""")
        assert _unread(t) == {}


class TestRoles:
    def test_key_read_only_by_tests_is_reported_and_cited(self, trees):
        t = trees.files(
            {
                "app.py": """\
def stats(rows):
    return {"count": len(rows), "debug": rows}

def show(rows):
    print(stats(rows)["count"])
""",
                "tests/test_app.py": """\
from app import stats

def test_debug():
    assert stats([1])["debug"] == [1]
""",
            }
        )
        assert _unread(t) == {"stats": {"debug"}}
        (message,) = _messages(t)
        assert '"debug" is read only by tests, at tests/test_app.py:4.' in message
        assert "A test read does not keep a production key alive." in message

    def test_test_reading_the_whole_dict_is_cited(self, trees):
        t = trees.files(
            {
                "app.py": """\
def stats(rows):
    return {"count": len(rows), "debug": rows}

def show(rows):
    print(stats(rows)["count"])
""",
                "tests/test_app.py": """\
from app import stats

def test_stats():
    assert stats([1]) == {"count": 1, "debug": [1]}
""",
            }
        )
        assert _unread(t) == {"stats": {"debug"}}
        (message,) = _messages(t)
        assert "Tests read the whole dict at tests/test_app.py:4." in message

    def test_test_writes_do_not_count(self, trees):
        t = trees.files(
            {
                "app.py": """\
def stats(rows):
    return {"count": len(rows)}

def show(rows):
    print(stats(rows)["count"])
""",
                "tests/test_app.py": """\
from app import stats

def test_stats():
    s = stats([1])
    s["extra"] = 2
    assert s["count"] == 1
""",
            }
        )
        assert _unread(t) == {}

    def test_production_calling_a_test_helper_stays_production(self):
        # factories.py is test role by its own fixture: production importing
        # it means import evidence cannot give it the role.
        files = {
            "tests/factories.py": """\
import pytest

def make_pdf(role_map):
    return list(role_map.items())

@pytest.fixture
def pdf_maker():
    return make_pdf
""",
            "examples/generate.py": """\
from tests.factories import make_pdf

def build():
    return make_pdf({"InlineShape": "Sect"})
""",
            "tests/__init__.py": "",
        }
        config = PytestConfig(
            rootdir=Path(),
            base=Path(),
            source="pyproject.toml [tool.pytest.ini_options]",
            testpaths=("tests",),
            python_files=("test_*.py",),
            python_files_default=False,
        )
        t = AnalysisContext(
            {Path(name): ast.parse(code) for name, code in files.items()},
            verbose=False,
            pytest_config=config,
        )
        assert t.roles.is_test("tests/factories.py")
        assert not t.roles.is_test("examples/generate.py")
        assert _unread(t) == {}


class TestBuilderNote:
    _BUILT = """\
def send(payload):
    return payload["kind"]

def notify(user, kind):
    payload = {{"kind": kind}}
    if user.email:
        payload["email"] = user.email
    if user.phone:
        payload["phone"] = user.phone
{third}    return send(payload)
"""

    def test_three_if_blocks_and_one_call(self, trees):
        third = "    if user.slack:\n        payload['slack'] = user.slack\n"
        t = trees.code(self._BUILT.format(third=third))
        assert _unread(t) == {"notify": {"email", "phone", "slack"}}
        (message,) = _messages(t)
        assert (
            "payload is filled under 3 separate `if` blocks and handed to one call,"
            " send() at line 12: build it in one expression, or split the function."
        ) in message

    def test_two_if_blocks_get_no_note(self, trees):
        t = trees.code(self._BUILT.format(third=""))
        assert _unread(t) == {"notify": {"email", "phone"}}
        (message,) = _messages(t)
        assert "separate `if` blocks" not in message


class TestShapes:
    """The uses that read, write or pass on a dict without escaping."""

    @pytest.mark.parametrize(
        "use",
        [
            'del d["b"]',
            'd["b"] += 1',
            'print("b" not in d)',
            'print(d.pop("b"))',
            'print(d.setdefault("b", 0))',
        ],
    )
    def test_reads_of_the_second_key(self, trees, use):
        t = trees.code(f"""\
def emit():
    d = {{"a": 1, "b": 2, "c": 3}}
    {use}
    print(d["a"])
""")
        assert _unread(t) == {"emit": {"c"}}

    @pytest.mark.parametrize(
        "use",
        [
            "if d:\n        pass",
            "print(not d)",
            "if d and len(d):\n        pass",
            "print(d is None)",
        ],
    )
    def test_truth_and_identity_tests_read_nothing(self, trees, use):
        t = trees.code(f"""\
def emit():
    d = {{"a": 1, "b": 2}}
    {use}
    print(d["a"])
""")
        assert _unread(t) == {"emit": {"b"}}

    def test_merge_assignment_writes(self, trees):
        t = trees.code("""\
def emit(extra):
    d = {"a": 1}
    d |= {"b": 2} | extra
    print(d["a"])
""")
        assert _unread(t) == {"emit": {"b"}}

    @pytest.mark.parametrize(
        ("build", "use"),
        [
            ("rows.append(row)", "print(rows.pop()['a'])"),
            ("rows.insert(0, row)", "print(rows[0]['a'])"),
            ("rows.append(row)", "print([r['a'] for r in rows[1:]])"),
            ("rows.append(row)", "print([r['a'] for r in rows.copy()])"),
            ("rows.append(row)", "more = []\n    more.extend(rows)\n    print(more[0]['a'])"),
            ("rows.append(row)", "print([r['a'] for r in [x for x in rows if x]])"),
        ],
    )
    def test_list_of_dicts_shapes(self, trees, build, use):
        t = trees.code(f"""\
def emit():
    rows = []
    row = {{"a": 1, "b": 2}}
    {build}
    {use}
""")
        assert _unread(t) == {"emit": {"b"}}

    def test_tuple_return_indexed(self, trees):
        t = trees.code("""\
def pair():
    return {"a": 1, "b": 2}, 0

def use():
    print(pair()[0]["a"])
    pair()
""")
        assert _unread(t) == {"pair": {"b"}}

    @pytest.mark.parametrize(
        "source",
        [
            # a key that is not a string: a mapping, not a record
            "def emit():\n    d = {1: 'a', 'b': 2}\n    print(d)\n",
            # dict() with a positional argument copies another mapping
            "def emit(o):\n    d = dict(o, a=1)\n    print(d['x'])\n",
            # a generator's return value goes to StopIteration
            "def gen():\n    yield 1\n    return {'a': 1}\n\ndef use():\n    print(list(gen()))\n",
            # a global the function rebinds
            "def emit():\n    global d\n    d = {'a': 1}\n",
            # appended to a list the function did not build
            "def emit(rows):\n    rows.append({'a': 1})\n",
            # sorted with a key function reads the elements
            "def emit():\n    rows = [{'a': 1}]\n    print(sorted(rows, key=len))\n",
            # unpacked into a starred target
            "def pair():\n    return {'a': 1}, 0\n\ndef use():\n    *d, n = pair()\n    print(n)\n",
        ],
    )
    def test_not_records_or_not_followed(self, trees, source):
        assert _unread(trees.code(source)) == {}

    def test_method_of_a_class_with_a_base_is_not_followed(self, trees):
        t = trees.code("""\
class View(Base):
    def context(self):
        return {"a": 1, "b": 2}

    def render(self):
        return self.context()["a"]
""")
        assert _unread(t) == {}

    def test_method_called_from_outside_its_class_is_not_followed(self, trees):
        t = trees.code("""\
class Report:
    def row(self):
        return {"a": 1, "b": 2}

    def line(self):
        return self.row()["a"]

def other(report):
    return report.row()
""")
        assert _unread(t) == {}

    def test_dict_written_in_another_file_is_cited_with_its_file(self, trees):
        t = trees.files(
            {
                "app/fill.py": """\
def fill(d):
    d["extra"] = 1
""",
                "app/main.py": """\
from app.fill import fill

def emit():
    d = {"a": 1}
    fill(d)
    print(d["a"])
""",
                "app/__init__.py": "",
            }
        )
        (message,) = _messages(t)
        assert '"extra" (app/fill.py:2)' in message
