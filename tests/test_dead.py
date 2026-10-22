"""Tests for dead code extension checks (dead-exceptions, dead-dispatch-entries, orphaned-test-helpers)."""

import ast
from dataclasses import replace
from pathlib import Path

from pysmelly.checks.dead import (
    check_broken_backends,
    check_dead_abstractions,
    check_dead_dispatch_entries,
    check_dead_exceptions,
    check_orphaned_test_helpers,
)
from pysmelly.context import AnalysisContext
from pysmelly.pytest_config import NO_PYTEST_CONFIG
from pysmelly.registry import Severity


class TestDeadExceptions:
    def test_finds_unused_exception(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 1
        assert "AppError" in findings[0].message
        assert "no raise/except references" in findings[0].message

    def test_ignores_raised_with_args(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

raise AppError("something went wrong")
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_bare_raise(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

raise AppError
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_caught_single_is_not_unreferenced(self, trees):
        """A catch is a reference; never being raised is the other kind's question."""
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    pass
except AppError:
    pass
""")
        findings = check_dead_exceptions(t)
        assert [f.severity for f in findings] == [Severity.MEDIUM]
        assert "no raise/except references" not in findings[0].message

    def test_caught_in_tuple_is_not_unreferenced(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    pass
except (ValueError, AppError):
    pass
""")
        findings = check_dead_exceptions(t)
        assert [f.severity for f in findings] == [Severity.MEDIUM]
        assert "no raise/except references" not in findings[0].message

    def test_ignores_imported_elsewhere(self, trees):
        t = trees.files(
            {
                "errors.py": """\
class AppError(Exception):
    pass
""",
                "main.py": """\
from errors import AppError
""",
            }
        )
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_subclassed(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

class SpecificError(AppError):
    pass

raise SpecificError("oops")
""")
        findings = check_dead_exceptions(t)
        # AppError is subclassed, SpecificError is raised
        assert len(findings) == 0

    def test_ignores_isinstance_target(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

if isinstance(e, AppError):
    handle()
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_dunder_all_member(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

__all__ = ["AppError"]
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_dict_value_reference(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

ERROR_MAP = {"app": AppError}
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_list_reference(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

ERRORS = [AppError]
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_call_arg_reference(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

register(AppError)
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_dotted_string_reference(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

HANDLERS = ["myapp.errors.AppError"]
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_cross_file_raise(self, trees):
        t = trees.files(
            {
                "errors.py": """\
class AppError(Exception):
    pass
""",
                "main.py": """\
from errors import AppError
raise AppError("fail")
""",
            }
        )
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_detects_error_suffix_base(self, trees):
        """Exception classes with custom Error-suffix bases are detected."""
        t = trees.code("""\
class CustomBaseError(Exception):
    pass

class SpecificErr(CustomBaseError):
    pass

raise CustomBaseError("x")
""")
        findings = check_dead_exceptions(t)
        # CustomBaseError is raised, SpecificErr has no references
        assert len(findings) == 1
        assert "SpecificErr" in findings[0].message

    def test_severity_is_high(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.HIGH


def _caught_never_raised(t) -> list[str]:
    """The caught-never-raised messages dead-exceptions gives for *t*."""
    return [f.message for f in check_dead_exceptions(t) if "is caught (" in f.message]


_ERRORS = """\
class AppError(Exception):
    pass
"""


class TestCaughtNeverRaised:
    """dead-exceptions' second kind: caught in production, raised nowhere in production."""

    def test_caught_never_raised_is_reported(self, trees):
        t = trees.files(
            {
                "errors.py": _ERRORS,
                "app.py": """\
from errors import AppError

def run():
    try:
        work()
    except AppError:
        return None
""",
            }
        )
        findings = check_dead_exceptions(t)
        assert len(findings) == 1
        assert findings[0].file == "errors.py"
        assert findings[0].line == 1
        assert findings[0].severity == Severity.MEDIUM
        assert findings[0].check == "dead-exceptions"
        assert findings[0].message == (
            "AppError (exception class) is caught (app.py:6) but no production code "
            "raises or constructs AppError — the handler is dead code"
        )

    def test_tuple_catch_is_reported(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    work()
except (ValueError, AppError):
    pass
""")
        assert len(_caught_never_raised(t)) == 1

    def test_tuple_catch_with_raise_is_live(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    raise AppError("boom")
except (ValueError, AppError):
    pass
""")
        assert _caught_never_raised(t) == []

    def test_catch_with_as_is_reported(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    work()
except AppError as err:
    log(err)
""")
        assert len(_caught_never_raised(t)) == 1

    def test_catch_with_as_and_raise_from_is_live(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

def run():
    try:
        work()
    except KeyError as err:
        raise AppError("missing") from err

try:
    run()
except AppError as err:
    log(err)
""")
        assert _caught_never_raised(t) == []

    def test_reraise_does_not_originate(self, trees):
        """A bare ``raise`` or ``raise err`` in the handler re-raises what was caught."""
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    work()
except AppError:
    cleanup()
    raise

try:
    work()
except AppError as err:
    raise err
""")
        messages = _caught_never_raised(t)
        assert len(messages) == 1
        assert "(test.py:6, :12)" in messages[0]

    def test_raise_cause_does_not_raise_the_cause(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    work()
except AppError as err:
    raise RuntimeError("wrapped") from err
""")
        assert len(_caught_never_raised(t)) == 1

    def test_module_attribute_raise_is_live(self, trees):
        t = trees.files(
            {
                "errors.py": _ERRORS,
                "app.py": """\
import errors

def run():
    raise errors.AppError("nope")

try:
    run()
except errors.AppError:
    pass
""",
            }
        )
        assert _caught_never_raised(t) == []

    def test_module_attribute_catch_is_reported(self, trees):
        t = trees.files(
            {
                "errors.py": _ERRORS,
                "app.py": """\
import errors

try:
    work()
except errors.AppError:
    pass
""",
            }
        )
        assert len(_caught_never_raised(t)) == 1

    def test_raised_subclass_makes_the_base_live(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

class NotFound(AppError):
    pass

class Gone(NotFound):
    pass

def fetch():
    raise Gone()

try:
    fetch()
except AppError:
    pass
""")
        assert _caught_never_raised(t) == []

    def test_unraised_subclasses_are_named(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

class NotFound(AppError):
    pass

try:
    work()
except AppError:
    pass
""")
        messages = _caught_never_raised(t)
        assert len(messages) == 1
        assert "raises or constructs AppError or a subclass (NotFound)" in messages[0]

    def test_raised_base_does_not_make_a_caught_subclass_live(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

class NotFound(AppError):
    pass

def fetch():
    raise AppError()

try:
    fetch()
except NotFound:
    pass
""")
        messages = _caught_never_raised(t)
        assert len(messages) == 1
        assert messages[0].startswith("NotFound ")

    def test_variable_raise_is_live(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

def run():
    err = AppError("built first")
    raise err

try:
    run()
except AppError:
    pass
""")
        assert _caught_never_raised(t) == []

    def test_test_only_raise_does_not_make_it_live(self, trees):
        t = trees.files(
            {
                "errors.py": _ERRORS,
                "app.py": """\
from errors import AppError

def run():
    try:
        work()
    except AppError:
        return None
""",
                "tests/test_app.py": """\
from errors import AppError

def test_run(monkeypatch):
    def boom():
        raise AppError("simulated")
    monkeypatch.setattr("app.work", boom)
    err = AppError("again")
    raise err
""",
            }
        )
        messages = _caught_never_raised(t)
        assert len(messages) == 1
        assert (
            "; only tests raise it (tests/test_app.py:5, :8), and a test-only raise does not "
            "make a production handler live — the handler is dead code"
        ) in messages[0]

    def test_construction_counts_as_a_possible_raise(self, trees):
        """``return AppError(...)`` hands the exception to a raise we do not follow."""
        t = trees.code("""\
class AppError(Exception):
    pass

def wrap(exc):
    return AppError(str(exc))

try:
    work()
except AppError:
    pass
""")
        assert _caught_never_raised(t) == []

    def test_value_reference_counts_as_a_possible_raise(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

ERRORS = {"app": AppError}

try:
    work()
except AppError:
    pass
""")
        assert _caught_never_raised(t) == []

    def test_dotted_string_counts_as_a_possible_raise(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

PLUGIN_ERROR = "myapp.errors.AppError"

try:
    work()
except AppError:
    pass
""")
        assert _caught_never_raised(t) == []

    def test_type_positions_do_not_count_as_raises(self, trees):
        """isinstance, a class base, an annotation and suppress() raise nothing."""
        t = trees.code("""\
from contextlib import suppress

class AppError(Exception):
    pass

class Report:
    error: AppError | None = None

def handle(exc: Exception) -> AppError | None:
    if isinstance(exc, AppError):
        return exc
    return None

with suppress(AppError):
    work()
""")
        messages = _caught_never_raised(t)
        assert len(messages) == 1
        assert "is caught (test.py:14)" in messages[0]

    def test_catch_only_in_tests_is_not_reported(self, trees):
        t = trees.files(
            {
                "errors.py": _ERRORS,
                "tests/test_errors.py": """\
from errors import AppError

def test_catch():
    try:
        work()
    except AppError:
        pass
""",
            }
        )
        assert _caught_never_raised(t) == []

    def test_class_defined_in_tests_is_not_reported(self, trees):
        t = trees.files(
            {
                "tests/test_errors.py": """\
class FakeError(Exception):
    pass

def test_catch():
    try:
        raise FakeError()
    except FakeError:
        pass
""",
                "app.py": """\
try:
    work()
except FakeError:
    pass
""",
            }
        )
        assert _caught_never_raised(t) == []

    def test_builtin_named_class_is_not_reported(self, trees):
        """``except TimeoutError`` elsewhere catches the builtin, not the shadowing class."""
        t = trees.code("""\
class TimeoutError(Exception):
    pass

try:
    work()
except TimeoutError:
    pass
""")
        assert _caught_never_raised(t) == []


class TestDeadDispatchEntries:
    def test_finds_unreferenced_key(self, trees):
        t = trees.code("""\
def handle_create():
    pass

def handle_update():
    pass

def handle_delete():
    pass

HANDLERS = {
    "create": handle_create,
    "update": handle_update,
    "delete": handle_delete,
}

action = "create"
action2 = "update"
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 1
        assert '"delete"' in findings[0].message
        assert "never appears as a string elsewhere" in findings[0].message

    def test_ignores_when_all_keys_referenced(self, trees):
        t = trees.code("""\
def handle_create():
    pass

def handle_update():
    pass

def handle_delete():
    pass

HANDLERS = {
    "create": handle_create,
    "update": handle_update,
    "delete": handle_delete,
}

action = "create"
action2 = "update"
action3 = "delete"
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_ignores_small_dicts(self, trees):
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

D = {"a": fa, "b": fb}
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_ignores_non_dispatch_dicts(self, trees):
        """Dicts with non-Name values (e.g., string values) are not dispatch dicts."""
        t = trees.code("""\
LABELS = {
    "create": "Create",
    "update": "Update",
    "delete": "Delete",
}
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_suppresses_when_dict_passed_to_function(self, trees):
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

def fc():
    pass

D = {"a": fa, "b": fb, "c": fc}
process(D)
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_suppresses_when_dict_is_returned(self, trees):
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

def fc():
    pass

def get_handlers():
    D = {"a": fa, "b": fb, "c": fc}
    return D
""")
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_cross_file_key_reference(self, trees):
        t = trees.files(
            {
                "handlers.py": """\
def handle_create():
    pass

def handle_update():
    pass

def handle_delete():
    pass

HANDLERS = {
    "create": handle_create,
    "update": handle_update,
    "delete": handle_delete,
}
""",
                "main.py": """\
action = "create"
action2 = "update"
action3 = "delete"
""",
            }
        )
        findings = check_dead_dispatch_entries(t)
        assert len(findings) == 0

    def test_key_in_definition_not_self_counted(self, trees):
        """The key string in the dict definition itself should not count as a reference."""
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

def fc():
    pass

HANDLERS = {
    "alpha": fa,
    "beta": fb,
    "gamma": fc,
}
""")
        findings = check_dead_dispatch_entries(t)
        # All 3 keys appear only in the dict itself, so all 3 are dead
        assert len(findings) == 3

    def test_multiple_dead_entries(self, trees):
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

def fc():
    pass

def fd():
    pass

HANDLERS = {
    "a": fa,
    "b": fb,
    "c": fc,
    "d": fd,
}

x = "a"
""")
        findings = check_dead_dispatch_entries(t)
        # "b", "c", "d" are dead
        assert len(findings) == 3

    def test_severity_is_medium(self, trees):
        t = trees.code("""\
def fa():
    pass

def fb():
    pass

def fc():
    pass

HANDLERS = {
    "alpha": fa,
    "beta": fb,
    "gamma": fc,
}
""")
        findings = check_dead_dispatch_entries(t)
        assert all(f.severity == Severity.MEDIUM for f in findings)


class TestOrphanedTestHelpers:
    def test_finds_uncalled_helper(self, trees):
        t = trees.files(
            {
                "tests/test_models.py": """\
def make_user():
    return {"name": "alice"}

def test_something():
    pass
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 1
        assert "make_user()" in findings[0].message
        assert "has no callers" in findings[0].message

    def test_ignores_called_helper(self, trees):
        t = trees.files(
            {
                "tests/test_models.py": """\
def make_user():
    return {"name": "alice"}

def test_create_user():
    user = make_user()
    assert user["name"] == "alice"
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_ignores_test_functions(self, trees):
        t = trees.files(
            {
                "tests/test_models.py": """\
def test_create():
    pass

def test_delete():
    pass
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_ignores_used_fixture(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def client():
    return TestClient()
""",
                "tests/test_api.py": """\
def test_index(client):
    assert client.get("/").status_code == 200
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_finds_unused_fixture(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def old_client():
    return OldTestClient()
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 1
        assert "old_client" in findings[0].message
        assert "is never requested" in findings[0].message

    def test_ignores_imported_helper(self, trees):
        t = trees.files(
            {
                "tests/helpers.py": """\
def make_user():
    return {"name": "alice"}
""",
                "tests/test_models.py": """\
from helpers import make_user

def test_something():
    user = make_user()
""",
            }
        )
        # make_user is imported elsewhere, so not orphaned
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_fixture_used_by_another_fixture(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def db_connection():
    return connect()

@pytest.fixture
def client(db_connection):
    return TestClient(db=db_connection)
""",
                "tests/test_api.py": """\
def test_index(client):
    assert client.get("/").status_code == 200
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        # db_connection is used as param in client fixture, client is used in test
        assert len(findings) == 0

    def test_non_test_file_helpers_not_flagged(self, trees):
        """Functions in non-test files are handled by dead-code, not this check."""
        t = trees.files(
            {
                "src/utils.py": """\
def make_thing():
    return {}
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_decorated_non_fixture_suppressed(self, trees):
        """Decorated functions (not fixtures) may be framework-registered."""
        t = trees.files(
            {
                "tests/test_models.py": """\
@app.callback
def on_event():
    pass

def test_something():
    pass
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_ignores_methods_in_test_classes(self, trees):
        """Methods inside test classes are not flagged (they're part of the class)."""
        t = trees.files(
            {
                "tests/test_models.py": """\
class TestUser:
    def make_user(self):
        return {"name": "alice"}

    def test_create(self):
        user = self.make_user()
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        # make_user is a method, not a top-level function
        assert len(findings) == 0

    def test_severity_is_medium(self, trees):
        t = trees.files(
            {
                "tests/test_models.py": """\
def make_user():
    return {"name": "alice"}

def test_something():
    pass
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM

    def test_skips_fixture_findings_when_test_files_excluded(self, trees):
        """When test files are excluded, conftest fixtures can't be checked."""
        t = trees.files(
            {
                "conftest.py": """\
import pytest

@pytest.fixture
def client():
    return TestClient()

@pytest.fixture
def db():
    return connect()
""",
            }
        )
        # Only conftest.py, no test files — fixtures should not be flagged
        findings = check_orphaned_test_helpers(t)
        assert len(findings) == 0

    def test_finds_unused_fixture_when_the_suite_is_test_classes(self, trees):
        """A suite of test classes is parsed tests too: its methods request fixtures."""
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def client():
    return TestClient()

@pytest.fixture
def old_client():
    return OldTestClient()
""",
                "tests/test_api.py": """\
class TestApi:
    def test_index(self, client):
        assert client.get("/").status_code == 200
""",
            }
        )
        findings = check_orphaned_test_helpers(t)
        assert [f.message for f in findings] == [
            "old_client fixture in conftest.py is never requested"
        ]

    def test_skips_fixtures_when_only_test_support_is_parsed(self, trees):
        """Test-role files with no collected test (helpers, conftest) do not consume fixtures."""
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def client():
    return TestClient()
""",
                "tests/helpers.py": """\
import pytest

class Builder:
    def build(self):
        return 1
""",
            }
        )
        assert check_orphaned_test_helpers(t) == []


def _orphans(ctx: AnalysisContext) -> list[str]:
    return [f"{f.file}:{f.line}: {f.message}" for f in check_orphaned_test_helpers(ctx)]


def _under_testpaths(files: dict[str, str], **config: tuple[str, ...]) -> AnalysisContext:
    """A context whose pytest config sets ``testpaths = ["tests"]``."""
    trees = {Path(name): ast.parse(code) for name, code in files.items()}
    pytest_config = replace(NO_PYTEST_CONFIG, testpaths=("tests",), **config)
    return AnalysisContext(trees, verbose=False, pytest_config=pytest_config)


class TestOrphanedTestHelpersPytestInvokes:
    """What pytest calls without a request is never orphaned (24-6)."""

    def test_autouse_fixture_is_not_reported(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr("socket.socket", None)

@pytest.fixture(scope="session", autouse=True)
def _fast_retries():
    return None
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == []

    def test_unrequested_non_autouse_fixture_is_still_reported(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture(autouse=True)
def _no_network():
    return None

@pytest.fixture(autouse=False)
def old_client():
    return None

@pytest.fixture(scope="module")
def stale_db():
    return None
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == [
            "tests/conftest.py:8: old_client fixture in conftest.py is never requested",
            "tests/conftest.py:12: stale_db fixture in conftest.py is never requested",
        ]

    def test_fixture_requested_by_string_is_not_reported(self, trees):
        """usefixtures (decorator or pytestmark) and getfixturevalue request by name."""
        t = trees.files(
            {
                "tests/conftest.py": """\
import pytest

@pytest.fixture
def by_decorator():
    return None

@pytest.fixture
def by_pytestmark():
    return None

@pytest.fixture
def by_lookup():
    return None

@pytest.fixture
def unrequested():
    return None
""",
                "tests/test_api.py": """\
import pytest
from pytest import mark

pytestmark = [mark.usefixtures("by_pytestmark")]

@pytest.mark.usefixtures("by_decorator")
def test_index(request):
    request.getfixturevalue("by_lookup")
""",
            }
        )
        assert _orphans(t) == [
            "tests/conftest.py:16: unrequested fixture in conftest.py is never requested"
        ]

    def test_fixture_is_resolved_through_imports(self, trees):
        """``from pytest import fixture`` declares fixtures as ``@pytest.fixture`` does."""
        t = trees.files(
            {
                "tests/conftest.py": """\
from pytest import fixture
import pytest as pt

@fixture(autouse=True)
def _isolated():
    return None

@fixture
def unused_one():
    return None

@pt.fixture
def unused_two():
    return None
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == [
            "tests/conftest.py:9: unused_one fixture in conftest.py is never requested",
            "tests/conftest.py:13: unused_two fixture in conftest.py is never requested",
        ]

    def test_conftest_hook_is_not_reported(self, trees):
        t = trees.files(
            {
                "tests/conftest.py": """\
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: slow tests")

def pytest_collection_modifyitems(config, items):
    items.sort(key=lambda item: item.name)
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == []

    def test_hook_named_function_in_a_test_module_is_reported(self, trees):
        """pytest registers no test module as a plugin, so no hook there is ever called."""
        t = trees.files(
            {
                "tests/test_api.py": """\
def pytest_configure(config):
    pass

def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == [
            "tests/test_api.py:1: pytest_configure() in test_api.py has no callers"
        ]

    def test_hook_in_a_pytest_plugins_module_is_not_reported(self):
        t = _under_testpaths(
            {
                "tests/conftest.py": 'pytest_plugins = ["tests.plugins.timing"]\n',
                "tests/plugins/timing.py": """\
def pytest_runtest_setup(item):
    item.start = 0

def unused_helper():
    return 1
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == [
            "tests/plugins/timing.py:4: unused_helper() in timing.py has no callers"
        ]

    def test_hook_in_a_pytest_plugins_module_without_testpaths(self, trees):
        """With pytest's defaults the declaration alone makes the plugin test support."""
        t = trees.files(
            {
                "tests/conftest.py": 'pytest_plugins = ["tests.plugins.timing"]\n',
                "tests/plugins/timing.py": """\
def pytest_runtest_setup(item):
    item.start = 0

def unused_helper():
    return 1
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == [
            "tests/plugins/timing.py:4: unused_helper() in timing.py has no callers"
        ]

    def test_pytest_plugins_string_and_transitive_declarations(self):
        """A single string names one plugin, and a plugin's own declaration is followed."""
        t = _under_testpaths(
            {
                "tests/conftest.py": 'pytest_plugins = "tests.plugins.outer"\n',
                "tests/plugins/outer.py": """\
pytest_plugins = ("tests.plugins.inner",)

def pytest_sessionstart(session):
    pass
""",
                "tests/plugins/inner.py": """\
def pytest_sessionfinish(session, exitstatus):
    pass
""",
                "tests/test_api.py": """\
def test_index():
    pass
""",
            }
        )
        assert _orphans(t) == []

    def test_hook_in_a_pytest11_entry_point_module_is_not_reported(self):
        # Only a test imports plugin.py: test support under testpaths, where
        # location alone is no evidence (34-1).
        files = {
            "tests/support/plugin.py": """\
def pytest_report_header(config):
    return "header"
""",
            "tests/test_api.py": """\
from tests.support import plugin

def test_index():
    pass
""",
        }
        plain = _under_testpaths(files)
        registered = _under_testpaths(files, entry_point_plugins=("tests.support.plugin",))
        assert _orphans(plain) == [
            "tests/support/plugin.py:1: pytest_report_header() in plugin.py has no callers"
        ]
        assert _orphans(registered) == []


class TestDeadAbstraction:
    def test_finds_abc_with_no_subclasses(self, trees):
        t = trees.code("""\
from abc import ABC, abstractmethod

class BasePlugin(ABC):
    @abstractmethod
    def initialize(self, config):
        pass

    @abstractmethod
    def execute(self):
        pass
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 1
        assert "BasePlugin" in findings[0].message
        assert "2 abstract method(s)" in findings[0].message

    def test_single_implementation_fires_low(self, trees):
        """An ABC with exactly one implementer is indirection without
        polymorphism — flagged at LOW."""
        t = trees.code("""\
from abc import ABC, abstractmethod

class BaseHandler(ABC):
    @abstractmethod
    def handle(self, data):
        pass

class ConcreteHandler(BaseHandler):
    def handle(self, data):
        return data
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.LOW
        assert "exactly one implementation (ConcreteHandler)" in findings[0].message

    def test_no_finding_with_two_implementations(self, trees):
        t = trees.code("""\
from abc import ABC, abstractmethod

class BaseHandler(ABC):
    @abstractmethod
    def handle(self, data):
        pass

class JsonHandler(BaseHandler):
    def handle(self, data):
        return data

class XmlHandler(BaseHandler):
    def handle(self, data):
        return data
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 0

    def test_single_impl_cross_file_fires_low(self, trees):
        t = trees.files(
            {
                "base.py": """\
from abc import ABC, abstractmethod

class BaseService(ABC):
    @abstractmethod
    def run(self):
        pass
""",
                "impl.py": """\
from base import BaseService

class MyService(BaseService):
    def run(self):
        return "done"
""",
            }
        )
        findings = check_dead_abstractions(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.LOW
        assert "MyService" in findings[0].message

    def test_single_impl_exported_abc_ok(self, trees):
        """An ABC in __all__ is a published extension point — external
        implementers are expected."""
        t = trees.code("""\
from abc import ABC, abstractmethod

__all__ = ["BaseHandler"]

class BaseHandler(ABC):
    @abstractmethod
    def handle(self, data):
        pass

class ConcreteHandler(BaseHandler):
    def handle(self, data):
        return data
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 0

    def test_finds_metaclass_abc(self, trees):
        t = trees.code("""\
from abc import ABCMeta, abstractmethod

class BaseMiddleware(metaclass=ABCMeta):
    @abstractmethod
    def before_request(self, request):
        pass
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 1

    def test_finds_abc_detected_by_abstractmethod(self, trees):
        t = trees.code("""\
from abc import abstractmethod

class Serializer:
    @abstractmethod
    def serialize(self, data):
        pass

    @abstractmethod
    def deserialize(self, raw):
        pass
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 1
        assert "2 abstract method(s)" in findings[0].message

    def test_no_finding_when_in_dunder_all(self, trees):
        t = trees.code("""\
from abc import ABC, abstractmethod

__all__ = ["BasePlugin"]

class BasePlugin(ABC):
    @abstractmethod
    def run(self):
        pass
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 0

    def test_no_finding_concrete_class(self, trees):
        """Non-abstract classes should not be flagged."""
        t = trees.code("""\
class RegularClass:
    def do_stuff(self):
        pass
""")
        findings = check_dead_abstractions(t)
        assert len(findings) == 0


class TestBrokenBackends:
    def test_finds_all_not_implemented(self, trees):
        t = trees.code("""\
class RedisBackend:
    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def clear(self):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 1
        assert "RedisBackend" in findings[0].message
        assert "4 methods" in findings[0].message

    def test_skips_abc(self, trees):
        t = trees.code("""\
from abc import ABC

class Backend(ABC):
    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 0

    def test_skips_abstractmethod(self, trees):
        t = trees.code("""\
from abc import abstractmethod

class Backend:
    @abstractmethod
    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 0

    def test_skips_one_real_method(self, trees):
        t = trees.code("""\
class Backend:
    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError

    def ping(self):
        return True
""")
        findings = check_broken_backends(t)
        assert len(findings) == 0

    def test_skips_single_method(self, trees):
        t = trees.code("""\
class Backend:
    def get(self, key):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 0

    def test_init_not_counted(self, trees):
        t = trees.code("""\
class Backend:
    def __init__(self, url):
        self.url = url

    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 1
        assert "2 methods" in findings[0].message

    def test_docstring_plus_raise_still_flagged(self, trees):
        t = trees.code("""\
class Backend:
    def get(self, key):
        \"\"\"Get a value.\"\"\"
        raise NotImplementedError

    def set(self, key, value):
        \"\"\"Set a value.\"\"\"
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 1

    def test_skips_abcmeta_metaclass(self, trees):
        t = trees.code("""\
from abc import ABCMeta

class Backend(metaclass=ABCMeta):
    def get(self, key):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError
""")
        findings = check_broken_backends(t)
        assert len(findings) == 0
