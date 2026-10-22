"""Finding chains (``chains.py``): findings one edit resolves print once, as one work item.

The end-to-end tests run the CLI over files on disk, since chains are
joined after suppression and filtering; the rest call ``chain_findings`` on
what the checks return.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

from pysmelly.chains import Chain, chain_findings
from pysmelly.cli import _narrow_sites, main
from pysmelly.context import AnalysisContext
from pysmelly.output import format_text, printed_findings
from pysmelly.registry import Finding, Severity, Site, Subject, parameter_subject, run_check

LAYERS = """\
def outer(path, mode=None):
    print(path)
    return middle(path, mode)


def middle(path, mode=None):
    print(path)
    return inner(path, mode)


def inner(path, mode=None):
    print(path)
    return mode.upper()
"""

APP = """\
from pkg.layers import outer


def main():
    outer("a.txt", "fast")
    outer("b.txt", "fast")


if __name__ == "__main__":
    main()
"""

# The six findings the one threaded ``mode`` parameter produces.
MODE_FINDINGS = [
    "pkg/layers.py:1: [constant-args]",
    "pkg/layers.py:1: [pass-through-params]",
    "pkg/layers.py:1: [unused-defaults]",
    "pkg/layers.py:6: [pass-through-params]",
    "pkg/layers.py:6: [unused-defaults]",
    "pkg/layers.py:11: [unused-defaults]",
]

CHECKS = ["unused-defaults", "constant-args", "pass-through-params"]


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")


def _cli(tmp_path: Path, capsys, *flags: str) -> str:
    with pytest.raises(SystemExit):
        main(["--no-context", *flags, str(tmp_path)])
    return capsys.readouterr().out


def _section(out: str, heading: str) -> list[str]:
    """The lines under the first ``=== heading`` line, up to the blank line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"=== {heading}"))
    end = lines.index("", start)
    return lines[start:end]


def _ctx(files: dict[str, str]) -> AnalysisContext:
    return AnalysisContext(
        {Path(name): ast.parse(textwrap.dedent(code)) for name, code in files.items()},
        verbose=False,
    )


def _chained(files: dict[str, str], checks: list[str]) -> list[Finding]:
    ctx = _ctx(files)
    return chain_findings(ctx, [f for name in checks for f in run_check(name, ctx)])


def _groups(findings: list[Finding]) -> list[list[tuple[str, int, str]]]:
    """Each chain's members as ``(file, line, check)``, sorted, chains in order."""
    by_chain: dict[Chain, list[tuple[str, int, str]]] = {}
    for f in findings:
        if f.chain is not None:
            by_chain.setdefault(f.chain, []).append((f.file, f.line, f.check))
    return sorted(sorted(members) for members in by_chain.values())


class TestThreeLayerPassThrough:
    @pytest.fixture
    def tree(self, tmp_path):
        _write(tmp_path, {"pkg/__init__.py": "", "pkg/layers.py": LAYERS, "pkg/app.py": APP})
        return tmp_path

    def test_one_chain_lists_every_touchpoint(self, tree, capsys):
        section = _section(_cli(tree, capsys, "--more-please"), "finding chains")
        assert section[0] == "=== finding chains (1 work item(s), 6 finding(s)) ==="
        assert section[1] == (
            "  parameter chain over 3 function(s): outer(mode) pkg/layers.py:1, "
            "middle(mode) pkg/layers.py:6, inner(mode) pkg/layers.py:11"
        )
        assert [re.sub(r"\] .*", "]", line.strip()) for line in section[2:]] == MODE_FINDINGS

    def test_chained_findings_are_not_printed_again_under_their_checks(self, tree, capsys):
        out = _cli(tree, capsys, "--more-please")
        assert "=== unused-defaults" not in out
        assert "=== constant-args" not in out
        assert "=== pass-through-params" not in out
        assert out.count("outer() param 'mode' always receives 'fast'") == 1

    def test_summary_and_total_still_count_every_finding(self, tree, capsys):
        full = _cli(tree, capsys, "--more-please")
        summary = _cli(tree, capsys, "--summary")
        counts = dict(re.findall(r"^  (\S+)\s+\[\w+\s*\]\s+(\d+)$", summary, re.MULTILINE))
        assert counts["unused-defaults"] == "3"
        assert counts["constant-args"] == "1"
        assert counts["pass-through-params"] == "2"
        assert "finding chains" not in summary
        total = int(re.search(r"^Total: (\d+) finding", full, re.MULTILINE).group(1))
        assert total == sum(int(n) for n in counts.values())

    def test_an_ignored_touchpoint_leaves_the_chain(self, tree, capsys):
        layers = LAYERS.replace("def inner(", "# pysmelly: ignore[unused-defaults]\ndef inner(")
        _write(tree, {"pkg/layers.py": layers})
        section = _section(_cli(tree, capsys, "--more-please"), "finding chains")
        assert section[0] == "=== finding chains (1 work item(s), 5 finding(s)) ==="
        assert "inner(mode)" not in section[1]


class TestParameterChains:
    def test_same_function_and_parameter_join(self):
        findings = _chained(
            {
                "lib.py": """\
                def scale(value, factor=None):
                    return value * factor
                """,
                "app.py": """\
                from lib import scale

                scale(1, 2)
                scale(3, 2)
                """,
            },
            ["unused-defaults", "constant-args"],
        )
        assert _groups(findings) == [
            [("lib.py", 1, "constant-args"), ("lib.py", 1, "unused-defaults")]
        ]
        assert (
            findings[0].chain.title == "parameter chain over 1 function(s): scale(factor) lib.py:1"
        )

    def test_two_parameters_of_one_function_do_not_join(self):
        findings = _chained(
            {
                "lib.py": """\
                def scale(value=None, factor=None):
                    return value * factor
                """,
                "app.py": """\
                from lib import scale

                scale(1, 2)
                scale(3, 2)
                """,
            },
            ["unused-defaults"],
        )
        assert len(findings) == 2
        assert _groups(findings) == []

    def test_a_layer_that_also_reads_the_parameter_is_not_a_conduit(self):
        findings = _chained(
            {
                "lib.py": """\
                def outer(mode=None):
                    print(mode)
                    return inner(mode)


                def inner(mode=None):
                    return mode.upper()
                """,
                "app.py": "from lib import outer\n\nouter('a')\n",
            },
            CHECKS,
        )
        assert [f.check for f in findings] == ["unused-defaults", "unused-defaults"]
        assert _groups(findings) == []

    def test_a_layer_that_also_hands_it_to_an_unflagged_parameter_is_not_a_conduit(self):
        findings = _chained(
            {
                "lib.py": """\
                def outer(mode=None):
                    report(mode)
                    return inner(mode)


                def inner(mode=None):
                    return mode.upper()


                def report(mode):
                    print(mode)
                """,
                "app.py": "from lib import outer, report\n\nouter('a')\nreport('b')\n",
            },
            ["unused-defaults"],
        )
        assert [f.check for f in findings] == ["unused-defaults", "unused-defaults"]
        assert _groups(findings) == []

    def test_a_layer_may_thread_it_to_several_flagged_parameters(self):
        findings = _chained(
            {
                "lib.py": """\
                def outer(mode=None):
                    left(mode)
                    return right(mode)


                def left(mode=None):
                    return mode.upper()


                def right(mode=None):
                    return mode.lower()
                """,
                "app.py": "from lib import outer\n\nouter('a')\n",
            },
            ["unused-defaults"],
        )
        assert _groups(findings) == [
            [
                ("lib.py", 1, "unused-defaults"),
                ("lib.py", 6, "unused-defaults"),
                ("lib.py", 10, "unused-defaults"),
            ]
        ]

    def test_a_different_value_passed_on_does_not_thread(self):
        findings = _chained(
            {
                "lib.py": """\
                def outer(mode=None):
                    other = mode.lower()
                    return inner(other)


                def inner(mode=None):
                    return mode.upper()
                """,
                "app.py": "from lib import outer\n\nouter('a')\n",
            },
            CHECKS,
        )
        assert _groups(findings) == []

    def test_a_nested_function_parameter_shadows_the_outer_one(self):
        findings = _chained(
            {
                "lib.py": """\
                def outer(mode=None):
                    def run(mode):
                        return inner(mode)
                    return run(mode.lower())


                def inner(mode=None):
                    return mode.upper()
                """,
                "app.py": "from lib import outer\n\nouter('a')\n",
            },
            CHECKS,
        )
        assert _groups(findings) == []

    def test_a_library_method_of_the_same_name_is_not_a_forward(self):
        files = {
            "lib.py": """\
            def fetch(body, size=None):
                return body.read(size)


            class Stream:
                def read(self, size):
                    return b""
            """,
            "app.py": "from lib import fetch\n\nfetch(object(), 4)\n",
        }
        findings = _chained(files, ["unused-defaults", "vestigial-params"])
        assert {f.check for f in findings} == {"unused-defaults", "vestigial-params"}
        assert _groups(findings) == []

    def test_a_self_call_forwards_to_a_method_of_the_same_class(self):
        findings = _chained(
            {
                "lib.py": """\
                class _Runner:
                    def run(self, mode=None):
                        return self.step(mode)

                    def step(self, mode=None):
                        return mode.upper()


                def go():
                    _Runner().run("a")
                """,
            },
            ["unused-defaults"],
        )
        assert _groups(findings) == [
            [("lib.py", 2, "unused-defaults"), ("lib.py", 5, "unused-defaults")]
        ]

    def test_titles_list_callers_before_the_functions_they_forward_to(self):
        findings = _chained(
            {
                "lib.py": """\
                def inner(mode=None):
                    return mode.upper()


                def outer(mode=None):
                    return inner(mode)
                """,
                "app.py": "from lib import outer\n\nouter('a')\n",
            },
            CHECKS,
        )
        titles = {f.chain.title for f in findings if f.chain is not None}
        assert titles == {
            "parameter chain over 2 function(s): outer(mode) lib.py:5, inner(mode) lib.py:1"
        }

    def test_a_title_names_each_function_once_at_its_def_line(self):
        source = "@cache\ndef fetch(url, retries, delay):\n    return url\n"
        ctx = _ctx({"lib.py": source})
        node = ctx.all_trees[Path("lib.py")].body[0]

        def about(*names: str) -> Finding:
            subjects = tuple(
                parameter_subject("lib.py", node, name, vestigial=True) for name in names
            )
            return Finding("lib.py", 2, "demo", "/".join(names), Severity.LOW, subjects=subjects)

        chained = chain_findings(ctx, [about("retries"), about("delay", "retries")])
        assert chained[0].chain is chained[1].chain is not None
        assert chained[0].chain.title == (
            "parameter chain over 1 function(s): fetch(delay, retries) lib.py:2"
        )

    def test_a_vestigial_and_a_forwarded_view_of_one_parameter_join(self):
        source = "def fetch(url, retries):\n    return url\n"
        ctx = _ctx({"lib.py": source})
        node = ctx.all_trees[Path("lib.py")].body[0]
        findings = [
            Finding(
                "lib.py",
                1,
                check,
                check,
                Severity.LOW,
                subjects=(parameter_subject("lib.py", node, "retries", vestigial=vestigial),),
            )
            for check, vestigial in [("a", True), ("b", False)]
        ]
        chained = chain_findings(ctx, findings)
        assert chained[0].chain is chained[1].chain is not None

    def test_a_literal_or_a_definition_is_no_chain_node(self):
        source = "def fetch(url, mode):\n    return url\n"
        ctx = _ctx({"lib.py": source})
        literal = Subject("mode", "lib.py", (1, 2), None, vestigial=True)
        definition = Subject("fetch", "lib.py", None, None, vestigial=True)
        node = ctx.all_trees[Path("lib.py")].body[0]
        parameter = parameter_subject("lib.py", node, "mode", vestigial=True)
        findings = [
            Finding("lib.py", 1, "a", "a", Severity.LOW, subjects=(literal,)),
            Finding("lib.py", 1, "b", "b", Severity.LOW, subjects=(definition,)),
            Finding("lib.py", 1, "c", "c", Severity.LOW, subjects=(parameter,)),
        ]
        assert all(f.chain is None for f in chain_findings(ctx, findings))


LIVE_APP = 'from lib import keep\n\n\ndef main():\n    keep()\n\n\nif __name__ == "__main__":\n    main()\n'


class TestCascadeChains:
    def test_findings_in_what_a_removal_frees_join_the_dead_function(self):
        findings = _chained(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep():
                    pass


                def report(rows):
                    return _render(rows, None)


                def _render(rows, width):
                    return rows
                """,
            },
            ["dead-code", "vestigial-params"],
        )
        assert _groups(findings) == [
            [("lib.py", 5, "dead-code"), ("lib.py", 9, "vestigial-params")]
        ]
        assert findings[0].chain.title == (
            "delete report() (lib.py:5) and the 1 definition(s) its removal frees: "
            "_render() (lib.py:9)"
        )

    def test_a_finding_in_live_code_stays_out(self):
        findings = _chained(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep(width):
                    return _render(1, None)


                def report(rows):
                    return _render(rows, None)


                def _render(rows, width):
                    return rows
                """,
            },
            ["dead-code", "vestigial-params"],
        )
        assert sorted(f.check for f in findings) == [
            "dead-code",
            "vestigial-params",
            "vestigial-params",
        ]
        assert _groups(findings) == []

    def test_a_dead_function_another_one_frees_shares_its_chain(self):
        findings = _chained(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep():
                    pass


                def report(target):
                    return getattr(target, "summarize")()


                def summarize():
                    return 1
                """,
            },
            ["dead-code"],
        )
        # dead-code reads calls and imports by name, so ``summarize`` is dead
        # too; the unit graph follows the getattr literal, so removing
        # ``report`` frees it.
        assert _groups(findings) == [[("lib.py", 5, "dead-code"), ("lib.py", 9, "dead-code")]]
        assert findings[0].chain.title == (
            "delete report() (lib.py:5) and the 1 definition(s) its removal frees: "
            "summarize() (lib.py:9)"
        )

    def test_a_multi_site_finding_joins_only_when_every_site_is_removed(self):
        ctx = _ctx(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep():
                    pass


                def report():
                    return 1
                """,
            }
        )
        dead = run_check("dead-code", ctx)

        def spread(*sites: Site) -> Finding:
            return Finding(
                "lib.py", 6, "demo", "m", Severity.LOW, sites=sites, render=lambda _kept: "m"
            )

        inside = spread(Site("lib.py", 5), Site("lib.py", 6))
        outside = spread(Site("lib.py", 6), Site("lib.py", 2))
        chained = chain_findings(ctx, [*dead, inside, outside])
        assert chained[1].chain is chained[0].chain is not None
        assert chained[2].chain is None

    def test_a_dead_function_inside_a_module_level_block_roots_no_chain(self):
        ctx = _ctx(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep():
                    pass


                if True:
                    def report(rows):
                        return rows
                """,
            }
        )
        dead = run_check("dead-code", ctx)
        assert [(f.file, f.line) for f in dead] == [("lib.py", 6)]
        inside = Finding("lib.py", 7, "demo", "m", Severity.LOW)
        assert all(f.chain is None for f in chain_findings(ctx, [*dead, inside]))

    def test_cascade_chains_take_their_findings_first(self):
        findings = _chained(
            {
                "app.py": LIVE_APP,
                "lib.py": """\
                def keep():
                    pass


                def report(mode=None):
                    return _render(mode)


                def _render(mode):
                    return mode
                """,
                "use.py": "from lib import report\n\n\ndef unused():\n    report('a')\n",
            },
            ["dead-code", "unused-defaults"],
        )
        assert _groups(findings) == [[("lib.py", 5, "unused-defaults"), ("use.py", 4, "dead-code")]]


# Registered checks standing in for any three: the printed text reads the registry.
HIGH, LOW, OTHER = "dead-code", "lone-dissenter", "long-function"


def _f(line: int, check: str, chain: Chain | None, severity: Severity = Severity.MEDIUM) -> Finding:
    return Finding("a.py", line, check, f"{check} at {line}", severity, chain=chain)


class TestFormat:
    def test_no_chain_no_section(self):
        out = format_text([_f(1, HIGH, None)], 1, None, summary=False, max_findings=0, header=[])
        assert "finding chains" not in out

    def test_a_chain_with_a_displayed_member_lists_every_member(self):
        chain = Chain("work")
        findings = [
            _f(1, HIGH, chain, Severity.HIGH),
            _f(9, LOW, chain, Severity.LOW),
            *(_f(20 + n, OTHER, None, Severity.LOW) for n in range(3)),
        ]
        out = format_text(findings, 1, None, summary=False, max_findings=2, header=[])
        assert _section(out, "finding chains") == [
            "=== finding chains (1 work item(s), 2 finding(s)) ===",
            "  work",
            f"    a.py:1: [{HIGH}] {findings[0].text}",
            f"    a.py:9: [{LOW}] {findings[1].text}",
        ]
        assert f"=== {LOW}" not in out
        assert "Total: 5 finding(s)" in out

    def test_a_check_heading_counts_its_unchained_findings_shown_or_not(self):
        """Chain members print in the chain; the cap's leftovers are still counted."""
        chain = Chain("work")
        findings = [
            _f(1, HIGH, chain, Severity.HIGH),
            _f(9, LOW, chain, Severity.LOW),
            _f(20, LOW, None, Severity.LOW),
            _f(21, LOW, None, Severity.LOW),
        ]
        capped = format_text(findings, 1, None, summary=False, max_findings=3, header=[])
        assert f"=== {LOW} (2 finding(s), 1 shown) ===" in capped.splitlines()
        full = format_text(findings, 1, None, summary=False, max_findings=0, header=[])
        assert f"=== {LOW} (2 finding(s)) ===" in full.splitlines()

    def test_a_chain_member_prints_its_notes(self):
        chain = Chain("work")
        noted = Finding("a.py", 1, HIGH, "x at 1", Severity.HIGH, chain=chain, notes=("why",))
        other = _f(2, LOW, chain)
        out = format_text([noted, other], 1, None, summary=False, max_findings=0, header=[])
        assert _section(out, "finding chains")[2:] == [
            f"    a.py:1: [{HIGH}] {noted.text}",
            "      why",
            f"    a.py:2: [{LOW}] {other.text}",
        ]

    def test_members_beyond_the_cap_are_printed_findings(self):
        chain = Chain("work")
        high = _f(1, HIGH, chain, Severity.HIGH)
        low = _f(9, LOW, chain, Severity.LOW)
        other = _f(5, OTHER, None, Severity.MEDIUM)
        printed = printed_findings([low, other, high], summary=False, max_findings=1)
        assert printed == [high, low]
        assert printed_findings([low, high], summary=True, max_findings=1) == []

    def test_a_chain_with_no_displayed_member_is_not_printed(self):
        chain = Chain("work")
        findings = [
            _f(1, HIGH, None, Severity.HIGH),
            _f(9, LOW, chain, Severity.LOW),
            _f(10, LOW, chain, Severity.LOW),
        ]
        out = format_text(findings, 1, None, summary=False, max_findings=1, header=[])
        assert "finding chains" not in out

    def test_summary_prints_no_chains(self):
        chain = Chain("work")
        findings = [_f(1, "x", chain), _f(2, "x", chain)]
        out = format_text(findings, 1, None, summary=True, max_findings=0, header=[])
        assert "finding chains" not in out
        assert "  x" in out


def test_narrowing_a_multi_site_finding_keeps_its_parameter_subjects():
    parameter = Subject("x", "a.py", (1, 1), "f", vestigial=False)
    finding = Finding(
        "a.py",
        1,
        "demo",
        "m",
        Severity.LOW,
        subjects=(parameter,),
        sites=(Site("a.py", 1), Site("a.py", 2)),
        render=lambda _kept: "m",
    )
    lines = {"a.py": ["x = 1\n", "y = 2  # pysmelly: ignore[demo]\n"]}
    narrowed = _narrow_sites(finding, lines, _ctx({"a.py": "x = 1\ny = 2\n"}))
    assert narrowed is not None
    assert narrowed.sites == (Site("a.py", 1),)
    assert narrowed.subjects == (parameter,)
