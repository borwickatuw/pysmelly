"""Tests for pattern misc checks (env-fallbacks, monkey-patch, booleans, arrow-code, and more)."""

from pysmelly.checks.patterns_misc import (
    check_arrow_code,
    check_boolean_param_explosion,
    check_env_fallbacks,
    check_fossilized_toggles,
    check_inconsistent_returns,
    check_isinstance_chain,
    check_law_of_demeter,
    check_runtime_monkey_patch,
    check_temp_accumulators,
)


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
        t = trees.files(
            {
                "tests/test_deep.py": """\
def test_something():
    for item in data:
        if item.valid:
            for sub in item.children:
                if sub.active:
                    with open(sub.path) as f:
                        pass
"""
            }
        )
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
        t = trees.files(
            {
                "tests/test_deep.py": """\
x = order.user.address.city
"""
            }
        )
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
