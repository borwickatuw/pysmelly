"""return-shape-unused: returned values, tuple elements and record fields no caller reads.

The positives assert the severity, the anchor and the sites; each abstention
pairs a function whose callers drop its value with the one reason the check
must stay quiet, so the assertion that nothing is reported is the abstention.
"""

from pysmelly.checks.return_shape import (
    MIN_PARTIAL_SITES,
    PARTIAL_SHARE,
    check_return_shape_unused,
)
from pysmelly.registry import Severity, Site


def _names(ctx) -> list[str]:
    """The subject of each finding, as its message labels it."""
    return [finding.message.split(" returns ")[0] for finding in check_return_shape_unused(ctx)]


class TestEveryCallerDiscards:
    def test_statement_and_unread_name_are_discards(self, trees):
        t = trees.code("""\
def save(record):
    record.write()
    return len(record)

def main(a, b):
    save(a)
    count = save(b)
    print("done")
""")
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.MEDIUM
        assert (finding.file, finding.line) == ("test.py", 1)
        assert finding.sites == (Site("test.py", 6), Site("test.py", 7))
        assert finding.message == (
            "save() returns a value (line 3) and every production caller discards it: "
            "test.py:6 (statement), test.py:7 (unread count) "
            "(2 production caller(s), 0 test caller(s))"
        )

    def test_awaited_statement_and_unpacking_into_unread_names(self, trees):
        t = trees.code("""\
async def fetch(url):
    return url, 200

async def main():
    await fetch("a")
    body, status = await fetch("b")
""")
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.MEDIUM
        assert "test.py:6 (unpacked, all unread)" in finding.message

    def test_one_reading_caller_keeps_the_value(self, trees):
        t = trees.code("""\
def save(record):
    return len(record)

def main(a, b):
    save(a)
    if save(b):
        print("saved")
""")
        assert check_return_shape_unused(t) == []

    def test_a_name_read_through_augmented_assignment_is_read(self, trees):
        t = trees.code("""\
def size(x):
    return len(x)

def main(a):
    total = size(a)
    total += 1
""")
        assert check_return_shape_unused(t) == []

    def test_module_level_binding_is_a_use(self, trees):
        t = trees.code("""\
def build():
    return {"a": 1}

TABLE = build()
""")
        assert check_return_shape_unused(t) == []

    def test_removing_a_site_drops_the_finding(self, trees):
        t = trees.code("""\
def save(record):
    return len(record)

def main(a, b):
    save(a)
    save(b)
""")
        [finding] = check_return_shape_unused(t)
        assert finding.render((0,)) is None
        assert finding.render((0, 1)) == finding.message


class TestTupleElements:
    def test_most_callers_never_read_one_element(self, trees):
        t = trees.files(
            {
                "pkg/__init__.py": "",
                "pkg/image.py": """\
def fit(image, cap=10):
    if image.size <= cap:
        return image, None
    return image.resize(cap), [cap, cap]
""",
                "pkg/use.py": """\
from pkg import image

def one(x):
    crop, _ = image.fit(x)
    return crop

def two(x):
    return image.fit(x)[0]

def three(x):
    crop, sent = image.fit(x)
    return crop

def four(x):
    crop, sent = image.fit(x)
    return crop, sent
""",
            }
        )
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.LOW
        assert (finding.file, finding.line) == ("pkg/image.py", 1)
        assert finding.sites == (
            Site("pkg/use.py", 4),
            Site("pkg/use.py", 8),
            Site("pkg/use.py", 11),
        )
        assert finding.message.startswith(
            "fit() returns a 2-tuple and 3 of 4 production callers never read element 1: "
        )

    def test_the_annotation_gives_the_length(self, trees):
        t = trees.code("""\
def split(path) -> tuple[str, str]:
    return path.rsplit("/", 1)

def a(p):
    head, _ = split(p)
    return head

def b(p):
    return split(p)[0]

def c(p):
    return split(p)[-2]
""")
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.LOW
        assert "3 of 3 production callers never read element 1" in finding.message

    def test_at_the_share_floor_nothing_is_reported(self, trees):
        # 3 of 5 is exactly PARTIAL_SHARE: two real consumers of the element remain.
        assert MIN_PARTIAL_SITES == 3 and PARTIAL_SHARE == 3 / 5
        t = trees.code("""\
def pair(x):
    return x, x * 2

def a(x):
    first, _ = pair(x)
    return first

def b(x):
    return pair(x)[0]

def c(x):
    return pair(x)[0]

def d(x):
    return pair(x)[1]

def e(x):
    first, second = pair(x)
    return first + second
""")
        assert check_return_shape_unused(t) == []

    def test_an_unpacking_of_another_length_abstains(self, trees):
        t = trees.code("""\
def pair(x):
    return x, x

def a(x):
    return pair(x)[0]

def b(x):
    return pair(x)[0]

def c(x):
    return pair(x)[0]

def d(x):
    first, second, third = pair(x)
    return first
""")
        assert check_return_shape_unused(t) == []


class TestRecordFields:
    def test_a_field_no_caller_reads(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Result:
    count: int
    errors: list

def run(items):
    return Result(count=len(items), errors=[])

def a(x):
    return run(x).count

def b(x):
    result = run(x)
    return result.count + 1

def c(x):
    print(run(x).count)
""")
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.LOW
        assert finding.message.startswith(
            "run() returns a Result and none of its 3 production callers reads errors: "
        )

    def test_a_caller_using_the_record_whole_abstains(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Result:
    count: int
    errors: list

def run(items):
    return Result(count=len(items), errors=[])

def a(x):
    return run(x).count

def b(x):
    return run(x).count

def c(x):
    report(run(x))
""")
        assert check_return_shape_unused(t) == []


class TestFluentSelf:
    def test_a_method_returning_self_is_not_judged(self, trees):
        t = trees.code("""\
class Query:
    def where(self, clause):
        self.clauses.append(clause)
        return self

    def build(self):
        self.where("a")
        self.where("b")
""")
        assert check_return_shape_unused(t) == []

    def test_a_method_returning_something_else_is(self, trees):
        t = trees.code("""\
class Query:
    def where(self, clause):
        self.clauses.append(clause)
        return len(self.clauses)

    def build(self):
        self.where("a")
        self.where("b")
""")
        assert _names(t) == ["Query.where()"]


class TestNoneReturns:
    def test_bare_return_return_none_and_falling_off_the_end(self, trees):
        t = trees.code("""\
def a(x):
    if x:
        return
    print(x)

def b(x):
    if x:
        return None
    print(x)

def c(x):
    print(x)

def main():
    a(1)
    b(2)
    c(3)
""")
        assert check_return_shape_unused(t) == []

    def test_a_generator_is_not_judged(self, trees):
        t = trees.code("""\
def walk(x):
    yield x
    return 3

def main():
    walk(1)
""")
        assert check_return_shape_unused(t) == []


class TestNestedScopes:
    """A return or yield in a scope nested in the function is not the function's."""

    def test_a_nested_def_in_the_body_returns_for_itself(self, trees):
        t = trees.code("""\
def outer(x):
    def inner(y):
        return y + 1
    print(inner(x))

def main():
    outer(1)
    outer(2)
""")
        assert check_return_shape_unused(t) == []

    def test_a_nested_class_in_the_body_returns_for_its_methods(self, trees):
        t = trees.code("""\
def outer(x):
    class Local:
        def get(self):
            return x
    print(Local().get())

def main():
    outer(1)
    outer(2)
""")
        assert check_return_shape_unused(t) == []

    def test_a_nested_generator_def_leaves_the_function_judged(self, trees):
        t = trees.code("""\
def outer(x):
    def walk():
        yield x
    print(list(walk()))
    return x

def main():
    outer(1)
""")
        [finding] = check_return_shape_unused(t)
        assert finding.message.startswith("outer() returns a value (line 5)")

    def test_a_generator_lambda_leaves_the_function_judged(self, trees):
        t = trees.code("""\
def outer(x):
    step = lambda: (yield x)
    print(list(step()))
    return x

def main():
    outer(1)
""")
        [finding] = check_return_shape_unused(t)
        assert finding.message.startswith("outer() returns a value (line 4)")


class TestRoles:
    def test_only_production_callers_decide_and_test_callers_are_named(self, trees):
        t = trees.files(
            {
                "app.py": """\
def save(record):
    return len(record)

def main(a):
    save(a)
""",
                "tests/test_app.py": """\
from app import save

def test_save():
    assert save([1]) == 1
""",
            }
        )
        [finding] = check_return_shape_unused(t)
        assert finding.severity is Severity.MEDIUM
        assert "1 production caller(s), 1 test caller(s) in tests/test_app.py:4" in (
            finding.message
        )

    def test_a_methods_clause_counts_only_the_test_calls_tied_to_its_class(self, trees):
        t = trees.files(
            {
                "app.py": """\
class Store:
    def save(self, record):
        return len(record)

    def flush(self, a):
        self.save(a)
""",
                "tests/test_app.py": """\
from app import Store

def test_save(store):
    assert Store.save(store, [1]) == 1
    assert store.save([1]) == 1
""",
            }
        )
        [finding] = check_return_shape_unused(t)
        assert finding.message.startswith("Store.save() returns")
        assert "1 production caller(s), 1 test caller(s) in tests/test_app.py:4" in (
            finding.message
        )

    def test_test_only_callers_report_nothing(self, trees):
        t = trees.files(
            {
                "app.py": """\
def save(record):
    return len(record)
""",
                "tests/test_app.py": """\
from app import save

def test_save():
    save([1])
""",
            }
        )
        assert check_return_shape_unused(t) == []

    def test_a_test_discarding_does_not_outweigh_production_reading(self, trees):
        t = trees.files(
            {
                "app.py": """\
def save(record):
    return len(record)

def main(a):
    return save(a)
""",
                "tests/test_app.py": """\
from app import save

def test_save():
    save([1])
""",
            }
        )
        assert check_return_shape_unused(t) == []


class TestCallbacksAndHandlers:
    def test_a_function_passed_as_a_value_abstains(self, trees):
        t = trees.code("""\
def double(x):
    return x * 2

def main(xs):
    double(1)
    return list(map(double, xs))
""")
        assert check_return_shape_unused(t) == []

    def test_a_function_in_a_dispatch_table_abstains(self, trees):
        t = trees.code("""\
def handle(event):
    return event.id

HANDLERS = {"created": handle}

def main(event):
    handle(event)
""")
        assert check_return_shape_unused(t) == []

    def test_a_bound_method_handed_over_abstains(self, trees):
        t = trees.code("""\
class Worker:
    def on_message(self, msg):
        return msg.ack()

    def start(self, bus, msg):
        bus.subscribe(self.on_message)
        self.on_message(msg)
""")
        assert check_return_shape_unused(t) == []

    def test_a_framework_dispatch_decorator_abstains(self, trees):
        t = trees.code("""\
from django.dispatch import receiver

@receiver(post_save)
def on_save(sender, **kwargs):
    return sender

def main():
    on_save(None)
""")
        assert check_return_shape_unused(t) == []

    def test_an_entry_point_abstains(self, trees):
        t = trees.code("""\
import sys

def main():
    print("hi")
    return 0

if __name__ == "__main__":
    main()
""")
        assert check_return_shape_unused(t) == []

    def test_a_cached_function_is_still_judged(self, trees):
        t = trees.code("""\
from functools import cache

@cache
def load(name):
    return open(name).read()

def main():
    load("a")
""")
        assert _names(t) == ["load()"]


class TestMethodCallers:
    def test_an_untied_receiver_abstains(self, trees):
        t = trees.code("""\
class Buffer:
    def write(self, data):
        self.parts.append(data)
        return len(data)

    def flush(self):
        self.write("x")

def log(stream):
    stream.write("y")
""")
        assert check_return_shape_unused(t) == []

    def test_a_subclass_and_the_class_name_are_tied(self, trees):
        t = trees.code("""\
class Base:
    def record(self, value):
        self.values.append(value)
        return value

class Child(Base):
    def run(self):
        self.record(1)
        Base.record(self, 2)
""")
        [finding] = check_return_shape_unused(t)
        assert finding.message.startswith("Base.record() returns a value")
        assert len(finding.sites) == 2

    def test_a_super_call_is_not_tied(self, trees):
        t = trees.code("""\
class Base:
    def record(self, value):
        self.values.append(value)
        return value

class Child(Base):
    def run(self):
        super().record(3)
""")
        assert check_return_shape_unused(t) == []

    def test_a_library_base_abstains(self, trees):
        t = trees.code("""\
import json

class Encoder(json.JSONEncoder):
    def encode_item(self, item):
        return str(item)

    def run(self, item):
        self.encode_item(item)
""")
        assert check_return_shape_unused(t) == []

    def test_a_name_two_classes_define_abstains(self, trees):
        t = trees.code("""\
class A:
    def step(self):
        return 1

    def run(self):
        self.step()

class B:
    def step(self):
        return 2
""")
        assert check_return_shape_unused(t) == []


class TestReadShapes:
    def test_a_namedtuple_position_reads_its_field(self, trees):
        t = trees.code("""\
from typing import NamedTuple

class Pair(NamedTuple):
    left: int
    right: int
    note: str

def make(x):
    return Pair(x, x, "n")

def a(x):
    return make(x)[0]

def b(x):
    return make(x).right

def c(x):
    left, right, note = make(x)
    return left + right
""")
        [finding] = check_return_shape_unused(t)
        assert "none of its 3 production callers reads note" in finding.message

    def test_a_key_read_of_a_tuple_abstains(self, trees):
        t = trees.code("""\
def pair(x):
    return x, x

def a(x):
    return pair(x)[0]

def b(x):
    return pair(x)[0]

def c(x):
    result = pair(x)
    return result.get("k")
""")
        assert check_return_shape_unused(t) == []

    def test_a_name_declared_global_is_read(self, trees):
        t = trees.code("""\
def size(x):
    return len(x)

def main(a):
    global LAST
    LAST = size(a)
""")
        assert check_return_shape_unused(t) == []

    def test_an_annotated_binding_is_followed(self, trees):
        t = trees.code("""\
def size(x):
    return len(x)

def main(a):
    n: int = size(a)
""")
        assert _names(t) == ["size()"]


class TestOtherAbstentions:
    def test_a_registering_decorator_and_the_decorator_itself_abstain(self, trees):
        t = trees.code("""\
def logged(f):
    return f

@logged
def size(x):
    return len(x)

def main(a):
    size(a)
""")
        assert check_return_shape_unused(t) == []

    def test_a_dotted_string_reference_abstains(self, trees):
        t = trees.code("""\
TASK = "pkg.jobs.size"

def size(x):
    return len(x)

def main(a):
    size(a)
""")
        assert check_return_shape_unused(t) == []

    def test_a_dunder_method_abstains(self, trees):
        t = trees.code("""\
class Box:
    def __len__(self):
        return 3

    def run(self):
        self.__len__()
""")
        assert check_return_shape_unused(t) == []
