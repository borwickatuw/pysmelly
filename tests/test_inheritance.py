"""Tests for inheritance checks."""

from pysmelly.checks.inheritance import (
    check_deep_inheritance,
    check_mi_method_collision,
    check_refused_bequest,
)
from pysmelly.registry import Severity


class TestRefusedBequest:
    def test_stubbed_overrides_fire(self, trees):
        t = trees.code("""\
class Vehicle:
    def start_engine(self): ...
    def refuel(self, g): ...
    def drive(self, m): ...
    def maintenance(self): ...

class Skateboard(Vehicle):
    def start_engine(self):
        raise NotImplementedError

    def refuel(self, g):
        raise NotImplementedError

    def drive(self, m):
        raise NotImplementedError("use roll")

    def maintenance(self):
        raise NotImplementedError
""")
        findings = check_refused_bequest(t)
        assert len(findings) == 1
        assert "Skateboard" in findings[0].message
        assert findings[0].severity == Severity.MEDIUM

    def test_pass_stubs_fire(self, trees):
        t = trees.code("""\
class Vehicle:
    def start_engine(self): ...
    def stop_engine(self): ...
    def refuel(self, g): ...
    def drive(self, m): ...

class Bicycle(Vehicle):
    def start_engine(self):
        pass

    def stop_engine(self):
        pass

    def refuel(self, g):
        pass

    def drive(self, m):
        self.odometer += m
        return "pedaled"
""")
        findings = check_refused_bequest(t)
        assert len(findings) == 1
        assert "Bicycle" in findings[0].message

    def test_real_overrides_ok(self, trees):
        t = trees.code("""\
class Base:
    def a(self): ...
    def b(self): ...
    def c(self): ...

class Good(Base):
    def a(self):
        return 1

    def b(self):
        return 2

    def c(self):
        return 3
""")
        findings = check_refused_bequest(t)
        assert len(findings) == 0

    def test_below_three_stubs_ok(self, trees):
        t = trees.code("""\
class Base:
    def a(self): ...
    def b(self): ...
    def c(self): ...
    def d(self): ...

class Sub(Base):
    def a(self):
        raise NotImplementedError

    def b(self):
        raise NotImplementedError

    def c(self):
        return 3

    def d(self):
        return 4
""")
        findings = check_refused_bequest(t)
        assert len(findings) == 0

    def test_new_methods_dont_count_as_overrides(self, trees):
        """Stub methods that aren't inherited don't trigger refused-bequest."""
        t = trees.code("""\
class Base:
    def a(self):
        return 1

class Sub(Base):
    def new_x(self):
        raise NotImplementedError

    def new_y(self):
        raise NotImplementedError

    def new_z(self):
        raise NotImplementedError
""")
        findings = check_refused_bequest(t)
        assert len(findings) == 0


class TestDeepInheritance:
    def test_five_deep_chain_fires(self, trees):
        t = trees.code("""\
class A: pass
class B(A): pass
class C(B): pass
class D(C): pass
class E(D): pass
""")
        findings = check_deep_inheritance(t)
        assert len(findings) == 1
        assert "E" in findings[0].message
        assert "A -> B -> C -> D -> E" in findings[0].message

    def test_four_deep_ok(self, trees):
        t = trees.code("""\
class A: pass
class B(A): pass
class C(B): pass
class D(C): pass
""")
        findings = check_deep_inheritance(t)
        assert len(findings) == 0

    def test_reports_only_leaf(self, trees):
        """A single chain reports once, at the deepest class."""
        t = trees.code("""\
class A: pass
class B(A): pass
class C(B): pass
class D(C): pass
class E(D): pass
class F(E): pass
""")
        findings = check_deep_inheritance(t)
        assert len(findings) == 1
        assert findings[0].message.startswith("F ")

    def test_framework_base_not_counted(self, trees):
        """Depth counts only in-codebase links; a framework base is depth 1."""
        t = trees.code("""\
from django.db import models

class MyModel(models.Model): pass
class Child(MyModel): pass
class Grandchild(Child): pass
""")
        findings = check_deep_inheritance(t)
        assert len(findings) == 0


class TestMiMethodCollision:
    def test_competing_definitions_fire(self, trees):
        t = trees.code("""\
class A:
    def run(self):
        return "a"

class B:
    def run(self):
        return "b"

class C(A, B):
    pass
""")
        findings = check_mi_method_collision(t)
        assert len(findings) == 1
        assert "run" in findings[0].message
        assert findings[0].severity == Severity.LOW

    def test_diamond_shared_ancestor_ok(self, trees):
        """A method defined only in the shared diamond top is not a
        collision — both branches inherit the same definition."""
        t = trees.code("""\
class Top:
    def shared(self):
        return 1

class Left(Top):
    def left_only(self):
        return 2

class Right(Top):
    def right_only(self):
        return 3

class Bottom(Left, Right):
    pass
""")
        findings = check_mi_method_collision(t)
        assert len(findings) == 0

    def test_subclass_override_resolves(self, trees):
        t = trees.code("""\
class A:
    def run(self):
        return "a"

class B:
    def run(self):
        return "b"

class C(A, B):
    def run(self):
        return "c"
""")
        findings = check_mi_method_collision(t)
        assert len(findings) == 0

    def test_single_inheritance_override_ok(self, trees):
        """Overriding a method down a linear chain is normal, not a
        collision."""
        t = trees.code("""\
class A:
    def run(self):
        return "a"

class B(A):
    def run(self):
        return "b"

class C(B):
    pass
""")
        findings = check_mi_method_collision(t)
        assert len(findings) == 0
