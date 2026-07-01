"""Tests for structural checks."""

from pysmelly.checks.structure import (
    _shorten_paths,
    check_duplicate_blocks,
    check_duplicate_except_blocks,
    check_large_class,
    check_long_elif_chain,
    check_long_function,
    check_middle_man,
    check_param_clumps,
    check_shadowed_methods,
)


class TestShortenPaths:
    def test_unique_basenames_use_basename(self):
        result = _shorten_paths(["a/foo.py", "b/bar.py"])
        assert result == {"a/foo.py": "foo.py", "b/bar.py": "bar.py"}

    def test_colliding_basenames_expand_to_distinguish(self):
        result = _shorten_paths(["app_a/views.py", "app_b/views.py"])
        assert result == {
            "app_a/views.py": "app_a/views.py",
            "app_b/views.py": "app_b/views.py",
        }

    def test_expands_only_as_far_as_needed(self):
        result = _shorten_paths(["x/pkg/mod.py", "y/pkg/mod.py"])
        assert result == {"x/pkg/mod.py": "x/pkg/mod.py", "y/pkg/mod.py": "y/pkg/mod.py"}

    def test_mixed_collision_and_unique(self):
        result = _shorten_paths(["a/dup.py", "b/dup.py", "c/uniq.py"])
        assert result["a/dup.py"] == "a/dup.py"
        assert result["b/dup.py"] == "b/dup.py"
        assert result["c/uniq.py"] == "uniq.py"


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

    def test_same_basename_different_dirs_disambiguated(self, trees):
        """Duplicates across same-named files in different dirs must show the
        distinguishing path, not a collapsed basename that looks like a self-match.
        """
        body = """\
def ensure_partition(parent, snapshot_date):
    if parent not in indexes:
        raise ValueError("unknown")
    name = partition_name(parent, snapshot_date)
    start = snapshot_date.isoformat()
    end = tomorrow(snapshot_date).isoformat()
    sql = build_sql(name, parent, start, end)
    with connection.cursor() as cur:
        cur.execute(sql)
    return name
"""
        t = trees.files(
            {
                "app_a/partitions.py": body,
                "app_b/partitions.py": body,
            }
        )
        findings = check_duplicate_blocks(t)
        assert len(findings) == 1
        msg = findings[0].message
        # Both distinct files appear, each qualified by its directory —
        # never the same label twice (which would read as a bogus self-overlap).
        assert "app_a/partitions.py:ensure_partition()" in msg
        assert "app_b/partitions.py:ensure_partition()" in msg
        assert msg.count("app_a/partitions.py:ensure_partition()") == 1
        assert msg.count("app_b/partitions.py:ensure_partition()") == 1


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

    def test_click_command_decorators_excluded(self, trees):
        """Click CLI commands share params by design, not by accident."""
        t = trees.code("""\
import click

@click.command()
@click.option("--env")
def deploy(environment, service, region):
    pass

@click.command()
@click.option("--env")
def rollback(environment, service, region):
    pass

@click.command()
def status(environment, service, region):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_abstractmethod_excluded(self, trees):
        """Abstract method implementations share params by protocol, not clumping."""
        t = trees.files(
            {
                "base.py": """\
from abc import abstractmethod

class Validator:
    @abstractmethod
    def validate(self, app_config, env_config, context):
        pass
""",
                "impl_a.py": """\
from abc import abstractmethod

class ValidatorA:
    @abstractmethod
    def validate(self, app_config, env_config, context):
        pass
""",
                "impl_b.py": """\
from abc import abstractmethod

class ValidatorB:
    @abstractmethod
    def validate(self, app_config, env_config, context):
        pass
""",
            }
        )
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_override_decorator_excluded(self, trees):
        """@override functions share params by interface conformance."""
        t = trees.code("""\
from typing import override

class A:
    @override
    def process(self, data, config, options):
        pass

class B:
    @override
    def process(self, data, config, options):
        pass

class C:
    @override
    def process(self, data, config, options):
        pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_non_interface_decorators_still_flagged(self, trees):
        """Regular decorators don't suppress param-clumps."""
        t = trees.code("""\
def my_decorator(f):
    return f

@my_decorator
def create_user(first_name, last_name, email):
    pass

@my_decorator
def update_user(first_name, last_name, email):
    pass

@my_decorator
def validate_user(first_name, last_name, email):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1

    def test_ignores_exit_protocol_dunders(self, trees):
        """__exit__/__aexit__ signatures are dictated by the context-manager
        protocol — (exc_type, exc, tb) is not a clump the author chose."""
        t = trees.code("""\
class JsonlWriter:
    def __exit__(self, exc_type, exc, tb):
        pass

class ArchiveCheckpoint:
    def __exit__(self, exc_type, exc, tb):
        pass

class Checkpoint:
    def __exit__(self, exc_type, exc, tb):
        pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_ignores_click_callback_signature(self, trees):
        """Functions matching the Click callback protocol (ctx, param, value),
        with optional underscore prefixes for unused args, are not a clump —
        the signature is a Click contract."""
        t = trees.code("""\
def _strip_s3_prefix(_ctx, _param, value):
    return value.removeprefix("s3://")

def _validate_finding_id_a(_ctx, _param, value):
    return value

def _validate_finding_id_b(ctx, param, value):
    return value

def _validate_finding_id_c(_ctx, _param, value):
    return value
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0

    def test_still_flags_three_other_named_params(self, trees):
        """A near-miss that ISN'T the Click signature should still be flagged
        when it recurs."""
        t = trees.code("""\
def a(context, parameter, value):
    pass

def b(context, parameter, value):
    pass

def c(context, parameter, value):
    pass
""")
        findings = check_param_clumps(t)
        assert len(findings) == 1

    def test_click_callback_method_also_skipped(self, trees):
        """Methods with the Click callback signature (self, ctx, param, value)
        are skipped too."""
        t = trees.code("""\
class A:
    def cb(self, _ctx, _param, value):
        return value

class B:
    def cb(self, _ctx, _param, value):
        return value

class C:
    def cb(self, _ctx, _param, value):
        return value
""")
        findings = check_param_clumps(t)
        assert len(findings) == 0


class TestMiddleMan:
    def test_finds_pure_delegation_class(self, trees):
        t = trees.code("""\
class ReportMiddleman:
    def __init__(self, generator):
        self.generator = generator

    def get_user_report(self):
        return self.generator.generate_user_report()

    def get_order_report(self):
        return self.generator.generate_order_report()

    def get_inventory_report(self):
        return self.generator.generate_inventory_report()

    def get_all_reports(self):
        return self.generator.generate_all_reports()
""")
        findings = check_middle_man(t)
        assert len(findings) == 1
        assert "ReportMiddleman" in findings[0].message
        assert "4/4" in findings[0].message
        assert "self.generator" in findings[0].message

    def test_finds_void_delegation(self, trees):
        """Methods that delegate without returning (void delegation)."""
        t = trees.code("""\
class LogProxy:
    def __init__(self, logger):
        self.logger = logger

    def info(self, msg):
        self.logger.info(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)
""")
        findings = check_middle_man(t)
        assert len(findings) == 1

    def test_no_finding_too_few_methods(self, trees):
        t = trees.code("""\
class ThinWrapper:
    def __init__(self, inner):
        self.inner = inner

    def do_thing(self):
        return self.inner.do_thing()

    def do_other(self):
        return self.inner.do_other()
""")
        findings = check_middle_man(t)
        assert len(findings) == 0

    def test_no_finding_methods_add_logic(self, trees):
        """Methods that transform results are not pure delegation."""
        t = trees.code("""\
import json

class ReportAdapter:
    def __init__(self, generator):
        self.generator = generator

    def get_user_report(self):
        return json.dumps(self.generator.generate_user_report())

    def get_order_report(self):
        return json.dumps(self.generator.generate_order_report())

    def get_inventory_report(self):
        return json.dumps(self.generator.generate_inventory_report())
""")
        findings = check_middle_man(t)
        assert len(findings) == 0

    def test_no_finding_mixed_delegation_targets(self, trees):
        """Methods delegate to different attributes — not a middleman."""
        t = trees.code("""\
class Coordinator:
    def __init__(self, a, b, c):
        self.a = a
        self.b = b
        self.c = c

    def do_a(self):
        return self.a.run()

    def do_b(self):
        return self.b.run()

    def do_c(self):
        return self.c.run()
""")
        findings = check_middle_man(t)
        assert len(findings) == 0

    def test_ratio_threshold(self, trees):
        """Some methods delegate, some add logic — below 75% threshold."""
        t = trees.code("""\
class Wrapper:
    def __init__(self, inner):
        self.inner = inner

    def get_a(self):
        return self.inner.get_a()

    def get_b(self):
        return self.inner.get_b()

    def get_c(self):
        return self.inner.get_c()

    def get_d(self):
        result = self.inner.get_d()
        return result.upper()

    def get_e(self):
        result = self.inner.get_e()
        return result.upper()
""")
        findings = check_middle_man(t)
        # 3 out of 5 delegate = 60% < 75%
        assert len(findings) == 0


class TestShadowedMethod:
    def test_finds_diamond_with_shared_method(self, trees):
        t = trees.code("""\
class Task:
    def execute(self):
        pass

class RecurringTask(Task):
    def execute(self):
        return "recurring"

class ConditionalTask(Task):
    def execute(self):
        if self.condition():
            return "conditional"

class ConditionalRecurringTask(RecurringTask, ConditionalTask):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 1
        assert "ConditionalRecurringTask" in findings[0].message
        assert "execute()" in findings[0].message
        assert "RecurringTask" in findings[0].message
        assert "ConditionalTask" in findings[0].message
        assert "silently shadowed" in findings[0].message

    def test_no_finding_child_overrides(self, trees):
        """If the child overrides the conflicting method, no issue."""
        t = trees.code("""\
class A:
    def process(self):
        pass

class B:
    def process(self):
        pass

class C(A, B):
    def process(self):
        return A.process(self) and B.process(self)
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_no_finding_single_base(self, trees):
        t = trees.code("""\
class Base:
    def run(self):
        pass

class Child(Base):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_no_finding_different_methods(self, trees):
        """Multiple bases with different method names — no conflict."""
        t = trees.code("""\
class A:
    def method_a(self):
        pass

class B:
    def method_b(self):
        pass

class C(A, B):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_skips_dunder_methods(self, trees):
        """__init__, __repr__ etc. are commonly inherited — skip them."""
        t = trees.code("""\
class A:
    def __init__(self):
        pass
    def __repr__(self):
        return "A"

class B:
    def __init__(self):
        pass
    def __repr__(self):
        return "B"

class C(A, B):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_multiple_shadowed_methods(self, trees):
        t = trees.code("""\
class A:
    def execute(self):
        pass
    def validate(self):
        pass

class B:
    def execute(self):
        pass
    def validate(self):
        pass

class C(A, B):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 2
        methods = {f.message.split("inherits ")[1].split("()")[0] for f in findings}
        assert methods == {"execute", "validate"}

    def test_cross_file_bases(self, trees):
        t = trees.files(
            {
                "recurring.py": """\
class RecurringTask:
    def execute(self):
        return "recurring"
""",
                "conditional.py": """\
class ConditionalTask:
    def execute(self):
        return "conditional"
""",
                "combined.py": """\
from recurring import RecurringTask
from conditional import ConditionalTask

class CombinedTask(RecurringTask, ConditionalTask):
    pass
""",
            }
        )
        findings = check_shadowed_methods(t)
        assert len(findings) == 1
        assert "CombinedTask" in findings[0].message

    def test_no_finding_when_winner_calls_super(self, trees):
        """If the MRO winner calls super(), the losers still participate."""
        t = trees.code("""\
class Task:
    def execute(self):
        pass

class RecurringTask(Task):
    def execute(self):
        super().execute()
        return "recurring"

class ConditionalTask(Task):
    def execute(self):
        return "conditional"

class ConditionalRecurringTask(RecurringTask, ConditionalTask):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_no_finding_super_with_args(self, trees):
        """super(ClassName, self).method() also counts."""
        t = trees.code("""\
class A:
    def process(self):
        super(A, self).process()

class B:
    def process(self):
        pass

class C(A, B):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 0

    def test_finding_when_no_super(self, trees):
        """If the winner does NOT call super(), still report finding."""
        t = trees.code("""\
class A:
    def process(self):
        return "a only"

class B:
    def process(self):
        return "b only"

class C(A, B):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 1

    def test_mro_winner_is_leftmost_base(self, trees):
        """Verify the message correctly identifies which base MRO picks."""
        t = trees.code("""\
class Alpha:
    def run(self):
        pass

class Beta:
    def run(self):
        pass

class Combined(Alpha, Beta):
    pass
""")
        findings = check_shadowed_methods(t)
        assert len(findings) == 1
        assert "MRO uses Alpha's version" in findings[0].message
        assert "Beta's is silently shadowed" in findings[0].message


class TestLargeClass:
    def test_finds_class_with_20_methods(self, trees):
        methods = "\n".join([f"    def method_{i}(self): pass" for i in range(20)])
        t = trees.code(f"class BigClass:\n{methods}\n")
        findings = check_large_class(t)
        assert len(findings) == 1
        assert "BigClass" in findings[0].message
        assert "20 methods" in findings[0].message
        assert findings[0].severity.value == "low"

    def test_no_finding_19_methods(self, trees):
        methods = "\n".join([f"    def method_{i}(self): pass" for i in range(19)])
        t = trees.code(f"class SmallEnough:\n{methods}\n")
        findings = check_large_class(t)
        assert len(findings) == 0

    def test_excludes_dunder_methods(self, trees):
        """Dunder methods don't count toward the threshold."""
        regular = "\n".join([f"    def method_{i}(self): pass" for i in range(15)])
        dunders = "\n".join(
            [
                "    def __init__(self): pass",
                "    def __repr__(self): pass",
                "    def __str__(self): pass",
                "    def __eq__(self, other): pass",
                "    def __hash__(self): pass",
                "    def __len__(self): pass",
            ]
        )
        t = trees.code(f"class MixedClass:\n{regular}\n{dunders}\n")
        findings = check_large_class(t)
        # 15 regular + 6 dunders = 21 total, but only 15 counted
        assert len(findings) == 0


class TestLongFunction:
    def test_finds_long_function(self, trees):
        body = "\n".join([f"    x_{i} = {i}" for i in range(100)])
        t = trees.code(f"def long_func():\n{body}\n")
        findings = check_long_function(t)
        assert len(findings) == 1
        assert "long_func()" in findings[0].message
        assert "lines" in findings[0].message
        assert findings[0].severity.value == "low"

    def test_no_finding_short_function(self, trees):
        body = "\n".join([f"    x_{i} = {i}" for i in range(50)])
        t = trees.code(f"def short_func():\n{body}\n")
        findings = check_long_function(t)
        assert len(findings) == 0

    def test_finds_long_method(self, trees):
        body = "\n".join([f"        x_{i} = {i}" for i in range(100)])
        t = trees.code(f"class Foo:\n    def long_method(self):\n{body}\n")
        findings = check_long_function(t)
        assert len(findings) == 1
        assert "long_method()" in findings[0].message


class TestLongElifChain:
    def test_finds_chain_of_eight(self, trees):
        branches = "\n".join(
            [
                "    if x == 1: return 'a'",
                "    elif x == 2: return 'b'",
                "    elif x == 3: return 'c'",
                "    elif x == 4: return 'd'",
                "    elif x == 5: return 'e'",
                "    elif x == 6: return 'f'",
                "    elif x == 7: return 'g'",
                "    else: return 'h'",
            ]
        )
        t = trees.code(f"def classify(x):\n{branches}\n")
        findings = check_long_elif_chain(t)
        assert len(findings) == 1
        assert "8-branch" in findings[0].message
        assert "classify()" in findings[0].message
        assert "x" in findings[0].message

    def test_no_finding_seven_branches(self, trees):
        branches = "\n".join(
            [
                "    if x == 1: return 'a'",
                "    elif x == 2: return 'b'",
                "    elif x == 3: return 'c'",
                "    elif x == 4: return 'd'",
                "    elif x == 5: return 'e'",
                "    elif x == 6: return 'f'",
                "    else: return 'g'",
            ]
        )
        t = trees.code(f"def classify(x):\n{branches}\n")
        findings = check_long_elif_chain(t)
        assert len(findings) == 0

    def test_identifies_compared_variable(self, trees):
        branches = "\n".join(
            [
                "    if status == 'a': pass",
                "    elif status == 'b': pass",
                "    elif status == 'c': pass",
                "    elif status == 'd': pass",
                "    elif status == 'e': pass",
                "    elif status == 'f': pass",
                "    elif status == 'g': pass",
                "    else: pass",
            ]
        )
        t = trees.code(f"def check(status):\n{branches}\n")
        findings = check_long_elif_chain(t)
        assert len(findings) == 1
        assert "comparing status to literals" in findings[0].message
        assert "dict or enum" in findings[0].message

    def test_mixed_comparisons_no_variable(self, trees):
        """When branches compare different variables, don't name one."""
        branches = "\n".join(
            [
                "    if a == 1: pass",
                "    elif b == 2: pass",
                "    elif c == 3: pass",
                "    elif d == 4: pass",
                "    elif e == 5: pass",
                "    elif f == 6: pass",
                "    elif g == 7: pass",
                "    else: pass",
            ]
        )
        t = trees.code(f"def mixed(a, b, c, d, e, f, g):\n{branches}\n")
        findings = check_long_elif_chain(t)
        assert len(findings) == 1
        assert "dispatch table or decomposition" in findings[0].message

    def test_does_not_double_count_sub_chains(self, trees):
        """A 10-branch chain should produce 1 finding, not 3."""
        branches = "\n".join(
            [
                "    if x == 1: pass",
                "    elif x == 2: pass",
                "    elif x == 3: pass",
                "    elif x == 4: pass",
                "    elif x == 5: pass",
                "    elif x == 6: pass",
                "    elif x == 7: pass",
                "    elif x == 8: pass",
                "    elif x == 9: pass",
                "    else: pass",
            ]
        )
        t = trees.code(f"def dispatch(x):\n{branches}\n")
        findings = check_long_elif_chain(t)
        assert len(findings) == 1
        assert "10-branch" in findings[0].message
