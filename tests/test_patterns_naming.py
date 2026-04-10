"""Tests for pattern naming checks (hungarian-notation, getattr-strings, plaintext-passwords, late-binding-closures)."""

from pysmelly.checks.patterns_naming import (
    check_getattr_strings,
    check_hungarian_notation,
    check_late_binding_closures,
    check_plaintext_passwords,
)


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
        t = trees.files(
            {
                "tests/test_naming.py": """\
strName = "hello"
"""
            }
        )
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
        t = trees.files(
            {
                "tests/test_auth.py": """\
def test_check():
    if password == "expected":
        pass
"""
            }
        )
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
        t = trees.files(
            {
                "tests/test_getattr.py": """\
x = getattr(obj, 'name')
"""
            }
        )
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
        t = trees.files(
            {
                "tests/test_closures.py": """\
for i in range(5):
    f = lambda x: x * i
"""
            }
        )
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
