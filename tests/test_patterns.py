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
    def test_finds_many_foo_foo_args(self, trees):
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 1
        assert "gathers 4 intermediate variables" in findings[0].message

    def test_ignores_below_threshold(self, trees):
        t = trees.code("""\
def build(name, age):
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
