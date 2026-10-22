"""Tests for unnamed-constant-use and type-encoded-as-data."""

from pysmelly.checks.constant_roles import check_type_encoded_as_data, check_unnamed_constant_use
from pysmelly.registry import CHECK_SEVERITY, Severity

REGISTRY = """\
STAMP_POLICY = "policy"
STAMP_ROUTE = "route"


def make(**kw):
    return kw


DEFAULT = make(stamp_key=STAMP_POLICY)
ROUTED = make(stamp_key=STAMP_ROUTE)
"""

# Importing the registry from somewhere makes it a shared module, not a script.
USER = "from pkg import registry\n"


def _project(**modules: str) -> dict[str, str]:
    files = {"pkg/__init__.py": "", "pkg/registry.py": REGISTRY, "pkg/user.py": USER}
    files.update({f"pkg/{name}.py": code for name, code in modules.items()})
    return files


def _unnamed(trees, **modules: str):
    return check_unnamed_constant_use(trees.files(_project(**modules)))


class TestUnnamedConstantUse:
    def test_registered_medium(self):
        assert CHECK_SEVERITY["unnamed-constant-use"] is Severity.MEDIUM

    def test_comparison_in_the_constants_keyword_role(self, trees):
        findings = _unnamed(trees, stream='def f(stamp_key):\n    return stamp_key == "policy"\n')
        assert [(f.file, f.line) for f in findings] == [("pkg/stream.py", 2)]
        message = findings[0].message
        assert "`STAMP_POLICY` (pkg/registry.py:1)" in message
        assert "fills `stamp_key`" in message
        assert message.endswith("fills at pkg/registry.py:9")

    def test_return_from_a_function_named_for_the_role(self, trees):
        code = (
            'def _stamp_key(version):\n    if version:\n        return "route"\n    return None\n'
        )
        findings = _unnamed(trees, stream=code)
        assert [f.line for f in findings] == [3]
        assert "STAMP_ROUTE" in findings[0].message

    def test_role_learned_from_a_comparison_through_a_module_attribute(self, trees):
        files = _project(
            kinds='KIND_WIDGET = "widget"\n',
            reader="import pkg.kinds\n\n"
            "def g(part):\n    return part.kind == pkg.kinds.KIND_WIDGET\n",
            other='def h(part):\n    if part.kind != "widget":\n        return 1\n',
        )
        findings = check_unnamed_constant_use(trees.files(files))
        assert [(f.file, f.line) for f in findings] == [("pkg/other.py", 2)]
        assert "`KIND_WIDGET` (pkg/kinds.py:1)" in findings[0].message
        assert "pkg/reader.py:4" in findings[0].message

    def test_membership_tuple_shares_the_comparison_role(self, trees):
        code = 'def f(stamp_key):\n    return stamp_key in ("policy", "other")\n'
        findings = _unnamed(trees, stream=code)
        assert [f.line for f in findings] == [2]

    def test_match_case_takes_the_subject_role(self, trees):
        code = (
            "def f(stamp_key):\n"
            "    match stamp_key:\n"
            '        case "policy":\n'
            "            return 1\n"
            "    return 0\n"
        )
        findings = _unnamed(trees, stream=code)
        assert [f.line for f in findings] == [3]

    def test_parameter_default_and_conditional_expression(self, trees):
        code = (
            'def f(stamp_key="policy"):\n'
            "    return stamp_key\n\n"
            "def g(flag):\n"
            '    stamp_key = "route" if flag else None\n'
            "    return stamp_key\n"
        )
        findings = _unnamed(trees, stream=code)
        assert [f.line for f in findings] == [1, 5]

    def test_dict_value_under_the_roles_key(self, trees):
        code = 'def f():\n    return {"stamp_key": "policy"}\n'
        findings = _unnamed(trees, stream=code)
        assert [f.line for f in findings] == [2]

    def test_key_of_the_same_container(self, trees):
        code = (
            'SIZES = "sizes"\n\n'
            "def f(params, sizes):\n"
            "    params[SIZES] = sizes\n"
            '    return params["sizes"], params.get("sizes")\n'
        )
        findings = _unnamed(trees, writer=code)
        assert [(f.line, "a key of `params`" in f.message) for f in findings] == [
            (5, True),
            (5, True),
        ]

    def test_dict_construction_key_takes_the_dicts_role(self, trees):
        code = (
            'SIZES = "sizes"\n\n'
            "def f(sizes):\n"
            "    params = {SIZES: sizes}\n"
            '    params = {"sizes": sizes}\n'
            "    return params\n"
        )
        findings = _unnamed(trees, writer=code)
        assert [f.line for f in findings] == [5]

    def test_negative_number(self, trees):
        code = "FLOOR = -40\n\ndef f(x):\n    return x.floor == FLOOR or x.floor == -40\n"
        findings = _unnamed(trees, temps=code)
        assert [f.line for f in findings] == [4]
        assert "-40" in findings[0].message

    def test_same_value_in_another_role_is_a_coincidence(self, trees):
        code = 'def f(mode):\n    return mode == "policy" or make(kind="route")\n'
        assert _unnamed(trees, stream=code) == []

    def test_another_constant_with_the_same_value_is_not_a_use(self, trees):
        """Q5's trap: two named constants that coincide are scattered-constants' business."""
        code = 'OTHER_POLICY = "policy"\n\ndef f(stamp_key=OTHER_POLICY):\n    return stamp_key\n'
        assert _unnamed(trees, stream=code) == []

    def test_constant_never_seen_in_a_role_matches_nothing(self, trees):
        code = 'UNUSED = "lonely"\n\ndef f(x):\n    return x == "lonely"\n'
        assert _unnamed(trees, stream=code) == []

    def test_trivial_values_are_skipped(self, trees):
        code = (
            'ONE = 1\nEMPTY = ""\nLETTER = "x"\n\n'
            "def f(n=ONE, s=EMPTY, c=LETTER):\n    return f(n=1, s='', c='x')\n"
        )
        assert _unnamed(trees, small=code) == []

    def test_reassigned_constant_is_not_a_constant(self, trees):
        code = 'MODE = "fast"\nMODE = "slow"\n\ndef f(mode=MODE):\n    return mode == "slow"\n'
        assert _unnamed(trees, modes=code) == []

    def test_private_constant_is_its_own_modules_business(self, trees):
        files = _project(
            a='_KIND = "widget"\n\ndef f(kind=_KIND):\n    return kind\n',
            b='from pkg import a\n\ndef g(kind):\n    return kind == "widget"\n',
        )
        assert check_unnamed_constant_use(trees.files(files)) == []

    def test_private_constant_still_fires_in_its_own_module(self, trees):
        code = '_KIND = "widget"\n\ndef f(kind=_KIND):\n    return kind == "widget"\n'
        findings = _unnamed(trees, a=code)
        assert [f.line for f in findings] == [4]

    def test_constant_of_a_script_nobody_imports(self, trees):
        files = {
            "tools/report.py": 'OUTCOME = "kill line met"\n\ndef f(outcome=OUTCOME):\n    pass\n',
            "tools/census.py": 'def g(outcome):\n    return outcome == "kill line met"\n',
        }
        assert check_unnamed_constant_use(trees.files(files)) == []

    def test_number_needs_its_module_imported_where_the_literal_is(self, trees):
        base = "TIMEOUT_SECONDS = 60\n\ndef run(timeout=TIMEOUT_SECONDS):\n    pass\n"
        files = _project(
            base=base,
            importer="from pkg import base\n\ndef f():\n    return base.run(timeout=60)\n",
            stranger="import subprocess\n\ndef g():\n    subprocess.run([], timeout=60)\n",
        )
        findings = check_unnamed_constant_use(trees.files(files))
        assert [f.file for f in findings] == ["pkg/importer.py"]

    def test_call_result_is_not_a_role(self, trees):
        code = "ARGS = 4\n\ndef f(a, b):\n    return len(a) == ARGS or len(b) == 4\n"
        assert _unnamed(trees, sizes=code) == []

    def test_several_constants_with_the_value_are_all_named(self, trees):
        files = _project(
            a='KIND = "widget"\n\ndef f(kind=KIND):\n    return kind\n',
            b='WIDGET = "widget"\n\ndef g(kind=WIDGET):\n    return kind\n',
            c="from pkg import a, b\n\ndef h(kind):\n    return kind == 'widget'\n",
        )
        findings = check_unnamed_constant_use(trees.files(files))
        assert len(findings) == 1
        assert "`KIND` (pkg/a.py:1), or `WIDGET` (pkg/b.py:1)" in findings[0].message


ENGINES = {
    "pkg/__init__.py": "",
    "pkg/staves.py": 'ENGINE_NAME = "staves"\n',
    "pkg/blank.py": 'ENGINE_NAME = "blank"\n',
}


def _decoded(trees, code: str):
    return check_type_encoded_as_data(trees.files({**ENGINES, "pkg/dispatch.py": code}))


class TestTypeEncodedAsData:
    def test_registered_low(self):
        assert CHECK_SEVERITY["type-encoded-as-data"] is Severity.LOW

    def test_unpacked_result_name_compared_with_foreign_constants(self, trees):
        code = (
            "from pkg import blank, staves\n\n"
            "def route(source):\n"
            "    header, blocks = run_or_blank(source)\n"
            "    (finder,) = header.segmenters\n"
            "    if finder.tool.name == staves.ENGINE_NAME:\n"
            "        return 1\n"
            "    if finder.tool.name != blank.ENGINE_NAME:\n"
            "        raise ValueError(source)\n"
            "    return blocks\n"
        )
        findings = _decoded(trees, code)
        assert [(f.file, f.line) for f in findings] == [("pkg/dispatch.py", 4)]
        message = findings[0].message
        assert "`run_or_blank(...)`" in message
        assert "line 6: `finder.tool.name` vs `staves.ENGINE_NAME` (pkg/staves.py:1)" in message
        assert "line 8: `finder.tool.name` vs `blank.ENGINE_NAME` (pkg/blank.py:1)" in message

    def test_kind_subscript_and_membership(self, trees):
        code = (
            "from pkg.staves import ENGINE_NAME as STAVES\n"
            "from pkg import blank\n\n"
            "async def f(x):\n"
            "    result = await fetch(x)\n"
            '    if result.params["engine"] in (STAVES, blank.ENGINE_NAME):\n'
            "        return 1\n"
            "    return result.kind == blank.ENGINE_NAME\n"
        )
        findings = _decoded(trees, code)
        assert [f.line for f in findings] == [5]
        assert findings[0].message.count("line ") == 3

    def test_comparison_beyond_three_statements_is_state(self, trees):
        code = (
            "from pkg import staves\n\n"
            "def f(x):\n"
            "    result = run(x)\n"
            "    a = 1\n"
            "    b = 2\n"
            "    c = 3\n"
            "    return result.name == staves.ENGINE_NAME\n"
        )
        assert _decoded(trees, code) == []

    def test_third_following_statement_is_still_right_after(self, trees):
        code = (
            "from pkg import staves\n\n"
            "def f(x):\n"
            "    result = run(x)\n"
            "    a = 1\n"
            "    b = 2\n"
            "    return result.name == staves.ENGINE_NAME\n"
        )
        assert [f.line for f in _decoded(trees, code)] == [4]

    def test_constant_of_the_same_module_is_not_foreign(self, trees):
        code = (
            'LOCAL = "local"\n\ndef f(x):\n    result = run(x)\n    return result.name == LOCAL\n'
        )
        assert _decoded(trees, code) == []

    def test_data_fields_and_literals_and_bare_results(self, trees):
        code = (
            "from pkg import staves\n\n"
            "def f(x):\n"
            "    stats = evaluate(x)\n"
            "    if stats.mime_type == staves.ENGINE_NAME:\n"
            "        return 1\n"
            '    if stats.name == "staves":\n'
            "        return 2\n"
            "    return stats == staves.ENGINE_NAME\n"
        )
        assert _decoded(trees, code) == []

    def test_rebound_result_is_no_longer_the_calls(self, trees):
        code = (
            "from pkg import staves\n\n"
            "def f(x, other):\n"
            "    result = run(x)\n"
            "    result = other\n"
            "    return result.name == staves.ENGINE_NAME\n"
        )
        assert _decoded(trees, code) == []

    def test_iterating_the_result_reads_its_content(self, trees):
        code = (
            "from pkg import staves\n\n"
            "def f(x):\n"
            "    blocks = run(x)\n"
            "    return any(b.type == staves.ENGINE_NAME for b in blocks)\n"
        )
        assert _decoded(trees, code) == []

    def test_unresolved_name_is_not_a_constant(self, trees):
        code = "def f(x):\n    result = run(x)\n    return result.name == unknown.ENGINE_NAME\n"
        assert _decoded(trees, code) == []
