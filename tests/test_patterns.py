"""Tests for pattern-based checks."""

from pysmelly.checks.patterns import (
    check_constant_dispatch_dicts,
    check_foo_equals_foo,
    check_suspicious_fallbacks,
    check_temp_accumulators,
)


class TestFooEqualsFoo:
    def test_finds_many_foo_foo_args(self, trees):
        t = trees.code("""\
def build(name, age, email, role):
    return Thing(name=name, age=age, email=email, role=role)
""")
        findings = check_foo_equals_foo(t, verbose=False)
        assert len(findings) == 1
        assert "4 foo=foo args" in findings[0].message

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
