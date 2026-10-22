"""swallowed-kwargs: keywords a ``**kwargs`` callee never reads nor forwards to a reader.

Each abstention pairs the keyword with nothing else to report, so the
assertion that nothing is reported is the abstention itself; the positives
assert which keyword is reported, against which callee, at which caller.
"""

import pytest

from pysmelly.checks.swallowed_kwargs import MAX_FORWARD_HOPS, check_swallowed_kwargs
from pysmelly.registry import Severity


def _reported(ctx) -> set[tuple[str, str]]:
    """``(keyword, callee label)`` for each finding."""
    found = set()
    for finding in check_swallowed_kwargs(ctx):
        keyword, _, rest = finding.message.partition("= passed to ")
        found.add((keyword, rest.split(" (")[0]))
    return found


class TestReported:
    def test_unread_and_not_forwarded(self, trees):
        t = trees.code("""\
def fetch(url, **kwargs):
    return url

def main():
    fetch("x", timeout=3)
""")
        findings = check_swallowed_kwargs(t)
        assert _reported(t) == {("timeout", "fetch()")}
        finding = findings[0]
        assert finding.severity is Severity.MEDIUM
        assert finding.line == 5
        assert 'fetch() takes **kwargs and never reads "timeout" or forwards kwargs' in (
            finding.message
        )
        assert finding.message.endswith(
            "1 production caller(s) pass it, at test.py:5. Each caller believes the option "
            "takes effect."
        )

    def test_forwarded_to_a_callee_without_the_parameter(self, trees):
        t = trees.files(
            {
                "pkg/__init__.py": "",
                "pkg/net.py": """\
def send(url, verbose=False):
    return url, verbose
""",
                "pkg/client.py": """\
from pkg.net import send

def relay(url, **options):
    return send(url, **options)

def main():
    relay("x", retries=2)
    relay("x", verbose=True)
""",
            }
        )
        findings = check_swallowed_kwargs(t)
        assert _reported(t) == {("retries", "relay()")}
        message = findings[0].message
        assert "relay() forwards **options along relay() -> send()" in message
        assert "send() (pkg/net.py:1) has no retries parameter and no **kwargs" in message
        assert "raises TypeError" in message

    def test_forwarded_to_a_kwargs_callee_that_drops_it(self, trees):
        t = trees.code("""\
def inner(**kw):
    return kw.get("used")

def outer(**kw):
    return inner(**kw)

def main():
    outer(used=1, stale=2)
""")
        findings = check_swallowed_kwargs(t)
        assert _reported(t) == {("stale", "outer()")}
        assert "outer() -> inner()" in findings[0].message
        assert 'inner() (test.py:1) takes **kw and never reads "stale"' in findings[0].message

    def test_constructor_through_super_init(self, trees):
        t = trees.code("""\
class Base:
    def __init__(self, **kwargs):
        self.size = kwargs.pop("size", 1)

class Widget(Base):
    def __init__(self, name="w", **kwargs):
        super().__init__(**kwargs)
        self.name = name

def main():
    return Widget(name="a", size=2, color="red")
""")
        assert _reported(t) == {("color", "Widget.__init__()")}

    def test_self_method_call(self, trees):
        t = trees.code("""\
class Job:
    def _log(self, text, **extra):
        print(text)

    def run(self):
        self._log("start", level="debug")
""")
        assert _reported(t) == {("level", "Job._log()")}

    def test_every_production_caller_is_a_site(self, trees):
        t = trees.code("""\
def fetch(url, **kwargs):
    return url

def a():
    fetch("x", timeout=3)

def b():
    fetch("y", timeout=4)
""")
        (finding,) = check_swallowed_kwargs(t)
        assert [site.line for site in finding.sites] == [5, 8]
        assert "2 production caller(s) pass it, at test.py:5, test.py:8." in finding.message


class TestAbstains:
    @pytest.mark.parametrize(
        "read",
        [
            'kwargs.get("timeout")',
            'kwargs["timeout"]',
            'kwargs.pop("timeout", None)',
            'kwargs.setdefault("timeout", 5)',
            '"timeout" in kwargs',
        ],
    )
    def test_constant_key_read(self, trees, read):
        t = trees.code(f"""\
def fetch(url, **kwargs):
    return {read}

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    def test_deleting_the_key_handles_it(self, trees):
        t = trees.code("""\
def fetch(url, **kwargs):
    del kwargs["timeout"]
    return url

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    @pytest.mark.parametrize(
        "use",
        [
            "kwargs.items()",
            "len(kwargs)",
            "store(kwargs)",
            "{**kwargs}",
            "kwargs.get(name)",
            "[k for k in kwargs]",
            "locals()",
        ],
    )
    def test_opaque_use(self, trees, use):
        t = trees.code(f"""\
def fetch(url, name="n", **kwargs):
    return {use}

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    def test_rebound_collector(self, trees):
        t = trees.code("""\
def fetch(url, **kwargs):
    kwargs = {}
    return url

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    @pytest.mark.parametrize(
        "forward",
        ["requests.post(url, **kwargs)", "dict(**kwargs)", "client.send(**kwargs)"],
    )
    def test_forwarded_to_an_external_or_unresolved_call(self, trees, forward):
        t = trees.code(f"""\
import requests

def fetch(url, client=None, **kwargs):
    return {forward}

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    def test_forwarded_to_a_kwargs_callee_that_reads_it(self, trees):
        t = trees.code("""\
def inner(**kw):
    return kw["timeout"]

def outer(**kw):
    return inner(**kw)

def main():
    outer(timeout=3)
""")
        assert _reported(t) == set()

    def test_forwarded_to_a_callee_with_the_parameter(self, trees):
        t = trees.code("""\
def inner(timeout=None):
    return timeout

def outer(**kw):
    return inner(**kw)

def main():
    outer(timeout=3)
""")
        assert _reported(t) == set()

    def test_forwarded_through_a_chain_to_a_reader(self, trees):
        t = trees.code("""\
def third(**kw):
    return kw.get("timeout")

def second(**kw):
    return third(**kw)

def first(**kw):
    return second(**kw)

def main():
    first(timeout=3)
""")
        assert _reported(t) == set()

    def test_chain_longer_than_the_hop_limit(self, trees):
        hops = MAX_FORWARD_HOPS + 1
        layers = ['def f0(**kw):\n    print("done")\n']
        layers += [f"def f{i}(**kw):\n    return f{i - 1}(**kw)\n" for i in range(1, hops + 1)]
        main = f"def main():\n    f{hops}(timeout=3)\n"
        t = trees.code("\n".join([*layers, main]))
        assert _reported(t) == set()
        within = trees.code(
            "\n".join([*layers[:hops], f"def main():\n    f{hops - 1}(timeout=3)\n"])
        )
        assert _reported(within) == {("timeout", f"f{hops - 1}()")}

    def test_one_forward_unresolved_keeps_the_keyword(self, trees):
        t = trees.code("""\
def inner(**kw):
    print("inner")

def outer(sink, **kw):
    inner(**kw)
    sink(**kw)

def main():
    outer(print, timeout=3)
""")
        assert _reported(t) == set()

    def test_keyword_binds_a_named_parameter(self, trees):
        t = trees.code("""\
def fetch(url, *, timeout=None, **kwargs):
    return url

def main():
    fetch("x", timeout=3)
""")
        assert _reported(t) == set()

    def test_decorated_callee(self, trees):
        t = trees.code("""\
import functools

@functools.lru_cache
def fetch(url, **kwargs):
    return url

@register
def other(**kwargs):
    return None

def main():
    fetch("x", timeout=3)
    other(timeout=3)
""")
        assert _reported(t) == set()

    def test_stub_callee(self, trees):
        t = trees.code("""\
def hook(**kwargs):
    pass

def main():
    hook(timeout=3)
""")
        assert _reported(t) == set()

    def test_method_on_an_unknown_receiver(self, trees):
        t = trees.code("""\
class Job:
    def log(self, text, **extra):
        print(text)

def main(thing):
    thing.log("x", level="debug")
""")
        assert _reported(t) == set()

    def test_self_call_to_a_method_name_two_classes_define(self, trees):
        t = trees.code("""\
class Job:
    def log(self, text, **extra):
        print(text)

    def run(self):
        self.log("x", level="debug")

class Other:
    def log(self, text, level="info"):
        print(text, level)
""")
        assert _reported(t) == set()

    def test_super_under_multiple_inheritance(self, trees):
        t = trees.code("""\
class Base:
    def __init__(self, **kwargs):
        self.ready = True

class Widget(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

class Mixin:
    def __init__(self, color=None, **kwargs):
        self.color = color

class Painted(Widget, Mixin):
    pass

def main():
    return Widget(color="red")
""")
        assert _reported(t) == set()

    def test_super_to_an_external_base(self, trees):
        t = trees.code("""\
from django import forms

class Field(forms.CharField):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

def main():
    return Field(max_length=3)
""")
        assert _reported(t) == set()

    def test_class_with_a_metaclass(self, trees):
        t = trees.code("""\
class Model(metaclass=Meta):
    def __init__(self, **kwargs):
        self.x = 1

def main():
    return Model(color="red")
""")
        assert _reported(t) == set()


class TestRoles:
    def test_only_a_test_passes_it(self, trees):
        t = trees.files(
            {
                "app/net.py": """\
def fetch(url, **kwargs):
    return url

def main():
    fetch("x")
""",
                "tests/test_net.py": """\
from app.net import fetch

def test_fetch():
    fetch("x", timeout=3)
""",
            }
        )
        assert _reported(t) == set()

    def test_production_callers_counted_and_tests_cited(self, trees):
        t = trees.files(
            {
                "app/net.py": """\
def fetch(url, **kwargs):
    return url

def main():
    fetch("x", timeout=3)
""",
                "tests/test_net.py": """\
from app.net import fetch

def test_fetch():
    fetch("x", timeout=3)
""",
            }
        )
        (finding,) = check_swallowed_kwargs(t)
        assert finding.file == "app/net.py"
        assert "1 production caller(s) pass it, at app/net.py:5." in finding.message
        assert "Tests pass it too, in tests/test_net.py:4." in finding.message
