"""Test-only surface (Phase 26-4): production code only tests use, and the tests that go with it.

A function, a parameter, a constant, an exception class or a record field
that only test-role code references is reported, and the finding names the
tests to delete (``checks/test_surface.py``).
"""

import ast
from pathlib import Path

from pysmelly.checks.architecture import check_write_only_attributes
from pysmelly.checks.caller_decided import check_always_default_params
from pysmelly.checks.callers import check_dead_code
from pysmelly.checks.dead import check_dead_exceptions
from pysmelly.checks.patterns_data import check_dead_constants
from pysmelly.checks.reachability import check_unreachable_from_entry_points
from pysmelly.checks.test_surface import surface_of_units
from pysmelly.context import AnalysisContext

LIB = "def parse():\n    return 1\n\n\ndef keep():\n    return 2\n"


def _messages(findings, filename: str = "lib.py") -> list[str]:
    return [f.message for f in findings if f.file == filename]


class TestFunction:
    def test_every_test_in_the_file_uses_it_so_the_file_goes(self, trees):
        t = trees.files(
            {
                "lib.py": "def parse():\n    return 1\n",
                "tests/test_lib.py": (
                    "from lib import parse\n\n\ndef test_a():\n    assert parse()\n\n\n"
                    "class TestParse:\n    def test_b(self):\n        assert parse() == 1\n"
                ),
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/test_lib.py (every test)"
        )

    def test_only_the_tests_that_use_it_are_named(self, trees):
        t = trees.files(
            {
                "lib.py": LIB,
                "main.py": "from lib import keep\n\nkeep()\n",
                "tests/test_lib.py": (
                    "from lib import keep, parse\n\n\n"
                    "def test_parse():\n    assert parse()\n\n\n"
                    "def test_keep():\n    assert keep()\n"
                ),
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/test_lib.py: test_parse()"
        )

    def test_a_fixture_that_builds_it_takes_the_tests_requesting_it(self, trees):
        t = trees.files(
            {
                "lib.py": LIB,
                "main.py": "from lib import keep\n\nkeep()\n",
                "tests/conftest.py": (
                    "import pytest\nfrom lib import parse\n\n\n"
                    "@pytest.fixture\ndef parsed():\n    return parse()\n"
                ),
                "tests/test_lib.py": (
                    "from lib import keep\n\n\n"
                    "def test_parsed(parsed):\n    assert parsed\n\n\n"
                    "def test_keep():\n    assert keep()\n"
                ),
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/conftest.py: parsed(); tests/test_lib.py: test_parsed()"
        )

    def test_a_conftest_fixture_is_not_requested_outside_its_directory(self, trees):
        t = trees.files(
            {
                "lib.py": LIB,
                "main.py": "from lib import keep\n\nkeep()\n",
                "tests/unit/conftest.py": (
                    "import pytest\nfrom lib import parse\n\n\n"
                    "@pytest.fixture\ndef parsed():\n    return parse()\n"
                ),
                "tests/other/test_other.py": "def test_parsed(parsed):\n    assert parsed\n",
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/unit/conftest.py: parsed()"
        )

    def test_the_cascade_brings_the_tests_of_what_it_frees(self, trees):
        t = trees.files(
            {
                "lib.py": (
                    "def report():\n    return _render()\n\n\n"
                    "def _render():\n    return 1\n\n\ndef keep():\n    return 2\n"
                ),
                "main.py": "from lib import keep\n\nkeep()\n",
                "tests/test_render.py": (
                    "from lib import _render, keep\n\n\n"
                    "def test_render():\n    assert _render()\n\n\n"
                    "def test_keep():\n    assert keep()\n"
                ),
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message == (
            "report(): no production callers, no test callers. Removing report() "
            "also frees 1: _render() (lib.py:5). Tests that use it, to delete or rewrite: "
            "tests/test_render.py: test_render()"
        )

    def test_no_tests_means_no_tests_sentence(self, trees):
        t = trees.files({"lib.py": "def parse():\n    return 1\n"})
        assert _messages(check_dead_code(t)) == ["parse(): no production callers, no test callers"]

    def test_a_support_module_never_goes_whole(self, trees):
        t = trees.files(
            {
                "lib.py": "def parse():\n    return 1\n",
                "tests/conftest.py": "from lib import parse\n\n\ndef helper():\n    return parse()\n",
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/conftest.py: helper()"
        )


DEAD = "def parse():\n    return 1\n"


class TestInsideATestClass:
    """A reserved_types(): 2 of a TestCase's 5 tests call it, and the
    class used to count as one test, so its file read "(every test)"."""

    def _message(self, trees, test_code: str) -> str:
        t = trees.files(
            {"lib.py": DEAD, "tests/test_lib.py": "from lib import parse\n\n\n" + test_code}
        )
        [message] = _messages(check_dead_code(t))
        return message.split("Tests that use it, to delete or rewrite: ")[1]

    def test_only_the_methods_that_call_it_are_named(self, trees):
        tests = (
            "class TestShow:\n"
            "    def test_a(self):\n        assert parse()\n\n"
            "    def test_b(self):\n        assert True\n\n"
            "    def test_c(self):\n        assert parse() == 1\n"
        )
        assert self._message(trees, tests) == (
            "tests/test_lib.py: TestShow.test_a(), TestShow.test_c()"
        )

    def test_every_method_calling_it_names_the_class_and_the_file(self, trees):
        tests = (
            "class TestShow:\n"
            "    def test_a(self):\n        assert parse()\n\n"
            "    def test_b(self):\n        assert parse() == 1\n"
        )
        assert self._message(trees, tests) == "tests/test_lib.py (every test)"

    def test_set_up_is_every_test_of_its_class(self, trees):
        tests = (
            "class TestShow:\n"
            "    def setUp(self):\n        self.value = parse()\n\n"
            "    def test_a(self):\n        assert self.value\n\n"
            "    def test_b(self):\n        assert True\n\n\n"
            "def test_other():\n    assert True\n"
        )
        assert self._message(trees, tests) == "tests/test_lib.py: TestShow"

    def test_a_helper_method_takes_the_tests_that_call_it(self, trees):
        tests = (
            "class TestShow:\n"
            "    def _parsed(self):\n        return parse()\n\n"
            "    def test_a(self):\n        assert self._parsed()\n\n"
            "    def test_b(self):\n        assert True\n"
        )
        assert self._message(trees, tests) == "tests/test_lib.py: TestShow.test_a()"

    def test_a_fixture_method_takes_the_tests_requesting_it(self, trees):
        tests = (
            "import pytest\n\n\nclass TestShow:\n"
            "    @pytest.fixture\n    def parsed(self):\n        return parse()\n\n"
            "    def test_a(self, parsed):\n        assert parsed\n\n"
            "    def test_b(self):\n        assert True\n"
        )
        assert self._message(trees, tests) == "tests/test_lib.py: TestShow.test_a()"

    def test_a_top_level_fixture_takes_the_methods_requesting_it(self, trees):
        tests = (
            "import pytest\n\n\n@pytest.fixture\ndef parsed():\n    return parse()\n\n\n"
            "class TestShow:\n"
            "    def test_a(self, parsed):\n        assert parsed\n\n"
            "    def test_b(self):\n        assert True\n"
        )
        assert self._message(trees, tests) == ("tests/test_lib.py: parsed(), TestShow.test_a()")

    def test_a_class_attribute_is_every_test_of_its_class(self, trees):
        t = trees.files(
            {
                "lib.py": "LIMIT = 3\n",
                "tests/test_lib.py": (
                    "from lib import LIMIT\n\n\nclass TestCap:\n    cap = LIMIT\n\n"
                    "    def test_a(self):\n        assert self.cap\n\n"
                    "    def test_b(self):\n        assert True\n"
                ),
            }
        )
        [message] = _messages(check_dead_constants(t))
        assert message.endswith(
            "Tests that use it, to delete or rewrite: tests/test_lib.py (every test)"
        )

    def test_a_field_read_in_one_method_names_that_method(self, trees):
        t = trees.files(
            {
                "lib.py": TestRecordField.RECORD,
                "tests/test_lib.py": (
                    "from lib import build\n\n\nclass TestJob:\n"
                    "    def test_retries(self):\n        assert build().retries == 3\n\n"
                    "    def test_name(self):\n        assert build().name\n"
                ),
            }
        )
        [message] = _messages(check_write_only_attributes(t))
        assert "only tests read it, in tests/test_lib.py: TestJob.test_retries() —" in message


class TestUnreachable:
    def test_the_cluster_names_the_tests_that_use_it(self):
        files = {
            "app.py": (
                "from lib import used\n\n\ndef main():\n    used()\n\n\n"
                'if __name__ == "__main__":\n    main()\n'
            ),
            "lib.py": (
                "def used():\n    pass\n\n\ndef _island():\n    return _shore()\n\n\n"
                "def _shore():\n    return _island()\n"
            ),
            "tests/test_lib.py": (
                "from lib import _island, used\n\n\n"
                "def test_island():\n    assert _island() is None\n\n\n"
                "def test_used():\n    assert used() is None\n"
            ),
        }
        parsed = {Path(name): ast.parse(code) for name, code in files.items()}
        [finding] = check_unreachable_from_entry_points(AnalysisContext(parsed, verbose=False))
        assert finding.message.endswith(
            "Tests that use them, to delete or rewrite: tests/test_lib.py: test_island()"
        )


class TestParameter:
    def test_the_tests_passing_a_parameter_production_never_passes_are_named(self, trees):
        t = trees.files(
            {
                "app.py": (
                    "def fetch(url, timeout=30):\n    return url, timeout\n\n\n"
                    "def main():\n    fetch('x')\n"
                ),
                "tests/test_app.py": (
                    "from app import fetch\n\n\n"
                    "def test_fetch_timeout():\n    assert fetch('x', timeout=1)\n\n\n"
                    "def test_fetch():\n    assert fetch('x')\n"
                ),
            }
        )
        [finding] = check_always_default_params(t)
        assert finding.message.endswith(
            "only tests pass it, at tests/test_app.py:5 (tests/test_app.py: test_fetch_timeout())"
        )


class TestConstant:
    def test_a_constant_only_tests_read_is_test_only_surface(self, trees):
        t = trees.files(
            {
                "lib.py": "LIMIT = 3\n",
                "tests/test_lib.py": (
                    "from lib import LIMIT\n\n\ndef test_limit():\n    assert LIMIT == 3\n"
                ),
            }
        )
        assert _messages(check_dead_constants(t)) == [
            "LIMIT = 3 is never referenced in production code — test-only surface. "
            "Tests that use it, to delete or rewrite: tests/test_lib.py "
            "(every test)"
        ]

    def test_a_test_import_alone_names_the_file(self, trees):
        """An import is no edge to the definition, so no test in the file is tied to it."""
        t = trees.files({"lib.py": "LIMIT = 3\n", "tests/test_lib.py": "from lib import LIMIT\n"})
        [message] = _messages(check_dead_constants(t))
        assert message.endswith("Tests that use it, to delete or rewrite: tests/test_lib.py")

    def test_a_production_read_keeps_it(self, trees):
        t = trees.files(
            {
                "lib.py": "LIMIT = 3\n\n\ndef cap(n):\n    return min(n, LIMIT)\n",
                "tests/test_lib.py": "from lib import LIMIT\n\n\ndef test_a():\n    assert LIMIT\n",
            }
        )
        assert _messages(check_dead_constants(t)) == []

    def test_a_production_import_keeps_it(self, trees):
        t = trees.files({"lib.py": "LIMIT = 3\n", "app.py": "from lib import LIMIT\n"})
        assert _messages(check_dead_constants(t)) == []

    def test_unreferenced_anywhere_keeps_its_message(self, trees):
        t = trees.files({"lib.py": "LIMIT = 3\n"})
        assert _messages(check_dead_constants(t)) == [
            "LIMIT = 3 is never referenced anywhere in the codebase"
        ]


class TestExceptionClass:
    def test_an_exception_only_tests_raise_is_test_only_surface(self, trees):
        t = trees.files(
            {
                "lib.py": "class StaleError(Exception):\n    pass\n",
                "tests/test_lib.py": (
                    "import pytest\nfrom lib import StaleError\n\n\n"
                    "def test_stale():\n    with pytest.raises(StaleError):\n"
                    "        raise StaleError()\n"
                ),
            }
        )
        assert _messages(check_dead_exceptions(t)) == [
            "StaleError (exception class) has no raise/except references in production code "
            "— test-only surface. Tests that use it, to delete or "
            "rewrite: tests/test_lib.py (every test)"
        ]

    def test_a_production_raise_keeps_it(self, trees):
        t = trees.files(
            {
                "lib.py": (
                    "class StaleError(Exception):\n    pass\n\n\n"
                    "def check():\n    raise StaleError()\n"
                ),
                "tests/test_lib.py": "from lib import StaleError\n",
            }
        )
        assert _messages(check_dead_exceptions(t)) == []


class TestRecordField:
    RECORD = (
        "from dataclasses import dataclass\n\n\n@dataclass\nclass Job:\n"
        "    name: str\n    retries: int\n\n\n"
        "def build():\n    return Job(name='a', retries=3)\n\n\n"
        "def show(job):\n    return job.name\n"
    )

    def test_a_field_only_tests_read_names_the_tests(self, trees):
        t = trees.files(
            {
                "lib.py": self.RECORD,
                "tests/test_lib.py": (
                    "from lib import build\n\n\n"
                    "def test_retries():\n    assert build().retries == 3\n\n\n"
                    "def test_name():\n    assert build().name\n"
                ),
            }
        )
        assert _messages(check_write_only_attributes(t)) == [
            "Job.retries is never read in production code; only tests read it, in "
            "tests/test_lib.py: test_retries() — test-only surface, vestigial field"
        ]

    def test_every_test_reading_it_is_still_not_a_whole_file(self, trees):
        t = trees.files(
            {
                "lib.py": self.RECORD,
                "tests/test_lib.py": (
                    "from lib import build\n\n\ndef test_retries():\n"
                    "    assert build().retries == 3\n"
                ),
            }
        )
        [message] = _messages(check_write_only_attributes(t))
        assert "in tests/test_lib.py: test_retries() —" in message

    def test_a_production_read_keeps_it(self, trees):
        record = self.RECORD + "\n\ndef delay(job):\n    return job.retries\n"
        t = trees.files(
            {
                "lib.py": record,
                "tests/test_lib.py": "def test_a():\n    assert True\n",
            }
        )
        assert _messages(check_write_only_attributes(t)) == []


class TestSurfaceShape:
    def test_a_file_the_graph_cannot_tie_is_still_named(self, trees):
        t = trees.files(
            {
                "lib.py": "def parse():\n    return 1\n",
                "tests/test_other.py": "def test_a():\n    assert True\n",
            }
        )
        surface = surface_of_units(t, [], ["tests/test_other.py"])
        assert surface.describe() == "tests/test_other.py"

    def test_a_production_file_is_never_named(self, trees):
        t = trees.files({"lib.py": "def parse():\n    return 1\n"})
        assert not surface_of_units(t, [], ["lib.py"])


class TestCalibration:
    """What 26-4's fleet measurement turned up once tests stopped keeping code alive."""

    def test_a_django_settings_variant_keeps_its_constants(self, trees):
        """A collectstatic_settings.py: loaded by a Dockerfile's DJANGO_SETTINGS_MODULE."""
        t = trees.files(
            {
                "config/__init__.py": "",
                "config/base_settings.py": "INSTALLED_APPS = ['app']\n",
                "config/build_settings.py": "from .base_settings import *\n\nSTORAGES = {}\n",
                "tests/test_build.py": (
                    "from config import build_settings\n\n\n"
                    "def test_storages():\n    assert build_settings.STORAGES == {}\n"
                ),
            }
        )
        assert [f for f in check_dead_constants(t) if f.file.startswith("config/")] == []

    def test_an_exception_production_builds_for_a_caller_to_raise_is_live(self, trees):
        """A GlobusSessionExpiredError: returned by a translator, then raised."""
        t = trees.files(
            {
                "lib.py": (
                    "class StaleError(Exception):\n    pass\n\n\n"
                    "def translate(error):\n    return StaleError(str(error))\n"
                ),
                "tests/test_lib.py": (
                    "from lib import StaleError\n\n\n"
                    "def test_a():\n    assert issubclass(StaleError, Exception)\n"
                ),
            }
        )
        assert _messages(check_dead_exceptions(t)) == []

    def test_an_exception_named_in_production_by_attribute_chain_is_live(self, trees):
        t = trees.files(
            {
                "pkg/__init__.py": "",
                "pkg/errors.py": "class StaleError(Exception):\n    pass\n",
                "pkg/audit.py": (
                    "import pkg.errors\n\n"
                    "ACTIONS = {f'error:{pkg.errors.StaleError.__name__}': 'renew'}\n"
                ),
            }
        )
        assert _messages(check_dead_exceptions(t), "pkg/errors.py") == []


class TestListingCap:
    def test_a_file_lists_five_tests_then_counts_the_rest(self, trees):
        tests = "".join(f"def test_{i}():\n    assert parse()\n\n\n" for i in range(7))
        t = trees.files(
            {
                "lib.py": LIB,
                "main.py": "from lib import keep\n\nkeep()\n",
                "tests/test_lib.py": (
                    "from lib import keep, parse\n\n\n" + tests + "def test_keep():\n    keep()\n"
                ),
            }
        )
        [message] = _messages(check_dead_code(t))
        assert message.endswith(
            "tests/test_lib.py: test_0(), test_1(), test_2(), test_3(), test_4() (+2 more)"
        )
