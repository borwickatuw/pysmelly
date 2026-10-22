"""Phase 33-2: the blind spots behind one project's standing ignores.

inconsistent-error-handling sets aside the catching sites inside entry
points, and scattered-constants skips literals passed by keyword to a call
that resolves outside the analyzed code. Two judgment idioms keep flagging:
the CLI catch-print-exit handler (duplicate-except-blocks) and
return-none on a ``*_or_none`` name.
"""

from pysmelly.checks.callers import (
    check_inconsistent_error_handling,
    check_return_none_instead_of_raise,
)
from pysmelly.checks.repetition import check_scattered_constants
from pysmelly.checks.structure import check_duplicate_except_blocks

LIB = """\
class ConfigError(Exception):
    pass


def load():
    raise ConfigError("missing")
"""

# A library function that lets ConfigError through; nothing above it catches.
HELPER = """\
from lib import load


def summarize():
    return load()
"""


def _click_command(name: str, handler: str = "        pass\n") -> str:
    return (
        "import click\n\n"
        "from lib import ConfigError, load\n\n\n"
        "@click.command()\n"
        f"def {name}():\n"
        "    try:\n"
        "        load()\n"
        "    except ConfigError as e:\n"
        f"{handler}"
    )


class TestEntryPointCatches:
    def test_catch_inside_an_entry_point_no_longer_counts(self, trees):
        """Entry points catch, a library function raises: deliberate layering."""
        t = trees.files(
            {
                "lib.py": LIB,
                "helper.py": HELPER,
                "sync.py": _click_command("sync"),
                "audit.py": _click_command("audit"),
            }
        )
        assert check_inconsistent_error_handling(t) == []

    def test_main_guard_and_argparse_handler_are_entry_points(self, trees):
        """``__main__`` roots and the handlers ``set_defaults`` names both count."""
        cli = """\
import argparse

from lib import ConfigError, load


def cmd_sync(args):
    try:
        load()
    except ConfigError:
        return 1


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    sub.add_parser("sync").set_defaults(func=cmd_sync)
    args = parser.parse_args()
    try:
        load()
    except ConfigError:
        return 1
    return args.func(args)


if __name__ == "__main__":
    main()
"""
        t = trees.files({"lib.py": LIB, "helper.py": HELPER, "cli.py": cli})
        assert check_inconsistent_error_handling(t) == []

    def test_library_catch_still_counts_and_entry_sites_are_reported(self, trees):
        """A library function that catches is evidence; the entry-point catch is reported apart."""
        library_catch = """\
from lib import ConfigError, load


def preview():
    try:
        return load()
    except ConfigError:
        return None
"""
        t = trees.files(
            {
                "lib.py": LIB,
                "helper.py": HELPER,
                "preview.py": library_catch,
                "sync.py": _click_command("sync"),
            }
        )
        assert [f.message for f in check_inconsistent_error_handling(t)] == [
            "load() has 3 callers with inconsistent error handling: "
            "1 catch specific (ConfigError), 1 unhandled, 1 entry-point site catches "
            "— error contract is unclear"
        ]

    def test_entry_point_catch_still_credits_the_library_that_propagates_to_it(self, trees):
        """A library caller whose own caller is a catching entry point propagates on purpose.

        The counted library site catches KeyError, so only the name the entry
        points catch (ConfigError) can credit ``summarize``. Pins behaviour
        the merge-base already had, when entry-point sites counted.
        """
        library_catch = """\
from lib import load


def preview():
    try:
        return load()
    except KeyError:
        return None
"""
        command = """\
import click

from helper import summarize
from lib import ConfigError


@click.command()
def report():
    try:
        summarize()
    except ConfigError:
        pass
"""
        t = trees.files(
            {
                "lib.py": LIB,
                "helper.py": HELPER,
                "preview.py": library_catch,
                "sync.py": _click_command("sync"),
                "report.py": command,
            }
        )
        assert check_inconsistent_error_handling(t) == []


class TestLibraryKeywordLiterals:
    def test_keyword_to_a_library_call_is_exempt(self, trees):
        """``extra="allow"``, ``metavar="FILE"``, ``datefmt=...``: each library's API contract."""
        module = """\
import argparse
import logging

import click
from pydantic import ConfigDict

config = ConfigDict(extra="allow")
option = click.option("--analysis", metavar="FILE")
logging.basicConfig(datefmt="%Y-%m-%d %H:%M:%S")
parser = argparse.ArgumentParser()
parser.add_argument("--out", metavar="OUTDIR")
table = dict(kind="widget")
"""
        t = trees.files({f"{name}.py": module for name in ("a", "b", "c")})
        assert check_scattered_constants(t) == []

    def test_keyword_to_a_project_call_still_counts(self, trees):
        """A project function, a project class's method and a local alias resolve to project code."""
        lib = """\
def make(kind=None):
    return kind


class Widget:
    def configure(self, mode=None):
        return mode
"""
        module = """\
from pydantic import ConfigDict

from lib import Widget, make

config = ConfigDict(extra="allow")
made = make(kind="widget")
Widget().configure(mode="turbo")
build = make
built = build(kind="gadget")
"""
        t = trees.files({"lib.py": lib, **{f"{n}.py": module for n in ("a", "b", "c")}})
        assert sorted(f.message.split(" appears")[0] for f in check_scattered_constants(t)) == [
            "Literal 'gadget'",
            "Literal 'turbo'",
            "Literal 'widget'",
        ]

    def test_a_coincidental_collision_keeps_its_other_sites(self, trees):
        """Only the library keyword drops out; deciding the rest are unrelated is the judgment."""
        constant = 'if partner_format == "json": pass\n'
        library = 'from pydantic import BaseModel\n\nBaseModel().model_dump(mode="json")\n'
        t = trees.files({"a.py": constant, "b.py": constant, "c.py": constant, "d.py": library})
        assert [f.message for f in check_scattered_constants(t)] == [
            "Literal 'json' appears in 3 files (a.py:1, b.py:1, c.py:1)"
        ]


class TestJudgmentIdiomsKeepFlagging:
    def test_cli_catch_print_exit_is_still_a_duplicate_handler(self, trees):
        """Entry points set aside for error handling still duplicate their handler."""
        handler = "        click.echo(str(e), err=True)\n        raise SystemExit(1)\n"
        t = trees.files(
            {
                "lib.py": LIB,
                "helper.py": HELPER,
                "sync.py": _click_command("sync", handler),
                "audit.py": _click_command("audit", handler),
            }
        )
        assert check_inconsistent_error_handling(t) == []
        findings = check_duplicate_except_blocks(t)
        assert len(findings) == 1
        assert "ConfigError" in findings[0].message

    def test_or_none_name_is_still_flagged(self, trees):
        """A ``*_or_none`` name is not an exemption from return-none."""
        lib = """\
def find_user_or_none(users, name):
    for user in users:
        if user.name == name:
            return user
    return None
"""
        caller = """\
from lib import find_user_or_none

user = find_user_or_none([], "a")
if user is None:
    raise SystemExit(1)
"""
        t = trees.files({"lib.py": lib, "a.py": caller, "b.py": caller})
        assert [f.message for f in check_return_none_instead_of_raise(t)] == [
            "find_user_or_none() returns None in some branches and 2 of 2 caller(s) guard "
            "against None"
        ]
