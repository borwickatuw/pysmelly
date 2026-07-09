"""Tests for architectural checks."""

from pysmelly.checks.architecture import (
    check_anemic_domain,
    check_feature_envy,
    check_shared_mutable_module_state,
    check_temporal_coupling,
    check_write_only_attributes,
    check_write_only_globals,
)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
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
        findings = check_shared_mutable_module_state(t)
        assert len(findings) == 1


class TestWriteOnlyAttributes:
    def test_finds_unread_dataclass_field(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30
    vestigial_field: str = "never_used"

def use_config(c):
    return c.timeout
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert "vestigial_field" in findings[0].message
        assert "Config" in findings[0].message

    def test_no_finding_when_field_is_read(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30
    retries: int = 3

def use_config(c):
    print(c.timeout, c.retries)
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 0

    def test_cross_file_read(self, trees):
        t = trees.files(
            {
                "config.py": """\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30
    secret_field: str = "hidden"
""",
                "app.py": """\
from config import Config

c = Config()
print(c.timeout)
""",
            }
        )
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert "secret_field" in findings[0].message

    def test_skips_private_fields(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    _internal: int = 0
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 0

    def test_no_finding_non_dataclass(self, trees):
        t = trees.code("""\
class Config:
    timeout: int = 30
    vestigial: str = "unused"
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 0

    def test_field_read_in_own_method(self, trees):
        """Field read by the class's own methods should not be flagged."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30
    max_retries: int = 3

    def validate(self):
        if self.timeout <= 0:
            raise ValueError("bad timeout")
        if self.max_retries < 0:
            raise ValueError("bad retries")
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 0

    def test_multiple_vestigial_fields(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class FetchConfig:
    base_url: str = ""
    async_max_connections: int = 100
    cache_compression: bool = False
    experimental_features: dict = None

def fetch(config):
    return config.base_url
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 3
        names = {f.message.split(".")[1].split(" ")[0] for f in findings}
        assert "async_max_connections" in names
        assert "cache_compression" in names
        assert "experimental_features" in names

    def test_dataclass_with_call_decorator(self, trees):
        """@dataclass(frozen=True) should also be detected."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    timeout: int = 30
    unused_field: str = "vestigial"

def use(c):
    return c.timeout
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert "unused_field" in findings[0].message

    def test_asdict_serialized_class_skipped(self, trees):
        """A dataclass that calls asdict(self) reads every field via
        serialization — nothing is write-only."""
        t = trees.code("""\
from dataclasses import asdict, dataclass

@dataclass
class Manifest:
    bucket: str = ""
    snapshot_id: str = ""

    def to_dict(self):
        return asdict(self)
""")
        findings = check_write_only_attributes(t)
        assert len(findings) == 0

    def test_exported_class_downgraded_to_low(self, trees):
        """Classes listed in __all__ are public API — still flagged, but
        at LOW with the export noted rather than suppressed."""
        t = trees.files(
            {
                "__init__.py": """\
from .client import Config
__all__ = ["Config"]
""",
                "client.py": """\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30
    vestigial_field: str = "never_used"

def use_config(c):
    return c.timeout
""",
            }
        )
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.LOW
        assert "vestigial_field" in findings[0].message
        assert "__all__" in findings[0].message

    def test_still_flags_class_not_in_dunder_all(self, trees):
        """Non-exported classes are still checked normally."""
        t = trees.files(
            {
                "__init__.py": """\
from .client import Config
__all__ = ["Config"]
""",
                "client.py": """\
from dataclasses import dataclass

@dataclass
class Config:
    timeout: int = 30

@dataclass
class InternalState:
    cache_key: str = ""

def use(c):
    return c.timeout
""",
            }
        )
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert "InternalState" in findings[0].message
        assert findings[0].severity == Severity.MEDIUM

    def test_dunder_all_as_tuple(self, trees):
        """__all__ defined as a tuple should also work."""
        t = trees.files(
            {
                "__init__.py": """\
__all__ = ("Config",)
""",
                "models.py": """\
from dataclasses import dataclass

@dataclass
class Config:
    unused: str = ""
""",
            }
        )
        findings = check_write_only_attributes(t)
        assert len(findings) == 1
        assert findings[0].severity == Severity.LOW


class TestTemporalCoupling:
    def test_finds_coupling(self, trees):
        t = trees.code("""\
class Server:
    def __init__(self):
        self.host = "localhost"

    def connect(self):
        self.connection = make_conn()

    def handle_request(self):
        return self.connection.send("hello")

    def close(self):
        self.connection.close()
""")
        findings = check_temporal_coupling(t)
        assert len(findings) >= 1
        messages = " ".join(f.message for f in findings)
        assert "connection" in messages
        assert "connect()" in messages

    def test_none_init_unguarded_deref_fires(self, trees):
        """self.x = None in __init__ is a placeholder, not initialization:
        methods dereferencing it unguarded before the setter runs crash."""
        t = trees.code("""\
class Server:
    def __init__(self):
        self.connection = None

    def connect(self):
        self.connection = make_conn()

    def handle_request(self):
        return self.connection.send("hello")

    def close(self):
        self.connection.close()
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 2
        messages = " ".join(f.message for f in findings)
        assert "only sets to None" in messages
        assert "connect()" in messages

    def test_none_init_guarded_deref_ok(self, trees):
        """A None-guard before the dereference shows the coupling is
        handled."""
        t = trees.code("""\
class Server:
    def __init__(self):
        self.connection = None

    def connect(self):
        self.connection = make_conn()

    def handle_request(self):
        if self.connection is None:
            raise RuntimeError("not connected")
        return self.connection.send("hello")

    def close(self):
        if not self.connection:
            return
        self.connection.close()
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_none_init_value_read_ok(self, trees):
        """Plain value reads of a None-initialized attr (returns,
        comparisons) don't crash — only dereferences fire."""
        t = trees.code("""\
class Server:
    def __init__(self):
        self.connection = None

    def connect(self):
        self.connection = make_conn()

    def get_connection(self):
        return self.connection

    def is_connected(self):
        return self.connection == "open"
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_init_set_to_real_value_ok(self, trees):
        """Attributes initialized to a real value in __init__ are fine."""
        t = trees.code("""\
class Server:
    def __init__(self):
        self.connection = make_conn()

    def reconnect(self):
        self.connection = make_conn()

    def handle_request(self):
        return self.connection.send("hello")

    def close(self):
        self.connection.close()
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_ignores_same_method_set_and_read(self, trees):
        t = trees.code("""\
class Worker:
    def __init__(self):
        self.x = 0

    def process(self):
        self.result = compute()
        return self.result

    def cleanup(self):
        pass
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_skips_property(self, trees):
        t = trees.code("""\
class Server:
    def __init__(self):
        pass

    def connect(self):
        self.connection = make_conn()

    @property
    def status(self):
        return self.connection

    def close(self):
        self.connection.close()
""")
        findings = check_temporal_coupling(t)
        # property methods are skipped
        assert not any("status()" in f.message for f in findings)

    def test_skips_classmethod(self, trees):
        t = trees.code("""\
class Factory:
    def __init__(self):
        pass

    def setup(self):
        self.data = load()

    @classmethod
    def create(cls):
        return cls()

    def process(self):
        return self.data
""")
        findings = check_temporal_coupling(t)
        assert not any("create()" in f.message for f in findings)

    def test_skips_small_class(self, trees):
        t = trees.code("""\
class Small:
    def setup(self):
        self.data = load()

    def process(self):
        return self.data
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_skips_dataclass(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    host: str
    port: int

    def setup(self):
        self.connection = connect()

    def query(self):
        return self.connection

    def close(self):
        pass
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_multiple_couplings(self, trees):
        t = trees.code("""\
class App:
    def __init__(self):
        pass

    def init_db(self):
        self.db = connect_db()

    def init_cache(self):
        self.cache = connect_cache()

    def run(self):
        self.db.query()
        self.cache.get("x")
""")
        findings = check_temporal_coupling(t)
        assert len(findings) >= 2

    def test_skips_testcase_setup(self, trees):
        """TestCase.setUp() is framework-guaranteed initialization."""
        t = trees.code("""\
from django.test import TestCase

class MyTests(TestCase):
    def setUp(self):
        self.client = self.client_class()

    def test_index(self):
        response = self.client.get("/")

    def test_about(self):
        response = self.client.get("/about")
""")
        findings = check_temporal_coupling(t)
        assert not any("client" in f.message for f in findings)


class TestModuleTemporalCoupling:
    def test_global_set_in_one_func_read_in_others(self, trees):
        t = trees.code("""\
CURRENT_USER = None

def login(name):
    global CURRENT_USER
    CURRENT_USER = name

def get_orders():
    return [o for o in ORDERS if o["user"] == CURRENT_USER]
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 1
        assert "CURRENT_USER" in findings[0].message
        assert "login()" in findings[0].message
        assert "get_orders()" in findings[0].message

    def test_lazy_init_getter_ok(self, trees):
        """Assigner and reader are the same function — the standard
        lazy-singleton getter."""
        t = trees.code("""\
_client = None

def get_client():
    global _client
    if _client is None:
        _client = make_client()
    return _client
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_guarded_reader_ok(self, trees):
        t = trees.code("""\
CURRENT_USER = None

def login(name):
    global CURRENT_USER
    CURRENT_USER = name

def get_orders():
    if CURRENT_USER is None:
        raise RuntimeError("not logged in")
    return [o for o in ORDERS if o["user"] == CURRENT_USER]
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_non_none_init_ok(self, trees):
        """Globals initialized to a real default aren't call-order traps."""
        t = trees.code("""\
CURRENT_LOCALE = "en_US"

def set_locale(loc):
    global CURRENT_LOCALE
    CURRENT_LOCALE = loc

def format_price(p):
    return format_for(CURRENT_LOCALE, p)
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0

    def test_never_assigned_via_global_ok(self, trees):
        """A module constant that's only read is not temporal coupling."""
        t = trees.code("""\
DEFAULT_USER = None

def get_user(u=None):
    return u or DEFAULT_USER
""")
        findings = check_temporal_coupling(t)
        assert len(findings) == 0


class TestWriteOnlyGlobals:
    def test_appended_never_read(self, trees):
        t = trees.code("""\
EVENTS_LIST = []

def log_event(e):
    EVENTS_LIST.append(e)

def reset():
    EVENTS_LIST.clear()
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 1
        assert "EVENTS_LIST" in findings[0].message
        assert "never read" in findings[0].message

    def test_read_via_iteration_ok(self, trees):
        t = trees.code("""\
EVENTS_LIST = []

def log_event(e):
    EVENTS_LIST.append(e)

def report():
    return [e for e in EVENTS_LIST]
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 0

    def test_read_in_other_file_ok(self, trees):
        t = trees.files(
            {
                "state.py": """\
EVENTS_LIST = []

def log_event(e):
    EVENTS_LIST.append(e)
""",
                "report.py": """\
from state import EVENTS_LIST

def report():
    return len(EVENTS_LIST)
""",
            }
        )
        findings = check_write_only_globals(t)
        assert len(findings) == 0

    def test_subscript_writes_count_as_mutation(self, trees):
        t = trees.code("""\
METRICS = {}

def record(key, value):
    METRICS[key] = value
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 1
        assert "METRICS" in findings[0].message

    def test_subscript_read_ok(self, trees):
        t = trees.code("""\
METRICS = {}

def record(key, value):
    METRICS[key] = METRICS[key] + value
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 0

    def test_module_scope_population_only_ok(self, trees):
        """A registry populated at module scope and never mutated from a
        function is dead-constants territory, not this check's."""
        t = trees.code("""\
REGISTRY = {}
REGISTRY["a"] = 1
REGISTRY["b"] = 2
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 0

    def test_exported_name_ok(self, trees):
        t = trees.code("""\
__all__ = ["EVENTS_LIST"]
EVENTS_LIST = []

def log_event(e):
    EVENTS_LIST.append(e)
""")
        findings = check_write_only_globals(t)
        assert len(findings) == 0


class TestFeatureEnvy:
    def test_finds_envy(self, trees):
        t = trees.code("""\
class Formatter:
    def render(self, document):
        title = document.title
        body = document.body
        author = document.author
        date = document.date
        return f"{title} by {author} on {date}: {body}"
""")
        findings = check_feature_envy(t)
        assert len(findings) == 1
        assert "document" in findings[0].message
        assert "Formatter.render()" in findings[0].message

    def test_ignores_framework_hooks(self, trees):
        """Django admin hooks access params more than self by design."""
        t = trees.code("""\
class MyAdmin:
    def formfield_for_foreignkey(self, db_field, request):
        x = db_field.name
        y = db_field.remote_field
        z = db_field.related_model
        w = db_field.formfield
        return w

    def add_arguments(self, parser):
        parser.add_argument("--dry-run")
        parser.add_argument("--verbose")
        parser.add_argument("--output")
        parser.add_argument("--format")
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_ignores_balanced_access(self, trees):
        t = trees.code("""\
class Formatter:
    def render(self, document):
        title = document.title
        body = document.body
        tmpl = self.template
        style = self.style
        fmt = self.format
        return tmpl.format(title=title, body=body, style=style, fmt=fmt)
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_ignores_dunder(self, trees):
        t = trees.code("""\
class MyClass:
    def __init__(self, other):
        self.x = other.a
        self.y = other.b
        self.z = other.c
        self.w = other.d
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_ignores_staticmethod(self, trees):
        t = trees.code("""\
class MyClass:
    @staticmethod
    def process(obj):
        return obj.a + obj.b + obj.c + obj.d
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_ignores_few_accesses(self, trees):
        t = trees.code("""\
class MyClass:
    def process(self, obj):
        return obj.a + obj.b
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_skips_test_files(self, trees):
        t = trees.files(
            {
                "tests/test_envy.py": """\
class TestFormatter:
    def test_render(self, document):
        x = document.title
        y = document.body
        z = document.author
        w = document.date
"""
            }
        )
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_message_identifies_envied_param(self, trees):
        t = trees.code("""\
class Reporter:
    def summarize(self, stats):
        return f"{stats.mean} {stats.median} {stats.mode} {stats.count}"
""")
        findings = check_feature_envy(t)
        assert len(findings) == 1
        assert "'stats'" in findings[0].message

    def test_ignores_classmethod(self, trees):
        t = trees.code("""\
class MyClass:
    @classmethod
    def from_config(cls, config):
        return cls(config.a, config.b, config.c, config.d)
""")
        findings = check_feature_envy(t)
        assert len(findings) == 0


class TestAnemicDomain:
    def test_finds_anemic(self, trees):
        t = trees.code("""\
class Config:
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
        self.timeout = 30
        self.retries = 3
        self.debug = False
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 1
        assert "Config" in findings[0].message
        assert "5 attributes" in findings[0].message

    def test_ignores_class_with_methods(self, trees):
        t = trees.code("""\
class Config:
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
        self.timeout = 30
        self.retries = 3
        self.debug = False

    def validate(self):
        if self.port < 0:
            raise ValueError("bad port")
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0

    def test_ignores_dataclass(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    host: str = "localhost"
    port: int = 8080
    timeout: int = 30
    retries: int = 3
    debug: bool = False
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0

    def test_ignores_namedtuple(self, trees):
        t = trees.code("""\
from typing import NamedTuple

class Config(NamedTuple):
    host: str
    port: int
    timeout: int
    retries: int
    debug: bool
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0

    def test_ignores_few_attrs(self, trees):
        t = trees.code("""\
class Config:
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0

    def test_ignores_base_with_methods(self, trees):
        t = trees.code("""\
class Base:
    def validate(self):
        pass

class Config(Base):
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
        self.timeout = 30
        self.retries = 3
        self.debug = False
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0

    def test_cross_file_envy_enhances_message(self, trees):
        t = trees.files(
            {
                "models.py": """\
class Config:
    def __init__(self):
        self.host = "localhost"
        self.port = 8080
        self.timeout = 30
        self.retries = 3
        self.debug = False
""",
                "app.py": """\
def setup(config):
    connect(config.host, config.port, config.timeout)
""",
            }
        )
        findings = check_anemic_domain(t)
        assert len(findings) == 1
        assert "external functions" in findings[0].message

    def test_ignores_pydantic(self, trees):
        t = trees.code("""\
from pydantic import BaseModel

class Config(BaseModel):
    host: str = "localhost"
    port: int = 8080
    timeout: int = 30
    retries: int = 3
    debug: bool = False
""")
        findings = check_anemic_domain(t)
        assert len(findings) == 0
