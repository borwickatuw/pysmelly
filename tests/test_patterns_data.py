"""Tests for pattern data checks (foo-equals-foo, constant-dispatch-dicts, trivial-wrappers, dead-constants)."""

from pysmelly.checks.patterns_data import (
    check_constant_dispatch_dicts,
    check_dead_constants,
    check_foo_equals_foo,
    check_trivial_wrappers,
)


class TestFooEqualsFoo:
    def test_finds_single_use_locals(self, trees):
        """Single-use locals gathered into a call — the real smell."""
        t = trees.code("""\
def build():
    name = get_name()
    age = get_age()
    email = get_email()
    role = get_role()
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 1
        assert "single-use locals" in findings[0].message
        assert findings[0].severity.value == "medium"

    def test_suppresses_pure_forwarding(self, trees):
        """def f(x): g(x=x) is just forwarding — not a smell."""
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 0

    def test_mixed_forwarded_and_single_use(self, trees):
        """Reports single-use locals even when some args are forwarded params."""
        t = trees.code("""\
def build(name):
    age = get_age()
    email = get_email()
    role = get_role()
    extra = get_extra()
    return Thing(name=name, age=age, email=email, role=role, extra=extra)
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 1
        assert "4 are single-use locals" in findings[0].message
        assert "name" not in findings[0].message.split("single-use locals")[1]

    def test_suppresses_multi_use_locals(self, trees):
        """Locals used elsewhere too are standard Python style — suppressed."""
        t = trees.code("""\
def build():
    name = get_name()
    age = get_age()
    email = get_email()
    role = get_role()
    log(name, age, email, role)
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 0

    def test_ignores_below_threshold(self, trees):
        t = trees.code("""\
def build():
    name = get_name()
    age = get_age()
    return Thing(name=name, age=age)
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 0

    def test_ignores_non_matching_kwargs(self, trees):
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=42, email="x", role="admin")
""")
        findings = check_foo_equals_foo(t)
        assert len(findings) == 0


class TestConstantDispatchDicts:
    def test_finds_dispatch_dict(self, trees):
        t = trees.code("""\
def handle_a(): pass
def handle_b(): pass
def handle_c(): pass

HANDLERS = {
    "a": handle_a,
    "b": handle_b,
    "c": handle_c,
}
""")
        findings = check_constant_dispatch_dicts(t)
        assert len(findings) == 1
        assert "dispatch dict" in findings[0].message

    def test_ignores_small_dict(self, trees):
        t = trees.code("""\
def handle_a(): pass
def handle_b(): pass

HANDLERS = {
    "a": handle_a,
    "b": handle_b,
}
""")
        findings = check_constant_dispatch_dicts(t)
        assert len(findings) == 0

    def test_ignores_non_name_values(self, trees):
        t = trees.code("""\
CONFIG = {
    "host": "localhost",
    "port": "8080",
    "debug": "true",
}
""")
        findings = check_constant_dispatch_dicts(t)
        assert len(findings) == 0

    def test_ignores_uppercase_constant_values(self, trees):
        """Dict mapping strings to UPPER_CASE names is config, not dispatch."""
        t = trees.code("""\
_o365_settings = {
    "O365_CLIENTID": O365_CLIENTID,
    "O365_CLIENTSECRET": O365_CLIENTSECRET,
    "O365_TENANT_ID": O365_TENANT_ID,
}
""")
        findings = check_constant_dispatch_dicts(t)
        assert len(findings) == 0

    def test_still_flags_mixed_case_values(self, trees):
        """Mixed case values (some functions, some constants) still flagged."""
        t = trees.code("""\
HANDLERS = {
    "a": handle_a,
    "b": FALLBACK,
    "c": handle_c,
}
""")
        findings = check_constant_dispatch_dicts(t)
        assert len(findings) == 1


class TestTrivialWrappers:
    def test_finds_dict_lookup(self, trees):
        t = trees.code("""\
def get_color(name):
    return COLORS[name]
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1
        assert "COLORS[...]" in findings[0].message

    def test_finds_attribute_access(self, trees):
        t = trees.code("""\
def get_name(user):
    return user.name
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1
        assert "user.name" in findings[0].message

    def test_finds_single_function_call(self, trees):
        t = trees.code("""\
def get_data(key):
    return fetch(key)
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1
        assert "fetch(...)" in findings[0].message

    def test_finds_with_docstring(self, trees):
        t = trees.code("""\
def get_config(key):
    \"\"\"Get a config value.\"\"\"
    return CONFIG[key]
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1

    def test_ignores_multi_statement(self, trees):
        t = trees.code("""\
def process(data):
    result = transform(data)
    return result
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _helper(key):
    return CONFIG[key]
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_complex_return(self, trees):
        t = trees.code("""\
def compute(a, b):
    return a + b
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_abstract_method_impl(self, trees):
        """Constant return in a subclass method = abstract method implementation."""
        t = trees.code("""\
class MyHandler(BaseHandler):
    def name(self):
        return "my-handler"
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_subclass_method_with_attr_return(self, trees):
        """Subclass methods are protocol implementations — can't inline."""
        t = trees.code("""\
class EventSitemap(Sitemap):
    def lastmod(self, obj):
        return obj.modified_date

    def location(self, obj):
        return obj.get_absolute_url()
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_flags_constant_return_without_base(self, trees):
        """Constant return in a class without bases is still flagged."""
        t = trees.code("""\
class MyHandler:
    def name(self):
        return "my-handler"
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1

    def test_ignores_self_method_chain(self, trees):
        """return self.to_dict() is part of a deliberate API chain."""
        t = trees.code("""\
class Config:
    def to_json(self):
        return self.to_dict()
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_from_dict_with_complex_args(self, trees):
        """from_dict doing data.get() mapping is real work, not trivial."""
        t = trees.code("""\
class Config:
    @classmethod
    def from_dict(cls, data):
        return cls(name=data.get("name", ""), port=data.get("port", 8080))
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_call_with_extra_constant_kwarg(self, trees):
        """Wrapper that adds compress=True is providing configuration, not trivial."""
        t = trees.code("""\
def encrypt_token_for_task(data):
    return signing.dumps(data, compress=True)
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_call_with_hardcoded_arg(self, trees):
        """Wrapper that passes a constant positional arg is adding configuration."""
        t = trees.code("""\
def get_rds_client():
    return boto3.client("rds")
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_flags_call_with_simple_passthrough(self, trees):
        """return func(name) with simple Name args is still trivial."""
        t = trees.code("""\
def get_data(key):
    return fetch(key)
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1

    def test_ignores_decorated_function(self, trees):
        """Decorated functions have framework-defined purpose — not trivial."""
        t = trees.code("""\
@pytest.fixture
def client():
    return Client()
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_property_decorator(self, trees):
        """@property returns are part of the class API."""
        t = trees.code("""\
class Config:
    @property
    def name(self):
        return self._name
""")
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_ignores_multi_caller_wrapper(self, trees):
        """Wrappers with 3+ callers provide a central point for change."""
        t = trees.files(
            {
                "client.py": """\
def get_data(key):
    return fetch(key)
""",
                "a.py": "from client import get_data\nget_data('x')\n",
                "b.py": "from client import get_data\nget_data('y')\n",
                "c.py": "from client import get_data\nget_data('z')\n",
            }
        )
        findings = check_trivial_wrappers(t)
        assert len(findings) == 0

    def test_flags_single_caller_pure_forwarding(self, trees):
        """Pure forwarding wrappers with few callers are still flagged."""
        t = trees.files(
            {
                "client.py": """\
def get_data(key):
    return fetch(key)
""",
                "a.py": "from client import get_data\nget_data('x')\n",
            }
        )
        findings = check_trivial_wrappers(t)
        assert len(findings) == 1


class TestDeadConstants:
    def test_finds_unreferenced_string_constant(self, trees):
        t = trees.code("""\
TASK_BEFORE_EXECUTE = "task:before_execute"
TASK_AFTER_EXECUTE = "task:after_execute"

def fire(event):
    pass
""")
        findings = check_dead_constants(t)
        assert len(findings) == 2
        names = {f.message.split(" = ")[0] for f in findings}
        assert "TASK_BEFORE_EXECUTE" in names
        assert "TASK_AFTER_EXECUTE" in names

    def test_no_finding_when_used_as_argument(self, trees):
        t = trees.code("""\
TASK_BEFORE_EXECUTE = "task:before_execute"

def setup():
    hook_manager.on(TASK_BEFORE_EXECUTE, my_callback)
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_no_finding_when_used_in_conditional(self, trees):
        t = trees.code("""\
MODE = "production"

if MODE == "production":
    do_stuff()
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_no_finding_when_imported_elsewhere(self, trees):
        t = trees.files(
            {
                "constants.py": """\
API_VERSION = "v2"
""",
                "client.py": """\
from constants import API_VERSION
""",
            }
        )
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_no_finding_when_in_dunder_all(self, trees):
        t = trees.code("""\
__all__ = ["EVENT_NAME"]
EVENT_NAME = "my_event"
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_no_finding_when_accessed_as_attribute(self, trees):
        t = trees.code("""\
STATUS_OK = 200

def check(response):
    return response.STATUS_OK
""")
        # STATUS_OK appears as an Attribute.attr in Load context
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_no_finding_when_reassigned(self, trees):
        """Reassigned constants are excluded (not truly constant)."""
        t = trees.code("""\
COUNTER = 0
COUNTER = 1
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_skips_private_constants(self, trees):
        t = trees.code("""\
_INTERNAL = "secret"
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_skips_lowercase_names(self, trees):
        t = trees.code("""\
my_constant = "hello"
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_cross_file_unreferenced(self, trees):
        t = trees.files(
            {
                "hooks.py": """\
EVENT_START = "start"
EVENT_STOP = "stop"
""",
                "executor.py": """\
def run():
    fire("start")
    fire("stop")
""",
            }
        )
        # Constants defined but executor uses string literals instead
        findings = check_dead_constants(t)
        assert len(findings) == 2

    def test_no_finding_numeric_constant_used(self, trees):
        t = trees.code("""\
MAX_RETRIES = 3

def retry(func):
    for i in range(MAX_RETRIES):
        func()
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_truncates_long_values(self, trees):
        t = trees.code("""\
VERY_LONG_CONSTANT = "this is a very long string value that exceeds forty characters in repr"
""")
        findings = check_dead_constants(t)
        assert len(findings) == 1
        assert "..." in findings[0].message

    def test_skips_settings_file(self, trees):
        """Django settings constants are read by the framework via getattr()."""
        t = trees.files(
            {
                "myapp/settings.py": """\
ROOT_URLCONF = "myapp.urls"
AUTH_USER_MODEL = "accounts.User"
DEBUG = True
""",
            }
        )
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_skips_settings_directory(self, trees):
        t = trees.files(
            {
                "config/settings/base.py": """\
SECRET_KEY = "django-insecure-abc123"
INSTALLED_APPS = []
""",
            }
        )
        findings = check_dead_constants(t)
        assert len(findings) == 0

    def test_non_settings_file_still_flagged(self, trees):
        t = trees.files(
            {
                "myapp/constants.py": """\
UNUSED_CONSTANT = "never_referenced"
""",
            }
        )
        findings = check_dead_constants(t)
        assert len(findings) == 1

    def test_finds_unreferenced_frozenset(self, trees):
        """Non-literal constants like frozenset() should also be caught."""
        t = trees.code("""\
SKIP_NAMES = frozenset({"foo", "bar", "baz"})
OTHER_SET = frozenset({"a", "b"})

def process(name):
    return name
""")
        findings = check_dead_constants(t)
        assert len(findings) == 2
        names = {f.message.split(" = ")[0] for f in findings}
        assert "SKIP_NAMES" in names
        assert "OTHER_SET" in names

    def test_finds_unreferenced_dict_literal(self, trees):
        t = trees.code("""\
MAPPING = {"a": 1, "b": 2}

def process():
    pass
""")
        findings = check_dead_constants(t)
        assert len(findings) == 1
        assert "MAPPING" in findings[0].message

    def test_no_finding_when_frozenset_is_used(self, trees):
        t = trees.code("""\
SKIP_NAMES = frozenset({"foo", "bar"})

def process(name):
    if name in SKIP_NAMES:
        return
""")
        findings = check_dead_constants(t)
        assert len(findings) == 0
