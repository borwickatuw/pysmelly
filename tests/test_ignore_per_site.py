"""An inline ignore speaks for its own line, in findings that report many sites.

Several checks report one pattern once, across all the places it occurs (the
same stdlib import in many files, the same block in many functions), and
anchor that finding on one of the places. Before per-site suppression, an
ignore on the anchor's line hid the whole finding, so one reasoned ignore at a
sanctioned site (a shared client factory, say) silenced every other file. Now
the ignore removes only its own site: the rest stay reported, re-anchored on
the first site left, and the finding goes only when every site is ignored.

These run the CLI end to end, because suppression reads the files on disk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pysmelly.cli import main
from pysmelly.registry import CHECK_DISMISSALS, CHECK_FIXES

IGNORE = "# pysmelly: ignore[{check}]\n"

# A finding as the text formatter prints it: indented ``file:line: message``.
FINDING_LINE = re.compile(r"^  \S+\.py:\d+: ")


def _run(tmp_path: Path, files: dict[str, str], check: str, capsys) -> list[str]:
    """The finding lines ``check`` prints for ``files``, each without the fix and
    dismissal every printed message closes with."""
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--no-context", "--more-please", "--check", check, str(tmp_path)])
    out = capsys.readouterr().out
    closing = f". Fix: {CHECK_FIXES[check]} {CHECK_DISMISSALS[check]}"
    lines = [line.strip() for line in out.splitlines() if FINDING_LINE.match(line)]
    assert all(line.endswith(closing) for line in lines), lines
    return [line.removesuffix(closing) for line in lines]


FTP = "import ftplib\n"


class TestStdlibAlternatives:
    CHECK = "stdlib-alternatives"

    def test_without_ignores_one_finding_names_every_file(self, tmp_path, capsys):
        files = {"a.py": FTP, "b.py": FTP, "c.py": FTP}
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "a.py:1: ftplib used in 3 files (a.py:1, b.py:1, c.py:1)"
            " — alternative: paramiko (SFTP) or cloud storage SDKs"
        ]

    def test_ignore_on_the_factory_that_sorts_first_keeps_the_others(self, tmp_path, capsys):
        """The Q8 case: the sanctioned factory takes the ignore, and only it leaves."""
        files = {"a_factory.py": IGNORE.format(check=self.CHECK) + FTP, "b.py": FTP, "c.py": FTP}
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "b.py:1: ftplib used in 2 files (b.py:1, c.py:1)"
            " — alternative: paramiko (SFTP) or cloud storage SDKs (1 other site ignored)"
        ]

    def test_ignore_on_a_later_site_removes_only_it(self, tmp_path, capsys):
        files = {"a.py": FTP, "b.py": IGNORE.format(check=self.CHECK) + FTP, "c.py": FTP}
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "a.py:1: ftplib used in 2 files (a.py:1, c.py:1)"
            " — alternative: paramiko (SFTP) or cloud storage SDKs (1 other site ignored)"
        ]

    def test_every_site_ignored_leaves_no_finding(self, tmp_path, capsys):
        files = {name: IGNORE.format(check=self.CHECK) + FTP for name in ("a.py", "b.py")}
        assert _run(tmp_path, files, self.CHECK, capsys) == []

    def test_an_ignore_for_another_check_removes_nothing(self, tmp_path, capsys):
        files = {"a.py": IGNORE.format(check="dead-code") + FTP, "b.py": FTP}
        (line,) = _run(tmp_path, files, self.CHECK, capsys)
        assert line.startswith("a.py:2: ftplib used in 2 files (a.py:2, b.py:1)")
        assert "ignored" not in line


DUPLICATE = """\
def {name}(items):
    result = []
    for item in items:
        if item.active:
            result.append(item.name)
    filtered = [r for r in result if r]
    output = process(filtered)
    validated = check(output)
    return validated
"""


class TestDuplicateBlocks:
    CHECK = "duplicate-blocks"

    def _files(self, ignored: set[str]) -> dict[str, str]:
        # The block starts on the function's first body line; the ignore sits above it.
        return {
            f"{name}.py": DUPLICATE.format(name=f"f_{name}").replace(
                "    result = []\n",
                ("    " + IGNORE.format(check=self.CHECK) if name in ignored else "")
                + "    result = []\n",
            )
            for name in ("a", "b", "c")
        }

    def test_without_ignores_anchors_on_the_first_copy(self, tmp_path, capsys):
        (line,) = _run(tmp_path, self._files(set()), self.CHECK, capsys)
        assert line.startswith("a.py:2: ")
        assert "repeated in: a.py:f_a():2-9, b.py:f_b():2-9, c.py:f_c():2-9" in line

    def test_ignore_on_the_first_copy_re_anchors(self, tmp_path, capsys):
        (line,) = _run(tmp_path, self._files({"a"}), self.CHECK, capsys)
        assert line.startswith("b.py:2: ")
        assert line.endswith("repeated in: b.py:f_b():2-9, c.py:f_c():2-9 (1 other site ignored)")

    def test_every_copy_ignored_leaves_no_finding(self, tmp_path, capsys):
        assert _run(tmp_path, self._files({"a", "b", "c"}), self.CHECK, capsys) == []

    def test_one_copy_left_is_no_duplicate(self, tmp_path, capsys):
        """A reasoned ignore on one of two copies: the other duplicates nothing visible."""
        files = self._files({"a"})
        del files["c.py"]
        assert _run(tmp_path, files, self.CHECK, capsys) == []


HANDLER = """\
def {name}():
    try:
        push()
{ignore}    except ValueError:
        log("Push failed")
        notify("Push failed")
"""


class TestDuplicateExceptBlocks:
    CHECK = "duplicate-except-blocks"

    def _files(self, ignored: set[str]) -> dict[str, str]:
        return {
            f"{name}.py": HANDLER.format(
                name=f"f_{name}",
                ignore="    " + IGNORE.format(check=self.CHECK) if name in ignored else "",
            )
            for name in ("a", "b", "c")
        }

    def test_ignore_on_the_first_handler_re_anchors(self, tmp_path, capsys):
        (line,) = _run(tmp_path, self._files({"a"}), self.CHECK, capsys)
        assert line.startswith("b.py:4: except ValueError")
        assert line.endswith("in: b.py:f_b():4, c.py:f_c():4 (1 other site ignored)")

    def test_every_handler_ignored_leaves_no_finding(self, tmp_path, capsys):
        assert _run(tmp_path, self._files({"a", "b", "c"}), self.CHECK, capsys) == []


class TestParamClumps:
    CHECK = "param-clumps"

    def _files(self, ignored: set[str]) -> dict[str, str]:
        return {
            f"{name}.py": (IGNORE.format(check=self.CHECK) if name in ignored else "")
            + f"def f_{name}(first_name, last_name, email):\n    pass\n"
            for name in ("a", "b", "c", "d")
        }

    def test_without_ignores_anchors_on_the_first_function(self, tmp_path, capsys):
        (line,) = _run(tmp_path, self._files(set()), self.CHECK, capsys)
        assert line.startswith("a.py:1: Parameters (email, first_name, last_name)")
        assert "appear together in 4 functions" in line

    def test_the_floor_counts_signatures_not_listed_functions(self, tmp_path, capsys):
        """A function defined twice in one file is two signatures and one listed
        function; the finding is unchanged and building it does not fail."""
        sig = "def f(first_name, last_name, email):\n    pass\n"
        files = {"a.py": sig + "\n\n" + sig, "b.py": sig}
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "a.py:1: Parameters (email, first_name, last_name) appear together in"
            " 2 functions: a.py:f():1, b.py:f():1"
        ]

    def test_ignore_on_the_first_function_re_anchors(self, tmp_path, capsys):
        (line,) = _run(tmp_path, self._files({"a"}), self.CHECK, capsys)
        assert line.startswith("b.py:1: Parameters (email, first_name, last_name)")
        assert "in 3 functions: b.py:f_b():1, c.py:f_c():1, d.py:f_d():1" in line
        assert line.endswith("(1 other site ignored)")


class TestScatteredConstants:
    CHECK = "scattered-constants"

    def _files(self, ignored: set[str]) -> dict[str, str]:
        return {
            f"{name}.py": (IGNORE.format(check=self.CHECK) if name in ignored else "")
            + "TIMEOUT = 4321\n"
            for name in ("a", "b", "c", "d")
        }

    def test_ignore_on_the_first_file_re_anchors(self, tmp_path, capsys):
        assert _run(tmp_path, self._files({"a"}), self.CHECK, capsys) == [
            "b.py:1: Constant TIMEOUT = 4321 appears in 3 files (b.py:1, c.py:1, d.py:1)"
            " (1 other site ignored)"
        ]

    def test_below_the_file_floor_once_ignored_leaves_no_finding(self, tmp_path, capsys):
        """Two files left do not reach the three-file floor the check reports at."""
        assert _run(tmp_path, self._files({"a", "b"}), self.CHECK, capsys) == []


class TestRepeatedStringParsing:
    CHECK = "repeated-string-parsing"

    def test_ignore_on_the_first_file_re_anchors(self, tmp_path, capsys):
        parse = 'def f(s):\n    return s.split(":")[1]\n'
        files = {
            "a.py": parse.replace(
                "    return", "    " + IGNORE.format(check=self.CHECK) + "    return"
            ),
            "b.py": parse,
            "c.py": parse,
            "d.py": parse,
        }
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            'b.py:2: .split(":")[1] appears in 3 locations (b.py:2, c.py:2, d.py:2)'
            " — ad-hoc serialization (1 other site ignored)"
        ]


class TestGetattrStrings:
    CHECK = "getattr-strings"

    def test_ignore_removes_its_site_from_the_cross_file_finding(self, tmp_path, capsys):
        use = 'def f(obj):\n    return getattr(obj, "colour")\n'
        ignored = use.replace("    return", "    " + IGNORE.format(check=self.CHECK) + "    return")
        files = {"a.py": ignored, "b.py": use, "c.py": use, "d.py": use}
        spread = [
            line for line in _run(tmp_path, files, self.CHECK, capsys) if "shotgun surgery" in line
        ]
        assert spread == [
            "b.py:2: 'colour' used in getattr/hasattr across 3 locations in 3 files"
            " (b.py:2, c.py:2, d.py:2) — shotgun surgery risk (1 other site ignored)"
        ]


LADDER = """\
from shapes import Circle, Square

def {name}(shape):
    if isinstance(shape, Circle): return 1
    elif isinstance(shape, Square): return 2
"""


class TestIsinstanceChain:
    CHECK = "isinstance-chain"

    def test_ignore_on_the_first_ladder_re_anchors(self, tmp_path, capsys):
        files = {
            "shapes.py": "class Circle: pass\nclass Square: pass\n",
            **{
                f"{name}.py": LADDER.format(name=name).replace(
                    f"def {name}", IGNORE.format(check=self.CHECK) + f"def {name}"
                )
                if name == "area"
                else LADDER.format(name=name)
                for name in ("area", "perimeter", "scale", "spin")
            },
        }
        (line,) = _run(tmp_path, files, self.CHECK, capsys)
        assert line.startswith("perimeter.py:3: type dispatch on (Circle, Square) repeated in 3")
        assert "area" not in line
        assert line.endswith("(1 other site ignored)")


class TestScatteredIsinstance:
    """Anchored on the class's definition, not on a peer site."""

    CHECK = "scattered-isinstance"
    USE = "from shapes import Circle\n\ndef f(x):\n    return isinstance(x, Circle)\n"

    def _files(self, *, ignore_class: bool, ignored: set[str]) -> dict[str, str]:
        files = {
            "shapes.py": (IGNORE.format(check=self.CHECK) if ignore_class else "")
            + "class Circle:\n    pass\n"
        }
        for name in ("a", "b", "c", "d"):
            files[f"{name}.py"] = (
                self.USE.replace(
                    "    return", "    " + IGNORE.format(check=self.CHECK) + "    return"
                )
                if name in ignored
                else self.USE
            )
        return files

    def test_ignore_on_a_use_removes_only_that_use(self, tmp_path, capsys):
        files = self._files(ignore_class=False, ignored={"a"})
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "shapes.py:1: isinstance(x, Circle) checks appear in 3 files (b.py:4, c.py:4, d.py:4)"
            " (1 other site ignored)"
        ]

    def test_ignore_on_the_definition_still_speaks_for_the_class(self, tmp_path, capsys):
        files = self._files(ignore_class=True, ignored=set())
        assert _run(tmp_path, files, self.CHECK, capsys) == []


class TestShotgunSurgery:
    CHECK = "shotgun-surgery"

    # Two classes define the attribute, so no one definition is the anchor.
    TWO_DEFINERS = (
        "class Config:\n    def __init__(self):\n        self.timeout = 30\n\n\n"
        "class Limits:\n    def __init__(self):\n        self.timeout = 5\n"
    )

    def test_ignore_re_anchors_when_no_one_class_defines_the_attribute(self, tmp_path, capsys):
        use = "x = config.timeout\n"
        files = {name: use for name in ("b.py", "c.py", "d.py", "e.py")}
        files["a.py"] = IGNORE.format(check=self.CHECK) + use
        files["models.py"] = self.TWO_DEFINERS
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "b.py:1: config.timeout accessed in 4 files (b.py:1, c.py:1, d.py:1, e.py:1)"
            " — changes to .timeout require updating many files (1 other site ignored)"
        ]

    def test_ignore_on_an_access_keeps_the_definition_anchor(self, tmp_path, capsys):
        use = "def f(config: Config):\n    return config.timeout\n"
        files = {name: use for name in ("b.py", "c.py", "d.py", "e.py")}
        files["a.py"] = IGNORE.format(check=self.CHECK) + "x = config.timeout\n"
        files["models.py"] = "class Config:\n    def __init__(self):\n        self.timeout = 30\n"
        (line,) = _run(tmp_path, files, self.CHECK, capsys)
        assert line.startswith("models.py:1: config.timeout (defined on Config) accessed in 4")
        assert "a.py" not in line


class TestSharedMutableModuleState:
    CHECK = "shared-mutable-module-state"

    def test_ignore_on_a_mutation_removes_only_that_mutation(self, tmp_path, capsys):
        mutate = "from registry import HANDLERS\n\nHANDLERS.append(1)\n"
        files = {
            "registry.py": "HANDLERS = []\n",
            "a.py": mutate.replace(
                "HANDLERS.append", IGNORE.format(check=self.CHECK) + "HANDLERS.append"
            ),
            "b.py": mutate,
        }
        assert _run(tmp_path, files, self.CHECK, capsys) == [
            "registry.py:1: HANDLERS (defined in registry.py:1) is mutated at module scope"
            " from 1 other file (b.py:3) (1 other site ignored)"
        ]
