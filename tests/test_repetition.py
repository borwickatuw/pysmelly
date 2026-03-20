"""Tests for cross-file repetition checks."""

from pysmelly.checks.repetition import (
    check_repeated_string_parsing,
    check_scattered_constants,
    check_scattered_isinstance,
    check_shotgun_surgery,
)
from pysmelly.registry import Severity


class TestScatteredConstants:
    def test_finds_string_in_3_files_assignments(self, trees):
        t = trees.files(
            {
                "a.py": 'status = "active"',
                "b.py": 'state = "active"',
                "c.py": 'default = "active"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        assert "'active'" in findings[0].message
        assert "3 files" in findings[0].message

    def test_finds_number_in_3_files_comparisons(self, trees):
        t = trees.files(
            {
                "a.py": "if x == 42: pass",
                "b.py": "if y == 42: pass",
                "c.py": "if z == 42: pass",
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        assert "42" in findings[0].message

    def test_ignores_value_in_only_2_files(self, trees):
        t = trees.files(
            {
                "a.py": 'status = "active"',
                "b.py": 'state = "active"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_trivial_values(self, trees):
        t = trees.files(
            {
                "a.py": "x = None\ny = True\nz = 0\nw = 1",
                "b.py": "x = None\ny = True\nz = 0\nw = 1",
                "c.py": "x = None\ny = True\nz = 0\nw = 1",
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_trivial_strings(self, trees):
        t = trees.files(
            {
                "a.py": 'enc = "utf-8"\nx = "a"',
                "b.py": 'enc = "utf-8"\nx = "a"',
                "c.py": 'enc = "utf-8"\nx = "a"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_http_status_codes(self, trees):
        t = trees.files(
            {
                "a.py": "if code == 200: pass\nif code == 404: pass",
                "b.py": "if code == 200: pass\nif code == 404: pass",
                "c.py": "if code == 200: pass\nif code == 404: pass",
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_migration_files(self, trees):
        t = trees.files(
            {
                "a.py": "max_length = 255",
                "app/migrations/0001_initial.py": "max_length = 255",
                "app/migrations/0002_update.py": "max_length = 255",
                "b.py": "max_length = 255",
            }
        )
        findings = check_scattered_constants(t)
        # Only a.py and b.py count — 2 files, below threshold
        assert len(findings) == 0

    def test_non_django_migrations_dir_not_skipped(self, trees):
        """A 'migrations' dir without numbered files is not skipped."""
        t = trees.files(
            {
                "a.py": 'x = "special"',
                "b.py": 'x = "special"',
                "migrations/helpers.py": 'x = "special"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1

    def test_ignores_common_round_numbers(self, trees):
        t = trees.files(
            {
                "a.py": "limit = 100",
                "b.py": "limit = 100",
                "c.py": "limit = 100",
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_dunder_main(self, trees):
        t = trees.files(
            {
                "a.py": 'if __name__ == "__main__": pass',
                "b.py": 'if __name__ == "__main__": pass',
                "c.py": 'if __name__ == "__main__": pass',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_argparse_actions(self, trees):
        t = trees.files(
            {
                "a.py": 'parser.add_argument("--verbose", action="store_true")',
                "b.py": 'parser.add_argument("--quiet", action="store_true")',
                "c.py": 'parser.add_argument("--debug", action="store_true")',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_two_char_strings(self, trees):
        """Strings of length <= 2 are too short to be interesting."""
        t = trees.files(
            {
                "a.py": 'x = "id"',
                "b.py": 'y = "id"',
                "c.py": 'z = "id"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_flags_three_char_strings(self, trees):
        """Strings of length 3+ are still flagged."""
        t = trees.files(
            {
                "a.py": 'x = "foo"',
                "b.py": 'y = "foo"',
                "c.py": 'z = "foo"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1

    def test_ignores_http_headers(self, trees):
        t = trees.files(
            {
                "a.py": 'h = d["Content-Type"]',
                "b.py": 'h = d["Content-Type"]',
                "c.py": 'h = d["Content-Type"]',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_media_types(self, trees):
        t = trees.files(
            {
                "a.py": 'ct = "application/json"',
                "b.py": 'ct = "application/json"',
                "c.py": 'ct = "application/json"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_dict_get_first_arg(self, trees):
        """config.get("key") — data-schema key, not a developer constant."""
        t = trees.files(
            {
                "a.py": 'x = config.get("infrastructure")',
                "b.py": 'y = data.get("infrastructure")',
                "c.py": 'z = settings.get("infrastructure")',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_dict_pop_first_arg(self, trees):
        t = trees.files(
            {
                "a.py": 'x = d.pop("item_id")',
                "b.py": 'y = d.pop("item_id")',
                "c.py": 'z = d.pop("item_id")',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_dict_setdefault_first_arg(self, trees):
        t = trees.files(
            {
                "a.py": 'd.setdefault("errors", [])',
                "b.py": 'd.setdefault("errors", [])',
                "c.py": 'd.setdefault("errors", [])',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_flags_non_dict_access_method_calls(self, trees):
        """Other method calls with string args are still flagged (if in interesting context)."""
        t = trees.files(
            {
                "a.py": 'x = process("special_value")',
                "b.py": 'y = handle("special_value")',
                "c.py": 'z = transform("special_value")',
            }
        )
        # Positional args to non-dict-access methods are not in "interesting context"
        # so they won't be flagged. This test documents the behavior.
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_test_files(self, trees):
        t = trees.files(
            {
                "a.py": 'status = "active"',
                "test_b.py": 'status = "active"',
                "tests/c.py": 'status = "active"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_docstrings(self, trees):
        t = trees.files(
            {
                "a.py": 'def foo():\n    """This is a docstring with magic value."""\n    pass',
                "b.py": 'def bar():\n    """This is a docstring with magic value."""\n    pass',
                "c.py": 'def baz():\n    """This is a docstring with magic value."""\n    pass',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_raise_messages(self, trees):
        t = trees.files(
            {
                "a.py": 'raise ValueError("something broke")',
                "b.py": 'raise RuntimeError("something broke")',
                "c.py": 'raise TypeError("something broke")',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_log_call_kwargs(self, trees):
        t = trees.files(
            {
                "a.py": 'logger.info("request", extra={"source": "api"})',
                "b.py": 'logger.warning("timeout", extra={"source": "api"})',
                "c.py": 'logger.error("failed", extra={"source": "api"})',
            }
        )
        # The keyword arg values in log calls should be ignored
        # but the positional args are also not in interesting context (Call args)
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_subscript_keys(self, trees):
        """Dict keys are API contracts, not scattered constants."""
        t = trees.files(
            {
                "a.py": 'x = d["config_key"]',
                "b.py": 'y = d["config_key"]',
                "c.py": 'z = d["config_key"]',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_includes_keyword_arg_values(self, trees):
        t = trees.files(
            {
                "a.py": "func(timeout=30)",
                "b.py": "other(timeout=30)",
                "c.py": "another(timeout=30)",
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        assert "30" in findings[0].message

    def test_mixed_contexts_count(self, trees):
        """Assignment in A, comparison in B, kwarg in C — all count."""
        t = trees.files(
            {
                "a.py": 'status = "pending"',
                "b.py": 'if x == "pending": pass',
                "c.py": 'func(status="pending")',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        assert "3 files" in findings[0].message

    def test_single_file_multiple_occurrences_not_flagged(self, trees):
        t = trees.files(
            {
                "a.py": 'x = "magic"\ny = "magic"\nz = "magic"',
                "b.py": 'a = "magic"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_ignores_dunder_all_entries(self, trees):
        t = trees.files(
            {
                "a.py": '__all__ = ["MyClass", "helper"]',
                "b.py": '__all__ = ["MyClass", "helper"]',
                "c.py": '__all__ = ["MyClass", "helper"]',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 0

    def test_message_format(self, trees):
        t = trees.files(
            {
                "a.py": 'x = "sentinel"',
                "b.py": 'y = "sentinel"',
                "c.py": 'z = "sentinel"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        msg = findings[0].message
        assert "Literal" in msg
        assert "consider a named constant" in msg

    def test_severity_is_low(self, trees):
        t = trees.files(
            {
                "a.py": 'x = "sentinel"',
                "b.py": 'y = "sentinel"',
                "c.py": 'z = "sentinel"',
            }
        )
        findings = check_scattered_constants(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.LOW


class TestScatteredIsinstance:
    def test_finds_isinstance_in_3_files(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if isinstance(y, MyModel): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1
        assert "MyModel" in findings[0].message
        assert "3 files" in findings[0].message

    def test_ignores_stdlib_types(self, trees):
        t = trees.files(
            {
                "a.py": "if isinstance(x, str): pass",
                "b.py": "if isinstance(x, int): pass\nif isinstance(x, dict): pass",
                "c.py": "if isinstance(x, list): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_ignores_types_not_in_codebase(self, trees):
        t = trees.files(
            {
                "a.py": "if isinstance(x, SomeExternal): pass",
                "b.py": "if isinstance(x, SomeExternal): pass",
                "c.py": "if isinstance(x, SomeExternal): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_ignores_checks_in_only_2_files(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if isinstance(y, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_ignores_test_files(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "test_b.py": "if isinstance(y, MyModel): pass",
                "tests/c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_includes_issubclass(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if issubclass(cls, MyModel): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1

    def test_tuple_isinstance_counted_per_element(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, (MyModel, str)): pass",
                "b.py": "if isinstance(y, (int, MyModel)): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1
        assert "MyModel" in findings[0].message

    def test_finding_anchored_at_class_definition(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if isinstance(y, MyModel): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1
        assert findings[0].file == "models.py"
        assert findings[0].line == 1

    def test_class_defined_in_multiple_files_skipped(self, trees):
        t = trees.files(
            {
                "a.py": "class Handler:\n    pass\nif isinstance(x, Handler): pass",
                "b.py": "class Handler:\n    pass\nif isinstance(x, Handler): pass",
                "c.py": "if isinstance(x, Handler): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_attribute_isinstance_counted(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, models.MyModel): pass",
                "b.py": "if isinstance(y, models.MyModel): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1

    def test_multiple_classes_flagged_independently(self, trees):
        t = trees.files(
            {
                "models.py": "class Foo:\n    pass\nclass Bar:\n    pass",
                "a.py": "isinstance(x, Foo)\nisinstance(x, Bar)",
                "b.py": "isinstance(x, Foo)\nisinstance(x, Bar)",
                "c.py": "isinstance(x, Foo)\nisinstance(x, Bar)",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 2
        names = {f.message.split("isinstance(x, ")[1].split(")")[0] for f in findings}
        assert names == {"Foo", "Bar"}

    def test_mixed_test_and_non_test_files(self, trees):
        """Only non-test files count toward the threshold."""
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if isinstance(y, MyModel): pass",
                "test_c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 0

    def test_severity_is_medium(self, trees):
        t = trees.files(
            {
                "models.py": "class MyModel:\n    pass",
                "a.py": "if isinstance(x, MyModel): pass",
                "b.py": "if isinstance(y, MyModel): pass",
                "c.py": "if isinstance(z, MyModel): pass",
            }
        )
        findings = check_scattered_isinstance(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM


class TestShotgunSurgery:
    def test_finds_4_file_access(self, trees):
        t = trees.files(
            {
                "a.py": "x = config.timeout",
                "b.py": "y = config.timeout",
                "c.py": "z = config.timeout",
                "d.py": "w = config.timeout",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 1
        assert "config.timeout" in findings[0].message
        assert "4 files" in findings[0].message

    def test_ignores_3_files(self, trees):
        t = trees.files(
            {
                "a.py": "x = config.timeout",
                "b.py": "y = config.timeout",
                "c.py": "z = config.timeout",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 0

    def test_ignores_common_attrs(self, trees):
        t = trees.files(
            {
                "a.py": "x = obj.name",
                "b.py": "y = obj.name",
                "c.py": "z = obj.name",
                "d.py": "w = obj.name",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 0

    def test_ignores_self(self, trees):
        t = trees.files(
            {
                "a.py": """\
class A:
    def f(self): return self.timeout
""",
                "b.py": """\
class B:
    def f(self): return self.timeout
""",
                "c.py": """\
class C:
    def f(self): return self.timeout
""",
                "d.py": """\
class D:
    def f(self): return self.timeout
""",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 0

    def test_ignores_private_attrs(self, trees):
        t = trees.files(
            {
                "a.py": "x = config._timeout",
                "b.py": "y = config._timeout",
                "c.py": "z = config._timeout",
                "d.py": "w = config._timeout",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "a.py": "x = config.timeout",
                "b.py": "y = config.timeout",
                "tests/test_c.py": "z = config.timeout",
                "tests/test_d.py": "w = config.timeout",
            }
        )
        findings = check_shotgun_surgery(t)
        # Only 2 non-test files, below threshold
        assert len(findings) == 0

    def test_different_var_names_not_grouped(self, trees):
        t = trees.files(
            {
                "a.py": "x = config.timeout",
                "b.py": "y = settings.timeout",
                "c.py": "z = opts.timeout",
                "d.py": "w = params.timeout",
            }
        )
        findings = check_shotgun_surgery(t)
        assert len(findings) == 0


class TestRepeatedStringParsing:
    def test_finds_same_split_index_in_3_locations(self, trees):
        t = trees.files(
            {
                "a.py": 'city = addr.split("|")[1]',
                "b.py": 'city = address.split("|")[1]',
                "c.py": 'city = loc.split("|")[1]',
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 1
        assert '.split("|")[1]' in findings[0].message
        assert "3 locations" in findings[0].message
        assert "dataclass" in findings[0].message

    def test_ignores_fewer_than_3(self, trees):
        t = trees.files(
            {
                "a.py": 'city = addr.split("|")[1]',
                "b.py": 'city = addr.split("|")[1]',
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 0

    def test_finds_intermediate_variable_pattern(self, trees):
        """parts = x.split(delim) then parts[N] is the real-world pattern."""
        t = trees.files(
            {
                "a.py": """\
def get_city(addr):
    parts = addr.split("|")
    return parts[1]
""",
                "b.py": """\
def get_state(addr):
    parts = addr.split("|")
    return parts[2]
""",
                "c.py": """\
def get_zip(addr):
    parts = addr.split("|")
    return parts[3]
""",
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) >= 1
        messages = " ".join(f.message for f in findings)
        assert '"|"' in messages

    def test_finds_multiple_indices_same_delimiter(self, trees):
        """3+ different indices with same delimiter = parsing a format."""
        t = trees.code("""\
street = addr.split("|")[0]
city = addr.split("|")[1]
state = addr.split("|")[2]
zip_code = addr.split("|")[3]
""")
        findings = check_repeated_string_parsing(t)
        assert len(findings) >= 1
        messages = " ".join(f.message for f in findings)
        assert "different indices" in messages or ".split" in messages

    def test_ignores_non_string_delimiter(self, trees):
        t = trees.files(
            {
                "a.py": "x = data.split(delim)[0]",
                "b.py": "x = data.split(delim)[0]",
                "c.py": "x = data.split(delim)[0]",
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "tests/test_parse.py": """\
x = data.split("|")[0]
y = data.split("|")[1]
z = data.split("|")[2]
""",
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 0

    def test_different_delimiters_not_grouped(self, trees):
        t = trees.files(
            {
                "a.py": 'x = data.split("|")[0]',
                "b.py": 'x = data.split(",")[0]',
                "c.py": 'x = data.split(":")[0]',
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 0

    def test_severity_is_medium(self, trees):
        t = trees.files(
            {
                "a.py": 'x = s.split("|")[0]',
                "b.py": 'x = s.split("|")[0]',
                "c.py": 'x = s.split("|")[0]',
            }
        )
        findings = check_repeated_string_parsing(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM
