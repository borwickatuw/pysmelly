"""Tests for architectural checks."""

from pysmelly.checks.architecture import check_shared_mutable_module_state
from pysmelly.registry import Severity


class TestSharedMutableModuleState:
    def test_finds_mutation_via_direct_import(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
MIDDLEWARE.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert "MIDDLEWARE" in findings[0].message

    def test_finds_subscript_mutation(self, trees):
        t = trees.files(
            {
                "config/base.py": "SETTINGS = {}",
                "config/settings.py": """\
from base import SETTINGS
SETTINGS["debug"] = True
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert "SETTINGS" in findings[0].message

    def test_finds_augmented_assignment(self, trees):
        t = trees.files(
            {
                "config/base.py": "APPS = []",
                "config/settings.py": """\
from base import APPS
APPS += ["debug_toolbar"]
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert "APPS" in findings[0].message

    def test_ignores_mutation_inside_function(self, trees):
        """Mutations inside functions are runtime, not import-time."""
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
def setup():
    MIDDLEWARE.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_mutation_inside_class(self, trees):
        t = trees.files(
            {
                "config/base.py": "REGISTRY = {}",
                "config/settings.py": """\
from base import REGISTRY
class Setup:
    REGISTRY["key"] = "value"
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_non_mutable_assignments(self, trees):
        t = trees.files(
            {
                "config/base.py": 'VERSION = "1.0"',
                "config/settings.py": """\
from base import VERSION
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_mutation_of_local_variable(self, trees):
        """Mutations of locally-defined variables are not cross-file."""
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
LOCAL_LIST = []
LOCAL_LIST.append("something")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_test_files(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "tests/test_config.py": """\
from base import MIDDLEWARE
MIDDLEWARE.append("test_middleware")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_same_file_mutations(self, trees):
        """Mutations within the defining file are not cross-file."""
        t = trees.files(
            {
                "config/settings.py": """\
MIDDLEWARE = []
MIDDLEWARE.append("common_middleware")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 0

    def test_multiple_mutation_sites_grouped(self, trees):
        t = trees.files(
            {
                "config/base.py": "APPS = []",
                "config/settings.py": """\
from base import APPS
APPS.append("app1")
APPS.append("app2")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1

    def test_finding_anchored_at_definition(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
MIDDLEWARE.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert findings[0].file == "config/base.py"
        assert findings[0].line == 1

    def test_message_lists_mutating_files(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
MIDDLEWARE.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert "config/settings.py" in findings[0].message

    def test_severity_is_medium(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
MIDDLEWARE.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM

    def test_mutation_inside_if_at_module_scope(self, trees):
        """if blocks at module scope run at import time — detect mutations."""
        t = trees.files(
            {
                "config/base.py": "APPS = []",
                "config/settings.py": """\
from base import APPS
DEBUG = True
if DEBUG:
    APPS.append("debug_toolbar")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1

    def test_extend_method_detected(self, trees):
        t = trees.files(
            {
                "config/base.py": "APPS = []",
                "config/settings.py": """\
from base import APPS
APPS.extend(["app1", "app2"])
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1

    def test_insert_method_detected(self, trees):
        t = trees.files(
            {
                "config/base.py": "MIDDLEWARE = []",
                "config/settings.py": """\
from base import MIDDLEWARE
MIDDLEWARE.insert(0, "first_middleware")
""",
            }
        )
        findings = check_shared_mutable_module_state(t, verbose=False)
        assert len(findings) == 1
