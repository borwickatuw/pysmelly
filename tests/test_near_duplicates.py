"""Tests for near-duplicate-with-drift and AnalysisContext.near_duplicate_pairs."""

import ast

from pysmelly.checks.near_duplicates import (
    MAX_DIFF_LINES,
    _render,
    check_near_duplicate_with_drift,
)

# An ink-coverage fallback, as a real project's engines copy it: three statements,
# the drift inside one large call.
FALLBACK = """\
def _fallback_block(placement, attrs):
    \"\"\"The ≥1-block rule, answered by measuring ink.\"\"\"
    coverage = ink_coverage(placement.surface.grayscale)
    blank = coverage < THRESHOLD
    return Block(
        anchor=placement.anchor,
        type=BLANK if blank else IMAGE_TYPE,
        kind=None if blank else SURROGATE,
        content=None,
        confidence=measured(basis=INK_COVERAGE, score=margin_score(coverage)),
        provenance=Provenance(
            source=ENGINE_NAME,
            attrs={
                **attrs,
                "threshold": THRESHOLD,
                "coverage": coverage,
                **(guarded_stamp(coverage) if blank else {}),
            },
        ),
        sequence=placement.sequence,
    )
"""

# The same block after one copy dropped the guard stamp and inlined the basis.
DRIFTED = """\
def _fallback_block(placement, attrs):
    coverage = ink_coverage(placement.surface.grayscale)
    is_blank = coverage < THRESHOLD
    return Block(
        anchor=placement.anchor,
        type=BLANK if is_blank else IMAGE_TYPE,
        kind=None if is_blank else SURROGATE,
        content=None,
        confidence=measured(basis="ink-coverage", score=margin_score(coverage)),
        provenance=Provenance(
            source=ENGINE_NAME,
            attrs={**attrs, "threshold": THRESHOLD, "coverage": coverage},
        ),
        sequence=placement.sequence,
    )
"""

# Two checks written to one skeleton: the same walk, guard and Finding(...)
# tail, filled with different names, calls and literals. Same shape, no copy.
ENV_CHECK = """\
def check_env_fallbacks(ctx):
    findings = []
    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key_name = _get_env_call_key(node)
            if key_name is None:
                continue
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="env-fallbacks",
                    message=f"os.environ.get({key_name!r}) has a fallback default",
                    severity=Severity.HIGH,
                )
            )
    return findings
"""

PASSWORD_CHECK = """\
def check_plaintext_passwords(ctx):
    found = []
    for path, module in source_trees(ctx.all_trees):
        for expr in ast.walk(module):
            if not isinstance(expr, ast.Compare):
                continue
            match = _plaintext_comparison(expr)
            if match is None:
                continue
            found.append(
                Finding(
                    file=str(path),
                    line=expr.lineno,
                    check="plaintext-passwords",
                    message=f"{match} compared with == — use hmac.compare_digest()",
                    severity=Severity.HIGH,
                )
            )
    return found
"""

UNRELATED = """\
def summarize(rows, out):
    totals = {}
    for row in rows:
        key = row.get("kind") or "other"
        totals[key] = totals.get(key, 0) + int(row["count"])
    with open(out, "w", encoding="utf-8") as handle:
        for key in sorted(totals):
            handle.write(f"{key}\\t{totals[key]}\\n")
    return len(totals)
"""


class TestNearDuplicateWithDrift:
    def test_drifted_copies_fire_with_a_unified_diff(self, trees):
        ctx = trees.files({"engines/layout.py": FALLBACK, "engines/omr.py": DRIFTED})
        findings = check_near_duplicate_with_drift(ctx)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.file == "engines/layout.py"
        assert finding.line == 3  # the first statement after the docstring
        message = finding.message
        assert message.startswith("2 copies of one block have drifted apart")
        assert "layout.py:_fallback_block():3-21" in message
        assert "omr.py:_fallback_block():2-15" in message
        assert "--- layout.py:_fallback_block():3-21 (drift at 10, 17)" in message
        assert "+++ omr.py:_fallback_block():2-15 (drift at 9)" in message
        assert "-    confidence=measured(basis=INK_COVERAGE, score=margin_score(coverage))," in (
            message
        )
        assert "+    confidence=measured(basis='ink-coverage', score=margin_score(coverage))," in (
            message
        )
        assert "-            **(guarded_stamp(coverage) if blank else {})," in message
        # Unchanged lines are context, not drift.
        assert "     anchor=placement.anchor," in message
        assert message.rstrip().endswith(
            "`# pysmelly: ignore[near-duplicate-with-drift]` on line 3."
        )

    def test_identical_copies_are_left_to_duplicate_blocks(self, trees):
        ctx = trees.files({"a.py": FALLBACK, "b.py": FALLBACK.replace("THRESHOLD", "LIMIT")})
        assert check_near_duplicate_with_drift(ctx) == []
        assert ctx.near_duplicate_pairs == []

    def test_dissimilar_functions_do_not_pair(self, trees):
        ctx = trees.files({"a.py": FALLBACK, "b.py": UNRELATED})
        assert check_near_duplicate_with_drift(ctx) == []

    def test_one_skeleton_filled_differently_is_not_a_copy(self, trees):
        ctx = trees.files({"env.py": ENV_CHECK, "passwords.py": PASSWORD_CHECK})
        assert check_near_duplicate_with_drift(ctx) == []
        assert ctx.near_duplicate_pairs == []

    def test_one_skeleton_filled_alike_is_a_copy(self, trees):
        # The same skeleton with the names and literals carried over is a
        # copy that drifted in its guard: the filter reads content, not shape.
        drifted = ENV_CHECK.replace("def check_env_fallbacks", "def check_env_defaults").replace(
            "            if key_name is None:\n                continue\n",
            "            if not key_name:\n                continue\n",
        )
        ctx = trees.files({"env.py": ENV_CHECK, "defaults.py": drifted})
        findings = check_near_duplicate_with_drift(ctx)
        assert len(findings) == 1
        assert findings[0].message.startswith("2 copies of one block")

    def test_blocks_under_the_size_floor_do_not_pair(self, trees):
        small_a = "def f(a, b):\n    x = a.left + b\n    return max(x, a.top)\n"
        small_b = "def g(a, b):\n    x = a.left - b\n    return min(x, a.top)\n"
        ctx = trees.files({"a.py": small_a, "b.py": small_b})
        assert check_near_duplicate_with_drift(ctx) == []

    def test_copies_within_one_module_are_left_alone(self, trees):
        renamed = DRIFTED.replace("def _fallback_block", "def _empty_surface")
        ctx = trees.files({"engines/layout.py": FALLBACK + "\n\n" + renamed})
        assert check_near_duplicate_with_drift(ctx) == []

    def test_test_files_are_not_compared(self, trees):
        ctx = trees.files({"engines/layout.py": FALLBACK, "tests/test_layout.py": DRIFTED})
        assert check_near_duplicate_with_drift(ctx) == []

    def test_colliding_basenames_print_unique_path_suffixes(self, trees):
        ctx = trees.files({"ocr/engine.py": FALLBACK, "omr/engine.py": DRIFTED})
        message = check_near_duplicate_with_drift(ctx)[0].message
        assert "ocr/engine.py:_fallback_block():3-21" in message
        assert "omr/engine.py:_fallback_block():2-15" in message

    def test_copy_at_the_tail_of_a_larger_function_pairs(self, trees):
        # The function's own first half is too large for the whole body to
        # pair; the tail alone is the copy.
        tail = (
            "def paragraph_block(placement, attrs, paragraph):\n"
            "    if paragraph.words:\n"
            "        words = [w.text.strip() for w in paragraph.words if w.text]\n"
            "        language = detect(words, hint=attrs.get('language'))\n"
            "        return read(paragraph, words=words, language=language)\n"
            "    log.warning('empty paragraph %s on %s', paragraph.id, placement.page)\n"
            + "\n".join(DRIFTED.splitlines()[1:])
            + "\n"
        )
        ctx = trees.files({"a.py": FALLBACK, "b.py": tail})
        findings = check_near_duplicate_with_drift(ctx)
        assert len(findings) == 1
        # The tail window that still clears the threshold, not the whole body.
        assert "b.py:paragraph_block():6-20" in findings[0].message

    def test_cluster_of_copies_is_one_finding(self, trees):
        third = DRIFTED.replace("sequence=placement.sequence,", "sequence=0,")
        ctx = trees.files({"a.py": FALLBACK, "b.py": DRIFTED, "c.py": third})
        findings = check_near_duplicate_with_drift(ctx)
        assert len(findings) == 1
        assert findings[0].message.startswith("3 copies of one block")

    def test_identical_twins_do_not_report_their_own_windows(self, trees):
        # a.py and b.py are the same code; c.py drifted from both. The head of
        # a.py against the whole of b.py is no drift and must not be shown.
        ctx = trees.files({"a.py": DRIFTED, "b.py": DRIFTED, "c.py": FALLBACK})
        findings = check_near_duplicate_with_drift(ctx)
        assert len(findings) == 1
        message = findings[0].message
        assert message.startswith("3 copies of one block")
        assert "+++ b.py" not in message  # a.py against b.py is never diffed

    def test_diff_is_capped_and_says_so(self, trees):
        keywords = [f"field_{n}=value.attr_{n}" for n in range(60)]
        changed = [f"field_{n}=other.attr_{n}" for n in range(60)]

        def builder(name, kws):
            return (
                f"def {name}(value, other):\n"
                f"    result = Record(\n        " + ",\n        ".join(kws) + ",\n    )\n"
                "    result.check()\n"
                "    return result\n"
            )

        ctx = trees.files(
            {"a.py": builder("build_a", keywords), "b.py": builder("build_b", changed)}
        )
        # The values differ only in names, so tokens match; drift needs shape.
        assert check_near_duplicate_with_drift(ctx) == []
        drifted = keywords[:30] + [f"field_{n}=value.attr_{n}.strip()" for n in range(30, 60)]
        ctx = trees.files(
            {"a.py": builder("build_a", keywords), "b.py": builder("build_b", drifted)}
        )
        message = check_near_duplicate_with_drift(ctx)[0].message
        diff_lines = [line for line in message.splitlines()[1:-1] if line.startswith("      ")]
        assert len(diff_lines) == MAX_DIFF_LINES + 1
        assert f"diff capped at {MAX_DIFF_LINES} lines" in message

    def test_nested_function_does_not_pair_with_its_parent(self, trees):
        nested = (
            "def outer(placement, attrs):\n"
            + "\n".join("    " + line for line in DRIFTED.splitlines())
            + "\n"
            + "\n".join(DRIFTED.splitlines()[1:])
            + "\n"
        )
        ctx = trees.files({"a.py": nested})
        assert ctx.near_duplicate_pairs == []


class TestNearDuplicatePairs:
    def test_pairs_carry_spans_similarity_and_aligned_lines(self, trees):
        ctx = trees.files({"layout.py": FALLBACK, "omr.py": DRIFTED})
        pairs = ctx.near_duplicate_pairs
        assert len(pairs) == 1
        pair = pairs[0]
        assert (pair.first.file, pair.first.scope) == ("layout.py", "_fallback_block")
        assert (pair.first.line_start, pair.first.line_end) == (3, 21)
        assert (pair.second.file, pair.second.line_start, pair.second.line_end) == (
            "omr.py",
            2,
            15,
        )
        assert 0.8 <= pair.similarity < 1.0
        # The stretches walk both copies in order, shared and drift alternating.
        first = pair.aligned[0]
        assert (first.first, first.second, first.same) == ((3, 10), (2, 9), True)
        assert [a.same for a in pair.aligned] == [True, False, True, False, True]
        drift = [(a.first, a.second) for a in pair.aligned if not a.same]
        # The basis constant became a literal (layout 10 / omr 9), and the
        # guard stamp (layout 17) has no counterpart in omr at all.
        assert drift == [((10, 10), (9, 9)), ((17, 17), None)]

    def test_message_names_the_drifted_lines(self, trees):
        ctx = trees.files({"layout.py": FALLBACK, "omr.py": DRIFTED})
        message = check_near_duplicate_with_drift(ctx)[0].message
        assert "--- layout.py:_fallback_block():3-21 (drift at 10, 17)" in message
        assert "+++ omr.py:_fallback_block():2-15 (drift at 9)" in message

    def test_pairs_are_memoized_per_context(self, trees):
        ctx = trees.files({"layout.py": FALLBACK, "omr.py": DRIFTED})
        assert ctx.near_duplicate_pairs is ctx.near_duplicate_pairs

    def test_sub_blocks_of_a_pair_are_not_pairs_of_their_own(self, trees):
        ctx = trees.files({"layout.py": FALLBACK, "omr.py": DRIFTED})
        spans = {(p.first.line_start, p.second.line_start) for p in ctx.near_duplicate_pairs}
        assert spans == {(3, 2)}


class TestRender:
    def test_long_calls_break_one_argument_per_line_and_round_trip(self):
        tree = ast.parse(FALLBACK)
        body = tree.body[0].body[1:]  # type: ignore[attr-defined]
        rendered = _render(body)
        assert "    content=None," in rendered
        assert "            **(guarded_stamp(coverage) if blank else {})," in rendered
        assert ast.dump(ast.parse("\n".join(rendered))) == ast.dump(
            ast.Module(body=body, type_ignores=[])
        )

    def test_compound_statements_render_their_headers(self):
        source = (
            "try:\n    x = 1\nexcept ValueError as err:\n    raise Boom() from err\n"
            "else:\n    y = 2\nfinally:\n    z = 3\n"
            "for a in b:\n    if a:\n        c()\n    elif d:\n        e()\n    else:\n        f()\n"
            "with open(p) as h, lock:\n    h.read()\n"
        )
        body = ast.parse(source).body
        rendered = "\n".join(_render(body))
        assert ast.dump(ast.parse(rendered)) == ast.dump(ast.Module(body=body, type_ignores=[]))
