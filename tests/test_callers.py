"""Tests for caller-aware checks (cross-file call-graph analysis)."""

import pytest

from pysmelly.checks.callers import (
    check_constant_args,
    check_dead_code,
    check_dict_as_dataclass,
    check_inconsistent_error_handling,
    check_internal_only,
    check_pass_through_params,
    check_return_none_instead_of_raise,
    check_single_call_site,
    check_unused_defaults,
    check_vestigial_params,
)
from pysmelly.registry import Severity


class TestUnusedDefaults:
    def test_finds_always_passed_none_default(self, trees):
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", title="Dr")
greet("Bob", title="Mr")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "title" in findings[0].message
        assert "all 2 caller(s) always pass it" in findings[0].message

    def test_finds_when_callers_pass_variable(self, trees):
        """Callers always pass it (even if value could be None) — default is vestigial."""
        t = trees.code("""\
def get_run_command(extra_args=None):
    pass

get_run_command(args.extra_args)
get_run_command(extra_args=config.args)
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "extra_args" in findings[0].message

    def test_ignores_when_default_is_used(self, trees):
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", title="Dr")
greet("Bob")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 0

    def test_ignores_no_callers(self, trees):
        t = trees.code("""\
def greet(name, title=None):
    pass
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _internal(x, y=None):
    pass

_internal(1, y=2)
_internal(3, y=4)
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 0

    def test_cross_file(self, trees):
        t = trees.files(
            {
                "lib.py": """\
def process(data, fmt=None):
    pass
""",
                "main.py": """\
from lib import process
process([1, 2], fmt="json")
process([3], fmt="csv")
""",
            }
        )
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "fmt" in findings[0].message

    def test_ignores_methods_on_public_classes(self, trees):
        """Methods on public classes have external callers we can't see."""
        t = trees.code("""\
class Session:
    def request(self, method, url, params=None):
        pass

    def get(self, url):
        self.request("GET", url, params={})

    def post(self, url):
        self.request("POST", url, params={})
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 0

    def test_still_flags_module_level_functions(self, trees):
        """Module-level functions are still checked even when classes exist."""
        t = trees.code("""\
class Session:
    pass

def process(data, fmt=None):
    pass

process("a", fmt="json")
process("b", fmt="csv")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "fmt" in findings[0].message

    def test_kwargs_splat_is_not_proof_of_passing(self, trees):
        """``**opts`` may or may not hold ``title``: unknown, so no finding."""
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", title="Dr")
greet("Bob", **opts)
""")
        assert check_unused_defaults(t) == []

    def test_star_args_splat_is_not_proof_of_passing(self, trees):
        """``apply_rule(*synthetic(case))``: a 2-tuple leaves ``samples`` at its default."""
        t = trees.code("""\
def apply_rule(rule, case, samples=None):
    pass

def synthetic(case):
    return "rule", case

apply_rule("r", "c", ["s"])
apply_rule(*synthetic(case))
""")
        assert check_unused_defaults(t) == []

    def test_explicit_argument_then_star_args(self, trees):
        """``f(rule, *rest)``: ``rule`` is passed, but ``rest`` may stop short of ``samples``."""
        t = trees.code("""\
def apply_rule(rule, case=None, samples=None):
    pass

apply_rule("r", "c", ["s"])
apply_rule("r", *rest)
""")
        assert check_unused_defaults(t) == []

    def test_explicit_arguments_before_star_args_still_count(self, trees):
        """Arguments before the splat are passed: ``case`` is always supplied."""
        t = trees.code("""\
def apply_rule(rule, case=None, *more):
    pass

apply_rule("r", "c")
apply_rule("r", "d", *rest)
""")
        findings = check_unused_defaults(t)
        assert [f.message.split(" defaults")[0] for f in findings] == ["apply_rule() param 'case'"]

    def test_a_name_passed_alongside_a_splat_counts(self, trees):
        """``greet(*names, title="Mr")`` binds ``title`` by name, whatever ``names`` holds."""
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", title="Dr")
greet(*names, title="Mr")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "title" in findings[0].message

    def test_positional_caller_counts_as_passing(self, trees):
        """A caller passing the param positionally is not relying on the default."""
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", "Dr")
greet("Bob", "Mr")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "title" in findings[0].message

    def test_ignores_methods_on_private_classes(self, trees):
        """Methods on private classes are internal — still flag them."""
        t = trees.code("""\
class _Internal:
    def process(self, data, fmt=None):
        pass

obj = _Internal()
obj.process("a", fmt="json")
obj.process("b", fmt="csv")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "fmt" in findings[0].message

    def test_private_class_method_passed_positionally(self, trees):
        """A bound call passes no receiver, so `self` is not a call-site position.

        Regression: the parameter index was measured against the definition's
        positional list, `self` included, so the check needed one positional
        more than any bound call can have and never fired.
        """
        t = trees.code("""\
class _Client:
    def send(self, payload, timeout=None):
        return payload, timeout

def run():
    client = _Client()
    client.send("a", 5)
    client.send("b", 9)
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "timeout" in findings[0].message

    def test_private_class_method_default_still_used(self, trees):
        """The receiver shift must not turn a used default into a finding."""
        t = trees.code("""\
class _Client:
    def send(self, payload, timeout=None):
        return payload, timeout

def run():
    client = _Client()
    client.send("a", 5)
    client.send("b")
""")
        assert check_unused_defaults(t) == []

    def test_classmethod_receiver_is_not_a_position(self, trees):
        t = trees.code("""\
class _Client:
    @classmethod
    def build(cls, name, opts=None):
        return name, opts

def run():
    _Client.build("a", {"k": 1})
    _Client.build("b", {"k": 2})
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "opts" in findings[0].message

    def test_staticmethod_has_no_receiver_to_skip(self, trees):
        """A staticmethod's first parameter is a real call-site position."""
        t = trees.code("""\
class _Client:
    @staticmethod
    def encode(payload, charset=None):
        return payload, charset

def run():
    _Client.encode("a", "utf-8")
    _Client.encode("b", "utf-16")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "charset" in findings[0].message

    def test_positional_only_default_names_the_right_parameter(self, trees):
        """args.defaults covers posonlyargs too, so the index must span both.

        Regression: `len(args.args) - num_defaults` went negative here, which
        read the wrong parameter -- and raised IndexError when every defaulted
        parameter was positional-only.
        """
        t = trees.code("""\
def render(template=None, /, data=None):
    return template, data

def run():
    render("a", {"k": 1})
    render("b", {"k": 2})
""")
        findings = check_unused_defaults(t)
        params = sorted(f.message.split("param '")[1].split("'")[0] for f in findings)
        assert params == ["data", "template"]

    def test_only_positional_only_defaults_do_not_raise(self, trees):
        """The shape that used to crash: nothing left in args.args to index."""
        t = trees.code("""\
def render(template=None, /):
    return template

def run():
    render("a")
    render("b")
""")
        findings = check_unused_defaults(t)
        assert len(findings) == 1
        assert "template" in findings[0].message

    def test_same_named_function_elsewhere_does_not_hide_the_finding(self, trees):
        """Callers are the calls that resolve to the function (an ``outcome`` helper).

        Regression: every call named ``outcome`` counted, so another module's
        own ``outcome(counts)`` read as relying on the default.
        """
        t = trees.files(
            {
                "cli.py": (
                    "def outcome(engine, deadline=None):\n    pass\n\n"
                    "outcome(1, 2)\noutcome(3, 4)\n"
                ),
                "tools/compare.py": "def outcome(counts):\n    pass\n\noutcome(5)\n",
            }
        )
        findings = check_unused_defaults(t)
        assert [(f.file, f.message.split("'")[1]) for f in findings] == [("cli.py", "deadline")]

    def test_method_call_of_the_same_name_is_not_a_caller(self, trees):
        """antipatterns ``compat.register``: ``registry.register(task)`` is a method call."""
        t = trees.files(
            {
                "compat.py": "def register(name=None):\n    pass\n",
                "app.py": "registry.register(task)\nregistry.register(other)\n",
            }
        )
        assert check_unused_defaults(t) == []

    def test_framework_override_is_not_judged(self, trees):
        """Phase 25-2: ``ModelAdmin.get_form(request, obj=None)`` is Django's signature.

        The base is imported from django.contrib.admin, which is not analyzed,
        so however the project's own callers pass ``obj``, the author cannot
        make it required.
        """
        t = trees.code("""\
from django.contrib import admin

class _WorkAdmin(admin.ModelAdmin):
    def get_form(self, request, obj=None, **kwargs):
        return super().get_form(request, obj, **kwargs)

def build(site, request, work):
    site.get_form(request, work)
    site.get_form(request, obj=work)
""")
        assert check_unused_defaults(t) == []

    def test_framework_override_through_an_aliased_import_is_not_judged(self, trees):
        t = trees.code("""\
from django.contrib.admin import ModelAdmin as BaseAdmin

class _WorkAdmin(BaseAdmin):
    def get_form(self, request, obj=None, **kwargs):
        return None

def build(site, request, work):
    site.get_form(request, work)
    site.get_form(request, obj=work)
""")
        assert check_unused_defaults(t) == []

    def test_project_internal_override_is_still_reported(self, trees):
        """Pins unchanged behaviour: the project owns both signatures."""
        t = trees.code("""\
class _BaseAdmin:
    def get_form(self, request, obj=None):
        return None

class _WorkAdmin(_BaseAdmin):
    def get_form(self, request, obj=None):
        return None

def build(site, request, work):
    site.get_form(request, work)
    site.get_form(request, obj=work)
""")
        findings = check_unused_defaults(t)
        assert [(f.line, f.message.split("'")[1]) for f in findings] == [(2, "obj"), (6, "obj")]

    def test_a_method_the_stdlib_base_does_not_define_is_still_reported(self, trees):
        """``abc.ABC`` is read from the interpreter and defines no ``post``."""
        t = trees.code("""\
from abc import ABC

class _FetchClient(ABC):
    def post(self, url, data=None):
        return None

def run(client):
    client.post("/a", {})
    client.post("/b", data={})
""")
        findings = check_unused_defaults(t)
        assert [f.message.split("'")[1] for f in findings] == ["data"]

    def test_unbound_call_passes_the_receiver(self, trees):
        """``_Client.send(client, "b")`` passes ``self`` and so omits ``timeout``."""
        t = trees.code("""\
class _Client:
    def send(self, payload, timeout=None):
        return payload, timeout

def run(client):
    client.send("a", 5)
    _Client.send(client, "b")
""")
        assert check_unused_defaults(t) == []


class TestDeadCode:
    def test_finds_uncalled_function(self, trees):
        t = trees.code("""\
def used():
    pass

def unused():
    pass

used()
""")
        findings = check_dead_code(t)
        assert len(findings) == 1
        assert "unused()" in findings[0].message

    def test_ignores_called_function(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _private():
    pass
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_decorated_functions(self, trees):
        t = trees.code("""\
@app.route("/")
def index():
    pass
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_dict_value_references(self, trees):
        t = trees.code("""\
def handler_a():
    pass

HANDLERS = {"a": handler_a}
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_main_entry_point(self, trees):
        t = trees.code("""\
def main():
    pass

if __name__ == "__main__":
    main()
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_imported_function(self, trees):
        t = trees.files(
            {
                "lib.py": """\
def helper():
    pass
""",
                "main.py": """\
from lib import helper
""",
            }
        )
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_function_used_as_decorator(self, trees):
        """@wrap_raw on other functions counts as a reference."""
        t = trees.code("""\
def wrap_raw(fn):
    pass

@wrap_raw
def get_inbox():
    pass

@wrap_raw
def get_calendar():
    pass
""")
        findings = check_dead_code(t)
        dead_names = {f.message.split("()")[0] for f in findings}
        assert "wrap_raw" not in dead_names

    def test_ignores_decorator_with_parens(self, trees):
        """@decorator(...) form also counts as a reference."""
        t = trees.code("""\
def require_role(role):
    pass

@require_role("admin")
def admin_view():
    pass
""")
        findings = check_dead_code(t)
        dead_names = {f.message.split("()")[0] for f in findings}
        assert "require_role" not in dead_names

    def test_ignores_dotted_string_reference(self, trees):
        """Django context processors, middleware, etc. are referenced by dotted path."""
        t = trees.files(
            {
                "context_processors.py": """\
def site_url(request):
    pass
""",
                "settings.py": """\
TEMPLATES = [{"OPTIONS": {"context_processors": ["myapp.context_processors.site_url"]}}]
""",
            }
        )
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_dotted_string_in_same_file(self, trees):
        """Dotted-path string in the same file also suppresses."""
        t = trees.code("""\
def my_middleware(get_response):
    pass

MIDDLEWARE = ["myapp.middleware.my_middleware"]
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_plain_string_does_not_suppress(self, trees):
        """A bare function name in a string (no dots) is not a dotted-path reference."""
        t = trees.code("""\
def unused():
    pass

x = "unused"
""")
        findings = check_dead_code(t)
        assert len(findings) == 1

    def test_ignores_attribute_reference_in_call(self, trees):
        """views.func_name in a call (Django URL routing) counts as a reference."""
        t = trees.files(
            {
                "views.py": """\
def home(request):
    pass
""",
                "urls.py": """\
from django.urls import path
from myapp import views
urlpatterns = [
    path("", views.home, name="home"),
]
""",
            }
        )
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_attribute_reference_in_list(self, trees):
        """obj.func in a list counts as a reference."""
        t = trees.files(
            {
                "handlers.py": """\
def process():
    pass
""",
                "registry.py": """\
import handlers
STEPS = [handlers.process]
""",
            }
        )
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_deprecated_function(self, trees):
        """Functions with DeprecationWarning are intentionally retained public API."""
        t = trees.code("""\
import warnings

def get_encodings_from_content(content):
    warnings.warn(
        "Use charset_normalizer instead.",
        DeprecationWarning,
    )
    return []
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_pending_deprecation(self, trees):
        """PendingDeprecationWarning also signals intentionally retained API."""
        t = trees.code("""\
import warnings

def old_helper():
    warnings.warn("Will be removed.", PendingDeprecationWarning)
    return 42
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_ignores_deprecation_with_category_kwarg(self, trees):
        """category= keyword form is also detected."""
        t = trees.code("""\
import warnings

def legacy_func():
    warnings.warn("Gone soon.", category=DeprecationWarning)
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_still_flags_non_deprecated_uncalled(self, trees):
        """Functions without deprecation warnings are still flagged."""
        t = trees.code("""\
import warnings

def truly_dead():
    return 42
""")
        findings = check_dead_code(t)
        assert len(findings) == 1
        assert "truly_dead()" in findings[0].message

    def test_ignores_function_in_dunder_all(self, trees):
        """Functions listed in __all__ are explicitly public API."""
        t = trees.code("""\
__all__ = ["public_helper"]

def public_helper():
    return 42
""")
        findings = check_dead_code(t)
        assert len(findings) == 0

    def test_still_flags_function_not_in_dunder_all(self, trees):
        """Functions NOT in __all__ are still flagged when uncalled."""
        t = trees.code("""\
__all__ = ["other_thing"]

def not_exported():
    return 42
""")
        findings = check_dead_code(t)
        assert len(findings) == 1
        assert "not_exported()" in findings[0].message


# A live script: main() is the entry point, and whatever it calls stays alive.
LIVE_APP = """\
from lib import keep


def main():
    keep()


if __name__ == "__main__":
    main()
"""


class TestDeadCodeCascade:
    """dead-code names what removing the dead function frees (``checks/cascade.py``)."""

    def _message(self, trees, lib: str, **extra: str) -> str:
        files = {"app.py": LIVE_APP, "lib.py": lib, **extra}
        [finding] = check_dead_code(trees.files(files))
        return finding.message

    def test_a_three_deep_cascade_is_listed_whole(self, trees):
        lib = """\
WIDTH = 8


def keep():
    pass


def report():
    return _format()


def _format():
    return _pad()


def _pad():
    return " " * WIDTH
"""
        assert self._message(trees, lib) == (
            "report(): no production callers, no test callers. "
            "Removing report() also frees 3: WIDTH (lib.py:1), _format() (lib.py:12), "
            "_pad() (lib.py:16)"
        )

    def test_a_cycle_only_the_dead_function_enters_is_freed_whole(self, trees):
        lib = """\
def keep():
    pass


def report():
    return _ping(3)


def _ping(n):
    return _pong(n - 1)


def _pong(n):
    return _ping(n - 1) if n else 0
"""
        assert self._message(trees, lib).endswith(
            "Removing report() also frees 2: _ping() (lib.py:9), _pong() (lib.py:13)"
        )

    def test_a_helper_live_code_also_uses_is_not_freed(self, trees):
        lib = """\
def keep():
    return _shared() + _only_report()


def report():
    return _shared() + _private()


def _shared():
    return 1


def _private():
    return 2


def _only_report():
    return 3
"""
        message = self._message(trees, lib)
        assert message.endswith("Removing report() also frees 1: _private() (lib.py:13)")
        assert "_shared" not in message

    def test_what_live_code_reaches_through_a_kept_helper_stays(self, trees):
        lib = """\
def keep():
    return _middle()


def report():
    return _middle() + _leaf()


def _middle():
    return _leaf()


def _leaf():
    return 1
"""
        assert self._message(trees, lib) == "report(): no production callers, no test callers"

    def test_a_test_reference_does_not_keep_a_helper_alive(self, trees):
        lib = """\
def keep():
    pass


def report():
    return _helper()


def _helper():
    return 1
"""
        test = "from lib import _helper\n\n\ndef test_helper():\n    assert _helper() == 1\n"
        message = self._message(trees, lib, **{"tests/test_lib.py": test})
        assert message.endswith(
            "Removing report() also frees 1: _helper() (lib.py:9). "
            "Tests that use it, to delete or rewrite: tests/test_lib.py (every test)"
        )

    def test_a_constant_only_the_dead_function_reads_is_freed(self, trees):
        lib = """\
import os

LIMIT = 3
TIMEOUT = 5


def keep():
    return TIMEOUT


def report():
    return LIMIT + TIMEOUT
"""
        assert self._message(trees, lib).endswith(
            "Removing report() also frees 1: LIMIT (lib.py:3)"
        )

    def test_a_cascade_crosses_modules_and_frees_a_class(self, trees):
        lib = """\
from fmt import Formatter


def keep():
    pass


def report():
    return Formatter().render()
"""
        fmt = "SEP = ', '\n\n\nclass Formatter:\n    def render(self):\n        return SEP\n"
        assert self._message(trees, lib, **{"fmt.py": fmt}).endswith(
            "Removing report() also frees 2: SEP (fmt.py:1), Formatter (fmt.py:4)"
        )

    def test_module_level_code_keeps_a_helper_alive(self, trees):
        lib = """\
def keep():
    pass


def report():
    return _helper()


def _helper():
    return 1


CACHE = _helper()
"""
        assert self._message(trees, lib) == "report(): no production callers, no test callers"

    def test_a_freed_member_that_is_its_own_finding_is_marked(self, trees):
        lib = """\
import sys


def keep():
    pass


def report():
    return getattr(sys.modules[__name__], "fallback")()


def fallback():
    return 0
"""
        findings = check_dead_code(trees.files({"app.py": LIVE_APP, "lib.py": lib}))
        by_name = {finding.message.partition("(")[0]: finding.message for finding in findings}
        assert set(by_name) == {"report", "fallback"}
        assert by_name["report"].endswith(
            "Removing report() also frees 1: fallback() (lib.py:12, reported separately)"
        )

    def test_the_listing_is_capped_and_counts_the_rest(self, trees):
        helpers = "".join(f"\n\ndef _h{i:02}():\n    return {i}\n" for i in range(12))
        calls = " + ".join(f"_h{i:02}()" for i in range(12))
        lib = f"def keep():\n    pass\n\n\ndef report():\n    return {calls}\n{helpers}"
        message = self._message(trees, lib)
        listed = message.partition("also frees 12: ")[2]
        assert listed.count("(lib.py:") == 10
        assert listed.startswith("_h00() (lib.py:9), ")
        assert listed.endswith("_h09() (lib.py:45) (+2 more)")


class TestSingleCallSite:
    def test_library_attribute_call_is_not_a_caller(self, trees):
        """``os.environ.get`` does not call a project ``get`` (36-3)."""
        t = trees.files(
            {
                "registry.py": "def get(kind):\n    return kind\n\nHANDLER = get('x')\n",
                "settings.py": "import os\nA = os.environ.get('A')\n",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "exactly 1 call site (registry.py:4)" in findings[0].message

    def test_finds_single_caller(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "exactly 1 call site" in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_includes_param_count(self, trees):
        t = trees.code("""\
def format_row(name, age, role):
    pass

format_row("Alice", 30, "admin")
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "3 params" in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_instance_method_collision_not_counted(self, trees):
        """Same-named method calls (w.helper()) must not hide a genuine
        single-call-site by inflating the free function's caller count."""
        t = trees.code("""\
def helper():
    pass

helper()


class Widget:
    def helper(self):
        pass


def uses(w):
    w.helper()
    w.helper()
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "helper" in findings[0].message
        assert "exactly 1 call site" in findings[0].message

    def test_bumps_severity_for_many_params(self, trees):
        """Functions with 4+ params and 1 caller are likely bad extractions."""
        t = trees.code("""\
def format_row(name, age, role, dept):
    pass

format_row("Alice", 30, "admin", "eng")
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "4 params" in findings[0].message
        assert findings[0].severity == Severity.MEDIUM

    def test_detects_single_object_args(self, trees):
        """All args from one object = decomposing a data structure."""
        t = trees.code("""\
def format_row(name, cpu, mem):
    pass

format_row(svc.name, svc.cpu, svc.mem)
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "all args from 'svc'" in findings[0].message
        assert findings[0].severity == Severity.MEDIUM

    def test_no_single_object_for_mixed_sources(self, trees):
        """Args from different objects shouldn't trigger single-object hint."""
        t = trees.code("""\
def process(name, count):
    pass

process(svc.name, config.count)
""")
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "all args from" not in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_ignores_cross_directory_calls(self, trees):
        """Cross-directory calls are public API boundaries, not inline candidates."""
        t = trees.files(
            {
                "src/myapp/aws/rds.py": """\
def stop():
    pass
""",
                "bin/manage_rds.py": """\
from myapp.aws.rds import stop
stop()
""",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_finds_same_directory_calls(self, trees):
        """Same-directory calls are genuine inline candidates."""
        t = trees.files(
            {
                "src/myapp/helpers.py": """\
def format_row():
    pass
""",
                "src/myapp/status.py": """\
import helpers
helpers.format_row()
""",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 1

    def test_ignores_long_functions_by_line_count(self, trees):
        """Functions spanning 10+ lines should be skipped even with few statements."""
        # 3 top-level statements but many lines (nested logic)
        body_lines = "\n".join(f"        x = {i}" for i in range(8))
        code = f"""\
def wait_for_task():
    while True:
{body_lines}
        break

wait_for_task()
"""
        t = trees.code(code)
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_ignores_multiple_callers(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
helper()
""")
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_ignores_zero_callers(self, trees):
        """Dead code is reported by dead-code, not single-call-site."""
        t = trees.code("""\
def helper():
    pass
""")
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_ignores_main_entry_point(self, trees):
        """Functions called from __main__ guard are entry points, not inline candidates."""
        t = trees.code("""\
def main():
    pass

if __name__ == "__main__":
    main()
""")
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_ignores_long_functions(self, trees):
        """Functions with 5+ statements were extracted for readability."""
        t = trees.code("""\
def setup_logging():
    logger = get_logger()
    logger.setLevel(DEBUG)
    handler = StreamHandler()
    handler.setFormatter(Formatter("%(message)s"))
    logger.addHandler(handler)

setup_logging()
""")
        findings = check_single_call_site(t)
        assert len(findings) == 0

    def test_ignores_production_dispatch_table_value(self, trees):
        """A function referenced as a value in a production dispatch table is
        a callback target — the dict entry is the real fan-out, not the lone
        direct call."""
        t = trees.files(
            {
                "src/myapp/lenses.py": """\
def flat_file_list(findings):
    pass

def flat_or_rollup(findings):
    return flat_file_list(findings)

CATEGORY_LENS = {
    "macos": flat_or_rollup,
    "corrupt": flat_file_list,
    "unicode": flat_file_list,
}
""",
            }
        )
        findings = check_single_call_site(t)
        # flat_file_list: 1 direct call + dispatch-table value → suppressed
        names = [f.message.split("(")[0].strip() for f in findings]
        assert not any("flat_file_list" in n for n in names)

    def test_test_only_dispatch_does_not_justify(self, trees):
        """A function referenced as a value ONLY from tests is still a real
        single-call-site finding — tests don't justify production code."""
        t = trees.files(
            {
                "src/myapp/helpers.py": """\
def helper():
    pass

helper()
""",
                "tests/test_helpers.py": """\
from myapp.helpers import helper
TEST_TABLE = {"k": helper}
""",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "helper" in findings[0].message

    def test_test_only_import_does_not_suppress(self, trees):
        """Imports from test files don't satisfy the 'imported elsewhere'
        suppression — tests don't justify production code."""
        t = trees.files(
            {
                "src/myapp/walker.py": """\
def build_lookup():
    pass

build_lookup()
""",
                "tests/test_walker.py": """\
from myapp.walker import build_lookup
build_lookup()
build_lookup()
""",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "build_lookup" in findings[0].message

    def test_mentions_test_callers_in_message(self, trees):
        """When production==1 and tests also call directly, the message must
        note the test callers so the inliner knows tests need refactoring."""
        t = trees.files(
            {
                "src/myapp/walker.py": """\
def build_lookup():
    pass

build_lookup()
""",
                "tests/test_walker.py": """\
from myapp.walker import build_lookup
build_lookup()
build_lookup()
""",
            }
        )
        findings = check_single_call_site(t)
        assert len(findings) == 1
        assert "test caller" in findings[0].message
        assert "2 test caller" in findings[0].message


class TestInternalOnly:
    def test_finds_internal_only_function(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
helper()
""")
        findings = check_internal_only(t)
        assert len(findings) == 1
        assert "only calls it within its own file" in findings[0].message

    def test_ignores_externally_called(self, trees):
        t = trees.files(
            {
                "lib.py": """\
def helper():
    pass

helper()
helper()
""",
                "main.py": """\
from lib import helper
helper()
""",
            }
        )
        findings = check_internal_only(t)
        assert len(findings) == 0

    def test_ignores_single_internal_call(self, trees):
        """Need 2+ internal calls to suggest making private."""
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_internal_only(t)
        assert len(findings) == 0

    def test_ignores_function_in_dunder_all(self, trees):
        """Functions in __all__ are explicitly public API — don't suggest making private."""
        t = trees.code("""\
__all__ = ["merge_setting"]

def merge_setting(a, b):
    pass

merge_setting(1, 2)
merge_setting(3, 4)
""")
        findings = check_internal_only(t)
        assert len(findings) == 0


class TestConstantArgs:
    def test_finds_same_literal_every_call(self, trees):
        t = trees.code("""\
def deploy(app, env):
    pass

deploy("myapp", "production")
deploy("myapp", "production")
deploy("myapp", "production")
""")
        findings = check_constant_args(t)
        assert len(findings) == 2
        names = {f.message.split("'")[1] for f in findings}
        assert names == {"app", "env"}

    def test_ignores_varying_args(self, trees):
        t = trees.code("""\
def process(data, fmt):
    pass

process("a", "json")
process("b", "csv")
""")
        findings = check_constant_args(t)
        assert len(findings) == 0

    def test_finds_one_constant_one_varying(self, trees):
        t = trees.code("""\
def send(msg, channel):
    pass

send("hello", "general")
send("world", "general")
""")
        findings = check_constant_args(t)
        assert len(findings) == 1
        assert "channel" in findings[0].message
        assert "'general'" in findings[0].message

    def test_ignores_single_caller(self, trees):
        """Need 2+ callers to establish a pattern."""
        t = trees.code("""\
def process(data, fmt):
    pass

process("x", "json")
""")
        findings = check_constant_args(t)
        assert len(findings) == 0

    def test_works_with_keyword_args(self, trees):
        t = trees.code("""\
def fetch(url, timeout):
    pass

fetch("http://a.com", timeout=30)
fetch("http://b.com", timeout=30)
""")
        findings = check_constant_args(t)
        assert len(findings) == 1
        assert "timeout" in findings[0].message

    def test_ignores_non_literal_args(self, trees):
        t = trees.code("""\
def process(data, fmt):
    pass

x = "json"
process("a", x)
process("b", x)
""")
        findings = check_constant_args(t)
        assert len(findings) == 0

    def test_test_callers_passing_one_fixture_literal_do_not_count(self, trees):
        """Tests that always pass "s3://b" say nothing about the parameter."""
        t = trees.files(
            {
                "app.py": "def list_uploads(client, uri):\n    return []\n",
                "tests/test_app.py": (
                    "from app import list_uploads\n\n"
                    "def test_a():\n    list_uploads(c, 's3://b')\n\n"
                    "def test_b():\n    list_uploads(c, 's3://b')\n"
                ),
            }
        )
        assert check_constant_args(t) == []

    def test_production_constant_is_found_whatever_tests_pass(self, trees):
        t = trees.files(
            {
                "app.py": "def fetch(url, timeout):\n    pass\n",
                "main.py": (
                    "from app import fetch\n\n"
                    "fetch('http://a.com', timeout=30)\n"
                    "fetch('http://b.com', timeout=30)\n"
                ),
                "tests/test_app.py": (
                    "from app import fetch\n\ndef test_a():\n    fetch('x', timeout=1)\n"
                ),
            }
        )
        assert [f.message for f in check_constant_args(t)] == [
            "fetch() param 'timeout' always receives 30 from all 2 caller(s)"
        ]

    def test_same_named_method_call_is_not_a_caller(self, trees):
        """deployer ``ssm.put_parameter``: a boto client's ``put_parameter`` is not it."""
        t = trees.files(
            {
                "ssm.py": "def put_parameter(name, value, overwrite):\n    pass\n",
                "app.py": (
                    "from ssm import put_parameter\n\n"
                    "put_parameter('a', 1, True)\nput_parameter('b', 2, overwrite=True)\n"
                    "client.put_parameter(Name='c', Value=3)\n"
                ),
            }
        )
        assert [f.message for f in check_constant_args(t)] == [
            "put_parameter() param 'overwrite' always receives True from all 2 caller(s)"
        ]

    def test_positional_index_skips_positional_only_parameters(self, trees):
        """Positions count positional-only parameters, which are not judged themselves."""
        t = trees.code("""\
def emit(sink, /, level, tag):
    pass

emit(a, "warn", tag)
emit(b, "warn", tag)
""")
        assert [f.message for f in check_constant_args(t)] == [
            "emit() param 'level' always receives 'warn' from all 2 caller(s)"
        ]


class TestReturnNoneInsteadOfRaise:
    _FIND = "def find(name):\n    if name in users:\n        return users[name]\n    return None\n"

    def test_test_assertions_on_none_are_not_guards(self, trees):
        """``assert find(x) is None`` pins the contract; it does not guard against it."""
        t = trees.files(
            {
                "app.py": self._FIND,
                "main.py": "from app import find\n\nuser = find(name)\nuser.greet()\n",
                "tests/test_app.py": (
                    "from app import find\n\n"
                    "def test_a():\n    assert find('x') is None\n\n"
                    "def test_b():\n    assert find('y') is not None\n"
                ),
            }
        )
        assert check_return_none_instead_of_raise(t) == []

    def test_production_guards_are_counted_without_the_tests(self, trees):
        t = trees.files(
            {
                "app.py": self._FIND,
                "main.py": (
                    "from app import find\n\n"
                    "a = find('a')\nif a is None:\n    raise SystemExit\n\n"
                    "b = find('b')\nif b is None:\n    raise SystemExit\n"
                ),
                "tests/test_app.py": (
                    "from app import find\n\ndef test_a():\n    assert find('x') is None\n"
                ),
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert [f.message for f in findings] == [
            "find() returns None in some branches and 2 of 2 caller(s) guard against None"
        ]

    def test_basic_mixed_returns_with_guards(self, trees):
        t = trees.code("""\
def find_user(name):
    for u in users:
        if u.name == name:
            return u
    return None

result = find_user("alice")
if result is None:
    handle_error()

result = find_user("bob")
if result is None:
    handle_error()
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "find_user()" in findings[0].message
        assert "2 of 2" in findings[0].message

    def test_bare_return_counts_as_none(self, trees):
        t = trees.code("""\
def lookup(key):
    if key not in data:
        return
    return data[key]

x = lookup("a")
if x is None:
    pass

y = lookup("b")
if y is None:
    pass
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1

    def test_instance_method_guards_not_counted(self, trees):
        """None-guards on a same-named method (repo.find()) must not count
        toward the free function's guarded-caller total."""
        t = trees.code("""\
def find(name):
    if not name:
        return None
    return lookup(name)

result = find("alice")
if result is None:
    handle()


class Repo:
    def find(self, name):
        return self._cache.get(name)


def uses(repo):
    a = repo.find("x")
    if a is None:
        handle()
    b = repo.find("y")
    if b is None:
        handle()
""")
        # Only the single bare-name caller guards; the two repo.find() method
        # guards are excluded, leaving 1 < 2 so nothing is flagged.
        findings = check_return_none_instead_of_raise(t)
        assert findings == []

    def test_is_not_none_guard(self, trees):
        t = trees.code("""\
def get_item(idx):
    if idx < 0:
        return None
    return items[idx]

val = get_item(0)
if val is not None:
    use(val)

val = get_item(1)
if val is not None:
    use(val)
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1

    def test_truthiness_guard(self, trees):
        t = trees.code("""\
def find(name):
    if name in db:
        return db[name]
    return None

r = find("x")
if not r:
    handle()

r = find("y")
if r:
    use(r)
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1

    def test_cross_file_callers(self, trees):
        t = trees.files(
            {
                "lib.py": """\
def resolve(name):
    if name in registry:
        return registry[name]
    return None
""",
                "a.py": """\
from lib import resolve
val = resolve("x")
if val is None:
    raise KeyError("x")
""",
                "b.py": """\
from lib import resolve
val = resolve("y")
if val is None:
    raise KeyError("y")
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1

    def test_ignores_void_function(self, trees):
        """Functions that only return None are void — not mixed."""
        t = trees.code("""\
def log_it(msg):
    print(msg)
    return None

x = log_it("hi")
if x is None:
    pass

y = log_it("bye")
if y is None:
    pass
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_single_caller(self, trees):
        t = trees.code("""\
def find_user(name):
    if name in users:
        return users[name]
    return None

result = find_user("alice")
if result is None:
    pass
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_no_callers(self, trees):
        t = trees.code("""\
def find_user(name):
    if name in users:
        return users[name]
    return None
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_unguarded_callers(self, trees):
        """Callers that assign but don't guard — no pattern to report."""
        t = trees.code("""\
def find_user(name):
    if name in users:
        return users[name]
    return None

result = find_user("alice")
use(result)

result = find_user("bob")
use(result)
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_generator(self, trees):
        t = trees.code("""\
def gen_items(data):
    for item in data:
        if item:
            yield item
    return None

x = gen_items([])
if x is None:
    pass

y = gen_items([1])
if y is None:
    pass
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_discarded_return(self, trees):
        """Bare call statements (not assigned) are not counted."""
        t = trees.code("""\
def find_user(name):
    if name in users:
        return users[name]
    return None

find_user("alice")
find_user("bob")
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    def test_ignores_single_return(self, trees):
        """Functions with only one return statement are not mixed."""
        t = trees.code("""\
def get_value(key):
    return data.get(key)

x = get_value("a")
if x is None:
    pass

y = get_value("b")
if y is None:
    pass
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 0

    # Guard shapes (Phase 21-2). The ratio is "N of M callers guard": a guard
    # the matcher misses prints a gap that is not in the code. Each test below
    # puts one guard shape beside two bare guards and an unguarded caller, so
    # the printed ratio shows exactly whether that shape was credited.
    GUARD_SHAPE_SOURCE = """\
def find_user(name):
    if name in users:
        return users[name]
    return None

a = find_user("alice")
if a is None:
    handle()

d = find_user("dave")
if d is None:
    handle()

b = find_user("bob")
use(b)

def shaped():
{shape}
"""

    def _ratio(self, trees, shape: str) -> str:
        body = "\n".join("    " + line for line in shape.splitlines())
        findings = check_return_none_instead_of_raise(
            trees.code(self.GUARD_SHAPE_SOURCE.format(shape=body))
        )
        assert len(findings) == 1
        message = findings[0].message
        return message.split(" and ", 1)[1].split(" caller(s)", 1)[0]

    @pytest.mark.parametrize(
        "shape",
        [
            # `or` with a test that is true whenever c is None
            "c = find_user('c')\nif c is None or not fallback:\n    return",
            "c = find_user('c')\nif not fallback or c is None:\n    return",
            "c = find_user('c')\nif not c or c.disabled:\n    return",
            # `and` with a test that is false whenever c is None
            "c = find_user('c')\nif c is not None and c.active:\n    use(c)",
            "c = find_user('c')\nif ready and c is not None:\n    use(c)",
            "c = find_user('c')\nif c and c.active:\n    use(c)",
            # negation flips the polarity
            "c = find_user('c')\nif not (c is not None and c.active):\n    return",
            # conditional expressions, in any statement's own expressions
            "c = find_user('c')\nname = c.name if c else None",
            "c = find_user('c')\nreturn [c.name] if c is not None else []",
            "c = find_user('c')\nlog(c.name if c is not None and c.ok else '-')",
            # the statement value is the identity test
            "c = find_user('c')\nreturn c is not None",
            "c = find_user('c')\nreturn c is None",
            # a direct comparison on the call is a caller that guards
            "return find_user('c') is not None",
            "if find_user('c') is None:\n    return",
        ],
    )
    def test_guard_shape_is_credited(self, trees, shape):
        assert self._ratio(trees, shape) == "3 of 4"

    @pytest.mark.parametrize(
        "shape",
        [
            # None reaches both branches when `other` is false / true
            "c = find_user('c')\nif c is None and not fallback:\n    return",
            "c = find_user('c')\nif not c and retry:\n    return",
            "c = find_user('c')\nif c is not None or fallback:\n    use(c)",
            "c = find_user('c')\nif c or fallback:\n    use(c)",
            # passing the value on is not a guard
            "c = find_user('c')\nreturn c",
            "c = find_user('c')\nname = c.name if ready else None",
            # a guard on a different name
            "c = find_user('c')\nif other is None or c.x:\n    return",
        ],
    )
    def test_non_guard_shape_is_not_credited(self, trees, shape):
        assert self._ratio(trees, shape) == "2 of 4"

    def test_compound_guard_counts_alone(self, trees):
        """Two callers guarding only with `x is None or ...` are enough to report."""
        t = trees.code("""\
def find_user(name):
    if name in users:
        return users[name]
    return None

a = find_user("alice")
if a is None or not fallback:
    handle()

b = find_user("bob")
if b is None or not fallback:
    handle()
""")
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "2 of 2 caller(s) guard" in findings[0].message

    def test_subprocess_run_in_prints_eight_of_eight(self, trees):
        """A subprocess wrapper, trend_snapshot.run_in: printed 4 of 8 before 21-2.

        Five callers guard with `result is None or result.returncode ...`,
        one guards two statements down; every caller guards.
        """
        t = trees.files(
            {
                "trend_snapshot.py": """\
import subprocess


def run_in(repo_path, cmd, timeout):
    try:
        return subprocess.run(cmd, cwd=repo_path, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def pysmelly_targets(repo_path):
    result = run_in(repo_path, ["make", "-n", "pysmelly"], timeout=30)
    if result is None or result.returncode != 0:
        return ["."]
    return result.stdout.split()


def pysmelly_total(repo_path, target):
    result = run_in(repo_path, ["pysmelly", target], timeout=600)
    if result is None:
        return "err"
    return result.stdout


def coverage(repo_path):
    result = run_in(repo_path, ["make", "test-cov"], timeout=1800)
    if result is None:
        return "err"
    return result.stdout


def outdated(venv_dir):
    result = run_in(venv_dir, ["uv", "pip", "list", "--outdated"], timeout=120)
    if result is None or result.returncode != 0:
        return "err"
    return result.stdout


def suppressions(repo_path, cmd):
    result = run_in(repo_path, cmd, timeout=60)
    if result is None or result.returncode not in {0, 1}:  # 1 = no matches
        return 0
    return len(result.stdout)
""",
                "fleet_check.py": """\
import time

from trend_snapshot import run_in


def run_target(repo_path, target):
    start = time.monotonic()
    proc = run_in(repo_path, ["make", target], timeout=60)
    seconds = time.monotonic() - start
    if proc is None:
        return {"result": "FAIL", "seconds": seconds}
    return {"result": "pass" if proc.returncode == 0 else "FAIL"}
""",
                "repo_secret_sweep.py": """\
from trend_snapshot import run_in


def git_lines(repo_path, cmd, timeout):
    result = run_in(repo_path, cmd, timeout=timeout)
    if result is None or result.returncode not in {0, 1}:  # 1 = no matches
        return [], "failed"
    return result.stdout.splitlines(), ""


def probe(repo_path, cmd):
    result = run_in(repo_path, cmd, timeout=10)
    if result is None or result.returncode != 0:
        return f"cannot use {repo_path}"
    return ""
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "8 of 8 caller(s) guard" in findings[0].message

    def test_s3_head_or_none_prints_six_of_six(self, trees):
        """An S3 wrapper, s3.head_or_none: printed 3 of 5 before 21-2.

        Two callers guard with a conditional expression and one compares the
        call itself (`return head_or_none(...) is not None`); every caller guards.
        """
        t = trees.files(
            {
                "s3.py": """\
from botocore.exceptions import ClientError


def head_or_none(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def rename_scope_keys(client, bucket_name, prefix_or_key):
    head = head_or_none(client, bucket_name, prefix_or_key)
    return [prefix_or_key] if head is not None else []


def rename_scope_etags(client, bucket_name, prefix_or_key):
    head = head_or_none(client, bucket_name, prefix_or_key)
    if head is None:
        return {}
    return {prefix_or_key: (head.get("ETag", "") or "").strip('"')}
""",
                "restore.py": """\
from s3 import head_or_none


def check_dest(s3, bucket, item):
    pre = head_or_none(s3, bucket, item.dest_key)
    pre_etag = (pre.get("ETag") or "").strip('"') if pre else None
    want_etag = (item.expected_dest_etag or "").strip('"') or None
    if pre_etag != want_etag:
        raise ValueError(item.dest_key)
""",
                "rename.py": """\
from s3 import head_or_none


def copy_one(client, bucket, src_key, dst_key, op):
    dst_head = head_or_none(client, bucket, dst_key)
    if dst_head is None:
        if op is not None:
            beat(op)
        copy(client, bucket, src_key, dst_key)
    verify_head = head_or_none(client, bucket, dst_key)
    if verify_head is None:
        raise ValueError(dst_key)
""",
                "views.py": """\
from s3 import head_or_none


def object_exists(client, bucket, key):
    return head_or_none(client, bucket, key) is not None
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "6 of 6 caller(s) guard" in findings[0].message

    def test_method_compared_to_none_is_not_a_caller(self, trees):
        """`obj.find(...) is None` on a same-named method credits nothing."""
        t = trees.code("""\
def find(name):
    if not name:
        return None
    return lookup(name)

a = find("alice")
if a is None:
    handle()


def uses(repo):
    if repo.find("x") is None:
        handle()
    return repo.find("y") is not None
""")
        assert check_return_none_instead_of_raise(t) == []

    # --- 21-3: callers are the real call sites of this function ---

    def test_docstring_naming_the_function_adds_no_caller(self, trees):
        """Cause (b) pin: prose such as ``data = run_aws_json(...)`` is not a call.

        The 13-repo sweep blamed deployer `aws/cli.py:48`'s 3 of 4 on a
        docstring; re-measured, the fourth caller was the uncredited
        `… if data else None`, credited since 8c5b1e8.
        """
        t = trees.code('''\
"""Callers write ``data = lookup(key)`` and then use(data)."""


def lookup(key):
    """Not ``lookup()``'s caller: x = lookup(k); x.get()."""
    if key not in table:
        return None
    return table[key]


a = lookup("a")
if a is None:
    handle()
b = lookup("b")
if b is None:
    handle()
''')
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "2 of 2 caller(s) guard" in findings[0].message

    def test_deployer_run_aws_json_prints_four_of_four(self, trees):
        """deployer `aws/cli.py` at f9a5843 (the sweep's tree): printed 3 of 4."""
        t = trees.files(
            {
                "src/deployer/__init__.py": "",
                "src/deployer/aws/__init__.py": "",
                "src/deployer/aws/cli.py": '''\
import json


def run_aws(*args):
    """Run aws; callers grep the error text, which is why this, and
    not run_aws_json(), is the workhorse."""
    return run_command(args)


def run_aws_json(*args):
    success, output = run_aws(*args)
    if not success:
        return None
    return json.loads(output)
''',
                "src/deployer/aws/cognito.py": """\
from .cli import run_aws, run_aws_json


def get_pool_name(pool_id):
    data = run_aws_json("cognito-idp", "describe-user-pool", pool_id)
    return data.get("UserPool", {}).get("Name") if data else None


def list_users(pool_id):
    users = []
    while True:
        data = run_aws_json("cognito-idp", "list-users", pool_id)
        if data is None:
            return users
        users.extend(data.get("Users", []))
""",
                "src/deployer/aws/rds.py": """\
from .cli import run_aws_json


def get_status(instance_id):
    data = run_aws_json("rds", "describe-db-instances", instance_id)
    if not data:
        return None
    return data["DBInstances"]
""",
                "src/deployer/aws/cloudwatch.py": """\
from .cli import run_aws_json


def get_events(*args):
    data = run_aws_json(*args)
    if data is None:
        return None
    return data.get("events", [])
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "4 of 4 caller(s) guard" in findings[0].message

    def test_s3store_get_object_bytes_ignores_the_method(self, trees):
        """Cause (d) pin, a free function in `shared/s3store.py` vs `_ref.py`.

        The same-named `S3Store.get_object_bytes` method's callers never
        guard; none of them is a caller of the free function. The sweep's
        6 of 7 was a conditional-expression guard, credited since 8c5b1e8.
        """
        t = trees.files(
            {
                "shared/s3store.py": """\
def get_object_bytes(s3, bucket, key, byte_range=None):
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except NoSuchKey:
        return None
""",
                "inventory/inventory/__init__.py": "",
                "inventory/inventory/_ref.py": """\
class S3Store:
    def get_object_bytes(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
""",
                "inventory/inventory/s3_storage.py": """\
def load(store, ref):
    body = store.get_object_bytes(ref.s3_key("summary.json"))
    return parse(body)


def archive(store, ref):
    body = store.get_object_bytes(ref.s3_key("archive.zip"))
    return unzip(body)
""",
                "inventory/encryption_census.py": """\
from shared.s3store import get_object_bytes


def rules(ctx, bucket):
    body = get_object_bytes(ctx.s3, bucket, RULES_KEY)
    if body is None:
        return None
    sig = get_object_bytes(ctx.s3, bucket, RULES_SIG_KEY)
    if sig is None:
        return body
    return body, sig


def probe(ctx, key):
    body = get_object_bytes(ctx.s3, ctx.bucket, key, byte_range="bytes=0-64")
    state = (
        sentinel_state(body, key)
        if body is not None
        else None
    )
    return state
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "3 of 3 caller(s) guard" in findings[0].message

    def test_unrelated_same_named_function_adds_no_caller(self, trees):
        """A bare ``find(...)`` bound to another function is not a caller.

        `other.py` defines its own (decorated, so unindexed) `find`, and
        `third.py` imports a library's; their unguarded callers must not
        dilute the ratio.
        """
        t = trees.files(
            {
                "lib.py": """\
def find(name):
    if name not in registry:
        return None
    return registry[name]


a = find("a")
if a is None:
    handle()
b = find("b")
if b is None:
    handle()
""",
                "other.py": """\
import functools


@functools.cache
def find(name):
    return lookup(name)


x = find("x")
use(x)
""",
                "third.py": """\
from gettext import find

y = find("domain")
use(y)
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "2 of 2 caller(s) guard" in findings[0].message

    def test_library_attribute_call_is_not_a_caller(self, trees):
        """``json.loads(...)`` does not call the project's own ``loads``."""
        t = trees.files(
            {
                "codec.py": """\
def loads(text):
    if not text:
        return None
    return decode(text)
""",
                "a.py": """\
import json

import codec

v = codec.loads(raw)
if v is None:
    handle()
w = codec.loads(raw2)
if w is None:
    handle()
z = json.loads(raw3)
use(z)
""",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "2 of 2 caller(s) guard" in findings[0].message

    @pytest.mark.parametrize(
        "caller",
        [
            "from pkg.mod import find\nv = find('x')",
            "from pkg.mod import find as lookup\nv = lookup('x')",
            "from pkg.mod import *\nv = find('x')",
            "from pkg import find\nv = find('x')",
            "from pkg import mod\nv = mod.find('x')",
            "import pkg.mod\nv = pkg.mod.find('x')",
            "import pkg.mod as m\nv = m.find('x')",
            "from .mod import find\nv = find('x')",
            "from mod import find\nv = find('x')",
        ],
    )
    def test_import_shapes_that_resolve_to_the_function(self, trees, caller):
        """Every way of binding the function counts: aliases, star imports,
        a package re-export, module attributes, relative imports, and a
        script-directory import that names the file by a shorter path."""
        t = trees.files(
            {
                "src/pkg/__init__.py": "from .mod import find\n",
                "src/pkg/mod.py": """\
def find(name):
    if name not in registry:
        return None
    return registry[name]


a = find("a")
if a is None:
    handle()
""",
                "src/pkg/user.py": caller + "\nif v is None:\n    handle()\n",
            }
        )
        findings = check_return_none_instead_of_raise(t)
        assert len(findings) == 1
        assert "2 of 2 caller(s) guard" in findings[0].message


class TestPassThroughParams:
    def test_basic_forwarding(self, trees):
        """B receives params and only forwards to C."""
        t = trees.code("""\
def inner(data, mode):
    pass

def outer(data, mode):
    inner(data, mode)

outer("x", "fast")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 2
        params = {f.message.split("'")[1] for f in findings}
        assert params == {"data", "mode"}
        assert "inner()" in findings[0].message

    def test_keyword_forwarding(self, trees):
        """Keyword argument forwarding is detected."""
        t = trees.code("""\
def inner(x, mode):
    pass

def outer(x, mode):
    inner(x, mode=mode)

outer(1, "test")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 2
        params = {f.message.split("'")[1] for f in findings}
        assert params == {"x", "mode"}

    def test_suppresses_substantial_function_body(self, trees):
        """Functions with 3+ statements do real work — forwarding is incidental."""
        t = trees.code("""\
def get_access_token(request):
    pass

def get_graph_root(request):
    token = get_access_token(request)
    root = GraphRoot(token)
    root.authenticate()
    return root

get_graph_root(req)
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_param_used_beyond_forwarding(self, trees):
        """Param used in condition + forwarding is not forwarding-only."""
        t = trees.code("""\
def inner(mode):
    pass

def outer(data, mode):
    if mode == "fast":
        inner(mode)
    return data

outer("x", "fast")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_forwarding_to_unknown_function(self, trees):
        """Forwarding to external/unknown function is not reported."""
        t = trees.code("""\
def outer(data):
    print(data)

outer("x")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_args_kwargs_excluded(self, trees):
        """*args and **kwargs are inherently forwarding mechanisms."""
        t = trees.code("""\
def inner(x):
    pass

def outer(*args, **kwargs):
    inner(*args)

outer(1)
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_param_with_attribute_access(self, trees):
        """mode.upper() is real usage, not forwarding."""
        t = trees.code("""\
def inner(x):
    pass

def outer(mode):
    x = mode.upper()
    inner(x)

outer("test")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_dead_function_not_reported(self, trees):
        """Function with no callers — dead-code handles it."""
        t = trees.code("""\
def inner(x):
    pass

def outer(x):
    inner(x)
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_private_functions_excluded(self, trees):
        """Private functions are not in the function index."""
        t = trees.code("""\
def inner(x):
    pass

def _wrapper(x):
    inner(x)

_wrapper(1)
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0

    def test_cross_file_forwarding(self, trees):
        """Forwarding detected across files."""
        t = trees.files(
            {
                "lib.py": """\
def process(data, mode):
    pass
""",
                "wrapper.py": """\
def handle(data, mode):
    process(data, mode)

handle("x", "fast")
""",
            }
        )
        findings = check_pass_through_params(t)
        assert len(findings) == 2
        params = {f.message.split("'")[1] for f in findings}
        assert params == {"data", "mode"}

    def test_multiple_targets(self, trees):
        """Param forwarded to multiple known functions."""
        t = trees.code("""\
def validate(data, mode):
    pass

def transform(data, mode):
    pass

def process(data, mode):
    validate(data, mode)
    transform(data, mode)

process("x", "fast")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 2
        for f in findings:
            assert "validate()" in f.message
            assert "transform()" in f.message

    def test_param_with_default_forwarded(self, trees):
        """Param with default value that is only forwarded."""
        t = trees.code("""\
def inner(mode):
    pass

def outer(mode="default"):
    inner(mode)

outer()
outer("fast")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 1
        assert "mode" in findings[0].message

    def test_param_used_in_comparison_alongside_forwarding(self, trees):
        """Param used in comparison + forwarded is not forwarding-only."""
        t = trees.code("""\
def inner(mode):
    pass

def outer(mode):
    inner(mode)
    if mode == "fast":
        pass

outer("fast")
""")
        findings = check_pass_through_params(t)
        assert len(findings) == 0


class TestInconsistentErrorHandling:
    def test_finds_divergent_handling(self, trees):
        """One specific, one broad, one unhandled — divergent."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": """\
from lib import fetch
try:
    fetch()
except ConnectionError:
    pass
""",
                "b.py": """\
from lib import fetch
try:
    fetch()
except Exception:
    pass
""",
                "c.py": """\
from lib import fetch
fetch()
""",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert "fetch()" in findings[0].message
        assert "inconsistent" in findings[0].message
        assert "ConnectionError" in findings[0].message

    def test_ignores_consistent_specific(self, trees):
        """All callers catch specific exceptions — consistent."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept TimeoutError:\n    pass",
                "c.py": "from lib import fetch\ntry:\n    fetch()\nexcept ValueError:\n    pass",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_ignores_consistent_broad(self, trees):
        """All callers catch broad Exception — consistently lazy."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "c.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_ignores_all_unhandled(self, trees):
        """All callers unhandled — no error handling anywhere."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\nfetch()",
                "b.py": "from lib import fetch\nfetch()",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_ignores_only_2_callers(self, trees):
        """Need 3+ callers to flag."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass",
                "b.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_ignores_test_file_callers(self, trees):
        """Test callers don't count toward the threshold."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass",
                "b.py": "from lib import fetch\nfetch()",
                "test_lib.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_nested_try_uses_innermost(self, trees):
        """Call inside nested try uses the inner handler."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": """\
from lib import fetch
try:
    try:
        fetch()
    except ConnectionError:
        pass
except Exception:
    pass
""",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        # a.py should be classified as "specific" (innermost)
        assert "ConnectionError" in findings[0].message

    def test_multiple_except_classified_as_specific(self, trees):
        """try with both specific and broad handlers counts as specific."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": """\
from lib import fetch
try:
    fetch()
except ValueError:
    pass
except Exception:
    pass
""",
                "b.py": "from lib import fetch\nfetch()",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert "ValueError" in findings[0].message

    def test_module_qualified_calls_counted(self, trees):
        """Module-qualified calls (lib.process()) resolve to the free function."""
        t = trees.files(
            {
                "lib.py": "def process():\n    pass",
                "a.py": "import lib\ntry:\n    lib.process()\nexcept ValueError:\n    pass",
                "b.py": "import lib\ntry:\n    lib.process()\nexcept Exception:\n    pass",
                "c.py": "import lib\nlib.process()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1

    def test_instance_method_collision_excluded(self, trees):
        """A same-named method (self.f()/obj.f()) must not inflate the free
        function's caller count — the reported get_collection_detail bug."""
        t = trees.files(
            {
                "common.py": """\
def get_detail(gcs, uuid):
    if not uuid:
        raise ValueError("no uuid")
    return gcs.fetch(uuid)


class Client:
    def get_detail(self, cid):
        return self._api.get(cid)
""",
                "callers.py": """\
from common import get_detail


def a(gcs, u):
    return get_detail(gcs, u)


def m1(client, cid):
    try:
        return client.get_detail(cid)
    except KeyError:
        return None


def m2(client, cid):
    return client.get_detail(cid)


def m3(client, cid):
    return client.get_detail(cid)
""",
            }
        )
        # Only the single bare-name call (a) resolves to the free function;
        # the three client.get_detail() method calls are excluded, so the
        # 3-caller threshold is not met and nothing is flagged.
        findings = check_inconsistent_error_handling(t)
        assert findings == []

    def test_broad_plus_unhandled_not_flagged(self, trees):
        """Broad + unhandled without any specific — weakest signal, skip."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "b.py": "from lib import fetch\nfetch()",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_message_format(self, trees):
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept TimeoutError:\n    pass",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        msg = findings[0].message
        assert "1 catch specific (TimeoutError)" in msg
        assert "1 catch broad Exception" in msg
        assert "1 unhandled" in msg
        assert "error contract is unclear" in msg

    def test_severity_is_medium(self, trees):
        t = trees.files(
            {
                "lib.py": "def fetch():\n    pass",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept Exception:\n    pass",
                "c.py": "from lib import fetch\nfetch()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM

    def test_exception_propagation_through_wrapper(self, trees):
        """Wrapper that propagates exception to callers that catch it."""
        t = trees.files(
            {
                "lib.py": "def get_token():\n    raise AuthError()",
                "auth.py": (
                    "from lib import get_token\n"
                    "def get_access_token():\n"
                    "    return get_token()['access']\n"
                ),
                "a.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                "b.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                "c.py": (
                    "from auth import get_access_token\n"
                    "try:\n    get_access_token()\nexcept AuthError:\n    pass"
                ),
            }
        )
        findings = check_inconsistent_error_handling(t)
        # get_access_token is a wrapper — its caller catches AuthError,
        # so get_token's "unhandled" call in auth.py should not be flagged
        assert len(findings) == 0

    def test_exception_propagation_multi_level(self, trees):
        """Exception propagates through multiple wrapper levels."""
        t = trees.files(
            {
                "lib.py": "def get_token():\n    raise AuthError()",
                "auth.py": (
                    "from lib import get_token\n"
                    "def get_access_token():\n"
                    "    return get_token()['access']\n"
                    "def get_root():\n"
                    "    return make_root(get_access_token())\n"
                ),
                "a.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                "b.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                "c.py": (
                    "from auth import get_root\ntry:\n    get_root()\nexcept AuthError:\n    pass"
                ),
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 0

    def test_still_flags_truly_unhandled(self, trees):
        """Wrapper whose callers don't catch the exception is still flagged."""
        t = trees.files(
            {
                "lib.py": "def get_token():\n    raise AuthError()",
                "auth.py": (
                    "from lib import get_token\n"
                    "def get_access_token():\n"
                    "    return get_token()['access']\n"
                ),
                "a.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                "b.py": (
                    "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass"
                ),
                # get_access_token called without any error handling
                "c.py": "from auth import get_access_token\nget_access_token()",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert "unhandled" in findings[0].message


class TestInconsistentErrorHandlingResolution:
    """Callers are the calls that resolve to the function (the Phase 21-3 rule).

    The shapes are from a real project (36-3): ``os.environ.get`` and
    ``cache.get`` were counted as callers of ``operations.registry.get``
    while ``from .registry import get as get_handler`` was not, and a
    database cursor's ``cur.execute(...)`` credited a module-level
    ``execute()`` with its caller's catch.
    """

    def test_library_attribute_calls_are_not_callers(self, trees):
        t = trees.files(
            {
                "registry.py": "def get(kind):\n    raise KeyError(kind)\n",
                "a.py": (
                    "from registry import get\ntry:\n    get('x')\nexcept KeyError:\n    pass\n"
                ),
                "settings.py": "import os\nA = os.environ.get('A')\nB = os.environ.get('B')\n",
                "views.py": "from django.core.cache import cache\ncache.get('k')\n",
            }
        )
        assert check_inconsistent_error_handling(t) == []

    def test_aliased_from_import_callers_count(self, trees):
        t = trees.files(
            {
                "registry.py": "def get(kind):\n    raise KeyError(kind)\n",
                "a.py": (
                    "from registry import get as get_handler\n"
                    "try:\n    get_handler('x')\nexcept KeyError:\n    pass\n"
                ),
                "b.py": (
                    "from registry import get as get_handler\n"
                    "try:\n    get_handler('y')\nexcept KeyError:\n    pass\n"
                ),
                "c.py": "import registry\nregistry.get('z')\n",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert "get() has 3 callers" in findings[0].message
        assert "2 catch specific (KeyError), 1 unhandled" in findings[0].message

    def test_same_named_method_call_does_not_credit_propagation(self, trees):
        t = trees.files(
            {
                "lib.py": "def fetch():\n    raise ConnectionError()\n",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass\n",
                "b.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass\n",
                "ops.py": "from lib import fetch\ndef execute():\n    fetch()\n",
                "db.py": (
                    "def run(cur):\n"
                    "    try:\n        cur.execute('select 1')\n"
                    "    except ConnectionError:\n        pass\n"
                ),
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        assert "1 unhandled" in findings[0].message

    def test_propagating_sites_are_reported_apart_from_catching_sites(self, trees):
        """A propagating site catches nothing, so it is not labelled with the catching sites' names."""
        t = trees.files(
            {
                "lib.py": "def fetch():\n    raise ConnectionError()\n",
                "a.py": "from lib import fetch\ntry:\n    fetch()\nexcept ConnectionError:\n    pass\n",
                "b.py": "from lib import fetch\nfetch()\n",
                "ops.py": "from lib import fetch\ndef load():\n    return fetch()\n",
                "c.py": "from ops import load\ntry:\n    load()\nexcept ConnectionError:\n    pass\n",
            }
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        message = findings[0].message
        assert "1 catch specific (ConnectionError)" in message
        assert "1 propagates to a caller that catches" in message
        assert "1 unhandled" in message


# deployer utils/cli.py exit_on, the boundary helper whose wrapped sites the
# check could not see.
EXIT_ON = """\
import sys
from collections.abc import Iterator
from contextlib import contextmanager


def get_environments_dir():
    raise RuntimeError("no environments dir")


@contextmanager
def exit_on(*excs: type[BaseException], prefix: str = "") -> Iterator[None]:
    try:
        yield
    except excs as e:
        print(f"{prefix}{e}", file=sys.stderr)
        sys.exit(1)
"""


def _with_sites(lib: str, callers: dict[str, str]) -> dict[str, str]:
    """A ``lib.py`` plus one caller module per entry, each importing ``lib``."""
    return {"lib.py": lib, **{name: f"import lib\n{body}" for name, body in callers.items()}}


class TestInconsistentErrorHandlingWith:
    """A ``with`` block is classified by what its context manager catches."""

    def test_exit_on_contextmanager_sites_are_specific(self, trees):
        """deployer's shape: every site wrapped in exit_on(RuntimeError) is
        specific, so with one bare site the finding names 2 specific + 1."""
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": "with lib.exit_on(RuntimeError):\n    lib.get_environments_dir()\n",
                    "b.py": (
                        "from lib import exit_on, get_environments_dir\n"
                        "def run():\n"
                        "    with exit_on(RuntimeError, prefix='x: '):\n"
                        "        return get_environments_dir()\n"
                    ),
                    "c.py": "lib.get_environments_dir()\n",
                },
            )
        )
        findings = check_inconsistent_error_handling(t)
        assert len(findings) == 1
        msg = findings[0].message
        assert "2 catch specific (RuntimeError)" in msg
        assert "1 unhandled" in msg

    def test_all_sites_in_exit_on_not_flagged(self, trees):
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    name: "with lib.exit_on(RuntimeError):\n    lib.get_environments_dir()\n"
                    for name in ("a.py", "b.py", "c.py")
                },
            )
        )
        assert check_inconsistent_error_handling(t) == []

    def test_contextmanager_catching_broad_is_broad(self, trees):
        lib = """\
from contextlib import contextmanager


def fetch():
    pass


@contextmanager
def swallow():
    try:
        yield
    except Exception:
        pass
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.swallow():\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch broad Exception" in msg
        assert "1 unhandled" in msg

    def test_contextmanager_without_except_lets_exceptions_through(self, trees):
        """A @contextmanager with only try/finally around its yield catches
        nothing: its sites stay unhandled."""
        lib = """\
import contextlib


def fetch():
    pass


@contextlib.contextmanager
def timed():
    try:
        yield
    finally:
        print("done")
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.timed():\n    lib.fetch()\n",
                    "c.py": "with lib.timed():\n    lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg
        assert "unresolved" not in msg

    def test_class_exit_that_suppresses_is_specific(self, trees):
        lib = """\
def fetch():
    pass


class Tolerate:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and issubclass(exc_type, (KeyError, LookupError)):
            return True
        return False
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.Tolerate():\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (KeyError, LookupError, ValueError)" in msg
        assert "1 unhandled" in msg

    def test_class_exit_that_suppresses_everything_is_broad(self, trees):
        lib = """\
def fetch():
    pass


class Quiet:
    def __enter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return True
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "async def run():\n    async with lib.Quiet():\n        lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch broad Exception" in msg

    def test_class_exit_that_returns_false_lets_exceptions_through(self, trees):
        lib = """\
def fetch():
    pass


class Lock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.Lock():\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg

    def test_with_open_is_neither_handled_nor_unhandled(self, trees):
        """open() is not in the analyzed code: its site no longer counts as
        unhandled, and the message says it sat inside an unresolved with."""
        t = trees.files(
            _with_sites(
                "def fetch(f):\n    pass\n",
                {
                    "a.py": "try:\n    lib.fetch(None)\nexcept ValueError:\n    pass\n",
                    "b.py": "with open('x') as f:\n    lib.fetch(f)\n",
                    "c.py": "lib.fetch(None)\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "has 3 callers" in msg
        assert "1 unhandled" in msg
        assert "1 inside an unresolved `with`" in msg

    def test_only_unresolved_with_sites_not_flagged(self, trees):
        t = trees.files(
            _with_sites(
                "def fetch(f):\n    pass\n",
                {
                    "a.py": "try:\n    lib.fetch(None)\nexcept ValueError:\n    pass\n",
                    "b.py": "with open('x') as f:\n    lib.fetch(f)\n",
                    "c.py": "with open('y') as f:\n    lib.fetch(f)\n",
                },
            )
        )
        assert check_inconsistent_error_handling(t) == []

    def test_try_inside_with_classified_by_the_try(self, trees):
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": (
                        "with open('x'):\n"
                        "    try:\n"
                        "        lib.get_environments_dir()\n"
                        "    except KeyError:\n"
                        "        pass\n"
                    ),
                    "b.py": (
                        "with lib.exit_on(RuntimeError):\n"
                        "    try:\n"
                        "        lib.get_environments_dir()\n"
                        "    except Exception:\n"
                        "        pass\n"
                    ),
                    "c.py": "lib.get_environments_dir()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (KeyError)" in msg
        assert "1 catch broad Exception" in msg
        assert "unresolved" not in msg

    def test_with_inside_try_classified_by_the_with(self, trees):
        """The innermost handler wins: exit_on inside a broad try is specific."""
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": (
                        "try:\n"
                        "    with lib.exit_on(ValueError):\n"
                        "        lib.get_environments_dir()\n"
                        "except Exception:\n"
                        "    pass\n"
                    ),
                    "b.py": "try:\n    lib.get_environments_dir()\nexcept Exception:\n    pass\n",
                    "c.py": "lib.get_environments_dir()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (ValueError)" in msg
        assert "1 catch broad Exception" in msg

    def test_unresolved_with_inside_try_classified_by_the_try(self, trees):
        """open()'s with passes over to the enclosing try."""
        t = trees.files(
            _with_sites(
                "def fetch():\n    pass\n",
                {
                    "a.py": (
                        "try:\n"
                        "    with open('x'):\n"
                        "        lib.fetch()\n"
                        "except OSError:\n"
                        "    pass\n"
                    ),
                    "b.py": "lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (OSError)" in msg
        assert "2 unhandled" in msg

    def test_nested_with_innermost_handler_wins(self, trees):
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": (
                        "with lib.exit_on(RuntimeError):\n"
                        "    with open('x'), lib.exit_on(KeyError):\n"
                        "        lib.get_environments_dir()\n"
                    ),
                    "b.py": (
                        "with lib.exit_on(RuntimeError):\n"
                        "    with open('x'):\n"
                        "        lib.get_environments_dir()\n"
                    ),
                    "c.py": "lib.get_environments_dir()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (KeyError, RuntimeError)" in msg
        assert "1 unhandled" in msg

    def test_call_in_with_item_is_not_inside_the_with(self, trees):
        """The context expression runs before the manager is entered."""
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": "with lib.exit_on(RuntimeError):\n    lib.get_environments_dir()\n",
                    "b.py": "with lib.exit_on(lib.get_environments_dir()):\n    pass\n",
                    "c.py": "with lib.exit_on(RuntimeError):\n    lib.get_environments_dir()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (RuntimeError)" in msg
        assert "1 unhandled" in msg

    def test_asynccontextmanager_is_resolved(self, trees):
        lib = """\
from contextlib import asynccontextmanager


def fetch():
    pass


@asynccontextmanager
async def guard(exc=TimeoutError):
    try:
        yield
    except exc:
        pass
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "async def r():\n    async with lib.guard():\n        lib.fetch()\n",
                    "b.py": (
                        "async def r():\n    async with lib.guard(exc=KeyError):\n        lib.fetch()\n"
                    ),
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (KeyError, TimeoutError)" in msg

    def test_contextlib_suppress(self, trees):
        t = trees.files(
            _with_sites(
                "def fetch():\n    pass\n",
                {
                    "a.py": (
                        "import contextlib\n"
                        "with contextlib.suppress(FileNotFoundError):\n"
                        "    lib.fetch()\n"
                    ),
                    "b.py": (
                        "from contextlib import suppress as quietly\n"
                        "with quietly(Exception):\n"
                        "    lib.fetch()\n"
                    ),
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (FileNotFoundError)" in msg
        assert "1 catch broad Exception" in msg
        assert "1 unhandled" in msg

    def test_project_suppress_is_not_contextlib(self, trees):
        """A same-named project function is resolved as itself, not as
        contextlib.suppress."""
        lib = """\
from contextlib import contextmanager


def fetch():
    pass


@contextmanager
def suppress(*excs):
    yield
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.suppress(KeyError):\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg

    def test_unpacked_arguments_leave_the_with_unresolved(self, trees):
        t = trees.files(
            _with_sites(
                EXIT_ON,
                {
                    "a.py": "with lib.exit_on(RuntimeError):\n    lib.get_environments_dir()\n",
                    "b.py": (
                        "ERRS = (RuntimeError,)\n"
                        "with lib.exit_on(*ERRS):\n"
                        "    lib.get_environments_dir()\n"
                    ),
                    "c.py": "lib.get_environments_dir()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 inside an unresolved `with`" in msg

    def test_unresolved_with_propagating_to_a_catching_caller(self, trees):
        """An unresolved-with site inside a wrapper whose callers catch is
        deliberate propagation, like an unhandled one."""
        t = trees.files(
            {
                "lib.py": "def get_token():\n    raise AuthError()",
                "auth.py": (
                    "from lib import get_token\n"
                    "def get_access_token():\n"
                    "    with open('x'):\n"
                    "        return get_token()\n"
                ),
                "a.py": "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass",
                "b.py": "from lib import get_token\nget_token()",
                "c.py": (
                    "from auth import get_access_token\n"
                    "try:\n    get_access_token()\nexcept AuthError:\n    pass"
                ),
            }
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (AuthError), 1 propagates to a caller that catches" in msg
        assert "unresolved" not in msg

    @pytest.mark.parametrize(
        ("manager", "with_item", "expected"),
        [
            pytest.param(
                "@contextmanager\ndef translate(exc_type):\n"
                "    try:\n        yield\n    except exc_type as e:\n"
                "        raise RuntimeError(str(e)) from e\n",
                "lib.translate(KeyError)",
                "2 catch specific (KeyError, ValueError)",
                id="positional-parameter",
            ),
            pytest.param(
                "@contextmanager\ndef anything():\n    try:\n        yield\n    except:\n        pass\n",
                "lib.anything()",
                "1 catch broad Exception",
                id="bare-except",
            ),
            pytest.param(
                "ERRORS = [KeyError]\n\n\n@contextmanager\ndef listed():\n"
                "    try:\n        yield\n    except ERRORS[0]:\n        pass\n",
                "lib.listed()",
                "1 inside an unresolved `with`",
                id="unreadable-except-type",
            ),
            pytest.param(
                "class Convert:\n    def __enter__(self):\n        return self\n\n"
                "    def __exit__(self, exc_type, exc, tb):\n"
                "        if exc_type is KeyError:\n            raise LookupError() from exc\n",
                "lib.Convert()",
                "2 catch specific (KeyError, ValueError)",
                id="exit-converts",
            ),
            pytest.param(
                "class OnlyOS:\n    def __enter__(self):\n        return self\n\n"
                "    def __exit__(self, exc_type, exc, tb):\n"
                "        return isinstance(exc, OSError)\n",
                "lib.OnlyOS()",
                "2 catch specific (OSError, ValueError)",
                id="exit-returns-isinstance",
            ),
            pytest.param(
                "",
                "contextlib.suppress()",
                "1 inside an unresolved `with`",
                id="suppress-nothing",
            ),
        ],
    )
    def test_context_manager_shapes(self, trees, manager, with_item, expected):
        lib = f"import contextlib\nfrom contextlib import contextmanager\n\n\ndef fetch():\n    pass\n\n\n{manager}"
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": f"import contextlib\nwith {with_item}:\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert expected in msg

    def test_inherited_exit_and_bare_manager_are_unresolved(self, trees):
        """A subclass without its own __exit__, and a manager held in a
        variable, are not seen into."""
        lib = """\
def fetch():
    pass


class Base:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return True


class Child(Base):
    pass


LOCK = Base()
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.Child():\n    lib.fetch()\n",
                    "c.py": "with lib.LOCK:\n    lib.fetch()\n",
                    "d.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 unhandled" in msg
        assert "2 inside an unresolved `with`" in msg

    def test_try_finally_catches_nothing(self, trees):
        """A try with no except clause was counted broad; it is transparent."""
        t = trees.files(
            _with_sites(
                "def fetch():\n    pass\n",
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "try:\n    lib.fetch()\nfinally:\n    print('done')\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg
        assert "broad" not in msg


# An importer's discard_list_on_failure: cleans up, re-raises.
DISCARD_ON_FAILURE = """\
from contextlib import contextmanager


def fetch():
    pass


@contextmanager
def discard_on_failure(record):
    try:
        yield
    except Exception:
        record.delete()
        raise
"""


def _reraise_sites(handler_body: str) -> dict[str, str]:
    """One specific site, one unhandled, and one under ``except Exception as e``
    whose body is ``handler_body``."""
    body = "".join(f"    {line}\n" for line in handler_body.splitlines())
    return _with_sites(
        "def fetch():\n    pass\n",
        {
            "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
            "b.py": f"import sys\ntry:\n    lib.fetch()\nexcept Exception as e:\n{body}",
            "c.py": "lib.fetch()\n",
        },
    )


class TestCatchAndReraise:
    """An ``except`` whose every path re-raises what it caught is cleanup,
    not handling: the walk continues outward (DECISIONS.md, Phase 33-1)."""

    def test_try_cleanup_and_reraise_is_unhandled(self, trees):
        t = trees.files(_reraise_sites("cleanup()\nraise"))
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (ValueError)" in msg
        assert "2 unhandled" in msg
        assert "broad" not in msg

    def test_try_cleanup_and_reraise_defers_to_enclosing_try(self, trees):
        t = trees.files(
            _with_sites(
                "def fetch():\n    pass\n",
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": (
                        "try:\n"
                        "    try:\n"
                        "        lib.fetch()\n"
                        "    except Exception:\n"
                        "        cleanup()\n"
                        "        raise\n"
                        "except KeyError:\n"
                        "    pass\n"
                    ),
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (KeyError, ValueError)" in msg
        assert "1 unhandled" in msg

    def test_reraising_clause_beside_a_handling_one(self, trees):
        """Only the handling clause classifies the try."""
        t = trees.files(
            _with_sites(
                "def fetch():\n    pass\n",
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": (
                        "try:\n"
                        "    lib.fetch()\n"
                        "except (KeyError, Exception):\n"
                        "    cleanup()\n"
                        "    raise\n"
                        "except BaseException:\n"
                        "    pass\n"
                    ),
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (ValueError)" in msg
        assert "1 catch broad Exception" in msg
        assert "KeyError" not in msg

    def test_contextmanager_cleanup_and_reraise_is_unhandled(self, trees):
        """The discard_list_on_failure shape lets exceptions through."""
        t = trees.files(
            _with_sites(
                DISCARD_ON_FAILURE,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.discard_on_failure(r):\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg
        assert "broad" not in msg
        assert "unresolved" not in msg

    def test_contextmanager_cleanup_and_reraise_defers_to_enclosing_try(self, trees):
        t = trees.files(
            _with_sites(
                DISCARD_ON_FAILURE,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": (
                        "try:\n"
                        "    with lib.discard_on_failure(r):\n"
                        "        lib.fetch()\n"
                        "except OSError:\n"
                        "    pass\n"
                    ),
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 catch specific (OSError, ValueError)" in msg
        assert "1 unhandled" in msg

    def test_contextmanager_log_and_reraise_is_unhandled(self, trees):
        lib = """\
import logging
from contextlib import contextmanager


def fetch():
    pass


@contextmanager
def logged():
    try:
        yield
    except Exception:
        logging.getLogger(__name__).exception("failed")
        raise
"""
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.logged():\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "2 unhandled" in msg
        assert "broad" not in msg

    def test_contextmanager_that_converts_still_handles(self, trees):
        """Raising a different exception converts it, like __exit__'s raise X."""
        lib = DISCARD_ON_FAILURE.replace(
            "    except Exception:\n        record.delete()\n        raise\n",
            "    except Exception as e:\n        record.delete()\n"
            "        raise RuntimeError('discarded') from e\n",
        )
        assert "RuntimeError" in lib
        t = trees.files(
            _with_sites(
                lib,
                {
                    "a.py": "try:\n    lib.fetch()\nexcept ValueError:\n    pass\n",
                    "b.py": "with lib.discard_on_failure(r):\n    lib.fetch()\n",
                    "c.py": "lib.fetch()\n",
                },
            )
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch broad Exception" in msg
        assert "1 unhandled" in msg

    def test_reraising_wrapper_is_credited_through_propagation(self, trees):
        """A wrapper that logs and re-raises propagates to a catching caller."""
        t = trees.files(
            {
                "lib.py": "def get_token():\n    raise AuthError()",
                "auth.py": (
                    "from lib import get_token\n"
                    "def get_access_token():\n"
                    "    try:\n"
                    "        return get_token()\n"
                    "    except Exception:\n"
                    "        log.exception('token')\n"
                    "        raise\n"
                ),
                "a.py": "from lib import get_token\ntry:\n    get_token()\nexcept AuthError:\n    pass",
                "b.py": "from lib import get_token\nget_token()",
                "c.py": (
                    "from auth import get_access_token\n"
                    "try:\n    get_access_token()\nexcept AuthError:\n    pass"
                ),
            }
        )
        msg = check_inconsistent_error_handling(t)[0].message
        assert "1 catch specific (AuthError), 1 propagates to a caller that catches" in msg
        assert "1 unhandled" in msg
        assert "broad" not in msg

    @pytest.mark.parametrize(
        "handler_body",
        [
            pytest.param("raise", id="bare-raise"),
            pytest.param("log.exception('failed')\nraise", id="log-and-reraise"),
            pytest.param("raise e", id="raise-caught-name"),
            pytest.param("raise e from None", id="raise-caught-from-none"),
            pytest.param("if verbose:\n    log(e)\nraise", id="if-then-falls-to-raise"),
            pytest.param("if retry:\n    raise\nelse:\n    raise e", id="every-branch-raises"),
            pytest.param("for f in files:\n    f.unlink()\nraise", id="loop-then-raise"),
            pytest.param(
                "while True:\n    if done():\n        break\nraise", id="break-inside-own-loop"
            ),
            pytest.param(
                "try:\n    rollback()\nexcept Exception:\n    pass\nraise",
                id="guarded-cleanup-then-raise",
            ),
            pytest.param("def later():\n    return 1\nraise", id="nested-def-return"),
        ],
    )
    def test_every_path_reraises_passes_through(self, trees, handler_body):
        msg = check_inconsistent_error_handling(trees.files(_reraise_sites(handler_body)))[
            0
        ].message
        assert "2 unhandled" in msg
        assert "broad" not in msg

    @pytest.mark.parametrize(
        "handler_body",
        [
            pytest.param("if retry:\n    return None\nraise", id="mixed-return-and-raise"),
            pytest.param("if fatal:\n    raise", id="falls-off-the-end"),
            pytest.param("raise RuntimeError('x') from e", id="converts-from-caught"),
            pytest.param("raise RuntimeError('x')", id="converts"),
            pytest.param("e = Wrapped(e)\nraise e", id="rebound-name-converts"),
            pytest.param("sys.exit(1)\nraise", id="exits-first"),
            pytest.param("for f in files:\n    return f\nraise", id="loop-may-return"),
            pytest.param("with lock:\n    raise", id="raise-inside-with-is-not-followed"),
            pytest.param("log(e)", id="swallows"),
        ],
    )
    def test_some_path_handles(self, trees, handler_body):
        """Mixed paths, conversion and exits count as handling (unchanged)."""
        msg = check_inconsistent_error_handling(trees.files(_reraise_sites(handler_body)))[
            0
        ].message
        assert "1 catch broad Exception" in msg
        assert "1 unhandled" in msg


class TestVestigialParams:
    def test_finds_unused_param(self, trees):
        t = trees.code("""\
def process(data, format_type):
    return data.upper()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 1
        assert "format_type" in findings[0].message
        assert "process()" in findings[0].message

    def test_includes_caller_count(self, trees):
        t = trees.files(
            {
                "lib.py": """\
def process(data, format_type):
    return data.upper()
""",
                "a.py": 'from lib import process\nprocess("hello", "json")',
                "b.py": 'from lib import process\nprocess("world", "xml")',
            }
        )
        findings = check_vestigial_params(t)
        assert len(findings) == 1
        assert "2 caller(s) still pass it" in findings[0].message

    def test_no_finding_all_params_used(self, trees):
        t = trees.code("""\
def process(data, format_type):
    if format_type == "json":
        return json.dumps(data)
    return str(data)
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_underscore_prefixed_params(self, trees):
        t = trees.code("""\
def callback(event, _context):
    handle(event)
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_self_and_cls(self, trees):
        t = trees.code("""\
class Foo:
    def method(self, data):
        return data.upper()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_pass(self, trees):
        t = trees.code("""\
def not_implemented(data, options):
    pass
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_ellipsis(self, trees):
        t = trees.code("""\
def protocol_method(data, options):
    ...
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_not_implemented(self, trees):
        t = trees.code("""\
def abstract_method(data, options):
    raise NotImplementedError
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_bare_return(self, trees):
        """bare return is a stub — often with a TODO comment."""
        t = trees.code("""\
def validate_css(value):
    return
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_return_none(self, trees):
        t = trees.code("""\
def validate_something(value):
    return None
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_stub_body_docstring_plus_return(self, trees):
        t = trees.code("""\
def validate_css(value):
    \"\"\"Validate CSS value.\"\"\"
    return
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_abstractmethod_decorated(self, trees):
        t = trees.code("""\
from abc import abstractmethod

class Base:
    @abstractmethod
    def process(self, data, options):
        return data
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_override_decorated(self, trees):
        t = trees.code("""\
class Child(Base):
    @override
    def process(self, data, options):
        return data.upper()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_multiple_unused_params(self, trees):
        t = trees.code("""\
def transform(data, retry_count, timeout, cache_key, batch_size):
    return data
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 4
        names = {f.message.split(" is")[0] for f in findings}
        assert names == {"retry_count", "timeout", "cache_key", "batch_size"}

    def test_skips_request_as_first_param(self, trees):
        """Django views: request is framework-required even if unused."""
        t = trees.code("""\
def my_view(request):
    return render_template("index.html")
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_request_as_first_param_in_method(self, trees):
        t = trees.code("""\
class MyView:
    def get(self, request, pk):
        return self.get_object(pk)
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_request_not_skipped_when_not_first(self, trees):
        """request in a non-first position is suspicious."""
        t = trees.code("""\
def process(data, request):
    return data.upper()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 1
        assert "request" in findings[0].message

    def test_skips_receiver_decorated(self, trees):
        """Django signal receivers have framework-required signatures."""
        t = trees.code("""\
from django.dispatch import receiver

@receiver(post_save)
def handle_save(sender, instance, created, **kwargs):
    log("saved")
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_celery_task_decorated(self, trees):
        t = trees.code("""\
from celery import shared_task

@shared_task
def process(data, retries):
    return data
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_docstring_only_stub(self, trees):
        t = trees.code("""\
def abstract_method(data, options):
    \"\"\"Must be implemented by subclasses.\"\"\"
    pass
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_finds_unused_in_method(self, trees):
        t = trees.code("""\
class Executor:
    def execute(self, task, track_metrics=False, run_async=False):
        return task.run()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 2
        names = {f.message.split(" is")[0] for f in findings}
        assert "track_metrics" in names
        assert "run_async" in names

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "tests/test_foo.py": """\
def helper(data, unused):
    return data
""",
            }
        )
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_django_admin_permission_methods(self, trees):
        """Django admin permission methods have framework-dictated signatures."""
        t = trees.code("""\
class MyAdmin:
    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request, obj=None):
        return False
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_context_manager_exit(self, trees):
        """__exit__ signature is dictated by the context manager protocol."""
        t = trees.code("""\
class Resource:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_async_context_manager_exit(self, trees):
        t = trees.code("""\
class AsyncResource:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_descriptor_protocol(self, trees):
        """Descriptor protocol methods have language-dictated signatures."""
        t = trees.code("""\
class CachedProperty:
    def __get__(self, instance, owner):
        return instance._cache

    def __set__(self, instance, value):
        instance._cache = None

    def __set_name__(self, owner, name):
        self._cache = None
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_skips_click_callback_signature(self, trees):
        """Click ``callback=`` callbacks receive (ctx, param, value) from
        the framework; the author cannot remove ctx/param even when unused.

        Catches both the underscore-prefixed (_ctx, _param) and bare
        (ctx, param) conventions.
        """
        t = trees.code("""\
def _click_validate_canonical_date(ctx, param, value):
    return value

def _click_validate_month(_ctx, _param, value):
    return value
""")
        findings = check_vestigial_params(t)
        assert len(findings) == 0

    def test_does_not_skip_three_param_non_click_signature(self, trees):
        """Unused params on a non-Click 3-arg signature should still fire."""
        t = trees.code("""\
def loader(source, options, value):
    return value
""")
        findings = check_vestigial_params(t)
        # source and options are unused → 2 findings
        assert len(findings) == 2


class TestDictAsDataclass:
    def test_finds_large_dict_return(self, trees):
        t = trees.code("""\
def get_user_data(user):
    return {
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
    }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1
        assert "get_user_data()" in findings[0].message
        assert "4 string keys" in findings[0].message

    def test_ignores_small_dict(self, trees):
        t = trees.code("""\
def get_info():
    return {"name": "foo", "age": 42}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_ignores_non_string_keys(self, trees):
        t = trees.code("""\
def get_mapping():
    return {1: "a", 2: "b", 3: "c", 4: "d"}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_cross_file_access(self, trees):
        t = trees.files(
            {
                "models.py": """\
def get_user_data(user):
    return {
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
    }
""",
                "views.py": """\
def show_user(user):
    data = get_user_data(user)
    print(data["name"])
    print(data["email"])
""",
            }
        )
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1
        assert "1 file" in findings[0].message

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "tests/test_data.py": """\
def get_fixture():
    return {"a": 1, "b": 2, "c": 3, "d": 4}
"""
            }
        )
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_severity_is_medium(self, trees):
        t = trees.code("""\
def get_config():
    return {"host": "h", "port": 80, "timeout": 30, "retries": 3}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM

    def test_lists_key_names(self, trees):
        t = trees.code("""\
def make_record():
    return {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1
        assert "first" in findings[0].message

    def test_suppresses_method_on_dataclass_projecting_self(self, trees):
        """A dataclass method that returns a dict of self.* is a wire-format
        serializer, not a missing dataclass."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class ArchiveOverlap:
    name: str
    size: int
    extra: int
    other: int

    def to_dict(self):
        return {
            "name": self.name,
            "size": self.size,
            "extra": self.extra,
            "other": self.other,
        }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_suppresses_function_with_dataclass_param(self, trees):
        """A free function whose dict values project a dataclass-typed
        parameter is also wire format."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class ArchiveOverlap:
    name: str
    size: int
    extra: int
    other: int

def _archive_overlap_dict(ov: ArchiveOverlap):
    return {
        "name": ov.name,
        "size": ov.size,
        "extra": ov.extra,
        "other": ov.other,
    }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_allows_one_computed_value_alongside_projections(self, trees):
        """A row serializer may transform one field (e.g. enum -> str) and
        should still be recognized as a projection."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Row:
    name: str
    size: int
    extra: int
    other: int

    def as_csv_row(self):
        return {
            "name": self.name,
            "size_kb": self.size // 1024,
            "extra": self.extra,
            "other_label": format(self.other),
        }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_does_not_suppress_method_on_plain_class(self, trees):
        """Only @dataclass / BaseModel / NamedTuple-derived classes count.
        A method on a plain class returning a 4+ key dict is still a smell."""
        t = trees.code("""\
class Plain:
    def to_dict(self):
        return {"a": 1, "b": 2, "c": 3, "d": 4}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1

    def test_does_not_suppress_when_param_is_not_dataclass(self, trees):
        """A function param typed as something we DON'T see as dataclass-like
        does not justify the projection suppression."""
        t = trees.code("""\
class Plain:
    pass

def make(p: Plain):
    return {
        "name": p.name,
        "size": p.size,
        "extra": p.extra,
        "other": p.other,
    }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1

    def test_suppresses_basemodel_projection(self, trees):
        """Pydantic BaseModel subclasses are dataclass-like."""
        t = trees.code("""\
from pydantic import BaseModel

class Config(BaseModel):
    host: str
    port: int
    timeout: int
    retries: int

    def to_dict(self):
        return {
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
            "retries": self.retries,
        }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_suppresses_namedtuple_projection(self, trees):
        """typing.NamedTuple subclasses are dataclass-like."""
        t = trees.code("""\
from typing import NamedTuple

class Point(NamedTuple):
    x: int
    y: int
    z: int
    w: int

    def to_dict(self):
        return {"x": self.x, "y": self.y, "z": self.z, "w": self.w}
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 0

    def test_does_not_suppress_when_too_many_values_are_unrelated(self, trees):
        """If most dict values aren't projections of the dataclass source,
        the function is constructing a heterogeneous dict — still a smell."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Thing:
    name: str

    def to_dict(self):
        return {
            "name": self.name,
            "timestamp": now(),
            "version": "1.0",
            "host": socket.gethostname(),
            "extra": compute_extra(),
        }
""")
        findings = check_dict_as_dataclass(t)
        assert len(findings) == 1
