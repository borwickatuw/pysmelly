"""Tests for pattern-based checks."""

from pysmelly.checks.patterns import (
    check_arrow_code,
    check_boolean_param_explosion,
    check_constant_dispatch_dicts,
    check_dead_constants,
    check_env_fallbacks,
    check_exception_flow_control,
    check_foo_equals_foo,
    check_fossilized_toggles,
    check_getattr_strings,
    check_hungarian_notation,
    check_inconsistent_returns,
    check_isinstance_chain,
    check_late_binding_closures,
    check_law_of_demeter,
    check_plaintext_passwords,
    check_runtime_monkey_patch,
    check_suspicious_fallbacks,
    check_temp_accumulators,
    check_trivial_wrappers,
    check_unreachable_after_return,
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


class TestSuspiciousFallbacks:
    def test_finds_nontrivial_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00", "blue": "#00f"}

x = COLORS.get("green", "#000")
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 1
        assert "non-trivial fallback" in findings[0].message

    def test_ignores_none_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green", None)
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 0

    def test_ignores_empty_string_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green", "")
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 0

    def test_ignores_no_default(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.get("green")
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 0

    def test_ignores_lowercase_dicts(self, trees):
        t = trees.code("""\
colors = {"red": "#f00"}

x = colors.get("green", "#000")
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 0

    def test_finds_setdefault_nontrivial(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00", "blue": "#00f"}

x = COLORS.setdefault("green", "#000")
""")
        findings = check_suspicious_fallbacks(t)
        assert len(findings) == 1
        assert "setdefault()" in findings[0].message
        assert "non-trivial fallback" in findings[0].message

    def test_ignores_setdefault_none(self, trees):
        t = trees.code("""\
COLORS = {"red": "#f00"}

x = COLORS.setdefault("green", None)
""")
        findings = check_suspicious_fallbacks(t)
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
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
        assert len(findings) == 1

    def test_ignores_single_append(self, trees):
        t = trees.code("""\
def build():
    parts = []
    parts.append("a")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 0

    def test_ignores_nonempty_initial_list(self, trees):
        t = trees.code("""\
def build():
    parts = ["header"]
    parts.append("a")
    parts.append("b")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
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
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        assert findings[0].severity.value == "medium"

    def test_names_dict_key_consumer(self, trees):
        """Accumulator consumed by dict key assignment gets specific message."""
        t = trees.code("""\
def build():
    entries = []
    for item in items:
        entries.append(item.name)
    manifest["metadata"] = entries
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        assert "manifest['metadata']" in findings[0].message
        assert "line 5" in findings[0].message

    def test_names_attribute_consumer(self, trees):
        """Accumulator consumed by attribute assignment gets specific message."""
        t = trees.code("""\
def build():
    tags = []
    tags.append("a")
    tags.append("b")
    result.tags = tags
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        assert "result.tags" in findings[0].message

    def test_consumer_with_conditional_appends(self, trees):
        """Conditional appends with single consumer still name the target."""
        t = trees.code("""\
def build(config):
    flags = []
    if config.verbose:
        flags.append("--verbose")
    if config.debug:
        flags.append("--debug")
    result.flags = flags
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        assert "result.flags" in findings[0].message

    def test_no_consumer_naming_for_join(self, trees):
        """Join is already clear — don't try to name a consumer."""
        t = trees.code("""\
def build():
    parts = []
    parts.append("a")
    parts.append("b")
    return ", ".join(parts)
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        assert "populate" not in findings[0].message

    def test_multiple_consumers_no_naming(self, trees):
        """Multiple assignment consumers — don't try to pick one."""
        t = trees.code("""\
def build():
    items = []
    for x in data:
        items.append(x.name)
    result.items = items
    backup.items = items
""")
        findings = check_temp_accumulators(t)
        assert len(findings) == 1
        # Should not name a specific consumer since there are two
        assert "populate" not in findings[0].message


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


class TestEnvFallbacks:
    def test_finds_environ_get_with_default(self, trees):
        t = trees.code("""\
import os
db = os.environ.get("DB_HOST", "localhost")
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 1
        assert "DB_HOST" in findings[0].message
        assert "fail fast" in findings[0].message

    def test_finds_getenv_with_default(self, trees):
        t = trees.code("""\
import os
port = os.getenv("PORT", "8080")
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 1
        assert "PORT" in findings[0].message

    def test_ignores_none_default(self, trees):
        t = trees.code("""\
import os
val = os.environ.get("KEY", None)
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 0

    def test_ignores_no_default(self, trees):
        t = trees.code("""\
import os
val = os.environ.get("KEY")
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 0

    def test_ignores_bracket_access(self, trees):
        t = trees.code("""\
import os
val = os.environ["KEY"]
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 0

    def test_finds_getenv_none_default_ignored(self, trees):
        t = trees.code("""\
import os
val = os.getenv("KEY", None)
""")
        findings = check_env_fallbacks(t)
        assert len(findings) == 0


class TestRuntimeMonkeyPatch:
    def test_finds_basic_monkey_patch(self, trees):
        """Module-level obj.attr = local_func is flagged."""
        t = trees.code("""\
def get_urls_with_maintenance(self):
    pass

admin.site.get_urls = get_urls_with_maintenance
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 1
        assert "admin.site.get_urls" in findings[0].message
        assert "monkey-patch" in findings[0].message

    def test_detects_captured_original(self, trees):
        """Capture-then-replace pattern noted in message."""
        t = trees.code("""\
original_get_urls = admin.site.get_urls

def get_urls_with_maintenance(self):
    pass

admin.site.get_urls = get_urls_with_maintenance
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 1
        assert "original_get_urls" in findings[0].message

    def test_simple_attr_assignment(self, trees):
        """Single-level attribute patch: MyClass.method = func."""
        t = trees.code("""\
def is_active_display(obj):
    return obj.is_active

MyModelAdmin.is_active_display = is_active_display
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 1
        assert "MyModelAdmin.is_active_display" in findings[0].message

    def test_ignores_non_function_value(self, trees):
        """String/constant assignments are not monkey-patches."""
        t = trees.code("""\
def something():
    pass

admin.site.site_header = "My Admin"
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 0

    def test_ignores_call_value(self, trees):
        """obj.attr = func() is configuration, not monkey-patching."""
        t = trees.code("""\
def make_handler():
    pass

obj.handler = make_handler()
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 0

    def test_ignores_non_local_function(self, trees):
        """Value not defined as a function in this file is not flagged."""
        t = trees.code("""\
admin.site.get_urls = imported_func
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 0

    def test_ignores_assignment_inside_function(self, trees):
        """Attribute assignments inside functions are not module-level."""
        t = trees.code("""\
def my_func():
    pass

def setup():
    admin.site.get_urls = my_func
""")
        findings = check_runtime_monkey_patch(t)
        assert len(findings) == 0


class TestFossilizedToggles:
    # --- positive cases ---

    def test_boolean_false_in_if(self, trees):
        """if FLAG: where FLAG = False is always False."""
        t = trees.code("""\
FLAG = False

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "FLAG = False" in findings[0].message
        assert "always False" in findings[0].message
        assert "(dead branch)" in findings[0].message

    def test_boolean_true_in_if(self, trees):
        """if FLAG: where FLAG = True is always True."""
        t = trees.code("""\
ENABLED = True

if ENABLED:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always True" in findings[0].message

    def test_if_not_false(self, trees):
        """if not FLAG: where FLAG = False evaluates to True."""
        t = trees.code("""\
FLAG = False

if not FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always True" in findings[0].message

    def test_while_loop(self, trees):
        """while FLAG: where FLAG = False is always False."""
        t = trees.code("""\
ENABLED = False

while ENABLED:
    process()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "`while`" in findings[0].message

    def test_ternary(self, trees):
        """Ternary (IfExp) with constant condition."""
        t = trees.code("""\
DEBUG = False

x = "debug" if DEBUG else "prod"
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "`ternary`" in findings[0].message

    def test_equality_string(self, trees):
        """if MODE == 'v2': where MODE = 'v1' is always False."""
        t = trees.code("""\
MODE = "v1"

if MODE == "v2":
    use_v2()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always False" in findings[0].message

    def test_inequality_int(self, trees):
        """if LEVEL != 0: where LEVEL = 0 is always False."""
        t = trees.code("""\
LEVEL = 0

if LEVEL != 0:
    log_level()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always False" in findings[0].message

    def test_reversed_comparison(self, trees):
        """Literal on the left: 'v2' == MODE."""
        t = trees.code("""\
MODE = "v1"

if "v2" == MODE:
    use_v2()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always False" in findings[0].message

    def test_cross_file(self, trees):
        """Constant in one file, conditional in another."""
        t = trees.files(
            {
                "config.py": "ENABLE_V2 = False\n",
                "main.py": """\
from config import ENABLE_V2

if ENABLE_V2:
    use_v2()
""",
            }
        )
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert findings[0].file == "config.py"

    def test_multiple_conditionals(self, trees):
        """Multiple conditionals from one constant get aggregate message."""
        t = trees.code("""\
FLAG = False

if FLAG:
    a()
if FLAG:
    b()
if FLAG:
    c()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "controls 3 conditionals" in findings[0].message
        assert "always False" in findings[0].message

    def test_none_constant_truthiness(self, trees):
        """None constant in truthiness test is always False."""
        t = trees.code("""\
HANDLER = None

if HANDLER:
    HANDLER.process()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1
        assert "always False" in findings[0].message

    # --- negative cases ---

    def test_reassigned_same_file(self, trees):
        """Constant reassigned later — not fossilized."""
        t = trees.code("""\
FLAG = False
FLAG = True

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_augmented_assignment(self, trees):
        """Augmented assignment means it's mutable."""
        t = trees.code("""\
COUNT = 0
COUNT += 1

if COUNT:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_lowercase_ignored(self, trees):
        """Lowercase names are not treated as constants."""
        t = trees.code("""\
flag = False

if flag:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_private_constant_ignored(self, trees):
        """_PRIVATE constants are skipped."""
        t = trees.code("""\
_FLAG = False

if _FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_non_literal_value_ignored(self, trees):
        """Non-literal values (function calls) are not constants."""
        t = trees.code("""\
FLAG = os.environ.get("FLAG", False)

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_locally_shadowed_in_function(self, trees):
        """Function that assigns to the name locally shadows it."""
        t = trees.code("""\
FLAG = False

def process():
    FLAG = compute_flag()
    if FLAG:
        do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_used_but_not_in_conditional(self, trees):
        """Constant used in non-conditional context — no finding."""
        t = trees.code("""\
MAX_RETRIES = 3

for i in range(MAX_RETRIES):
    try_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_compound_boolean_skipped(self, trees):
        """Compound expressions (if FLAG and other) are skipped."""
        t = trees.code("""\
FLAG = False

if FLAG and other_condition():
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_class_body_not_reassignment(self, trees):
        """Assignment in class body is not a reassignment of module constant."""
        t = trees.code("""\
FLAG = False

class Config:
    FLAG = True

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 1

    def test_global_reassignment_excluded(self, trees):
        """global NAME + assignment inside function means it's mutable."""
        t = trees.code("""\
FLAG = False

def toggle():
    global FLAG
    FLAG = True

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0

    def test_del_constant_excluded(self, trees):
        """del of the constant means it's mutable."""
        t = trees.code("""\
FLAG = False

del FLAG

if FLAG:
    do_something()
""")
        findings = check_fossilized_toggles(t)
        assert len(findings) == 0


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


class TestUnreachableAfterReturn:
    def test_code_after_return(self, trees):
        t = trees.code("""\
def foo():
    return 42
    x = 1
    y = 2
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 1
        assert "2 statement(s)" in findings[0].message
        assert "after return" in findings[0].message
        assert findings[0].severity.value == "high"

    def test_code_after_raise(self, trees):
        t = trees.code("""\
def foo():
    raise ValueError("bad")
    cleanup()
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 1
        assert "after raise" in findings[0].message

    def test_code_after_exhaustive_if_else(self, trees):
        t = trees.code("""\
def calculate(x):
    if x > 0:
        return x
    else:
        return -x
    # Was the main path before refactoring
    log(x)
    notify(x)
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 1
        assert "exhaustive if/else" in findings[0].message

    def test_code_after_if_elif_else_all_returning(self, trees):
        t = trees.code("""\
def classify(x):
    if x == "a":
        return 1
    elif x == "b":
        return 2
    else:
        return 3
    dead_code()
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 1

    def test_no_finding_if_without_else(self, trees):
        t = trees.code("""\
def foo(x):
    if x:
        return 1
    do_other_things()
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 0

    def test_no_finding_if_only_one_branch_returns(self, trees):
        t = trees.code("""\
def foo(x):
    if x > 0:
        return x
    else:
        log(x)
    process(x)
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 0

    def test_no_finding_return_is_last_statement(self, trees):
        t = trees.code("""\
def foo():
    x = compute()
    return x
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 0

    def test_no_finding_empty_function(self, trees):
        t = trees.code("""\
def foo():
    pass
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 0

    def test_nested_if_else_both_raise(self, trees):
        t = trees.code("""\
def validate(x):
    if x is None:
        raise ValueError("missing")
    else:
        if x < 0:
            raise ValueError("negative")
        else:
            raise ValueError("unknown")
    # Dead
    return x
""")
        findings = check_unreachable_after_return(t)
        assert len(findings) == 1


class TestIsinstanceChain:
    def test_finds_chain_of_five(self, trees):
        t = trees.code("""\
def process(x):
    if isinstance(x, int):
        return x + 1
    elif isinstance(x, str):
        return len(x)
    elif isinstance(x, list):
        return sum(x)
    elif isinstance(x, dict):
        return len(x)
    elif isinstance(x, tuple):
        return x[0]
    return None
""")
        findings = check_isinstance_chain(t)
        assert len(findings) == 1
        assert "5 isinstance()" in findings[0].message
        assert "process()" in findings[0].message

    def test_no_finding_four_checks(self, trees):
        t = trees.code("""\
def process(x):
    if isinstance(x, int):
        return x
    elif isinstance(x, str):
        return x
    elif isinstance(x, list):
        return x
    elif isinstance(x, dict):
        return x
    return None
""")
        findings = check_isinstance_chain(t)
        assert len(findings) == 0

    def test_separate_functions_counted_independently(self, trees):
        t = trees.code("""\
def foo(x):
    if isinstance(x, int): pass
    if isinstance(x, str): pass
    if isinstance(x, list): pass

def bar(x):
    if isinstance(x, int): pass
    if isinstance(x, str): pass
    if isinstance(x, list): pass
""")
        findings = check_isinstance_chain(t)
        assert len(findings) == 0

    def test_finds_high_count(self, trees):
        t = trees.code("""\
def flexible_add(a, b):
    if isinstance(a, int) and isinstance(b, int):
        return a + b
    if isinstance(a, str) and isinstance(b, str):
        return a + b
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    if isinstance(a, dict):
        return a
    if isinstance(b, dict):
        return b
    return None
""")
        findings = check_isinstance_chain(t)
        assert len(findings) == 1
        assert "8 isinstance()" in findings[0].message


class TestBooleanParamExplosion:
    def test_finds_four_booleans(self, trees):
        t = trees.code("""\
def execute(task, data, use_hooks=True, use_retry=False,
            validate_first=True, track_metrics=False):
    pass
""")
        findings = check_boolean_param_explosion(t)
        assert len(findings) == 1
        assert "4 boolean parameters" in findings[0].message
        assert "use_hooks" in findings[0].message

    def test_no_finding_three_booleans(self, trees):
        t = trees.code("""\
def execute(task, data, use_hooks=True, use_retry=False, validate=True):
    pass
""")
        findings = check_boolean_param_explosion(t)
        assert len(findings) == 0

    def test_counts_kwonly_booleans(self, trees):
        t = trees.code("""\
def execute(task, *, use_hooks=True, use_retry=False,
            validate=True, track=False):
    pass
""")
        findings = check_boolean_param_explosion(t)
        assert len(findings) == 1

    def test_ignores_non_boolean_defaults(self, trees):
        t = trees.code("""\
def execute(task, timeout=30, name="default", retries=3, verbose=True):
    pass
""")
        findings = check_boolean_param_explosion(t)
        assert len(findings) == 0

    def test_mixed_positional_and_kwonly(self, trees):
        t = trees.code("""\
def execute(task, use_hooks=True, use_retry=False, *,
            validate=True, track=False):
    pass
""")
        findings = check_boolean_param_explosion(t)
        assert len(findings) == 1
        assert "4 boolean parameters" in findings[0].message


class TestExceptionFlowControl:
    def test_finds_found_it_pattern(self, trees):
        t = trees.code("""\
class FoundIt(Exception):
    pass

def search(data, target):
    try:
        for item in data:
            if item == target:
                raise FoundIt()
    except FoundIt:
        return True
    return False
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 1
        assert "FoundIt" in findings[0].message
        assert "search()" in findings[0].message

    def test_finds_skip_item_pattern(self, trees):
        t = trees.code("""\
class SkipItem(Exception):
    pass

def process(items):
    for item in items:
        try:
            if bad(item):
                raise SkipItem()
            do_work(item)
        except SkipItem:
            continue
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 1

    def test_finds_exception_defined_inside_function(self, trees):
        t = trees.code("""\
def search(data, target):
    class FoundIt(Exception):
        pass
    try:
        for item in data:
            if item == target:
                raise FoundIt()
    except FoundIt:
        return True
    return False
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 1

    def test_no_finding_external_exception(self, trees):
        """Exception not defined in same file — could be from called function."""
        t = trees.code("""\
def process(data):
    try:
        result = transform(data)
    except ValueError:
        return None
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 0

    def test_no_finding_different_try_except(self, trees):
        """Exception raised in one try, caught in another — not flow control."""
        t = trees.code("""\
class AppError(Exception):
    pass

def outer():
    try:
        inner()
    except AppError:
        handle()

def inner():
    raise AppError()
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 0

    def test_no_finding_only_caught_not_raised(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

def handler():
    try:
        external_call()
    except AppError:
        log_error()
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 0

    def test_no_finding_stdlib_exception(self, trees):
        """Don't flag stdlib exceptions like ValueError — they come from external calls."""
        t = trees.code("""\
def parse(text):
    try:
        return int(text)
    except ValueError:
        return None
""")
        findings = check_exception_flow_control(t)
        assert len(findings) == 0


class TestArrowCode:
    def test_finds_deep_nesting(self, trees):
        t = trees.code("""\
def process(data):
    for item in data:
        if item.valid:
            for sub in item.children:
                if sub.active:
                    with open(sub.path) as f:
                        pass
""")
        findings = check_arrow_code(t)
        assert len(findings) == 1
        assert "nesting depth" in findings[0].message
        assert "process()" in findings[0].message

    def test_ignores_shallow_nesting(self, trees):
        t = trees.code("""\
def process(data):
    for item in data:
        if item.valid:
            do_thing(item)
""")
        findings = check_arrow_code(t)
        assert len(findings) == 0

    def test_class_method_starts_fresh(self, trees):
        """Methods inside deeply nested classes don't inherit class nesting."""
        t = trees.code("""\
class MyClass:
    def process(self, data):
        for item in data:
            if item.valid:
                for sub in item.children:
                    if sub.active:
                        with open(sub.path) as f:
                            pass
""")
        findings = check_arrow_code(t)
        assert len(findings) == 1
        assert "process()" in findings[0].message

    def test_mixed_nesting_types(self, trees):
        t = trees.code("""\
def process(data):
    if data:
        for x in data:
            while x > 0:
                with open("f") as f:
                    try:
                        pass
                    except Exception:
                        pass
""")
        findings = check_arrow_code(t)
        assert len(findings) == 1

    def test_nested_function_counted_separately(self, trees):
        """Nested functions start fresh — parent depth doesn't carry."""
        t = trees.code("""\
def outer():
    if True:
        if True:
            if True:
                def inner():
                    if True:
                        if True:
                            if True:
                                if True:
                                    if True:
                                        pass
""")
        findings = check_arrow_code(t)
        # inner has depth 5+, outer has depth 3
        assert len(findings) == 1
        assert "inner()" in findings[0].message

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_deep.py": """\
def test_something():
    for item in data:
        if item.valid:
            for sub in item.children:
                if sub.active:
                    with open(sub.path) as f:
                        pass
"""})
        findings = check_arrow_code(t)
        assert len(findings) == 0

    def test_try_except_counts_once(self, trees):
        """try/except together add only one level of nesting, not two."""
        t = trees.code("""\
def process():
    if True:
        for x in data:
            try:
                if x:
                    with open("f") as f:
                        pass
            except Exception:
                pass
""")
        findings = check_arrow_code(t)
        assert len(findings) == 1

    def test_reports_correct_depth(self, trees):
        t = trees.code("""\
def deep():
    if True:
        if True:
            if True:
                if True:
                    if True:
                        if True:
                            pass
""")
        findings = check_arrow_code(t)
        assert len(findings) == 1
        assert "depth 6" in findings[0].message


class TestHungarianNotation:
    def test_finds_assignment(self, trees):
        t = trees.code("""\
strName = "hello"
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 1
        assert "strName" in findings[0].message
        assert "str_name" in findings[0].message

    def test_finds_parameter(self, trees):
        t = trees.code("""\
def process(intCount, lstItems):
    pass
""")
        findings = check_hungarian_notation(t)
        assert len(findings) >= 1
        messages = " ".join(f.message for f in findings)
        assert "intCount" in messages or "lstItems" in messages

    def test_finds_for_target(self, trees):
        t = trees.code("""\
for objItem in items:
    pass
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 1
        assert "objItem" in findings[0].message

    def test_skips_upper_case(self, trees):
        t = trees.code("""\
STR_MAX = 100
INT_SIZE = 42
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 0

    def test_ignores_snake_case(self, trees):
        t = trees.code("""\
str_name = "hello"
int_count = 5
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 0

    def test_ignores_no_capital_after_prefix(self, trees):
        """'string' starts with 'str' but no capital letter follows."""
        t = trees.code("""\
string = "hello"
integer = 5
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_naming.py": """\
strName = "hello"
"""})
        findings = check_hungarian_notation(t)
        assert len(findings) == 0

    def test_finds_systems_hungarian(self, trees):
        """Systems Hungarian: szName, lpBuffer, dwFlags, fnCallback."""
        t = trees.code("""\
szName = "hello"
lpBuffer = bytearray(1024)
dwFlags = 0x01
fnCallback = lambda: None
""")
        findings = check_hungarian_notation(t)
        assert len(findings) == 4
        messages = " ".join(f.message for f in findings)
        assert "szName" in messages
        assert "lpBuffer" in messages
        assert "dwFlags" in messages
        assert "fnCallback" in messages

    def test_finds_systems_hungarian_params(self, trees):
        t = trees.code("""\
def process(piTestFunc, cbBytes):
    pass
""")
        findings = check_hungarian_notation(t)
        assert len(findings) >= 1
        messages = " ".join(f.message for f in findings)
        assert "piTestFunc" in messages or "cbBytes" in messages


class TestInconsistentReturns:
    def test_finds_mixed_returns(self, trees):
        t = trees.code("""\
def parse_value(text):
    if text == "none":
        return None
    if text.isdigit():
        return int(text)
    if text.startswith("["):
        return list(text)
    return text
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 1
        assert "parse_value()" in findings[0].message
        assert "distinct types" in findings[0].message

    def test_ignores_consistent_returns(self, trees):
        t = trees.code("""\
def get_name(obj):
    if obj.name:
        return obj.name
    return "default"
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_ignores_two_types(self, trees):
        t = trees.code("""\
def maybe_int(text):
    if text.isdigit():
        return int(text)
    return None
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_ignores_few_returns(self, trees):
        t = trees.code("""\
def process(x):
    if x:
        return 42
    return "hello"
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_ignores_test_function(self, trees):
        t = trees.code("""\
def test_parse():
    if True:
        return None
    if True:
        return 42
    return "hello"
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_ignores_short_private_function(self, trees):
        t = trees.code("""\
def _helper(x):
    if x == 1: return 1
    if x == 2: return "two"
    return None
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_detects_call_return_types(self, trees):
        t = trees.code("""\
def convert(data, fmt):
    if fmt == "json":
        return json_encode(data)
    if fmt == "xml":
        return xml_encode(data)
    if fmt == "csv":
        return csv_encode(data)
    return None
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 1

    def test_ignores_overloaded(self, trees):
        t = trees.code("""\
from typing import overload

@overload
def parse(x: str) -> str: ...
@overload
def parse(x: int) -> int: ...
def parse(x):
    if isinstance(x, str):
        return str(x)
    return int(x)
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0

    def test_ignores_wraps_decorator(self, trees):
        """Decorators/middleware legitimately return different types."""
        t = trees.code("""\
from functools import wraps

def require_login(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user:
            return redirect("/login")
        if request.user.banned:
            return HttpResponseForbidden()
        return view_func(request, *args, **kwargs)
    return wrapper
""")
        findings = check_inconsistent_returns(t)
        assert len(findings) == 0


class TestPlaintextPasswords:
    def test_finds_equality(self, trees):
        t = trees.code("""\
def check(password, expected):
    if password == expected:
        return True
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1
        assert "password" in findings[0].message
        assert "==" in findings[0].message
        assert findings[0].severity.value == "high"

    def test_finds_inequality(self, trees):
        t = trees.code("""\
def check(user_token, stored):
    if user_token != stored:
        return False
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1
        assert "!=" in findings[0].message

    def test_finds_attribute_comparison(self, trees):
        t = trees.code("""\
if request.api_key == stored_key:
    pass
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1
        assert "api_key" in findings[0].message

    def test_ignores_truthiness(self, trees):
        """if SECRET_KEY: checks config presence, not comparing secrets."""
        t = trees.code("""\
def check(password):
    if password:
        do_something()
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0

    def test_ignores_assignment(self, trees):
        """Assignment to password variable is not a comparison."""
        t = trees.code("""\
password = get_input()
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0

    def test_ignores_is_none(self, trees):
        """`is None` / `is not None` uses ast.Is, not ast.Eq."""
        t = trees.code("""\
if password is None:
    pass
if password is not None:
    pass
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_auth.py": """\
def test_check():
    if password == "expected":
        pass
"""})
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0

    def test_ignores_non_password_names(self, trees):
        t = trees.code("""\
if username == "admin":
    pass
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0


class TestGetattrStrings:
    def test_finds_getattr_without_default(self, trees):
        t = trees.code("""\
x = getattr(obj, 'name')
""")
        findings = check_getattr_strings(t)
        assert any("getattr" in f.message and "'name'" in f.message for f in findings)

    def test_ignores_getattr_with_default(self, trees):
        t = trees.code("""\
x = getattr(obj, 'name', None)
""")
        findings = check_getattr_strings(t)
        # No individual finding (but might have cross-file)
        assert not any("without default" in f.message for f in findings)

    def test_finds_hasattr(self, trees):
        t = trees.code("""\
if hasattr(obj, 'read'):
    pass
""")
        findings = check_getattr_strings(t)
        assert any("hasattr" in f.message for f in findings)

    def test_ignores_hasattr_self(self, trees):
        """hasattr(self, ...) is legitimate introspection (e.g. Django reverse relations)."""
        t = trees.code("""\
class MyModel:
    def has_profile(self):
        return hasattr(self, 'profile')
""")
        findings = check_getattr_strings(t)
        assert not any("hasattr" in f.message for f in findings)

    def test_ignores_variable_string(self, trees):
        t = trees.code("""\
x = getattr(obj, attr_name)
""")
        findings = check_getattr_strings(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_getattr.py": """\
x = getattr(obj, 'name')
"""})
        findings = check_getattr_strings(t)
        # Individual findings skipped for test files
        assert not any("without default" in f.message for f in findings)

    def test_cross_file_shotgun_surgery(self, trees):
        t = trees.files(
            {
                "a.py": "x = getattr(obj, 'read')",
                "b.py": "if hasattr(obj, 'read'): pass",
                "c.py": "x = getattr(obj, 'read')",
            }
        )
        findings = check_getattr_strings(t)
        assert any("shotgun surgery" in f.message for f in findings)

    def test_few_occurrences_no_cross_file(self, trees):
        t = trees.files(
            {
                "a.py": "x = getattr(obj, 'read')",
                "b.py": "x = getattr(obj, 'read')",
            }
        )
        findings = check_getattr_strings(t)
        assert not any("shotgun surgery" in f.message for f in findings)


class TestLateBindingClosures:
    def test_finds_lambda_in_for_loop(self, trees):
        t = trees.code("""\
def make_multipliers():
    multipliers = []
    for i in range(5):
        multipliers.append(lambda x: x * i)
    return multipliers
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 1
        assert "lambda" in findings[0].message
        assert "i" in findings[0].message
        assert findings[0].severity.value == "high"

    def test_finds_closure_in_for_loop(self, trees):
        t = trees.code("""\
def register_handlers():
    handlers = {}
    for name in ["save", "load", "delete"]:
        def handler():
            print(f"Clicked {name}")
        handlers[name] = handler
    return handlers
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 1
        assert "handler()" in findings[0].message
        assert "name" in findings[0].message

    def test_ignores_default_arg_capture(self, trees):
        """lambda x, i=i: x * i captures correctly via default."""
        t = trees.code("""\
def make_multipliers():
    multipliers = []
    for i in range(5):
        multipliers.append(lambda x, i=i: x * i)
    return multipliers
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 0

    def test_ignores_no_loop_var_reference(self, trees):
        t = trees.code("""\
x = 10
for i in range(5):
    f = lambda: x
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 0

    def test_finds_list_comprehension_lambda(self, trees):
        """List comprehension with lambda referencing outer loop var."""
        t = trees.code("""\
def make_adders():
    result = []
    for n in range(10):
        result.append(lambda x: x + n)
    return result
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 1

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_closures.py": """\
for i in range(5):
    f = lambda x: x * i
"""})
        findings = check_late_binding_closures(t)
        assert len(findings) == 0

    def test_finds_tuple_unpacking_loop(self, trees):
        t = trees.code("""\
for name, func in [("a", fa), ("b", fb)]:
    def logged():
        print(f"Calling {name}")
        return func()
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 1
        messages = findings[0].message
        assert "name" in messages or "func" in messages

    def test_finds_nested_loop_capture(self, trees):
        t = trees.code("""\
def make_grid():
    callbacks = []
    for i in range(3):
        for j in range(3):
            callbacks.append(lambda: (i, j))
    return callbacks
""")
        findings = check_late_binding_closures(t)
        assert len(findings) >= 1


class TestLawOfDemeter:
    def test_finds_deep_chain(self, trees):
        t = trees.code("""\
def get_city(order):
    return order.user.address.city
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 1
        assert "order.user.address.city" in findings[0].message
        assert "depth 4" in findings[0].message

    def test_ignores_short_chain(self, trees):
        t = trees.code("""\
def get_name(user):
    return user.profile.name
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 0

    def test_ignores_fluent_api(self, trees):
        """Method calls in chain = fluent API, not demeter violation."""
        t = trees.code("""\
qs = queryset.filter(active=True).exclude(banned=True).order_by("name")
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 0

    def test_ignores_stdlib_modules(self, trees):
        t = trees.code("""\
import os
x = os.path.sep.join(parts)
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files({"tests/test_deep.py": """\
x = order.user.address.city
"""})
        findings = check_law_of_demeter(t)
        assert len(findings) == 0

    def test_finds_deeper_chain(self, trees):
        t = trees.code("""\
x = self.service.repository.model.field.value
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 1
        assert "depth 6" in findings[0].message

    def test_deduplicates_per_line(self, trees):
        """Multiple chains on same line: only deepest reported."""
        t = trees.code("""\
x = a.b.c.d + a.b.c.d.e.f
""")
        findings = check_law_of_demeter(t)
        # Should report the deeper one
        assert len(findings) == 1
        assert "depth 6" in findings[0].message

    def test_self_chain_counted(self, trees):
        """self.config.db.host is depth 4 — self counts as root."""
        t = trees.code("""\
class App:
    def connect(self):
        return connect(self.config.db.host)
""")
        findings = check_law_of_demeter(t)
        assert len(findings) == 1
        assert "self.config.db.host" in findings[0].message
