"""The production/test caller clause the caller-counting checks share (Phase 24-3).

dead-code, unused-defaults, pass-through-params, internal-only and
single-call-site count production and test callers separately, decided by
the test role, and name the files they counted as tests.
"""

from pysmelly.checks.callers import (
    check_dead_code,
    check_internal_only,
    check_pass_through_params,
    check_single_call_site,
    check_unused_defaults,
)


def _messages(findings, filename: str = "app.py") -> list[str]:
    return [f.message for f in findings if f.file == filename]


class TestDeadCodeMessages:
    def test_no_callers_at_all(self, trees):
        t = trees.files({"app.py": "def parse():\n    return 1\n"})
        assert _messages(check_dead_code(t)) == ["parse(): no production callers, no test callers"]

    def test_test_only_function_is_dead_and_names_the_test_lines(self, trees):
        t = trees.files(
            {
                "app.py": "def parse():\n    return 1\n",
                "tests/test_app.py": (
                    "from app import parse\n\n"
                    "def test_a():\n    parse()\n\n"
                    "def test_b():\n    assert parse()\n"
                ),
            }
        )
        assert _messages(check_dead_code(t)) == [
            "parse(): no production callers; still referenced by "
            "tests/test_app.py:4, :7 — test-only surface. "
            "Tests that use it, to delete or rewrite: tests/test_app.py (every test)"
        ]

    def test_a_test_file_that_only_imports_is_named_without_a_line(self, trees):
        t = trees.files(
            {
                "app.py": "def parse():\n    return 1\n",
                "tests/test_app.py": "from app import parse\n\ndef test_a():\n    parse()\n",
                "tests/test_cases.py": "from app import parse\n\nCASES = [parse]\n",
            }
        )
        assert _messages(check_dead_code(t)) == [
            "parse(): no production callers; still referenced by "
            "tests/test_app.py:4, tests/test_cases.py — test-only surface. "
            "Tests that use it, to delete or rewrite: tests/test_app.py (every test); "
            "tests/test_cases.py: CASES"
        ]

    def test_lines_past_three_per_file_are_counted(self, trees):
        calls = "".join(f"def test_{i}():\n    parse()\n" for i in range(5))
        t = trees.files(
            {
                "app.py": "def parse():\n    return 1\n",
                "tests/test_app.py": "from app import parse\n" + calls,
            }
        )
        assert _messages(check_dead_code(t)) == [
            "parse(): no production callers; still referenced by "
            "tests/test_app.py:3, :5, :7 (+2 more) — test-only surface. "
            "Tests that use it, to delete or rewrite: tests/test_app.py (every test)"
        ]

    def test_a_production_caller_keeps_it_alive(self, trees):
        t = trees.files(
            {
                "app.py": "def parse():\n    return 1\n",
                "main.py": "from app import parse\n\nparse()\n",
                "tests/test_app.py": "from app import parse\n\ndef test_a():\n    parse()\n",
            }
        )
        assert _messages(check_dead_code(t)) == []

    def test_a_production_value_reference_keeps_it_alive(self, trees):
        t = trees.files(
            {
                "app.py": "def parse():\n    return 1\n\nHANDLERS = {'p': parse}\n",
                "tests/test_app.py": "from app import parse\n\ndef test_a():\n    parse()\n",
            }
        )
        assert _messages(check_dead_code(t)) == []


class TestUnusedDefaultsMessages:
    def test_production_and_test_counts(self, trees):
        t = trees.files(
            {
                "app.py": "def fetch(url, timeout=None):\n    return url, timeout\n",
                "main.py": "from app import fetch\n\nfetch('u', timeout=3)\n",
                "tests/test_app.py": (
                    "from app import fetch\n\ndef test_a():\n    fetch('u', timeout=1)\n"
                ),
            }
        )
        assert _messages(check_unused_defaults(t)) == [
            "fetch() param 'timeout' defaults to None but all 2 caller(s) always pass it "
            "(1 production caller(s), 1 test caller(s) in tests/test_app.py:4)"
        ]

    def test_production_only(self, trees):
        t = trees.files(
            {
                "app.py": "def fetch(url, timeout=None):\n    return url, timeout\n",
                "main.py": "from app import fetch\n\nfetch('u', timeout=3)\n",
            }
        )
        assert _messages(check_unused_defaults(t)) == [
            "fetch() param 'timeout' defaults to None but all 1 caller(s) always pass it "
            "(1 production caller(s), 0 test caller(s))"
        ]

    def test_no_production_callers(self, trees):
        t = trees.files(
            {
                "app.py": "def fetch(url, timeout=None):\n    return url, timeout\n",
                "tests/test_app.py": (
                    "from app import fetch\n\n"
                    "def test_a():\n    fetch('u', timeout=1)\n    fetch('v', timeout=2)\n"
                ),
            }
        )
        assert _messages(check_unused_defaults(t)) == [
            "fetch() param 'timeout' defaults to None but all 2 caller(s) always pass it "
            "(no production callers; still referenced by tests/test_app.py:4, :5)"
        ]


_FORWARDER = "def outer(cfg):\n    return inner(cfg)\n\ndef inner(cfg):\n    return cfg.x\n"


class TestPassThroughParamsMessages:
    def test_production_and_test_counts(self, trees):
        t = trees.files(
            {
                "app.py": _FORWARDER,
                "main.py": "from app import outer\n\nouter(1)\n",
                "tests/test_app.py": "from app import outer\n\ndef test_a():\n    outer(2)\n",
            }
        )
        assert _messages(check_pass_through_params(t)) == [
            "outer() receives 'cfg' but only forwards it to inner() "
            "(1 production caller(s), 1 test caller(s) in tests/test_app.py:4)"
        ]

    def test_no_production_callers(self, trees):
        t = trees.files(
            {
                "app.py": _FORWARDER,
                "tests/test_app.py": "from app import outer\n\ndef test_a():\n    outer(2)\n",
            }
        )
        assert _messages(check_pass_through_params(t)) == [
            "outer() receives 'cfg' but only forwards it to inner() "
            "(no production callers; still referenced by tests/test_app.py:4)"
        ]


_INTERNAL = "def helper():\n    return 1\n\ndef run():\n    return helper() + helper()\n"


class TestInternalOnlyMessages:
    def test_production_only(self, trees):
        t = trees.files({"app.py": _INTERNAL, "main.py": "from app import run\n\nrun()\n"})
        assert _messages(check_internal_only(t)) == [
            "helper() is public but production only calls it within its own file "
            "(2 production caller(s), 0 test caller(s))"
        ]

    def test_a_test_caller_does_not_make_it_public(self, trees):
        t = trees.files(
            {
                "app.py": _INTERNAL,
                "main.py": "from app import run\n\nrun()\n",
                "tests/test_app.py": "from app import helper\n\ndef test_a():\n    helper()\n",
            }
        )
        assert _messages(check_internal_only(t)) == [
            "helper() is public but production only calls it within its own file "
            "(2 production caller(s), 1 test caller(s) in tests/test_app.py:4)"
        ]

    def test_a_production_caller_elsewhere_still_makes_it_public(self, trees):
        t = trees.files(
            {
                "app.py": _INTERNAL,
                "main.py": "from app import helper, run\n\nrun()\nhelper()\n",
            }
        )
        assert _messages(check_internal_only(t)) == []


_SINGLE = "def shout(word):\n    return word.upper()\n\ndef run():\n    return shout('a')\n"


class TestSingleCallSiteMessages:
    def test_production_only(self, trees):
        t = trees.files({"app.py": _SINGLE})
        assert _messages(check_single_call_site(t)) == [
            "shout() has 1 params and exactly 1 call site (app.py:5) "
            "(1 production caller(s), 0 test caller(s))"
        ]

    def test_test_callers_are_named(self, trees):
        t = trees.files(
            {
                "app.py": _SINGLE,
                "tests/test_app.py": (
                    "from app import shout\n\ndef test_a():\n    shout('b')\n    shout('c')\n"
                ),
            }
        )
        assert _messages(check_single_call_site(t)) == [
            "shout() has 1 params and exactly 1 call site (app.py:5) "
            "(1 production caller(s), 2 test caller(s) in tests/test_app.py:4, :5)"
        ]
