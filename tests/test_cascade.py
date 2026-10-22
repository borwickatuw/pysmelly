"""Tests for the removal cascade's API (``checks/cascade.py``).

The dead-code messages it produces are pinned in ``test_callers.py``
(``TestDeadCodeCascade``); these pin what Phase 29's finding chains read.
"""

import ast
import textwrap
from pathlib import Path

from pysmelly.checks.cascade import MAX_CASCADE_LISTED, Cascade, removal_cascade
from pysmelly.checks.reachability import MemberKind, check_unreachable_from_entry_points
from pysmelly.context import AnalysisContext
from pysmelly.project_entry_points import load_project_entry_points

APP = Path("app.py")
LIB = Path("lib.py")

LIVE_APP = 'from lib import keep\n\n\ndef main():\n    keep()\n\n\nif __name__ == "__main__":\n    main()\n'


def _ctx(lib: str, **extra) -> AnalysisContext:
    files = {APP: LIVE_APP, LIB: textwrap.dedent(lib), **extra}
    return AnalysisContext(
        {Path(name): ast.parse(code) for name, code in files.items()}, verbose=False
    )


def _freed(cascade: Cascade) -> list[tuple[str, int, MemberKind]]:
    return [(member.name, member.line, member.kind) for member in cascade.freed]


class TestRemovalCascade:
    def test_freed_members_carry_kind_and_line_in_file_order(self):
        ctx = _ctx(
            """\
            WIDTH = 8


            def keep():
                pass


            def report():
                return _Box().pad()


            class _Box:
                def pad(self):
                    return " " * WIDTH
            """
        )
        cascade = removal_cascade(ctx, (LIB, "report"))
        assert cascade.removed == (LIB, "report")
        assert _freed(cascade) == [
            ("WIDTH", 1, MemberKind.ASSIGNMENT),
            ("_Box", 12, MemberKind.CLASS),
        ]

    def test_a_helper_another_definition_still_uses_is_kept(self):
        """Removing one of two dead functions leaves the helper they share to the other."""
        ctx = _ctx(
            """\
            def keep():
                pass


            def first():
                return _shared()


            def second():
                return _shared()


            def _shared():
                return 1
            """
        )
        assert removal_cascade(ctx, (LIB, "first")).freed == ()
        assert removal_cascade(ctx, (LIB, "second")).freed == ()

    def test_removing_the_last_user_frees_the_shared_helper(self):
        """A chain (Phase 29) removes its findings one at a time.

        Once ``second`` is gone, removing ``first`` frees the helper the two
        shared: the step-by-step form of removing both at once.
        """
        ctx = _ctx(
            """\
            def keep():
                pass


            def first():
                return _shared()


            def _shared():
                return 1
            """
        )
        assert _freed(removal_cascade(ctx, (LIB, "first"))) == [("_shared", 9, MemberKind.FUNCTION)]

    def test_an_entry_point_root_is_never_freed(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\ntool = "lib:serve"\n', encoding="utf-8"
        )
        lib = "def report():\n    return serve, _helper()\n\n\ndef serve():\n    pass\n\n\ndef _helper():\n    pass\n"
        ctx = AnalysisContext(
            {LIB: ast.parse(lib)},
            verbose=False,
            declared_entry_points=load_project_entry_points(tmp_path),
        )
        cascade = removal_cascade(ctx, (LIB, "report"))
        assert _freed(cascade) == [("_helper", 9, MemberKind.FUNCTION)]

    def test_an_unreachable_cluster_frees_nothing_beyond_itself(self):
        """A cluster is closed under the cascade, so unreachable prints none.

        Removing ``_ping`` takes the whole cluster with it, so its cascade is
        the cluster's own: exactly the other members, and nothing beyond them.
        ``_width`` is the outside definition the cluster refers to; live code
        also calls it, so it is kept. Removing any other member frees only
        members too.
        """
        ctx = _ctx(
            """\
            LIMIT = 3


            def keep():
                return _width()


            def _ping(n):
                return _pong(n - 1)


            def _pong(n):
                return _ping(n - 1) if n < LIMIT else _tail()


            def _tail():
                return _width()


            def _width():
                return 0
            """
        )
        [finding] = check_unreachable_from_entry_points(ctx)
        assert [(site.file, site.line) for site in finding.sites] == [
            ("lib.py", 1),
            ("lib.py", 8),
            ("lib.py", 12),
            ("lib.py", 16),
        ]
        cluster = {(LIB, name) for name in ("LIMIT", "_ping", "_pong", "_tail")}
        whole = removal_cascade(ctx, (LIB, "_ping"))
        assert {(LIB, "_ping")} | {(m.file, m.name) for m in whole.freed} == cluster
        for unit in cluster:
            assert {(m.file, m.name) for m in removal_cascade(ctx, unit).freed} <= cluster
        assert _freed(whole) == [
            ("LIMIT", 1, MemberKind.ASSIGNMENT),
            ("_pong", 12, MemberKind.FUNCTION),
            ("_tail", 16, MemberKind.FUNCTION),
        ]


class TestDescribe:
    def test_nothing_freed_describes_as_none(self):
        assert Cascade((LIB, "x"), ()).describe("x()", frozenset()) is None

    def test_the_listing_stops_at_the_cap_and_counts_the_rest(self):
        ctx = _ctx(
            "def keep():\n    pass\n\n\ndef report():\n    return "
            + " + ".join(f"_h{i:02}()" for i in range(MAX_CASCADE_LISTED + 3))
            + "\n"
            + "".join(
                f"\n\ndef _h{i:02}():\n    return {i}\n" for i in range(MAX_CASCADE_LISTED + 3)
            )
        )
        cascade = removal_cascade(ctx, (LIB, "report"))
        assert len(cascade.freed) == MAX_CASCADE_LISTED + 3
        described = cascade.describe("report()", frozenset({(LIB, "_h00")}))
        assert described.startswith(
            f"Removing report() also frees {MAX_CASCADE_LISTED + 3}: "
            "_h00() (lib.py:9, reported separately), _h01() (lib.py:13), "
        )
        assert described.count("(lib.py:") == MAX_CASCADE_LISTED
        assert described.endswith(" (+3 more)")
