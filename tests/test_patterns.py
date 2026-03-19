"""Tests for pattern-based checks."""

from pysmelly.checks.patterns import (
    check_constant_dispatch_dicts,
    check_env_fallbacks,
    check_foo_equals_foo,
    check_suspicious_fallbacks,
    check_temp_accumulators,
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
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 1
        assert "single-use locals" in findings[0].message
        assert findings[0].severity.value == "medium"

    def test_suppresses_pure_forwarding(self, trees):
        """def f(x): g(x=x) is just forwarding — not a smell."""
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t, verbose=False)
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
        findings = check_foo_equals_foo(t, verbose=False)
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
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_below_threshold(self, trees):
        t = trees.code("""\
def build():
    name = get_name()
    age = get_age()
    return Thing(name=name, age=age)
""")
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_non_matching_kwargs(self, trees):
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=42, email="x", role="admin")
""")
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 0


class TestSuspiciousFallbacks:
    def test_finds_nontrivial_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00", "blue": "#00f"}

x = COLORS.get("green", "#000")
""")
        findings = check_suspicious_fallbacks(t, verbose=False)
        assert len(findings) == 1
        assert "non-trivial fallback" in findings[0].message

    def test_ignores_none_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green", None)
""")
        findings = check_suspicious_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_empty_string_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green", "")
""")
        findings = check_suspicious_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_no_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green")
""")
        findings = check_suspicious_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_lowercase_dicts(self, trees):
        t = trees.code("""\
colors = {"red": "#f00"}

x = colors.get("green", "#000")
""")
        findings = check_suspicious_fallbacks(t, verbose=False)
        assert len(findings) == 0


class TestTempAccumulators:
    def test_finds_append_then_join(self, trees):
        t = trees.code("""\
def build():
    parts = []
    parts.append("a")
    parts.append("b")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 1
        assert "temporary accumulator" in findings[0].message

    def test_finds_append_then_check(self, trees):
        t = trees.code("""\
def build():
    errors = []
    errors.append("bad")
    errors.append("worse")
    if errors:
        raise ValueError(str(errors))
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 1

    def test_ignores_single_append(self, trees):
        t = trees.code("""\
def build():
    parts = []
    parts.append("a")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_nonempty_initial_list(self, trees):
        t = trees.code("""\
def build():
    parts = ["header"]
    parts.append("a")
    parts.append("b")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 0

    def test_loop_append_is_medium_severity(self, trees):
        """Loop-and-append is high confidence — should be a comprehension."""
        t = trees.code("""\
def build(items):
    parts = []
    for item in items:
        parts.append(item.name)
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 1
        assert findings[0].severity.value == "medium"
        assert "comprehension" in findings[0].message
        assert "loop-and-append" in findings[0].message

    def test_conditional_appends_are_low_severity(self, trees):
        """Independent conditional appends — accumulator is often appropriate."""
        t = trees.code("""\
def build(config):
    flags = []
    if config.verbose:
        flags.append("--verbose")
    if config.debug:
        flags.append("--debug")
    if flags:
        run(flags)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 1
        assert findings[0].severity.value == "low"
        assert "independent conditions" in findings[0].message

    def test_suppresses_batch_flush_with_reassign(self, trees):
        """Batch-flush pattern: append + reassign to [] within loop."""
        t = trees.code("""\
def process(items):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= 10:
            send(batch)
            batch = []
    if batch:
        send(batch)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 0

    def test_suppresses_batch_flush_with_clear(self, trees):
        """Batch-flush pattern: append + .clear() within loop."""
        t = trees.code("""\
def process(items):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= 10:
            send(batch)
            batch.clear()
    if batch:
        send(batch)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 0

    def test_mixed_loop_and_conditional_is_medium(self, trees):
        """When loop appends are present, stays MEDIUM even with conditionals."""
        t = trees.code("""\
def build(items, extra):
    parts = []
    for item in items:
        parts.append(item.name)
    if extra:
        parts.append(extra)
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t, verbose=False)
        assert len(findings) == 1
        assert findings[0].severity.value == "medium"


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
        findings = check_constant_dispatch_dicts(t, verbose=False)
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
        findings = check_constant_dispatch_dicts(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_non_name_values(self, trees):
        t = trees.code("""\
CONFIG = {
    "host": "localhost",
    "port": "8080",
    "debug": "true",
}
""")
        findings = check_constant_dispatch_dicts(t, verbose=False)
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
        findings = check_constant_dispatch_dicts(t, verbose=False)
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
        findings = check_constant_dispatch_dicts(t, verbose=False)
        assert len(findings) == 1


class TestTrivialWrappers:
    def test_finds_dict_lookup(self, trees):
        t = trees.code("""\
def get_color(name):
    return COLORS[name]
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1
        assert "COLORS[...]" in findings[0].message

    def test_finds_attribute_access(self, trees):
        t = trees.code("""\
def get_name(user):
    return user.name
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1
        assert "user.name" in findings[0].message

    def test_finds_single_function_call(self, trees):
        t = trees.code("""\
def get_data(key):
    return fetch(key)
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1
        assert "fetch(...)" in findings[0].message

    def test_finds_with_docstring(self, trees):
        t = trees.code("""\
def get_config(key):
    \"\"\"Get a config value.\"\"\"
    return CONFIG[key]
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1

    def test_ignores_multi_statement(self, trees):
        t = trees.code("""\
def process(data):
    result = transform(data)
    return result
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _helper(key):
    return CONFIG[key]
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_complex_return(self, trees):
        t = trees.code("""\
def compute(a, b):
    return a + b
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_abstract_method_impl(self, trees):
        """Constant return in a subclass method = abstract method implementation."""
        t = trees.code("""\
class MyHandler(BaseHandler):
    def name(self):
        return "my-handler"
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_flags_constant_return_without_base(self, trees):
        """Constant return in a class without bases is still flagged."""
        t = trees.code("""\
class MyHandler:
    def name(self):
        return "my-handler"
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1

    def test_ignores_self_method_chain(self, trees):
        """return self.to_dict() is part of a deliberate API chain."""
        t = trees.code("""\
class Config:
    def to_json(self):
        return self.to_dict()
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_from_dict_with_complex_args(self, trees):
        """from_dict doing data.get() mapping is real work, not trivial."""
        t = trees.code("""\
class Config:
    @classmethod
    def from_dict(cls, data):
        return cls(name=data.get("name", ""), port=data.get("port", 8080))
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_flags_call_with_simple_passthrough(self, trees):
        """return func(name) with simple Name args is still trivial."""
        t = trees.code("""\
def get_data(key):
    return fetch(key)
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1

    def test_ignores_decorated_function(self, trees):
        """Decorated functions have framework-defined purpose — not trivial."""
        t = trees.code("""\
@pytest.fixture
def client():
    return Client()
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_property_decorator(self, trees):
        """@property returns are part of the class API."""
        t = trees.code("""\
class Config:
    @property
    def name(self):
        return self._name
""")
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_multi_caller_wrapper(self, trees):
        """Wrappers with 3+ callers provide a central point for change."""
        t = trees.files(
            {
                "client.py": """\
def get_client():
    return boto3.client("rds")
""",
                "a.py": "from client import get_client\nget_client()\n",
                "b.py": "from client import get_client\nget_client()\n",
                "c.py": "from client import get_client\nget_client()\n",
            }
        )
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 0

    def test_flags_single_caller_wrapper(self, trees):
        """Wrappers with few callers are still flagged."""
        t = trees.files(
            {
                "client.py": """\
def get_client():
    return boto3.client("rds")
""",
                "a.py": "from client import get_client\nget_client()\n",
            }
        )
        findings = check_trivial_wrappers(t, verbose=False)
        assert len(findings) == 1


class TestEnvFallbacks:
    def test_finds_environ_get_with_default(self, trees):
        t = trees.code("""\
import os
db = os.environ.get("DB_HOST", "localhost")
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 1
        assert "DB_HOST" in findings[0].message
        assert "fail fast" in findings[0].message

    def test_finds_getenv_with_default(self, trees):
        t = trees.code("""\
import os
port = os.getenv("PORT", "8080")
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 1
        assert "PORT" in findings[0].message

    def test_ignores_none_default(self, trees):
        t = trees.code("""\
import os
val = os.environ.get("KEY", None)
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_no_default(self, trees):
        t = trees.code("""\
import os
val = os.environ.get("KEY")
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_bracket_access(self, trees):
        t = trees.code("""\
import os
val = os.environ["KEY"]
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 0

    def test_finds_getenv_none_default_ignored(self, trees):
        t = trees.code("""\
import os
val = os.getenv("KEY", None)
""")
        findings = check_env_fallbacks(t, verbose=False)
        assert len(findings) == 0
