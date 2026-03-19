"""Tests for caller-aware checks (cross-file call-graph analysis)."""

from pysmelly.checks.callers import (
    check_constant_args,
    check_dead_code,
    check_internal_only,
    check_single_call_site,
    check_unused_defaults,
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
        findings = check_unused_defaults(t, verbose=False)
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
        findings = check_unused_defaults(t, verbose=False)
        assert len(findings) == 1
        assert "extra_args" in findings[0].message

    def test_ignores_when_default_is_used(self, trees):
        t = trees.code("""\
def greet(name, title=None):
    pass

greet("Alice", title="Dr")
greet("Bob")
""")
        findings = check_unused_defaults(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_no_callers(self, trees):
        t = trees.code("""\
def greet(name, title=None):
    pass
""")
        findings = check_unused_defaults(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _internal(x, y=None):
    pass

_internal(1, y=2)
_internal(3, y=4)
""")
        findings = check_unused_defaults(t, verbose=False)
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
        findings = check_unused_defaults(t, verbose=False)
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
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 1
        assert "unused()" in findings[0].message

    def test_ignores_called_function(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_private_functions(self, trees):
        t = trees.code("""\
def _private():
    pass
""")
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_decorated_functions(self, trees):
        t = trees.code("""\
@app.route("/")
def index():
    pass
""")
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_dict_value_references(self, trees):
        t = trees.code("""\
def handler_a():
    pass

HANDLERS = {"a": handler_a}
""")
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_main_entry_point(self, trees):
        t = trees.code("""\
def main():
    pass

if __name__ == "__main__":
    main()
""")
        findings = check_dead_code(t, verbose=False)
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
        findings = check_dead_code(t, verbose=False)
        assert len(findings) == 0


class TestSingleCallSite:
    def test_finds_single_caller(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_single_call_site(t, verbose=False)
        assert len(findings) == 1
        assert "exactly 1 call site" in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_includes_param_count(self, trees):
        t = trees.code("""\
def format_row(name, age, role):
    pass

format_row("Alice", 30, "admin")
""")
        findings = check_single_call_site(t, verbose=False)
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
        findings = check_single_call_site(t, verbose=False)
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
        findings = check_single_call_site(t, verbose=False)
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
        findings = check_single_call_site(t, verbose=False)
        assert len(findings) == 1
        assert "all args from" not in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_ignores_multiple_callers(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
helper()
""")
        findings = check_single_call_site(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_zero_callers(self, trees):
        """Dead code is reported by dead-code, not single-call-site."""
        t = trees.code("""\
def helper():
    pass
""")
        findings = check_single_call_site(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_main_entry_point(self, trees):
        """Functions called from __main__ guard are entry points, not inline candidates."""
        t = trees.code("""\
def main():
    pass

if __name__ == "__main__":
    main()
""")
        findings = check_single_call_site(t, verbose=False)
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
        findings = check_single_call_site(t, verbose=False)
        assert len(findings) == 0


class TestInternalOnly:
    def test_finds_internal_only_function(self, trees):
        t = trees.code("""\
def helper():
    pass

helper()
helper()
""")
        findings = check_internal_only(t, verbose=False)
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
        findings = check_internal_only(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_single_internal_call(self, trees):
        """Need 2+ internal calls to suggest making private."""
        t = trees.code("""\
def helper():
    pass

helper()
""")
        findings = check_internal_only(t, verbose=False)
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
        findings = check_constant_args(t, verbose=False)
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
        findings = check_constant_args(t, verbose=False)
        assert len(findings) == 0

    def test_finds_one_constant_one_varying(self, trees):
        t = trees.code("""\
def send(msg, channel):
    pass

send("hello", "general")
send("world", "general")
""")
        findings = check_constant_args(t, verbose=False)
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
        findings = check_constant_args(t, verbose=False)
        assert len(findings) == 0

    def test_works_with_keyword_args(self, trees):
        t = trees.code("""\
def fetch(url, timeout):
    pass

fetch("http://a.com", timeout=30)
fetch("http://b.com", timeout=30)
""")
        findings = check_constant_args(t, verbose=False)
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
        findings = check_constant_args(t, verbose=False)
        assert len(findings) == 0
