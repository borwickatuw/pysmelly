"""Tests for dead code extension checks (dead-exceptions, dead-dispatch-entries, orphaned-test-helpers)."""

from pysmelly.checks.dead import (
    check_dead_abstractions,
    check_dead_dispatch_entries,
    check_dead_exceptions,
    check_orphaned_test_helpers,
)
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

    def test_ignores_caught_single(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    pass
except AppError:
    pass
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

    def test_ignores_caught_in_tuple(self, trees):
        t = trees.code("""\
class AppError(Exception):
    pass

try:
    pass
except (ValueError, AppError):
    pass
""")
        findings = check_dead_exceptions(t)
        assert len(findings) == 0

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
        assert "dead entry" in findings[0].message

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
        assert "orphaned test helper" in findings[0].message

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
        assert "unused fixture" in findings[0].message

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

    def test_no_finding_when_subclassed(self, trees):
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
        assert len(findings) == 0

    def test_no_finding_when_subclassed_cross_file(self, trees):
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
