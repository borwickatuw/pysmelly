"""Tests for structural checks."""

from pysmelly.checks.structure import check_duplicate_blocks, check_duplicate_except_blocks


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


class TestDuplicateExceptBlocks:
    def test_cross_file_duplicate_with_same_messages(self, trees):
        t = trees.files(
            {
                "deploy.py": """\
def push():
    try:
        run_command()
    except ValueError:
        log("Push failed")
        notify("Push failed")
""",
                "ci_deploy.py": """\
def push():
    try:
        run_command()
    except ValueError:
        log("Push failed")
        notify("Push failed")
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 1
        assert "except ValueError" in findings[0].message
        assert "duplicate handler" in findings[0].message

    def test_attribute_exception_types(self, trees):
        t = trees.files(
            {
                "a.py": """\
def run_a():
    try:
        do_thing()
    except subprocess.CalledProcessError:
        log("Command failed")
        cleanup()
""",
                "b.py": """\
def run_b():
    try:
        do_thing()
    except subprocess.CalledProcessError:
        log("Command failed")
        cleanup()
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 1
        assert "CalledProcessError" in findings[0].message

    def test_tuple_exception_types(self, trees):
        t = trees.files(
            {
                "a.py": """\
def run_a():
    try:
        do_thing()
    except (OSError, IOError):
        log("IO failed")
        cleanup()
""",
                "b.py": """\
def run_b():
    try:
        do_thing()
    except (IOError, OSError):
        log("IO failed")
        cleanup()
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 1

    def test_different_messages_no_match(self, trees):
        """Same structure but different string literals — not a duplicate."""
        t = trees.files(
            {
                "a.py": """\
def run_a():
    try:
        do_thing()
    except ValueError:
        log("Error in module A")
        notify("A failed")
""",
                "b.py": """\
def run_b():
    try:
        do_thing()
    except ValueError:
        log("Error in module B")
        notify("B failed")
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 0

    def test_different_exception_type_no_match(self, trees):
        """Same messages but different exception type — not a duplicate."""
        t = trees.files(
            {
                "a.py": """\
def run_a():
    try:
        do_thing()
    except ValueError:
        log("Failed")
        cleanup()
""",
                "b.py": """\
def run_b():
    try:
        do_thing()
    except TypeError:
        log("Failed")
        cleanup()
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 0

    def test_trivial_handlers_skipped(self, trees):
        """pass-only and bare-raise-only handlers are too trivial."""
        t = trees.files(
            {
                "a.py": """\
def run_a():
    try:
        do_thing()
    except ValueError:
        pass
""",
                "b.py": """\
def run_b():
    try:
        do_thing()
    except ValueError:
        pass
""",
            }
        )
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 0

    def test_same_file_only_no_match(self, trees):
        """Same-file duplicates should not be reported (handled by duplicate-blocks)."""
        t = trees.code("""\
def func_a():
    try:
        run_a()
    except ValueError:
        log("Failed")
        cleanup()

def func_b():
    try:
        run_b()
    except ValueError:
        log("Failed")
        cleanup()
""")
        findings = check_duplicate_except_blocks(t, verbose=False)
        assert len(findings) == 0
