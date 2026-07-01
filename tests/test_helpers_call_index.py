"""Tests for call-index receiver classification in helpers."""

import ast
from pathlib import Path

from pysmelly.checks.helpers import (
    attr_call_receiver_root,
    build_call_index,
    collect_imported_names,
    resolves_to_free_function,
)


def _trees(code: str) -> dict:
    return {Path("mod.py"): ast.parse(code)}


class TestCollectImportedNames:
    def test_plain_import(self):
        names = collect_imported_names(ast.parse("import os"))
        assert names == {"os"}

    def test_dotted_import_binds_root(self):
        names = collect_imported_names(ast.parse("import a.b.c"))
        assert names == {"a"}

    def test_aliased_import(self):
        names = collect_imported_names(ast.parse("import numpy as np"))
        assert names == {"np"}

    def test_from_import(self):
        names = collect_imported_names(ast.parse("from pkg import common, other as o"))
        assert names == {"common", "o"}


class TestAttrCallReceiverRoot:
    def _attr(self, code: str) -> ast.Attribute:
        call = ast.parse(code).body[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        return call.func

    def test_single_receiver(self):
        assert attr_call_receiver_root(self._attr("x.f()")) == "x"

    def test_chained_receiver_returns_root(self):
        assert attr_call_receiver_root(self._attr("ctx.client.f()")) == "ctx"

    def test_non_name_root(self):
        assert attr_call_receiver_root(self._attr('"s".upper()')) is None


class TestBuildCallIndexReceiverKind:
    def test_bare_call_is_name(self):
        idx = build_call_index(_trees("f()"))
        assert [c["receiver_kind"] for c in idx["f"]] == ["name"]

    def test_module_qualified_call_is_module(self):
        idx = build_call_index(_trees("import lib\nlib.f()"))
        assert idx["f"][0]["receiver_kind"] == "module"

    def test_instance_method_call_is_instance(self):
        idx = build_call_index(_trees("obj.f()"))
        assert idx["f"][0]["receiver_kind"] == "instance"

    def test_self_call_is_instance(self):
        idx = build_call_index(_trees("class C:\n    def m(self):\n        self.f()"))
        assert idx["f"][0]["receiver_kind"] == "instance"

    def test_resolves_to_free_function_filter(self):
        idx = build_call_index(_trees("import lib\nf()\nlib.f()\nobj.f()"))
        kinds = {c["receiver_kind"] for c in idx["f"] if resolves_to_free_function(c)}
        assert kinds == {"name", "module"}
        assert all(
            c["receiver_kind"] != "instance" for c in idx["f"] if resolves_to_free_function(c)
        )
