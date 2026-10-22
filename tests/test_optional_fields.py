"""coalesced-optional-field: optional record fields whose readers turn None into empty."""

from __future__ import annotations

from pysmelly.checks.optional_fields import check_coalesced_optional_field
from pysmelly.registry import CHECK_SEVERITY, Severity

RECORD = """\
from dataclasses import dataclass, field


@dataclass
class Block:
    kind: str
    tags: list[str] | None = None
"""


def _reader(coalescing: int, plain: int) -> str:
    lines = ["from pkg.stream import Block", ""]
    for index in range(coalescing):
        lines += [f"def coalesce_{index}(block: Block):", "    return len(block.tags or [])", ""]
    for index in range(plain):
        lines += [f"def plain_{index}(block: Block):", "    return block.tags is None", ""]
    return "\n".join(lines)


def _run(trees, record: str = RECORD, **modules: str):
    files = {"pkg/__init__.py": "", "pkg/stream.py": record}
    files.update({f"pkg/{name}.py": code for name, code in modules.items()})
    return check_coalesced_optional_field(trees.files(files))


class TestCoalescedOptionalField:
    def test_registered_medium(self):
        assert CHECK_SEVERITY["coalesced-optional-field"] is Severity.MEDIUM

    def test_mostly_coalesced_field_fires(self, trees):
        findings = _run(trees, reader=_reader(3, 1))
        assert len(findings) == 1
        finding = findings[0]
        assert (finding.file, finding.line) == ("pkg/stream.py", 7)
        assert [(site.file, site.line) for site in finding.sites] == [
            ("pkg/reader.py", 4),
            ("pkg/reader.py", 7),
            ("pkg/reader.py", 10),
        ]
        message = finding.message
        assert "`Block.tags` (pkg/stream.py:7) is typed `list[str] | None`" in message
        assert "3 of its 4 reads coalesce it to an empty list" in message
        assert message.endswith(
            "; declared without None it is `tags: list[str] = field(default_factory=list)`"
        )

    def test_two_coalescing_reads_are_below_the_bar(self, trees):
        assert _run(trees, reader=_reader(2, 0)) == []

    def test_half_is_not_mostly(self, trees):
        assert _run(trees, reader=_reader(3, 3)) == []

    def test_non_optional_field_is_never_reported(self, trees):
        record = RECORD.replace(
            "tags: list[str] | None = None", "tags: list[str] = field(default_factory=list)"
        )
        assert _run(trees, record=record, reader=_reader(4, 0)) == []

    def test_reads_of_another_records_field_of_that_name_do_not_count(self, trees):
        record = RECORD + (
            "\n\n@dataclass\nclass Page:\n    tags: list[str] = field(default_factory=list)\n"
        )
        reader = "\n".join(
            ["from pkg.stream import Block, Page", ""]
            + [
                line
                for index in range(4)
                for line in (f"def p{index}(page: Page):", "    return page.tags or []", "")
            ]
            + ["def b(block: Block):", "    return block.tags or []", ""]
        )
        assert _run(trees, record=record, reader=reader) == []

    def test_untied_reads_of_a_shared_name_count_only_against(self, trees):
        record = RECORD + (
            "\n\n@dataclass\nclass Page:\n    tags: list[str] = field(default_factory=list)\n"
        )
        untied = "\n".join(
            line
            for index in range(4)
            for line in (f"def u{index}(thing):", "    return thing.tags or []", "")
        )
        assert _run(trees, record=record, reader=_reader(3, 0), other=untied) == []
        assert len(_run(trees, record=record, reader=_reader(3, 0))) == 1

    def test_untied_reads_of_a_unique_name_count_for_the_field(self, trees):
        untied = "\n".join(
            line
            for index in range(3)
            for line in (f"def u{index}(thing):", "    return thing.tags or ()", "")
        )
        findings = _run(trees, reader=untied)
        assert len(findings) == 1
        assert "field(default_factory=tuple)" in findings[0].message

    def test_pydantic_optional_suggests_its_field(self, trees):
        record = (
            "from typing import Optional\n"
            "from pydantic import BaseModel\n\n"
            "class Block(BaseModel):\n"
            "    tags: Optional[dict] = None\n"
        )
        reader = "\n".join(
            ["from pkg.stream import Block", ""]
            + [
                line
                for index in range(3)
                for line in (f"def r{index}(block: Block):", "    return block.tags or dict()", "")
            ]
        )
        findings = _run(trees, record=record, reader=reader)
        assert len(findings) == 1
        assert "Field(default_factory=dict)" in findings[0].message
        assert "is typed `Optional[dict]`" in findings[0].message

    def test_attrs_union_with_none_as_a_string(self, trees):
        record = (
            "import attrs\n"
            "from typing import Union\n\n"
            "@attrs.define\n"
            "class Block:\n"
            '    tags: "Union[dict, None]" = None\n'
        )
        reader = "\n".join(
            ["from pkg.stream import Block", ""]
            + [
                line
                for index in range(3)
                for line in (f"def r{index}(block: Block):", "    return block.tags or {}", "")
            ]
        )
        findings = _run(trees, record=record, reader=reader)
        assert len(findings) == 1
        assert "attrs.field(factory=dict)" in findings[0].message

    def test_or_with_a_non_empty_value_is_not_coalescing(self, trees):
        reader = "\n".join(
            ["from pkg.stream import Block", ""]
            + [
                line
                for index in range(3)
                for line in (f"def r{index}(block: Block):", "    return block.tags or ['x']", "")
            ]
        )
        assert _run(trees, reader=reader) == []

    def test_reads_in_tests_do_not_count(self, trees):
        files = {"pkg/__init__.py": "", "pkg/stream.py": RECORD}
        files["tests/test_reader.py"] = _reader(3, 0)
        assert check_coalesced_optional_field(trees.files(files)) == []
        files["pkg/reader.py"] = _reader(3, 0)
        assert len(check_coalesced_optional_field(trees.files(files))) == 1
