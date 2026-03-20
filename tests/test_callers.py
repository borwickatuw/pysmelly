"""Tests for caller-aware checks (cross-file call-graph analysis)."""

from pysmelly.checks.callers import (
    check_constant_args,
    check_dead_code,
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
        assert "make it required" in findings[0].message

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


class TestSingleCallSite:
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
        assert "only called within same file" in findings[0].message

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


class TestReturnNoneInsteadOfRaise:
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
        assert "consider raising instead" in findings[0].message

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

    def test_method_calls_counted(self, trees):
        """Attribute calls (obj.method()) are still counted."""
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
