"""Tests for structural checks."""

from pysmelly.checks.structure import check_duplicate_blocks


class TestDuplicateBlocks:
    def test_finds_duplicate_blocks(self, trees):
        t = trees.code("""\
def func_a(items):
    result = []
    for item in items:
        if item.active:
            result.append(item.name)
    filtered = [r for r in result if r]
    output = process(filtered)
    validated = check(output)
    return validated

def func_b(things):
    result = []
    for item in things:
        if item.active:
            result.append(item.name)
    filtered = [r for r in result if r]
    output = process(filtered)
    validated = check(output)
    return validated
""")
        findings = check_duplicate_blocks(t, verbose=False)
        assert len(findings) >= 1
        assert "duplicate statements repeated in these places:" in findings[0].message

    def test_ignores_short_blocks(self, trees):
        t = trees.code("""\
def func_a():
    x = 1
    return x

def func_b():
    x = 1
    return x
""")
        findings = check_duplicate_blocks(t, verbose=False)
        assert len(findings) == 0

    def test_ignores_unique_blocks(self, trees):
        t = trees.code("""\
def func_a():
    x = compute()
    y = transform(x)
    z = validate(y)
    w = finalize(z)
    return save(w)

def func_b():
    a = load()
    b = parse(a)
    c = check(b)
    d = fix(c)
    return output(d)
""")
        findings = check_duplicate_blocks(t, verbose=False)
        assert len(findings) == 0
