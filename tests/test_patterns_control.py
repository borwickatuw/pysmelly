"""Tests for pattern control flow checks (suspicious-fallbacks, exception-flow-control, unreachable-after-return)."""

from pysmelly.checks.patterns_control import (
    check_exception_flow_control,
    check_suspicious_fallbacks,
    check_unreachable_after_return,
)


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
