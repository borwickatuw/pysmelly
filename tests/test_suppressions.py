"""Self-lint: the repo's own ``# pysmelly: ignore`` directives must be full-line.

Pysmelly honours a directive on the finding line or the single line directly
above it (``cli._is_suppressed``), so a trailing directive works -- until black
or ruff-format re-wraps the statement it rides on. The re-wrap moves the comment
off the line pysmelly checks and the suppression dies while still looking
present; 40 of one fleet repo's 105 standing ignores had drifted dead exactly
that way. The rule ("full line of its own, directly above the finding line;
rationale on the lines above that") is part of the maintainer's pysmelly
review guide, and this is the repo that defines it, so the rule is enforced
here rather than trusted.

Comment *tokens* are used rather than a text search so that the trailing-form
examples inside ``cli.py``'s guidance strings are not mistaken for directives.
"""

import io
import re
import tokenize
from pathlib import Path

import pytest

from pysmelly.cli import _is_suppressed
from pysmelly.registry import Finding, Severity

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# A directive comment: the marker is the whole comment, not prose mentioning it.
_DIRECTIVE = re.compile(r"^#\s*pysmelly:\s*ignore\b")


def _directives(path: Path) -> list[tuple[int, bool]]:
    """(line number, is_trailing) for each directive comment in *path*."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    found: list[tuple[int, bool]] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT or not _DIRECTIVE.match(token.string):
            continue
        row, col = token.start
        trailing = bool(lines[row - 1][:col].strip())
        found.append((row, trailing))
    return found


SOURCE_FILES = sorted(SRC_ROOT.rglob("*.py"))


def test_source_tree_is_not_empty():
    """Guard against the glob silently matching nothing."""
    assert SOURCE_FILES


def test_repo_has_standing_directives():
    """Non-vacuity: the trailing-form check below has something to check."""
    assert any(_directives(path) for path in SOURCE_FILES)


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_directives_are_on_their_own_line(path):
    trailing = [row for row, is_trailing in _directives(path) if is_trailing]
    assert not trailing, (
        f"{path}: trailing pysmelly directive(s) on line(s) {trailing} -- "
        "a formatter re-wrap silently kills these; put the directive on its own "
        "line directly above the finding line"
    )


class TestDirectiveDetection:
    """The detector itself, on synthetic sources."""

    def test_trailing_directive_is_flagged(self, tmp_path):
        path = tmp_path / "trailing.py"
        path.write_text("x = 1  # pysmelly: ignore[dead-code]\n")
        assert _directives(path) == [(1, True)]

    def test_full_line_directive_is_not_flagged(self, tmp_path):
        path = tmp_path / "above.py"
        path.write_text("# pysmelly: ignore[dead-code]\nx = 1\n")
        assert _directives(path) == [(1, False)]

    def test_prose_mentioning_the_marker_is_not_a_directive(self, tmp_path):
        path = tmp_path / "prose.py"
        path.write_text("# Specific check: pysmelly: ignore[check-name]\nx = 1\n")
        assert _directives(path) == []

    def test_marker_inside_a_string_is_not_a_directive(self, tmp_path):
        path = tmp_path / "docs.py"
        path.write_text('HELP = "x = 1  # pysmelly: ignore[dead-code]"\n')
        assert _directives(path) == []


class TestDecoratedDefinitionPlacement:
    """Where the full-line directive goes when the finding line is decorated.

    A finding on a class or function anchors on the ``class``/``def`` line, not
    on its decorators, and ``_is_suppressed`` looks at exactly two lines: the
    finding line and the one directly above it. So for a decorated definition
    the only placements that work are trailing (formatter-fragile) or a full
    line *between* the last decorator and the definition. Above the decorators
    is out of the window however many there are -- as is a directive separated
    from the definition by its own rationale lines.
    """

    @staticmethod
    def _suppressed(source: str, finding_line: int) -> bool:
        finding = Finding(
            file="mod.py",
            line=finding_line,
            check="shotgun-surgery",
            message="",
            severity=Severity.MEDIUM,
        )
        return _is_suppressed(finding, {"mod.py": source.splitlines()})

    def test_directive_under_the_decorator_suppresses(self):
        source = """\
@dataclass
# pysmelly: ignore[shotgun-surgery]
class Commit:
    pass
"""
        assert self._suppressed(source, 3)

    def test_directive_above_the_decorator_is_out_of_the_window(self):
        source = """\
# pysmelly: ignore[shotgun-surgery]
@dataclass
class Commit:
    pass
"""
        assert not self._suppressed(source, 3)

    def test_directive_above_two_decorators_falls_out_of_the_window(self):
        source = """\
# pysmelly: ignore[shotgun-surgery]
@final
@dataclass
class Commit:
    pass
"""
        assert not self._suppressed(source, 4)

    def test_directive_under_the_decorators_survives_a_second_decorator(self):
        source = """\
@final
@dataclass
# pysmelly: ignore[shotgun-surgery]
class Commit:
    pass
"""
        assert self._suppressed(source, 4)

    def test_rationale_lines_do_not_count_as_the_directive(self):
        source = """\
# pysmelly: ignore[shotgun-surgery]
# rationale line one
@dataclass
class Commit:
    pass
"""
        assert not self._suppressed(source, 4)

    def test_rationale_above_the_directive_is_the_working_order(self):
        source = """\
# rationale line one
# rationale line two
@dataclass
# pysmelly: ignore[shotgun-surgery]
class Commit:
    pass
"""
        assert self._suppressed(source, 5)
