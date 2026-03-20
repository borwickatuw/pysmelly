"""Tests for architectural checks."""

from pysmelly.checks.architecture import (
    check_anemic_domain,
    check_feature_envy,
    check_shared_mutable_module_state,
    check_temporal_coupling,
    check_write_only_attributes,
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

    def test_ignores_init_set_attrs(self, trees):
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


class TestFeatureEnvy:
    def test_finds_envy(self, trees):
        """Envy on second+ param (first param after self is excluded)."""
        t = trees.code("""\
class Formatter:
    def render(self, template, document):
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

    def test_ignores_first_param_after_self(self, trees):
        """First param is the method's subject — framework hooks pass it."""
        t = trees.code("""\
class Admin:
    def formfield_for_foreignkey(self, db_field, request):
        x = db_field.name
        y = db_field.remote_field
        z = db_field.related_model
        w = db_field.formfield
        return w
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
        t = trees.files({"tests/test_envy.py": """\
class TestFormatter:
    def test_render(self, document):
        x = document.title
        y = document.body
        z = document.author
        w = document.date
"""})
        findings = check_feature_envy(t)
        assert len(findings) == 0

    def test_message_identifies_envied_param(self, trees):
        """Second param triggers envy, message names it."""
        t = trees.code("""\
class Reporter:
    def summarize(self, topic, stats):
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
