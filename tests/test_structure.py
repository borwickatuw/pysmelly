"""Tests for structural checks."""

from pysmelly.checks.structure import (
    check_duplicate_blocks,
    check_duplicate_except_blocks,
    check_param_clumps,
)


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
        findings = check_duplicate_blocks(t)
        assert len(findings) >= 1
        assert "duplicate statements" in findings[0].message
        assert "repeated in:" in findings[0].message
        # Verify line ranges are included
        assert "lines" in findings[0].message

    def test_ignores_short_blocks(self, trees):
        t = trees.code("""\
def func_a():
    x = 1
    return x

def func_b():
    x = 1
    return x
""")
        findings = check_duplicate_blocks(t)
        assert len(findings) == 0

    def test_ignores_nested_function_same_code(self, trees):
        """Nested function body should not be double-counted as outer function duplicate."""
        t = trees.code("""\
def can_manage_list(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Not authenticated")
            logger.warning("Unauthenticated access attempt")
            audit.log(request, "denied")
            return redirect("login")
            next_url = request.get_full_path()
        return view_func(request, *args, **kwargs)
    return _wrapped_view
""")
        findings = check_duplicate_blocks(t)
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
        findings = check_duplicate_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
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
        findings = check_duplicate_except_blocks(t)
        assert len(findings) == 0


class TestParamClumps:
    def test_basic_clump(self, trees):
        """4 identical params in 3 free functions."""
        t = trees.code("""\
def create_user(first_name, last_name, email, phone):
    pass

def update_user(first_name, last_name, email, phone):
    pass

def validate_user(first_name, last_name, email, phone):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1
        assert "first_name" in findings[0].message
        assert "3 functions" in findings[0].message
        assert "consider extracting a dataclass" in findings[0].message

    def test_cross_file(self, trees):
        """Same param set across different files."""
        t = trees.files(
            {
                "users.py": """\
def create_user(first_name, last_name, email):
    pass
""",
                "api.py": """\
def update_user(first_name, last_name, email):
    pass
""",
                "admin.py": """\
def admin_create_user(first_name, last_name, email):
    pass
""",
            }
        )
        findings = check_param_clumps(t)
        assert len(findings) == 1

    def test_methods_included(self, trees):
        """Class methods with same params as free functions are detected."""
        t = trees.code("""\
class UserService:
    def create(self, first_name, last_name, email):
        pass

    def update(self, first_name, last_name, email):
        pass

def validate_user(first_name, last_name, email):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1

    def test_intersection_discovery(self, trees):
        """Functions with overlapping params detect common subset."""
        t = trees.code("""\
def func_a(a, b, c, d, e):
    pass

def func_b(a, b, c, x, y):
    pass

def func_c(a, b, c, z):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) >= 1
        # Should find {a, b, c} as common
        found = False
        for f in findings:
            if all(p in f.message for p in ("a", "b", "c")):
                found = True
        assert found

    def test_private_functions_included(self, trees):
        """Private functions participate in clumps."""
        t = trees.code("""\
def _validate(first_name, last_name, email):
    pass

def create_user(first_name, last_name, email):
    pass

def update_user(first_name, last_name, email):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1

    def test_only_two_functions_no_finding(self, trees):
        """Below threshold of 3 functions."""
        t = trees.code("""\
def create_user(first_name, last_name, email):
    pass

def update_user(first_name, last_name, email):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_only_two_shared_params_no_finding(self, trees):
        """Below clump size of 3."""
        t = trees.code("""\
def func_a(x, y, extra1):
    pass

def func_b(x, y, extra2):
    pass

def func_c(x, y, extra3):
    pass
""")
        findings = check_param_clumps(t)
        # Only x and y are shared, which is < 3
        assert len(findings) == 0

    def test_noise_params_filtered(self, trees):
        """Noise params should not count toward clumps."""
        t = trees.code("""\
def func_a(data, verbose, debug, timeout):
    pass

def func_b(data, verbose, debug, timeout):
    pass

def func_c(data, verbose, debug, timeout):
    pass
""")
        findings = check_param_clumps(t)
        # After filtering noise params, only {data} remains (< 3)
        assert len(findings) == 0

    def test_test_functions_excluded(self, trees):
        """Functions named test_* are excluded."""
        t = trees.code("""\
def test_create_user(first_name, last_name, email):
    pass

def test_update_user(first_name, last_name, email):
    pass

def test_validate_user(first_name, last_name, email):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_test_files_excluded(self, trees):
        """Files named test_* are excluded."""
        t = trees.files(
            {
                "test_users.py": """\
def create_user(first_name, last_name, email):
    pass

def update_user(first_name, last_name, email):
    pass

def validate_user(first_name, last_name, email):
    pass
""",
            }
        )
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_superset_suppresses_subset(self, trees):
        """If {a,b,c,d} appears in 3 functions, don't also report {a,b,c}."""
        t = trees.code("""\
def func_a(a, b, c, d):
    pass

def func_b(a, b, c, d):
    pass

def func_c(a, b, c, d):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1
        assert "d" in findings[0].message  # The full set should be reported

    def test_few_params_after_filtering(self, trees):
        """Functions with < 3 params after noise filtering don't qualify."""
        t = trees.code("""\
def func_a(x, y, verbose):
    pass

def func_b(x, y, verbose):
    pass

def func_c(x, y, verbose):
    pass
""")
        findings = check_param_clumps(t)
        # After filtering verbose, only {x, y} remains (< 3)
        assert len(findings) == 0
