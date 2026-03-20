"""Tests for import-related checks."""

from pysmelly.checks.imports import check_compat_shims


class TestCompatShims:
    def test_finds_import_error_shim(self, trees):
        t = trees.code("""\
try:
    import tomllib
except ImportError:
    import tomli as tomllib
""")
        findings = check_compat_shims(t)
        assert len(findings) == 1
        assert "compatibility shim" in findings[0].message
        assert "tomllib" in findings[0].message

    def test_finds_module_not_found_error(self, trees):
        t = trees.code("""\
try:
    from collections import OrderedDict
except ModuleNotFoundError:
    OrderedDict = dict
""")
        findings = check_compat_shims(t)
        assert len(findings) == 1

    def test_ignores_non_import_try(self, trees):
        t = trees.code("""\
try:
    result = compute()
except ImportError:
    result = None
""")
        findings = check_compat_shims(t)
        assert len(findings) == 0

    def test_ignores_other_exception_types(self, trees):
        t = trees.code("""\
try:
    import optional_dep
except ValueError:
    pass
""")
        findings = check_compat_shims(t)
        assert len(findings) == 0
