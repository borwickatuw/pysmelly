"""Tests for stdlib-alternatives check."""

from pysmelly.checks.recommendations import (
    _load_catalog,
    check_stdlib_alternatives,
)
from pysmelly.registry import Severity


class TestStdlibAlternatives:
    def test_flags_urllib_import(self, trees):
        t = trees.code("import urllib.request\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        assert len(findings) == 1
        assert "urllib.request" in findings[0].message

    def test_flags_from_import(self, trees):
        t = trees.code("from urllib.request import urlopen\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        assert len(findings) == 1
        assert "urllib.request" in findings[0].message

    def test_ignores_unrelated_stdlib(self, trees):
        t = trees.code("import json\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        assert len(findings) == 0

    def test_conditional_fires_when_both_present(self, trees):
        t = trees.files(
            {
                "a.py": "import os.path\n",
                "b.py": "import pathlib\n",
            }
        )
        findings = check_stdlib_alternatives(t, verbose=False)
        names = [f.check for f in findings]
        assert "stdlib-alternatives" in names
        matching = [f for f in findings if "os.path" in f.message]
        assert len(matching) == 1

    def test_conditional_skips_when_missing(self, trees):
        t = trees.code("import os.path\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        os_path_findings = [f for f in findings if "os.path" in f.message]
        assert len(os_path_findings) == 0

    def test_aggregates_across_files(self, trees):
        t = trees.files(
            {
                "a.py": "import urllib.request\n",
                "b.py": "import urllib.request\n",
                "c.py": "from urllib.request import urlopen\n",
            }
        )
        findings = check_stdlib_alternatives(t, verbose=False)
        urllib_findings = [f for f in findings if "urllib.request" in f.message]
        assert len(urllib_findings) == 1
        assert "3 files" in urllib_findings[0].message

    def test_severity_is_low(self, trees):
        t = trees.code("import urllib.request\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        for f in findings:
            assert f.severity == Severity.LOW

    def test_catalog_loads(self):
        catalog = _load_catalog()
        assert isinstance(catalog, list)
        assert len(catalog) > 0
        for entry in catalog:
            assert "name" in entry
            assert "imports" in entry
            assert "suggest" in entry
            assert "description" in entry

    def test_message_includes_suggestion(self, trees):
        t = trees.code("import urllib.request\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        assert len(findings) == 1
        assert "requests or httpx" in findings[0].message

    def test_argparse_simple_not_flagged(self, trees):
        """argparse with few arguments is fine — don't flag it."""
        t = trees.code("""\
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("filename")
parser.add_argument("--verbose", action="store_true")
""")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "argparse" in f.message]
        assert len(matching) == 0

    def test_argparse_complex_flagged(self, trees):
        """argparse with 5+ arguments suggests click/typer."""
        t = trees.code("""\
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("input")
parser.add_argument("output")
parser.add_argument("--verbose", action="store_true")
parser.add_argument("--format", choices=["json", "csv"])
parser.add_argument("--limit", type=int, default=100)
""")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "argparse" in f.message]
        assert len(matching) == 1
        assert "click or typer" in matching[0].message

    def test_argparse_subcommands_flagged(self, trees):
        """argparse with subcommands suggests click/typer."""
        t = trees.code("""\
import argparse
parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers()
subparsers.add_parser("init")
subparsers.add_parser("run")
""")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "argparse" in f.message]
        assert len(matching) == 1

    def test_argparse_mutually_exclusive_flagged(self, trees):
        """argparse with mutually exclusive groups suggests click/typer."""
        t = trees.code("""\
import argparse
parser = argparse.ArgumentParser()
group = parser.add_mutually_exclusive_group()
group.add_argument("--json", action="store_true")
group.add_argument("--csv", action="store_true")
""")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "argparse" in f.message]
        assert len(matching) == 1

    def test_flags_deprecated_stdlib(self, trees):
        t = trees.code("import cgi\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "cgi" in f.message]
        assert len(matching) == 1
        assert "removed" in matching[0].message.lower()

    def test_flags_deprecated_third_party(self, trees):
        t = trees.code("import six\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "six" in f.message]
        assert len(matching) == 1
        assert "Python 3" in matching[0].message

    def test_flags_pkg_resources(self, trees):
        t = trees.code("from pkg_resources import get_distribution\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "pkg_resources" in f.message]
        assert len(matching) == 1
        assert "importlib" in matching[0].message

    def test_unittest_conditional_on_pytest(self, trees):
        """unittest alone is fine; only flag when pytest is also used."""
        t = trees.code("import unittest\n")
        findings = check_stdlib_alternatives(t, verbose=False)
        unittest_findings = [f for f in findings if "unittest" in f.message]
        assert len(unittest_findings) == 0

    def test_unittest_flagged_with_pytest(self, trees):
        t = trees.files(
            {
                "test_old.py": "import unittest\n",
                "test_new.py": "import pytest\n",
            }
        )
        findings = check_stdlib_alternatives(t, verbose=False)
        matching = [f for f in findings if "unittest" in f.message]
        assert len(matching) == 1
        assert "pytest" in matching[0].message

    def test_catalog_has_all_categories(self):
        """Catalog includes unconditional, conditional, deprecated stdlib, and deprecated third-party."""
        catalog = _load_catalog()
        names = {e["name"] for e in catalog}
        # Unconditional
        assert "argparse-to-click" in names
        # Conditional
        assert "unittest-vs-pytest" in names
        # Deprecated stdlib
        assert "cgi-removed" in names
        # Deprecated third-party
        assert "six-obsolete" in names
