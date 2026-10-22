"""bypassed-helper and bypassed-helper-history: small helpers whose body is still spelled out."""

from __future__ import annotations

import ast
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pysmelly.checks.bypassed_helpers import (
    check_bypassed_helper,
    check_bypassed_helper_history,
)
from pysmelly.context import AnalysisContext, GitSettings
from pysmelly.registry import CHECK_CATEGORIES, CHECK_SEVERITY, GIT_HISTORY_CATEGORY, Severity

HELPERS = """\
import json


def load_json(path):
    \"\"\"Read a UTF-8 JSON file.\"\"\"
    return json.loads(path.read_text(encoding="utf-8"))
"""


def _project(**modules: str) -> dict[str, str]:
    files = {"pkg/__init__.py": "", "pkg/helpers.py": HELPERS}
    files.update({f"pkg/{name}.py": code for name, code in modules.items()})
    return files


def _sites(finding) -> list[tuple[str, int]]:
    return [(site.file, site.line) for site in finding.sites]


class TestRegistration:
    def test_both_checks_are_medium(self):
        assert CHECK_SEVERITY["bypassed-helper"] is Severity.MEDIUM
        assert CHECK_SEVERITY["bypassed-helper-history"] is Severity.MEDIUM

    def test_dated_twin_is_a_git_history_check(self):
        assert CHECK_CATEGORIES["bypassed-helper-history"] == GIT_HISTORY_CATEGORY
        assert CHECK_CATEGORIES["bypassed-helper"] != GIT_HISTORY_CATEGORY


class TestBypassedHelper:
    def test_inlined_body_with_other_arguments_fires(self, trees):
        reader = (
            "import json\n"
            "from pkg import helpers\n\n"
            "def read(config_path):\n"
            '    settings = json.loads(config_path.read_text(encoding="utf-8"))\n'
            "    return settings\n"
        )
        findings = check_bypassed_helper(trees.files(_project(reader=reader)))
        assert len(findings) == 1
        finding = findings[0]
        assert (finding.file, finding.line) == ("pkg/helpers.py", 4)
        assert _sites(finding) == [("pkg/reader.py", 5)]
        assert "`load_json` (pkg/helpers.py:4), a 1-statement helper" in finding.message
        assert "pkg/reader.py:5 in `read`" in finding.message
        assert "Fix: Replace each copy listed with a call to the helper" in finding.text

    def test_names_resolve_through_imports(self, trees):
        reader = (
            "from json import loads\n"
            "import pkg.helpers\n\n"
            "def read(p):\n"
            '    return loads(p.read_text(encoding="utf-8"))\n'
        )
        assert len(check_bypassed_helper(trees.files(_project(reader=reader)))) == 1

    def test_a_different_function_of_the_same_name_does_not_match(self, trees):
        reader = (
            "import yaml as json\n"
            "import pkg.helpers\n\n"
            "def read(p):\n"
            '    return json.loads(p.read_text(encoding="utf-8"))\n'
        )
        assert check_bypassed_helper(trees.files(_project(reader=reader))) == []

    def test_code_that_does_not_import_the_helper_is_not_bypassing_it(self, trees):
        reader = (
            'import json\n\ndef read(p):\n    return json.loads(p.read_text(encoding="utf-8"))\n'
        )
        assert check_bypassed_helper(trees.files(_project(reader=reader))) == []

    def test_trivial_helper_is_never_a_candidate(self, trees):
        code = (
            "def area(rect):\n"
            "    return rect.width * rect.height\n\n"
            "def total(rects):\n"
            "    return sum(r.width * r.height for r in rects)\n"
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_a_parameter_must_match_the_same_expression_everywhere(self, trees):
        code = (
            "import math\n\n"
            "def length(v):\n"
            "    return math.sqrt(v.x * v.x + v.y * v.y)\n\n"
            "def mixed(a, b):\n"
            "    print(a, b)\n"
            "    return math.sqrt(a.x * a.x + b.y * b.y)\n\n"
            "def same(a):\n"
            "    print(a)\n"
            "    return math.sqrt(a.x * a.x + a.y * a.y)\n"
        )
        findings = check_bypassed_helper(trees.code(code))
        assert [_sites(f) for f in findings] == [[("test.py", 12)]]

    def test_lambda_parameters_may_be_renamed(self, trees):
        code = (
            "def by_score(items):\n"
            "    return sorted(items, key=lambda item: item.score, reverse=True)\n\n"
            "def ranked(rows):\n"
            "    return sorted(rows, key=lambda row: row.score, reverse=True)\n"
        )
        findings = check_bypassed_helper(trees.code(code))
        assert [_sites(f) for f in findings] == [[("test.py", 5)]]

    def test_statement_run_matches_with_locals_renamed(self, trees):
        code = (
            "import json\n\n"
            "def save(path, rows):\n"
            "    text = json.dumps(rows, indent=2, sort_keys=True)\n"
            '    path.write_text(text, encoding="utf-8")\n\n'
            "def export(target, report):\n"
            "    print('exporting')\n"
            "    body = json.dumps(report.rows, indent=2, sort_keys=True)\n"
            '    target.write_text(body, encoding="utf-8")\n'
        )
        findings = check_bypassed_helper(trees.code(code))
        assert len(findings) == 1
        assert _sites(findings[0]) == [("test.py", 9)]
        assert "a 2-statement helper" in findings[0].message

    def test_final_return_matches_the_value_of_an_assignment(self, trees):
        code = (
            "import json\n\n"
            "def items_of(raw):\n"
            "    data = json.loads(raw, parse_float=str)\n"
            '    return data["items"]\n\n'
            "def handle(body):\n"
            "    payload = json.loads(body, parse_float=str)\n"
            '    entries = payload["items"]\n'
            "    return len(entries)\n"
        )
        findings = check_bypassed_helper(trees.code(code))
        assert [_sites(f) for f in findings] == [[("test.py", 8)]]

    def test_a_local_must_bind_the_same_name_everywhere(self, trees):
        code = (
            "import json\n\n"
            "def items_of(raw):\n"
            "    data = json.loads(raw, parse_float=str)\n"
            '    return data["items"]\n\n'
            "def handle(body, other):\n"
            "    payload = json.loads(body, parse_float=str)\n"
            '    entries = other["items"]\n'
            "    return len(entries)\n"
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_one_drifted_place_in_a_heavy_shape_fires_and_names_it(self, trees):
        code = (
            "import describe\n\n"
            "def describer(surface, directory):\n"
            "    return Describer(\n"
            "        many=lambda r: describe.regions(surface, r, directory=directory),\n"
            "        attrs={describe.VERSION_KEY: describe.VERSION},\n"
            "        identify=lambda r: describe.identify(surface, r, directory=directory),\n"
            "    )\n\n"
            "def other(surface, directory):\n"
            "    return Describer(\n"
            "        many=lambda r: describe.regions(surface, r, directory=directory),\n"
            "        attrs={**surface.params, describe.VERSION_KEY: describe.VERSION},\n"
            "        identify=lambda r: describe.identify(surface, r, directory=directory),\n"
            "    )\n\n"
            "class Describer:\n"
            "    pass\n"
        )
        findings = check_bypassed_helper(trees.code(code))
        assert len(findings) == 1
        message = findings[0].message
        assert "`describer` (test.py:3)" in message
        assert "differs: `{**surface.params" in message
        assert "make that place a parameter of the helper" in findings[0].text

    def test_two_drifted_places_do_not_match(self, trees):
        code = (
            "import describe\n\n"
            "def describer(surface, directory):\n"
            "    return Describer(\n"
            "        many=lambda r: describe.regions(surface, r, directory=directory),\n"
            "        attrs={describe.VERSION_KEY: describe.VERSION},\n"
            "        identify=lambda r: describe.identify(surface, r, directory=directory),\n"
            "    )\n\n"
            "def other(surface, directory):\n"
            "    return Describer(\n"
            "        many=lambda r: describe.regions(surface, r, directory=None),\n"
            "        attrs={**surface.params, describe.VERSION_KEY: describe.VERSION},\n"
            "        identify=lambda r: describe.identify(surface, r, directory=directory),\n"
            "    )\n\n"
            "class Describer:\n"
            "    pass\n"
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_a_light_shape_must_match_exactly(self, trees):
        code = (
            "import json\n\n"
            "def load(path):\n"
            '    return json.loads(path.read_text(encoding="utf-8"))\n\n'
            "def load_latin(path):\n"
            '    return json.loads(path.read_text(encoding="latin-1"))\n'
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_twin_helpers_are_one_finding_under_the_imported_one(self, trees):
        reader = (
            "import json\n"
            "from pkg import helpers\n\n"
            "def load_json(p):\n"
            '    return json.loads(p.read_text(encoding="utf-8"))\n'
        )
        findings = check_bypassed_helper(trees.files(_project(reader=reader)))
        assert [(f.file, _sites(f)) for f in findings] == [
            ("pkg/helpers.py", [("pkg/reader.py", 5)])
        ]

    def test_twins_in_one_module_report_the_first(self, trees):
        code = (
            "import json\n\n"
            "def load(path):\n"
            '    return json.loads(path.read_text(encoding="utf-8"))\n\n'
            "def read(source):\n"
            '    return json.loads(source.read_text(encoding="utf-8"))\n'
        )
        findings = check_bypassed_helper(trees.code(code))
        assert [(f.line, _sites(f)) for f in findings] == [(3, [("test.py", 7)])]

    def test_decorated_async_and_generator_functions_are_not_helpers(self, trees):
        code = (
            "import functools, json\n\n"
            "@functools.cache\n"
            "def cached(path):\n"
            '    return json.loads(path.read_text(encoding="utf-8"))\n\n'
            "async def fetch(path):\n"
            '    return json.loads(path.read_text(encoding="utf-8"))\n\n'
            "def gen(path):\n"
            '    yield json.loads(path.read_text(encoding="utf-8"))\n\n'
            "def use(p):\n"
            "    print(p)\n"
            '    return json.loads(p.read_text(encoding="utf-8"))\n'
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_long_helper_is_not_a_candidate(self, trees):
        code = (
            "import json\n\n"
            "def load(path):\n"
            "    a = 1\n    b = 2\n    c = 3\n"
            '    return json.loads(path.read_text(encoding="utf-8"))\n\n'
            "def use(p):\n"
            "    print(p)\n"
            '    return json.loads(p.read_text(encoding="utf-8"))\n'
        )
        assert check_bypassed_helper(trees.code(code)) == []

    def test_copies_in_test_files_are_not_sites(self, trees):
        files = _project()
        files["tests/test_reader.py"] = (
            "import json\nfrom pkg import helpers\n\n"
            "def test_read(tmp_path):\n"
            '    assert json.loads(tmp_path.read_text(encoding="utf-8"))\n'
        )
        assert check_bypassed_helper(trees.files(files)) == []


_OLD = 400
_YOUNG = 30

_HELPER = 'import json\n\n\ndef load_json(path):\n    return json.loads(path.read_text(encoding="utf-8"))\n'
_EARLY = 'import json\nimport helpers\n\n\ndef early(p):\n    return json.loads(p.read_text(encoding="utf-8"))\n'
_LATE = 'import json\nimport helpers\n\n\ndef late(p):\n    return json.loads(p.read_text(encoding="utf-8"))\n'


def _when(days_ago: int) -> datetime:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)


def _commit(repo: Path, files: dict[str, str], days_ago: int, message: str) -> None:
    """Write ``files``, stage them by path, and commit them dated ``days_ago``."""
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "--", name], cwd=repo, capture_output=True, check=True)
    stamp = _when(days_ago).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, capture_output=True, check=True
    )


def _ctx(repo: Path) -> AnalysisContext:
    trees = {
        path.relative_to(repo): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(repo.glob("*.py"))
    }
    return AnalysisContext(trees, verbose=False, git=GitSettings(root=repo))


class TestBypassedHelperHistory:
    def test_without_git_reports_nothing(self, trees):
        code = _HELPER + "\n\n" + _EARLY.replace("import json\nimport helpers\n", "")
        assert check_bypassed_helper(trees.code(code)) != []
        assert check_bypassed_helper_history(trees.code(code)) == []

    def test_dates_the_helper_and_splits_its_copies(self, git_repo):
        _commit(git_repo, {"early.py": _EARLY}, _OLD, "the early copy")
        _commit(git_repo, {"helpers.py": _HELPER}, 200, "add load_json")
        _commit(git_repo, {"late.py": _LATE}, _YOUNG, "the late copy")
        findings = check_bypassed_helper_history(_ctx(git_repo))
        assert len(findings) == 1
        message = findings[0].message
        assert (findings[0].file, findings[0].line) == ("helpers.py", 4)
        assert f"on {_when(200):%Y-%m-%d} ('add load_json')" in message
        assert "Written before it, never moved to it: early.py:6 in `early`" in message
        assert "Written after it: late.py:6 in `late`" in message

    def test_an_uncommitted_copy_is_listed_undated(self, git_repo):
        _commit(git_repo, {"helpers.py": _HELPER}, 200, "add load_json")
        (git_repo / "late.py").write_text(_LATE, encoding="utf-8")
        findings = check_bypassed_helper_history(_ctx(git_repo))
        assert "Not committed as analyzed, so undated: late.py:6" in findings[0].message

    def test_an_uncommitted_helper_is_not_reported(self, git_repo):
        _commit(git_repo, {"early.py": _EARLY}, _OLD, "the early copy")
        (git_repo / "helpers.py").write_text(_HELPER, encoding="utf-8")
        assert check_bypassed_helper_history(_ctx(git_repo)) == []
