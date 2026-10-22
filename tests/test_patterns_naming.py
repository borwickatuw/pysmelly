"""Tests for pattern naming checks (hungarian-notation, getattr-strings, plaintext-passwords, late-binding-closures)."""

import ast

from pysmelly.checks.patterns_naming import (
    _secret_operand_name,
    check_getattr_strings,
    check_hungarian_notation,
    check_late_binding_closures,
    check_numbered_variables,
    check_plaintext_passwords,
)


def _first_expr(src: str) -> ast.expr:
    """The expression of the first statement in ``src``."""
    stmt = ast.parse(src).body[0]
    assert isinstance(stmt, ast.Expr)
    return stmt.value


class TestNumberedVariables:
    def test_finds_module_series(self, trees):
        t = trees.code("""\
item1 = "apple"
item2 = "banana"
item3 = "cherry"
item4 = "date"
item5 = "elderberry"
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 1
        assert "item1..item5" in findings[0].message
        assert "module scope" in findings[0].message

    def test_finds_function_series(self, trees):
        t = trees.code("""\
def build():
    row1 = get(1)
    row2 = get(2)
    row3 = get(3)
    row4 = get(4)
    return [row1, row2, row3, row4]
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 1
        assert "build()" in findings[0].message

    def test_three_not_enough(self, trees):
        t = trees.code("""\
item1 = "a"
item2 = "b"
item3 = "c"
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 0

    def test_non_contiguous_not_flagged(self, trees):
        """Scattered years (account_2024...) aren't a hand-rolled list."""
        t = trees.code("""\
account2019 = "a"
account2020 = "b"
account2024 = "c"
account2030 = "d"
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 0

    def test_series_not_starting_at_zero_or_one(self, trees):
        t = trees.code("""\
http500 = "a"
http501 = "b"
http502 = "c"
http503 = "d"
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 0

    def test_separate_stems_counted_separately(self, trees):
        t = trees.code("""\
item1 = "a"
item2 = "b"
item3 = "c"
item4 = "d"
price1 = 1.0
price2 = 2.0
price3 = 3.0
price4 = 4.0
""")
        findings = check_numbered_variables(t)
        assert len(findings) == 2
        stems = sorted(f.message.split("..")[0].split()[-1] for f in findings)
        assert stems == ["item1", "price1"]


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

    def test_ignores_compare_digest(self, trees):
        """The recommended fix must not itself be flagged.

        ``hmac.compare_digest(password, user_input)`` is a Call, not a
        ==/!= comparison, so there is no operand with a name to judge.
        """
        t = trees.code("""\
import hmac

def check(password, user_input):
    if hmac.compare_digest(password, user_input):
        return True
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 0

    def test_flags_comparison_against_empty_string(self, trees):
        """Pins today's behaviour: a presence test spelled as != is flagged."""
        t = trees.code("""\
def check(token):
    if token != "":
        return True
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1
        assert "token" in findings[0].message
        assert "!=" in findings[0].message

    def test_chained_comparison_reports_once(self, trees):
        """A Compare node with several operators yields at most one finding."""
        t = trees.code("""\
def check(password, a, b):
    if password == a == b:
        return True
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1

    def test_secret_on_right_hand_side(self, trees):
        """Every operand is considered, not only the left one."""
        t = trees.code("""\
def check(submitted, stored_secret):
    if submitted == stored_secret:
        return True
""")
        findings = check_plaintext_passwords(t)
        assert len(findings) == 1
        assert "stored_secret" in findings[0].message

    def test_finds_wider_vocabulary(self, trees):
        """The shared vocabulary reaches names the old six-name set missed."""
        t = trees.code("""\
def check(pwd, cvv, record, expected):
    if pwd == expected:
        return True
    if cvv != expected:
        return False
    if record.ssn == expected:
        return True
""")
        findings = check_plaintext_passwords(t)
        assert {f.line for f in findings} == {2, 4, 6}
        assert sorted(f.message.split()[0] for f in findings) == ["cvv", "pwd", "ssn"]

    def test_ignores_metadata_suffixes(self, trees):
        """Comparing a secret's id or name compares metadata, not a secret."""
        t = trees.code("""\
def check(token_id, api_key_name, expected):
    if token_id == expected:
        return True
    if api_key_name == expected:
        return True
""")
        findings = check_plaintext_passwords(t)
        assert findings == []


class TestSecretOperandName:
    """The secret-name predicate extracted out of check_plaintext_passwords."""

    def test_bare_name(self):
        assert _secret_operand_name(_first_expr("user_password")) == "user_password"

    def test_attribute_uses_attr_not_receiver(self):
        assert _secret_operand_name(_first_expr("request.api_key")) == "api_key"
        assert _secret_operand_name(_first_expr("token.strip")) is None

    def test_case_insensitive(self):
        assert _secret_operand_name(_first_expr("PASSWORD")) == "PASSWORD"

    def test_non_secret_name(self):
        assert _secret_operand_name(_first_expr("username")) is None

    def test_calls_and_literals_have_no_name(self):
        assert _secret_operand_name(_first_expr("get_password()")) is None
        assert _secret_operand_name(_first_expr("'password'")) is None
        assert _secret_operand_name(_first_expr("creds['password']")) is None

    def test_metadata_suffix_is_exempt(self):
        """One vocabulary, shared with secrets-in-logs (plan 15-4).

        Comparing a secret's id, name or scope is a comparison of metadata,
        not of a secret. Was the opposite before the two predicates merged.
        """
        assert _secret_operand_name(_first_expr("token_id")) is None
        assert _secret_operand_name(_first_expr("request.api_key_name")) is None
        assert _secret_operand_name(_first_expr("token_scope")) is None

    def test_wider_vocabulary(self):
        """Names the old six-name vocabulary missed entirely."""
        for name in ("pwd", "passphrase", "cvv", "ssn", "credit_card", "creds"):
            assert _secret_operand_name(_first_expr(name)) == name

    def test_segment_match_not_substring(self):
        """max_tokens counts tokens; it is not a token."""
        assert _secret_operand_name(_first_expr("max_tokens")) is None
        assert _secret_operand_name(_first_expr("s3_key")) is None


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

    def test_lambda_and_def_in_one_loop_both_reported(self, trees):
        """A single pass over the loop body reports both closure kinds."""
        t = trees.code("""\
def build():
    out = []
    for i in range(3):
        out.append(lambda: i)
        def later():
            return i
        out.append(later)
    return out
""")
        findings = check_late_binding_closures(t)
        assert len(findings) == 2
        messages = sorted(f.message for f in findings)
        assert "lambda captures" in messages[0]
        assert messages[1].startswith("later()")

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
