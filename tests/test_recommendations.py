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
