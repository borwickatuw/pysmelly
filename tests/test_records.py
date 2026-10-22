"""write-only-attributes beyond @dataclass: pydantic, attrs, NamedTuple, TypedDict.

Each record kind gets a positive fixture (an unread field is reported) and a
negative one (a wholesale read, such as ``model_dump`` or ``**``, reads every
field). The negatives always pair the consumed class with an unconsumed one,
so they assert what is reported as well as what is not.
"""

from pysmelly.checks.architecture import check_write_only_attributes


def _flagged(ctx) -> set[str]:
    """``Class.field`` (or ``Class["key"]``) for each finding."""
    return {
        finding.message.split(" is never read")[0] for finding in check_write_only_attributes(ctx)
    }


class TestPydantic:
    def test_unread_field_is_reported(self, trees):
        t = trees.code("""\
from pydantic import BaseModel

class Settings(BaseModel):
    url: str
    legacy_timeout: int = 30

def connect(s: Settings):
    return s.url

connect(Settings(url="https://example.org", legacy_timeout=5))
""")
        findings = check_write_only_attributes(t)
        assert [f.message for f in findings] == [
            "Settings.legacy_timeout is never read anywhere in the codebase "
            "— vestigial pydantic model field"
        ]

    def test_module_import_and_project_subclass(self, trees):
        t = trees.files(
            {
                "app/base.py": """\
import pydantic

class Base(pydantic.BaseModel):
    id: int
""",
                "app/models.py": """\
from app.base import Base

class Item(Base):
    name: str
    stale: str = ""

def show(item):
    return item.id, item.name

show(Item(id=1, name="n"))
""",
                "app/__init__.py": "",
            }
        )
        assert _flagged(t) == {"Item.stale"}

    def test_recognised_by_import_not_by_bare_name(self, trees):
        t = trees.files(
            {
                "fake.py": """\
class BaseModel:
    pass

class Lookalike(BaseModel):
    unread: int = 0

Lookalike()
""",
                "real.py": """\
from pydantic import BaseModel as PydanticModel

class Real(PydanticModel):
    unread: int = 0

Real()
""",
            }
        )
        assert _flagged(t) == {"Real.unread"}

    def test_config_and_classvar_are_not_fields(self, trees):
        t = trees.code("""\
from typing import ClassVar
from pydantic import BaseModel, ConfigDict

class Row(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    kind: ClassVar[str] = "row"
    value: int

ROW = Row(value=1)
""")
        assert _flagged(t) == {"Row.value"}

    def test_model_dump_reads_every_field(self, trees):
        t = trees.code("""\
from pydantic import BaseModel

class Payload(BaseModel):
    a: int
    b: int

class Other(BaseModel):
    c: int

def send():
    payload = Payload(a=1, b=2)
    return payload.model_dump(mode="json"), Other(c=3)
""")
        assert _flagged(t) == {"Other.c"}

    def test_untied_model_dump_reads_the_models_its_module_names(self, trees):
        t = trees.files(
            {
                "models.py": """\
from pydantic import BaseModel

class Hit(BaseModel):
    score: int

class Unrelated(BaseModel):
    size: int

EMPTY = Unrelated(size=0)
""",
                "search.py": """\
from models import Hit

def dump_all(client):
    hits: list = []
    for raw in client.fetch():
        hits.append(Hit(score=raw))
    return [h for h in hits]

def first(client):
    return client.fetch()[0].model_dump()
""",
            }
        )
        assert _flagged(t) == {"Unrelated.size"}


class TestAttrs:
    def test_define_and_field_calls(self, trees):
        t = trees.code("""\
import attr
import attrs
from attrs import frozen

@attrs.define
class Job:
    name: str
    retries: int = 3

@attr.s
class Legacy:
    path = attr.ib()
    unused = attr.ib(default=None)

@frozen
class Point:
    x: int
    y: int

def run(job, legacy, p):
    return job.name, legacy.path, p.x

run(Job("build"), Legacy("p"), Point(1, 2))
""")
        assert _flagged(t) == {"Job.retries", "Legacy.unused", "Point.y"}

    def test_attrs_asdict_reads_every_field(self, trees):
        t = trees.code("""\
import attrs

@attrs.define
class Event:
    kind: str
    at: float

@attrs.define
class Kept:
    note: str

def serialize(event: Event) -> dict:
    return attrs.asdict(event), Kept(note="n")
""")
        assert _flagged(t) == {"Kept.note"}


class TestNamedTuple:
    def test_unread_field_is_reported(self, trees):
        t = trees.code("""\
from typing import NamedTuple

class Result(NamedTuple):
    ok: bool
    detail: str

def check() -> Result:
    return Result(True, "fine")

if check().ok:
    pass
""")
        findings = check_write_only_attributes(t)
        assert [f.message for f in findings] == [
            "Result.detail is never read anywhere in the codebase — vestigial NamedTuple field"
        ]

    def test_asdict_and_unpacking_read_every_field(self, trees):
        t = trees.code("""\
import typing

class Pair(typing.NamedTuple):
    left: int
    right: int

class Span(typing.NamedTuple):
    start: int
    end: int

class Idle(typing.NamedTuple):
    unused: int

def make_pair() -> Pair:
    return Pair(1, 2)

def use():
    left, right = make_pair()
    span = Span(0, 1)
    return left + right, span._asdict(), Idle(0)
""")
        assert _flagged(t) == {"Idle.unused"}

    def test_unpacking_a_tuple_display_reads_nothing(self, trees):
        t = trees.code("""\
from typing import NamedTuple

class Pair(NamedTuple):
    left: int
    right: int

def swap(p: Pair):
    a, b = p.right, 0
    return a, b

swap(Pair(1, 2))
""")
        assert _flagged(t) == {"Pair.left"}


class TestTypedDict:
    def test_keys_read_by_subscript_get_and_in(self, trees):
        t = trees.code("""\
from typing import TypedDict

class Row(TypedDict):
    id: int
    name: str
    tag: str
    stale: str

def make() -> Row:
    return {"id": 1, "name": "n", "tag": "t", "stale": "s"}

def show(row: Row):
    if "tag" in row:
        print(row["id"], row.get("name"))
""")
        findings = check_write_only_attributes(t)
        assert [f.message for f in findings] == [
            'Row["stale"] is never read anywhere in the codebase — vestigial TypedDict key'
        ]

    def test_attribute_read_does_not_read_a_key(self, trees):
        t = trees.code("""\
from typing_extensions import TypedDict

class Opts(TypedDict):
    verbose: bool

def run(args):
    opts: Opts = {"verbose": True}
    return args.verbose, opts
""")
        assert _flagged(t) == {'Opts["verbose"]'}

    def test_double_star_reads_every_key(self, trees):
        t = trees.code("""\
from typing import TypedDict

class Kwargs(TypedDict):
    timeout: int
    retries: int

class Unused(TypedDict):
    flag: bool

def call(fn, options: Kwargs):
    return fn(**options), Unused(flag=True)
""")
        assert _flagged(t) == {'Unused["flag"]'}

    def test_project_subclass_inherits_the_kind(self, trees):
        t = trees.code("""\
from typing import TypedDict

class Base(TypedDict):
    id: int

class Child(Base):
    extra: str

def fill(c: Child):
    c["extra"] = "x"

def show(c):
    return c["id"]
""")
        assert _flagged(t) == {'Child["extra"]'}


class TestWholesaleDataclassReads:
    def test_asdict_of_a_returned_record(self, trees):
        """``asdict(x)`` reads the class ``x``'s producer is annotated to return."""
        t = trees.code("""\
from dataclasses import asdict, dataclass

@dataclass
class Comparison:
    before: int = 0
    after: int = 0

@dataclass
class Untouched:
    left_over: int = 0

def compare() -> Comparison:
    return Comparison()

def row():
    found = compare()
    return asdict(found), Untouched()
""")
        assert _flagged(t) == {"Untouched.left_over"}

    def test_a_to_dict_built_on_fields_reads_every_field(self, trees):
        """A TranscodingJob: ``to_dict`` reads each field through
        ``dataclasses.fields(self)`` and ``getattr``, and a worker reads the
        wire dict's ``"work_permalink"``; the record is read whole, as by ``asdict``."""
        t = trees.code("""\
import dataclasses
from dataclasses import dataclass

@dataclass
class Job:
    file_id: int
    work_permalink: str | None = None

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}

@dataclass
class Untouched:
    left_over: int = 0

def send(file_id):
    return Job(file_id=file_id, work_permalink="p").to_dict(), Untouched()

def handle(job):
    return job.get("work_permalink")
""")
        assert _flagged(t) == {"Untouched.left_over"}

    def test_attrs_fields_reads_every_field(self, trees):
        t = trees.code("""\
import attrs

@attrs.define
class Job:
    retries: int = 0

def describe(job: Job):
    return [(a.name, getattr(job, a.name)) for a in attrs.fields(Job)]

describe(Job(retries=1))
""")
        assert _flagged(t) == set()

    def test_getattr_and_attrgetter_by_constant_name_read_a_field(self, trees):
        """A SuspectFinding: its lenses read ``getattr(f, "extension", "")``."""
        t = trees.code("""\
import operator
from dataclasses import dataclass

@dataclass
class Finding:
    extension: str = ""
    failed_check: str = ""
    owner: str = ""
    unread: str = ""

def build():
    return Finding(extension="wav", failed_check="decode", owner="x", unread="y")

def label(f):
    return getattr(f, "extension", "") + getattr(f, "failed_check")

by_owner = operator.attrgetter("owner.name")
""")
        assert _flagged(t) == {"Finding.unread"}

    def test_a_string_key_alone_does_not_read_a_dataclass_field(self, trees):
        """A key is a dict's; only a TypedDict's fields are read by key."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Job:
    work_permalink: str | None = None

def build():
    return Job(work_permalink="p")

def handle(job):
    return job.get("work_permalink"), job["work_permalink"]
""")
        assert _flagged(t) == {"Job.work_permalink"}

    def test_vars_and_dunder_dict_of_annotated_names(self, trees):
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class A:
    one: int = 0

@dataclass
class B:
    two: int = 0

@dataclass
class C:
    three: int = 0

def dump(a: A, b: "B | None"):
    return vars(a), b.__dict__

C()
""")
        assert _flagged(t) == {"C.three"}

    def test_untied_generic_consumer_abstains(self, trees):
        """``vars(args)`` in a function that names no record reads nothing."""
        t = trees.code("""\
from dataclasses import dataclass

@dataclass
class Config:
    unused: int = 0

CONFIG = Config()

def parse(parser):
    args = parser.parse_args()
    return vars(args)
""")
        assert _flagged(t) == {"Config.unused"}

    def test_serializing_self_reads_subclass_fields(self, trees):
        t = trees.code("""\
from dataclasses import asdict, dataclass

@dataclass
class Base:
    shared: int = 0

    def to_dict(self):
        return asdict(self)

@dataclass
class Child(Base):
    extra: int = 0

@dataclass
class Unrelated:
    lonely: int = 0

Unrelated()
""")
        assert _flagged(t) == {"Unrelated.lonely"}

    def test_loop_over_annotated_attribute(self, trees):
        t = trees.code("""\
from dataclasses import asdict, dataclass, field

@dataclass
class Skipped:
    key: str = ""

@dataclass
class Summary:
    skipped: list[Skipped] = field(default_factory=list)
    unread: int = 0

    def rows(self):
        return [asdict(s) for s in self.skipped]

Summary()
""")
        assert _flagged(t) == {"Summary.unread"}

    def test_untied_specific_consumer_with_nothing_named_abstains(self, trees):
        """``asdict(x)`` of a dynamically loaded value, in a module naming no
        dataclass, reads nothing: no candidate plausibly flows there."""
        t = trees.files(
            {
                "records.py": """\
from dataclasses import dataclass

@dataclass
class Report:
    unread: int = 0

Report()
""",
                "replay.py": """\
from dataclasses import asdict

def replay(loader):
    module = loader.load("churn")
    return asdict(module.compute())
""",
            }
        )
        assert _flagged(t) == {"Report.unread"}

    def test_foreign_calls_hand_back_their_arguments_records(self, trees):
        t = trees.code("""\
import copy
import itertools
from dataclasses import asdict, dataclass, replace
from typing import cast

@dataclass
class A:
    a: int = 0

@dataclass
class B:
    b: int = 0

@dataclass
class C:
    c: int = 0

@dataclass
class D:
    d: int = 0

@dataclass
class Never:
    n: int = 0

Never()

def dump(xs: list[A], ys: list[B], raw, maybe: C | None):
    first = next(iter(sorted(xs)))
    chained = itertools.chain(filter(None, ys))
    picked = maybe or copy.copy(maybe)
    cast_d = cast(D, raw)
    return [asdict(first), [asdict(y) for y in chained], asdict(replace(picked)), asdict(cast_d)]
""")
        assert _flagged(t) == {"Never.n"}

    def test_items_pairs_and_json_payloads(self, trees):
        """Unpacking ``.items()`` pairs unpacks no record; ``json.dumps`` of a
        TypedDict reads every key."""
        t = trees.code("""\
import json
from typing import NamedTuple, TypedDict

class Pair(NamedTuple):
    left: int
    right: int

class Body(TypedDict):
    text: str

def emit(table: dict, body: Body):
    p = Pair(1, 2)
    for key, value in table.items():
        print(key, value, p)
    return json.dumps(body)
""")
        assert _flagged(t) == {"Pair.left", "Pair.right"}


class TestTying:
    """How a consumed expression is tied to the record classes it can hold."""

    def test_field_of_a_record_and_a_method_return(self, trees):
        t = trees.code("""\
from pydantic import BaseModel

class File(BaseModel):
    size: int

class Hit(BaseModel):
    files: list[File]

class Page(BaseModel):
    number: int

class Lonely(BaseModel):
    unread: int

Lonely(unread=1)

class Client:
    def page(self) -> Page:
        return Page(number=1)

    def dump(self, hit: Hit):
        rows = [f.model_dump() for f in hit.files]
        return rows, self.page().model_dump(), Hit.model_validate({}).model_dump()
""")
        assert _flagged(t) == {"Lonely.unread"}

    def test_reading_a_subclass_reads_its_ancestors(self, trees):
        t = trees.code("""\
from dataclasses import asdict, dataclass

@dataclass
class Base:
    shared: int = 0

@dataclass
class Child(Base):
    extra: int = 0

@dataclass
class Other:
    alone: int = 0

Other()

class Plain(Child):
    def dump(self):
        return vars(self)
""")
        assert _flagged(t) == {"Other.alone"}

    def test_bindings_annotations_walrus_and_double_star_display(self, trees):
        t = trees.code("""\
from typing import Any, TypedDict

class Opts(TypedDict):
    quiet: bool

class Extra(TypedDict):
    depth: int

class Never(TypedDict):
    gone: str

NEVER = Never(gone="")

def merge(raw: Any):
    opts: Opts = load()
    if (extra := Extra(depth=1)):
        return {**opts, **extra, **raw}
""")
        assert _flagged(t) == {'Never["gone"]'}


class TestWriteSites:
    """A field is write-only only when production code writes it."""

    def test_parsed_only_pydantic_model_is_not_reported(self, trees):
        """Fields ``model_validate``, ``parse_obj`` or a ``**`` payload fill
        mirror someone else's schema: no project code writes them."""
        t = trees.code("""\
from pydantic import BaseModel

class Version(BaseModel):
    status: str | None = None

class Asset(BaseModel):
    id: str
    title: str | None = None
    versions: list[Version] = []

class User(BaseModel):
    first_name: str | None = None

class Group(BaseModel):
    group_type: str

def fetch(client) -> list[Asset]:
    users = [User.parse_obj(u) for u in client.users()]
    group = Group(**client.group())
    return [Asset.model_validate(a) for a in client.assets()], users, group

def by_json(raw: str) -> Asset:
    return Asset.model_validate_json(raw)
""")
        assert _flagged(t) == set()

    def test_constructor_keyword_writes_a_pydantic_field(self, trees):
        """A keyword, an alias keyword, ``model_construct`` and ``cls(...)``
        each write; an attribute assignment writes by name."""
        t = trees.code("""\
from pydantic import BaseModel, Field

class Job(BaseModel):
    name: str
    priority: int

class Doc(BaseModel):
    context: str = Field(alias="ctx")

class Cached(BaseModel):
    hits: int

class Built(BaseModel):
    origin: str

    @classmethod
    def here(cls):
        return cls(origin="here")

class Patched(BaseModel):
    note: str | None = None

def submit(queue):
    queue.put(Job(name="build", priority=1).name)
    queue.put(Doc(ctx="https://schema.org"))
    queue.put(Cached.model_construct(hits=0))

def patch(raw):
    p = Patched.model_validate(raw)
    p.note = "seen"
    return p
""")
        assert _flagged(t) == {
            "Job.priority",
            "Doc.context",
            "Cached.hits",
            "Built.origin",
            "Patched.note",
        }

    def test_construction_only_in_tests_writes_nothing(self, trees):
        t = trees.files(
            {
                "app/models.py": """\
from pydantic import BaseModel

class Row(BaseModel):
    seen: str
""",
                "tests/test_models.py": """\
from app.models import Row

def test_row():
    assert Row(seen="x")
""",
            }
        )
        assert _flagged(t) == set()

    def test_defaults_count_only_for_a_payload_free_construction(self, trees):
        """``Config()`` fills ``knob`` from its default; ``Loaded(**data)``
        may fill it from the data, and a class nothing builds writes nothing."""
        t = trees.code("""\
from dataclasses import dataclass, field

@dataclass
class Config:
    url: str = ""
    knob: int = 3
    tags: list = field(default_factory=list)
    required: int = field()

@dataclass
class Loaded:
    knob: int = 3

@dataclass
class NeverBuilt:
    knob: int = 3

def load(data):
    return Config(), Loaded(**data)
""")
        assert _flagged(t) == {"Config.url", "Config.knob", "Config.tags"}

    def test_positional_arguments_follow_inherited_field_order(self, trees):
        t = trees.code("""\
from dataclasses import dataclass, field
from typing import NamedTuple

@dataclass
class Base:
    a: int
    b: int

@dataclass
class Child(Base):
    c: int
    derived: int = field(init=False)

class Pair(NamedTuple):
    left: int
    right: int

def make(rows):
    return Child(1, 2, 3), Pair(*rows)
""")
        assert _flagged(t) == {"Base.a", "Base.b", "Child.c"}

    def test_typeddict_keys_written_by_tied_sites_only(self, trees):
        t = trees.code("""\
from typing import TypedDict

class Row(TypedDict):
    built: str
    appended: str
    updated: str
    parsed: str

def rows(raw) -> list[Row]:
    out: list[Row] = [dict(built="b")]
    out.append({"appended": "a"})
    out[0].update(updated="u")
    other = {"parsed": "p"}
    return out + raw, other
""")
        assert _flagged(t) == {'Row["built"]', 'Row["appended"]', 'Row["updated"]'}

    def test_setattr_copies_and_typeddict_construction_write(self, trees):
        """``setattr`` and copying helpers write by name; ``Row({...})``
        writes a TypedDict's keys; ``type(self)(...)`` and
        ``self.__class__(...)`` build the method's own class."""
        t = trees.code("""\
import dataclasses
from typing import NamedTuple, TypedDict

from pydantic import BaseModel

@dataclasses.dataclass
class Parsed:
    by_setattr: int
    by_replace: int

class Point(NamedTuple):
    by_underscore_replace: int

class Model(BaseModel):
    by_copy: int

class Row(TypedDict):
    positional: str
    by_keyword: str

class Clone(BaseModel):
    twin: int
    other: int

    def again(self):
        return type(self)(twin=1), self.__class__(other=2)

def touch(raw, p: Point, m: Model):
    obj = Parsed(**raw)
    setattr(obj, "by_setattr", 1)
    copied = dataclasses.replace(obj, by_replace=2)
    return copied, p._replace(by_underscore_replace=3), m.model_copy(update={"by_copy": 4})

def rows():
    return Row({"positional": "p"}), Row(by_keyword="k")
""")
        assert _flagged(t) == {
            "Parsed.by_setattr",
            "Parsed.by_replace",
            "Point.by_underscore_replace",
            "Model.by_copy",
            'Row["positional"]',
            'Row["by_keyword"]',
            "Clone.twin",
            "Clone.other",
        }


class TestNonFieldAnnotations:
    def test_dataclass_classvar_and_initvar_are_not_fields(self, trees):
        """A class constant read from a template, and an init-only value
        consumed by ``__post_init__``, are not fields."""
        t = trees.code("""\
from dataclasses import InitVar, dataclass
from typing import ClassVar

@dataclass
class PositionMarker:
    is_marker: ClassVar[bool] = True
    seed: InitVar[int] = 0
    position: int = 0

    def __post_init__(self, seed):
        self.offset = seed

MARK = PositionMarker(position=1, seed=2)
""")
        assert _flagged(t) == {"PositionMarker.position"}
