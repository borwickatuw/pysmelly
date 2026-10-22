"""Tests for the shared AST helpers: the reference-index builder and the
named predicates the cross-file checks resolve names through."""

import ast
from pathlib import Path

import pytest

from pysmelly.checks.helpers import (
    NameRebinding,
    _assigns_to_name,
    _class_body_ids,
    _dunder_all_elements,
    _inherits_from,
    _is_main_guard,
    _isinstance_operands,
    bare_target,
    build_function_index,
    build_module_rebindings,
    build_reference_indices,
    caught_exception_exprs,
    decorator_target,
    has_dataclass_decorator,
    is_caught_anywhere,
    is_constant_reassigned,
    is_in_dunder_all,
    is_isinstance_target,
    is_secret_name,
    is_subclassed,
    own_scope_nodes,
    referenced_name,
    walk_name_assignments,
)

FIXTURE = """from pkg.mod import imported_name, other_name


@plain_deco
@call_deco()
@mod.attr_deco
@mod.call_attr_deco()
def decorated():
    pass


@class_deco
class Decorated:
    pass


MAPPING = {"key": dict_value, "other": obj.dict_attr}
SPREAD = {**base, "k": spread_value}
SEQUENCE = [list_elt, obj.list_attr]
PAIR = (tuple_elt,)
DOTTED = "pkg.module.suffix_name"
result = fn(call_arg, obj.call_attr, kw=kw_value, kw2=obj.kw_attr)
"""

FILE = "mod.py"


def _indices():
    return build_reference_indices({Path(FILE): ast.parse(FIXTURE)})


class TestBuildReferenceIndices:
    def test_import_index(self):
        assert _indices().import_index == {
            "imported_name": {FILE},
            "other_name": {FILE},
        }

    def test_value_references(self):
        # Dict values (including a ** expansion operand, which is a value with
        # a None *key*), list/tuple elements, call args and keyword values.
        assert _indices().value_references == {
            "dict_value": {FILE},
            "dict_attr": {FILE},
            "base": {FILE},
            "spread_value": {FILE},
            "list_elt": {FILE},
            "list_attr": {FILE},
            "tuple_elt": {FILE},
            "call_arg": {FILE},
            "call_attr": {FILE},
            "kw_value": {FILE},
            "kw_attr": {FILE},
        }

    def test_dotted_string_suffixes(self):
        assert _indices().dotted_string_suffixes == {"suffix_name"}

    def test_decorator_names_cover_all_four_spellings(self):
        assert _indices().decorator_names == {
            "plain_deco",
            "call_deco",
            "attr_deco",
            "call_attr_deco",
            "class_deco",
        }

    def test_async_def_decorators_are_indexed(self):
        trees = {Path(FILE): ast.parse("@adeco\nasync def f():\n    pass\n")}
        assert build_reference_indices(trees).decorator_names == {"adeco"}

    def test_references_are_keyed_per_file(self):
        trees = {
            Path("a.py"): ast.parse("f(shared)"),
            Path("b.py"): ast.parse("g(shared, only_b)"),
        }
        indices = build_reference_indices(trees)
        assert indices.value_references["shared"] == {"a.py", "b.py"}
        assert indices.value_references["only_b"] == {"b.py"}


class TestReferencedName:
    """The one place the Name -> .id / Attribute -> .attr resolution lives."""

    def _expr(self, code: str) -> ast.expr:
        return ast.parse(code).body[0].value

    def test_bare_name(self):
        assert referenced_name(self._expr("thing")) == "thing"

    def test_attribute_yields_final_component(self):
        assert referenced_name(self._expr("pkg.mod.thing")) == "thing"

    def test_call_callee_is_resolved_by_the_caller_not_here(self):
        # A Call expression itself names nothing; callers pass ``call.func``.
        assert referenced_name(self._expr("f()")) is None

    def test_subscript_names_nothing(self):
        assert referenced_name(self._expr("table[key]")) is None


def _first(source: str, kind: type[ast.AST]) -> ast.AST:
    """The first node of type `kind` in `source`."""
    return next(n for n in ast.walk(ast.parse(source)) if isinstance(n, kind))


class TestBareTarget:
    """The bare name a single-target binding statement or clause binds."""

    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            ("x: int = 1", ast.AnnAssign),
            ("x += 1", ast.AugAssign),
            ("for x in xs: pass", ast.For),
            ("async def f():\n    async for x in xs: pass", ast.AsyncFor),
            ("[x for x in xs]", ast.comprehension),
            ("(x := 1)", ast.NamedExpr),
        ],
    )
    def test_bare_name_target_is_returned_as_the_name_node(self, source, kind):
        node = _first(source, kind)
        target = bare_target(node)
        assert target is node.target
        assert target.id == "x"

    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            ("self.x: int = 1", ast.AnnAssign),
            ("obj.x += 1", ast.AugAssign),
            ("table[k] += 1", ast.AugAssign),
            ("for a, b in pairs: pass", ast.For),
            ("for [a, b] in pairs: pass", ast.For),
            ("[a for a, b in pairs]", ast.comprehension),
        ],
    )
    def test_attribute_subscript_and_unpacking_bind_no_bare_name(self, source, kind):
        assert bare_target(_first(source, kind)) is None


def _expr(source: str) -> ast.expr:
    """The single expression `source` spells."""
    return ast.parse(source, mode="eval").body


def _module(source: str) -> ast.Module:
    """`source` parsed as a module."""
    return ast.parse(source)


def _only_class(source: str) -> ast.ClassDef:
    """The first class `source` defines."""
    return next(n for n in ast.walk(_module(source)) if isinstance(n, ast.ClassDef))


class TestDecoratorTarget:
    """All four decorator spellings resolve to the same name."""

    @pytest.mark.parametrize("decorator", ["@d", "@d()", "@d(1, k=2)", "@m.d", "@m.d()", "@m.n.d"])
    def test_target_resolves_to_d(self, decorator):
        cls = _only_class(f"{decorator}\nclass C:\n    pass\n")
        assert referenced_name(decorator_target(cls.decorator_list[0])) == "d"

    @pytest.mark.parametrize(
        "decorator", ["@dataclass", "@dataclass(frozen=True)", "@dataclasses.dataclass"]
    )
    def test_dataclass_is_recognized(self, decorator):
        assert has_dataclass_decorator(_only_class(f"{decorator}\nclass C:\n    pass\n"))

    @pytest.mark.parametrize("decorator", ["@other", "@dataclasses.field", "@d[0]"])
    def test_other_decorators_are_not_dataclasses(self, decorator):
        assert not has_dataclass_decorator(_only_class(f"{decorator}\nclass C:\n    pass\n"))


class TestOwnScopeNodes:
    """A nested def, class or lambda is skipped whole, wherever it sits."""

    def test_nested_scopes_at_the_top_of_the_body_are_skipped(self):
        source = "def inner():\n    return 1\nclass C:\n    x = 2\nlambda: 3\nkept = 4\n"
        constants = {
            node.value
            for node in own_scope_nodes(_module(source).body)
            if isinstance(node, ast.Constant)
        }
        assert constants == {4}

    def test_nested_scopes_below_a_statement_are_skipped(self):
        source = "if t:\n    def inner():\n        return 1\n    f = lambda: 2\n    kept = 3\n"
        constants = {
            node.value
            for node in own_scope_nodes(_module(source).body)
            if isinstance(node, ast.Constant)
        }
        assert constants == {3}


class TestMainGuard:
    @pytest.mark.parametrize(
        ("test", "expected"),
        [
            ('__name__ == "__main__"', True),
            ('"__main__" == __name__', True),
            ('__name__ != "__main__"', False),
            ('__name__ == "__other__"', False),
            ('other == "__main__"', False),
            ('mod.__name__ == "__main__"', False),
            ('__name__ == "__main__" == x', False),
            ("__name__", False),
        ],
    )
    def test_guard(self, test, expected):
        assert _is_main_guard(_expr(test)) is expected


class TestDunderAll:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ('__all__ = ["wanted"]', True),
            ('__all__ = ("wanted",)', True),
            ('__all__ = ["other", "wanted"]', True),
            ('__all__ = ["other"]', False),
            ('__all__ = {"wanted"}', False),
            ('__all__ = base + ["wanted"]', False),
            ('other = ["wanted"]', False),
            ('def f():\n    __all__ = ["wanted"]\n', False),
        ],
    )
    def test_membership(self, source, expected):
        assert is_in_dunder_all("wanted", _module(source)) is expected

    def test_elements_come_from_every_dunder_all_assignment(self):
        tree = _module('__all__ = ["a"]\n__all__ = ["b", "c"]\n')
        values = [e.value for e in _dunder_all_elements(tree) if isinstance(e, ast.Constant)]
        assert values == ["a", "b", "c"]


class TestExceptClauses:
    @pytest.mark.parametrize(
        ("clause", "count"),
        [("except Wanted:", 1), ("except (A, Wanted):", 2), ("except:", 0)],
    )
    def test_clause_arity(self, clause, count):
        source = f"try:\n    pass\n{clause}\n    pass\n"
        handler = next(n for n in ast.walk(_module(source)) if isinstance(n, ast.ExceptHandler))
        assert len(caught_exception_exprs(handler)) == count

    @pytest.mark.parametrize(
        ("clause", "expected"),
        [
            ("except Wanted:", True),
            ("except mod.Wanted:", True),
            ("except (A, Wanted):", True),
            ("except (A, mod.Wanted):", True),
            ("except A:", False),
            ("except:", False),
        ],
    )
    def test_is_caught_anywhere(self, clause, expected):
        source = f"try:\n    pass\n{clause}\n    pass\n"
        assert is_caught_anywhere("Wanted", {Path("a.py"): _module(source)}) is expected


class TestInheritance:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("class Sub(Base)", True),
            ("class Sub(mod.Base)", True),
            ("class Sub(Other, Base)", True),
            ("class Sub(Other)", False),
            ("class Sub", False),
            # A class listing its own name as a bare base is a self-reference,
            # but a dotted base of the same name is a different definition.
            ("class Base(Base)", False),
            ("class Base(mod.Base)", True),
        ],
    )
    def test_is_subclassed(self, header, expected):
        cls = _only_class(f"{header}:\n    pass\n")
        assert _inherits_from(cls, "Base") is expected
        assert is_subclassed("Base", {Path("a.py"): _module(f"{header}:\n    pass\n")}) is expected


class TestIsinstanceTargets:
    @pytest.mark.parametrize(
        ("target", "count"), [("Wanted", 1), ("(A, Wanted)", 2), ("mod.Wanted", 1)]
    )
    def test_operand_arity(self, target, count):
        assert len(_isinstance_operands(_expr(target))) == count

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            ("isinstance(x, Wanted)", True),
            ("issubclass(x, Wanted)", True),
            ("isinstance(x, (A, Wanted))", True),
            ("isinstance(x, A)", False),
            # Only a bare name counts: a dotted target names another module's type
            ("isinstance(x, mod.Wanted)", False),
        ],
    )
    def test_is_isinstance_target(self, call, expected):
        assert is_isinstance_target("Wanted", {Path("a.py"): _module(call)}) is expected


class TestNameAssignments:
    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("VALUE = 1", True),
            ("other = VALUE = 1", True),
            ("VALUE += 1", True),
            ("other = 1", False),
            ("obj.VALUE = 1", False),
            ("obj[VALUE] = 1", False),
            ("VALUE: int = 1", False),
        ],
    )
    def test_assigns_to_name(self, statement, expected):
        assert _assigns_to_name(_module(statement).body[0], "VALUE") is expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("    global VALUE\n    VALUE = 1\n", (True, True)),
            ("    VALUE = 1\n", (False, True)),
            ("    global VALUE\n", (True, False)),
            ("    VALUE.attr = 1\n", (False, False)),
            # A scope nested below the body's own statements is not descended into
            ("    if flag:\n        def inner():\n            VALUE = 1\n", (False, False)),
            ("    if flag:\n        class C:\n            VALUE = 1\n", (False, False)),
        ],
    )
    def test_walk_name_assignments_skips_nested_scopes(self, body, expected):
        func = _module(f"def f():\n{body}").body[0]
        assert isinstance(func, ast.FunctionDef)
        assert walk_name_assignments(func.body, "VALUE") == expected

    def test_a_scope_written_directly_in_the_body_is_still_descended_into(self):
        """Characterizes the filter's reach: it screens the children of each
        statement it pops, so a def written straight into *body* is walked.

        Harmless for the one caller — ``is_constant_reassigned`` visits that
        inner function on its own pass and reaches the same verdict — but the
        docstring's "skips nested scopes" is narrower than it sounds.
        """
        source = "def f():\n    def inner():\n        global VALUE\n        VALUE = 1\n"
        func = _module(source).body[0]
        assert isinstance(func, ast.FunctionDef)
        assert walk_name_assignments(func.body, "VALUE") == (True, True)


def _rebindings(source: str) -> dict[str, list[NameRebinding]]:
    """The one file's entry in the module-rebinding index built over *source*."""
    return build_module_rebindings({Path("a.py"): _module(source)})[Path("a.py")]


class TestModuleRebindings:
    """The index behind ``ctx.module_rebindings`` (plan item 23, sub-phase 23-2):
    one pass per file records every shape the old per-candidate
    ``is_constant_reassigned`` walk counted, and none it did not."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("VALUE = 1\n", [NameRebinding(1, "assign")]),
            ("other = VALUE = 1\n", [NameRebinding(1, "assign")]),
            ("VALUE += 1\n", [NameRebinding(1, "aug-assign")]),
            ("del VALUE\n", [NameRebinding(1, "delete")]),
            ("del other, VALUE\n", [NameRebinding(1, "delete")]),
            (
                "def f():\n    global VALUE\n    VALUE = 2\n",
                [NameRebinding(1, "global-assign")],
            ),
            (
                "def f():\n    global VALUE\n    VALUE += 2\n",
                [NameRebinding(1, "global-assign")],
            ),
            (
                "async def f():\n    global VALUE\n    VALUE = 2\n",
                [NameRebinding(1, "global-assign")],
            ),
            # Any depth: a method, and a function nested in a function
            (
                "class C:\n    def m(self):\n        global VALUE\n        VALUE = 2\n",
                [NameRebinding(2, "global-assign")],
            ),
            (
                "def f():\n    def g():\n        global VALUE\n        VALUE = 2\n",
                # g's own pass, plus f's: a def written straight into f's body
                # is walked one level (see _walk_function_scope)
                [NameRebinding(1, "global-assign"), NameRebinding(2, "global-assign")],
            ),
            # The global and the assignment may sit under any statement of the body
            (
                "def f():\n    if flag:\n        global VALUE\n    for x in y:\n        VALUE = x\n",
                [NameRebinding(1, "global-assign")],
            ),
        ],
    )
    def test_records_each_rebinding_shape(self, source, expected):
        assert _rebindings(source)["VALUE"] == expected

    @pytest.mark.parametrize(
        "source",
        [
            "obj.VALUE = 1\n",
            "obj[VALUE] = 1\n",
            "VALUE: int = 1\n",
            "VALUE, other = 1, 2\n",
            "del obj.VALUE\n",
            "del (VALUE, other)\n",
            # Only the module's own statements: not a class body, not under if/try
            "class C:\n    VALUE = 2\n",
            "if flag:\n    VALUE = 2\n",
            "try:\n    VALUE = 2\nexcept E:\n    pass\n",
            # A function needs both the global declaration and an assignment
            "def f():\n    VALUE = 2\n",
            "def f():\n    global VALUE\n    return VALUE\n",
            # ...in its own scope, not a scope nested under one of its statements
            "def f():\n    global VALUE\n    if flag:\n        def g():\n            VALUE = 2\n",
            "def f():\n    if flag:\n        def g():\n            global VALUE\n    VALUE = 2\n",
        ],
    )
    def test_ignores_shapes_that_bind_no_module_name(self, source):
        assert "VALUE" not in _rebindings(source)

    def test_indexes_every_file_separately(self):
        index = build_module_rebindings(
            {Path("a.py"): _module("VALUE = 1\n"), Path("b.py"): _module("other = 1\n")}
        )
        assert index == {
            Path("a.py"): {"VALUE": [NameRebinding(1, "assign")]},
            Path("b.py"): {"other": [NameRebinding(1, "assign")]},
        }

    def test_sites_accumulate_in_source_order(self):
        source = "VALUE = 1\nVALUE += 1\ndel VALUE\ndef f():\n    global VALUE\n    VALUE = 3\n"
        assert _rebindings(source)["VALUE"] == [
            NameRebinding(1, "assign"),
            NameRebinding(2, "aug-assign"),
            NameRebinding(3, "delete"),
            NameRebinding(4, "global-assign"),
        ]


class TestIsConstantReassigned:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("VALUE = 1\n", False),
            ("VALUE = 1\nVALUE = 2\n", True),
            ("VALUE = 1\nother = VALUE = 2\n", True),
            ("VALUE = 1\nVALUE += 1\n", True),
            ("VALUE = 1\ndel VALUE\n", True),
            ("VALUE = 1\ndef f():\n    global VALUE\n    VALUE = 2\n", True),
            ("VALUE = 1\nclass C:\n    VALUE = 2\n", False),
            ("VALUE = 1\nif flag:\n    VALUE = 2\n", False),
            ("VALUE = 1\nother = 2\n", False),
            # Only the plain assignment on the defining line is excused
            ("VALUE = 1; other = 2\n", False),
            ("VALUE = 1; VALUE += 1\n", True),
            ("VALUE = 1; del VALUE\n", True),
        ],
    )
    def test_every_site_but_the_definition_counts(self, source, expected):
        assert is_constant_reassigned(_rebindings(source), "VALUE", def_lineno=1) is expected

    def test_an_earlier_assignment_counts_against_a_later_definition(self):
        rebindings = _rebindings("VALUE = 1\nVALUE = 2\n")
        assert is_constant_reassigned(rebindings, "VALUE", def_lineno=2) is True

    def test_a_name_with_no_sites_is_not_reassigned(self):
        assert is_constant_reassigned(_rebindings("other = 1\n"), "VALUE", def_lineno=1) is False


class TestFunctionIndex:
    def test_class_body_ids_covers_every_statement_in_a_class(self):
        tree = _module("class C:\n    x: int\n    def m(self):\n        pass\n")
        assert len(_class_body_ids(tree)) == 2

    INDEX_SOURCE = (
        "def kept():\n    pass\n"
        "def _private():\n    pass\n"
        "def test_thing():\n    pass\n"
        "@deco\ndef decorated():\n    pass\n"
        "def outer():\n    def nested():\n        pass\n"
        "class C:\n    def method(self):\n        pass\n"
    )

    def test_index_keeps_only_plain_top_level_functions(self):
        path = Path("tests/test_a.py")
        index = build_function_index({path: _module(self.INDEX_SOURCE)}, frozenset({path}))
        assert sorted(index) == ["kept", "outer"]

    def test_test_name_in_a_production_file_is_an_ordinary_function(self):
        index = build_function_index({Path("a.py"): _module(self.INDEX_SOURCE)}, frozenset())
        assert sorted(index) == ["kept", "outer", "test_thing"]

    def test_main_guard_entry_point_is_excluded(self):
        source = 'def main():\n    pass\nif __name__ == "__main__":\n    main()\n'
        assert build_function_index({Path("a.py"): _module(source)}, frozenset()) == {}


class TestIsSecretName:
    """The one secret-name vocabulary, shared by secrets-in-logs (MEDIUM)
    and plaintext-passwords (HIGH). Plan item 15, sub-phase 15-4."""

    @pytest.mark.parametrize(
        "name",
        [
            "password",
            "passwd",
            "pwd",
            "passphrase",
            "secret",
            "secrets",
            "token",
            "cvv",
            "cvc",
            "ssn",
            "credentials",
            "creds",
        ],
    )
    def test_every_segment_in_the_vocabulary_matches(self, name):
        assert is_secret_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "api_key",
            "apikey",
            "private_key",
            "secret_key",
            "card_number",
            "cardnumber",
            "credit_card",
            "social_security",
        ],
    )
    def test_every_idiom_in_the_vocabulary_matches(self, name):
        assert is_secret_name(name)

    def test_matches_as_a_segment_anywhere_in_the_name(self):
        assert is_secret_name("user_password")
        assert is_secret_name("stored_secret")
        assert is_secret_name("db_password_value")

    def test_case_insensitive(self):
        assert is_secret_name("PASSWORD")
        assert is_secret_name("Api_Key")

    @pytest.mark.parametrize("name", ["username", "max_tokens", "s3_key", "keyword", "usertoken"])
    def test_non_secret_names(self, name):
        """A segment match, not a substring one: ``max_tokens`` counts
        tokens, ``usertoken`` is not a snake_case segment."""
        assert not is_secret_name(name)

    @pytest.mark.parametrize(
        "name",
        ["token_id", "api_key_name", "password_expiry", "token_scope", "secret_path"],
    )
    def test_trailing_metadata_segment_is_exempt(self, name):
        """A fact *about* a secret is neither a leak nor a secret compare."""
        assert not is_secret_name(name)

    def test_metadata_segment_only_disqualifies_in_trailing_position(self):
        assert is_secret_name("id_token")
        assert is_secret_name("name_of_password")

    def test_a_bare_metadata_word_is_not_a_secret(self):
        assert not is_secret_name("id")
        assert not is_secret_name("name")
