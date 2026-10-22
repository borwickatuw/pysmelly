"""Templates are production readers (26-4 fix round 1).

A Django project's ``DeadlineCut.unread_pages`` and ``BelowFloor.surrogate`` are read
only by Django templates (``{{ deadline_cut.unread_pages }}``). Once test
reads stopped keeping fields alive, write-only-attributes reported them,
because no Python code reads them. pysmelly now scans the files under a
``templates`` directory for dotted names, statically, and counts each as a
production read.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from pysmelly.checks.architecture import check_write_only_attributes
from pysmelly.cli import main
from pysmelly.context import AnalysisContext
from pysmelly.discovery import get_template_files
from pysmelly.templates import attribute_names

RECORD = """\
from dataclasses import dataclass


@dataclass
class DeadlineCut:
    unread_pages: int
    undescribed_pictures: int


def cut():
    return DeadlineCut(unread_pages=1, undescribed_pictures=2)
"""


class TestAttributeNames:
    def test_variable_and_tag_blocks(self):
        text = (
            "<p>{{ deadline_cut.unread_pages }} page{{ deadline_cut.unread_pages|pluralize }}"
            "</p>\n{% if below_floor.surrogate %}x{% endif %}\n"
            "{% for p in cut.pages %}{{ p.0.label }}{% endfor %}"
        )
        assert attribute_names(text) == {"unread_pages", "surrogate", "pages", "label"}

    def test_text_outside_blocks_is_not_read(self):
        assert attribute_names("Call cut.unread_pages now. {# cut.hidden #}") == set()

    def test_quoted_strings_are_not_read(self):
        assert attribute_names('{% static "css/app.css" %}{% url "a.b" x.slug %}') == {"slug"}

    def test_a_call_or_subscript_result_is_read(self):
        assert attribute_names("{{ f(x).name }}{{ rows[0].total }}") == {"name", "total"}

    def test_a_multiline_block(self):
        assert attribute_names("{% with total=a.b\n  other=c.d %}") == {"b", "d"}


class TestDiscovery:
    def test_files_under_a_templates_directory_at_any_depth(self, tmp_path):
        for name in ("app/templates/app/page.html", "templates/base.txt", "app/views.html"):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text("{{ x.y }}", encoding="utf-8")
        assert [path.relative_to(tmp_path).as_posix() for path in get_template_files(tmp_path)] == [
            "app/templates/app/page.html",
            "templates/base.txt",
        ]

    def test_in_a_git_repo_ignored_files_are_left_out(self, git_repo):
        for name in ("app/templates/page.html", "build/templates/page.html"):
            (git_repo / name).parent.mkdir(parents=True, exist_ok=True)
            (git_repo / name).write_text("{{ x.y }}", encoding="utf-8")
        (git_repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        found = [path.relative_to(git_repo).as_posix() for path in get_template_files(git_repo)]
        assert found == ["app/templates/page.html"]

    def test_a_file_named_templates_is_not_a_directory_of_them(self, tmp_path):
        (tmp_path / "templates").write_text("{{ x.y }}", encoding="utf-8")
        assert get_template_files(tmp_path) == []


def _outside_and_root(tmp_path: Path) -> tuple[Path, Path]:
    """A project root, and a directory beside it holding a template."""
    outside = tmp_path / "outside"
    (outside / "tpl").mkdir(parents=True)
    (outside / "tpl" / "page.html").write_text("{{ c.unread_pages }}", encoding="utf-8")
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "own.html").write_text("{{ x.y }}", encoding="utf-8")
    return outside, root


def _found(root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in get_template_files(root)]


class TestSymlinks:
    """A template is a file inside the analysed root, once symlinks are resolved."""

    def test_a_symlinked_file_pointing_outside_the_root_is_left_out(self, tmp_path):
        outside, root = _outside_and_root(tmp_path)
        (root / "templates" / "leak.html").symlink_to(outside / "tpl" / "page.html")
        assert _found(root) == ["templates/own.html"]

    def test_a_symlinked_directory_pointing_outside_the_root_is_left_out(self, tmp_path):
        outside, root = _outside_and_root(tmp_path)
        (root / "app").mkdir()
        (root / "app" / "templates").symlink_to(outside / "tpl", target_is_directory=True)
        assert _found(root) == ["templates/own.html"]

    def test_in_a_git_repo_a_symlink_pointing_outside_is_left_out(self, git_repo):
        outside, root = _outside_and_root(git_repo)
        subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
        (root / "templates" / "leak.html").symlink_to(outside / "tpl" / "page.html")
        assert _found(root) == ["templates/own.html"]

    def test_a_symlink_to_a_file_inside_the_root_is_kept(self, tmp_path):
        _, root = _outside_and_root(tmp_path)
        (root / "shared.html").write_text("{{ c.unread_pages }}", encoding="utf-8")
        (root / "templates" / "alias.html").symlink_to(root / "shared.html")
        assert _found(root) == ["templates/alias.html", "templates/own.html"]

    def test_a_read_through_an_outside_symlink_keeps_no_field_alive(self, tmp_path, capsys):
        outside, root = _outside_and_root(tmp_path)
        (root / "templates" / "leak.html").symlink_to(outside / "tpl" / "page.html")
        found = _findings(root, {"lib.py": RECORD}, capsys)
        assert [line.split(" is never")[0] for line in found] == [
            "lib.py:6: DeadlineCut.unread_pages",
            "lib.py:7: DeadlineCut.undescribed_pictures",
        ]


class TestWriteOnlyAttributes:
    def test_a_field_a_template_reads_is_read(self):
        trees = {Path("lib.py"): ast.parse(RECORD)}
        ctx = AnalysisContext(trees, verbose=False)
        ctx.template_attributes = frozenset({"unread_pages"})
        assert [f.message.split(" is never")[0] for f in check_write_only_attributes(ctx)] == [
            "DeadlineCut.undescribed_pictures"
        ]

    def test_a_typeddict_key_a_template_reads_is_read(self):
        code = (
            "from typing import TypedDict\n\n\nclass Row(TypedDict):\n    total: int\n"
            "    spare: int\n\n\ndef row() -> Row:\n    r: Row = {'total': 1, 'spare': 2}\n"
            "    return r\n"
        )
        ctx = AnalysisContext({Path("lib.py"): ast.parse(code)}, verbose=False)
        ctx.template_attributes = frozenset({"total"})
        assert [f.message.split(" is never")[0] for f in check_write_only_attributes(ctx)] == [
            'Row["spare"]'
        ]


def _findings(tmp_path: Path, files: dict[str, str], capsys) -> list[str]:
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--no-context", "--check", "write-only-attributes", str(tmp_path)])
    out = capsys.readouterr().out
    return [line.strip() for line in out.splitlines() if line.startswith("  lib.py:")]


class TestEndToEnd:
    def test_a_template_read_keeps_a_field_a_test_also_reads(self, tmp_path, capsys):
        """The founding shape: a test reads both fields, the template reads one."""
        found = _findings(
            tmp_path,
            {
                "lib.py": RECORD,
                "works/templates/works/read.html": "{{ deadline_cut.unread_pages }}\n",
                "tests/test_lib.py": (
                    "from lib import cut\n\n\ndef test_cut():\n"
                    "    assert cut().unread_pages and cut().undescribed_pictures\n"
                ),
            },
            capsys,
        )
        assert [line.split(" is never")[0] for line in found] == [
            "lib.py:7: DeadlineCut.undescribed_pictures"
        ]

    def test_an_excluded_template_reads_nothing(self, tmp_path, capsys):
        found = _findings(
            tmp_path,
            {
                "lib.py": RECORD,
                "pyproject.toml": '[tool.pysmelly]\nexclude = ["legacy/"]\n',
                "legacy/templates/old.html": "{{ c.unread_pages }}{{ c.undescribed_pictures }}",
            },
            capsys,
        )
        assert len(found) == 2

    def test_a_binary_file_among_templates_is_passed_over(self, tmp_path, capsys):
        (tmp_path / "templates").mkdir()
        (tmp_path / "templates" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        found = _findings(
            tmp_path,
            {"lib.py": RECORD, "templates/page.html": "{{ c.unread_pages }}"},
            capsys,
        )
        assert [line.split(" is never")[0] for line in found] == [
            "lib.py:7: DeadlineCut.undescribed_pictures"
        ]
