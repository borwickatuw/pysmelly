"""Tests for the catalog schema validator, applied to shipped and project entries alike."""

import pytest

from pysmelly.catalog import (
    CONDITION_FNS,
    Catalog,
    CatalogError,
    CatalogPair,
    CatalogPattern,
    PairSide,
    _parse_catalog,
    parse_user_catalog,
    shipped_catalog,
)


def _entry(**fields) -> dict:
    return {
        "name": "boto3-client-factory",
        "description": "AWS clients",
        "imports": ["boto3"],
        "suggest": "acme.aws.client()",
    } | fields


class TestShippedCatalog:
    def test_shipped_catalog_passes_the_validator(self):
        patterns = shipped_catalog().patterns
        assert patterns
        assert all(isinstance(p, CatalogPattern) for p in patterns)

    def test_shipped_conditions_are_parsed(self):
        by_name = {p.name: p for p in shipped_catalog().patterns}
        assert by_name["os-path-vs-pathlib"].also_imports == "pathlib"
        assert by_name["unittest-vs-pytest"].exclude_imports == ("unittest.mock",)
        assert by_name["argparse-to-click"].condition_fn == "complex_argparse"
        assert by_name["xml-minidom"].also_imports is None

    def test_every_shipped_condition_fn_is_registered(self):
        cited = {p.condition_fn for p in shipped_catalog().patterns if p.condition_fn}
        assert cited <= set(CONDITION_FNS)


class TestParseCatalog:
    def test_valid_entry(self):
        (pattern,) = _parse_catalog(
            {"patterns": [_entry(condition="also-imports:acme.aws")]}, "t"
        ).patterns
        assert pattern == CatalogPattern(
            name="boto3-client-factory",
            imports=("boto3",),
            suggest="acme.aws.client()",
            exclude_imports=(),
            also_imports="acme.aws",
            condition_fn=None,
        )

    def test_empty_patterns_is_valid(self):
        assert _parse_catalog({"patterns": []}, "t") == Catalog()

    def test_document_must_be_table(self):
        with pytest.raises(CatalogError, match="t: must be a table, got list"):
            _parse_catalog([_entry()], "t")

    def test_missing_patterns(self):
        with pytest.raises(CatalogError, match="needs a .patterns. or a .pairs. array"):
            _parse_catalog({}, "t")

    def test_patterns_must_be_array(self):
        with pytest.raises(CatalogError, match="'patterns' must be an array of tables"):
            _parse_catalog({"patterns": _entry()}, "t")

    def test_entry_must_be_table(self):
        with pytest.raises(CatalogError, match=r"t patterns\[0\]: must be a table, got str"):
            _parse_catalog({"patterns": ["boto3"]}, "t")

    def test_unknown_document_key(self):
        with pytest.raises(CatalogError, match=r"unknown key\(s\): extra"):
            _parse_catalog({"patterns": [], "extra": 1}, "t")

    def test_unknown_field(self):
        with pytest.raises(CatalogError, match=r"patterns\[1\]: unknown field\(s\): imprts"):
            _parse_catalog({"patterns": [_entry(name="a"), _entry(imprts=["x"])]}, "t")

    @pytest.mark.parametrize("field", ["name", "description", "imports", "suggest"])
    def test_required_field(self, field):
        entry = _entry()
        del entry[field]
        with pytest.raises(CatalogError, match=rf"missing required field\(s\): {field}"):
            _parse_catalog({"patterns": [entry]}, "t")

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"name": ""}, "'name' must be a non-empty string"),
            ({"suggest": 3}, "'suggest' must be a non-empty string"),
            ({"imports": []}, "'imports' must be a non-empty list"),
            ({"imports": ["boto3", 1]}, "'imports' must be a non-empty list"),
            ({"exclude_imports": "boto3.s3"}, "'exclude_imports' must be a non-empty list"),
            ({"condition": "also-imports:"}, "'condition' must be 'also-imports:<module>'"),
            ({"condition": "acme.aws"}, "'condition' must be 'also-imports:<module>'"),
            ({"condition_fn": "nope"}, "unknown condition_fn 'nope'"),
        ],
    )
    def test_invalid_values(self, fields, message):
        with pytest.raises(CatalogError, match=message):
            _parse_catalog({"patterns": [_entry(**fields)]}, "t")

    def test_duplicate_names(self):
        with pytest.raises(CatalogError, match="duplicate pattern name 'boto3-client-factory'"):
            _parse_catalog({"patterns": [_entry(), _entry(imports=["botocore"])]}, "t")


class TestParseUserCatalog:
    def test_new_name_is_accepted(self):
        (pattern,) = parse_user_catalog({"patterns": [_entry()]}, "cfg catalog").patterns
        assert pattern.name == "boto3-client-factory"

    @pytest.mark.parametrize("name", [p.name for p in shipped_catalog().patterns])
    def test_every_shipped_name_is_refused(self, name):
        with pytest.raises(CatalogError, match=f"'{name}' is already a shipped catalog entry"):
            parse_user_catalog({"patterns": [_entry(name=name)]}, "cfg catalog")


def _pair(**fields) -> dict:
    return {
        "name": "acme-http",
        "description": "internal HTTP",
        "sides": [
            {"label": "acme.legacy_http", "imports": ["acme.legacy_http"]},
            {"label": "acme.http", "imports": ["acme.http"]},
        ],
    } | fields


def _side(label: str, imports: list[str], **fields) -> dict:
    return {"label": label, "imports": imports} | fields


class TestShippedPairs:
    def test_shipped_pairs_pass_the_validator(self):
        names = [pair.name for pair in shipped_catalog().pairs]
        assert names == [
            "requests-httpx",
            "os-path-pathlib",
            "argparse-click",
            "attrs-dataclasses",
            "json-orjson",
        ]

    def test_os_path_names_what_pathlib_cannot_replace(self):
        by_name = {pair.name: pair for pair in shipped_catalog().pairs}
        os_path, pathlib = by_name["os-path-pathlib"].sides
        assert os_path.covers("os.path.join")
        assert not os_path.covers("os.path.normpath")
        assert not os_path.covers("os.environ")
        assert pathlib.covers("pathlib.Path")


class TestParsePairs:
    def test_valid_pair(self):
        catalog = _parse_catalog({"pairs": [_pair()]}, "t")
        assert catalog == Catalog(
            pairs=(
                CatalogPair(
                    name="acme-http",
                    description="internal HTTP",
                    sides=(
                        PairSide("acme.legacy_http", ("acme.legacy_http",)),
                        PairSide("acme.http", ("acme.http",)),
                    ),
                ),
            )
        )

    def test_pairs_alone_make_a_catalog(self):
        assert _parse_catalog({"pairs": []}, "t") == Catalog()

    def test_pairs_must_be_array(self):
        with pytest.raises(CatalogError, match="'pairs' must be an array of tables"):
            _parse_catalog({"pairs": _pair()}, "t")

    def test_pair_must_be_table(self):
        with pytest.raises(CatalogError, match=r"t pairs\[0\]: must be a table, got str"):
            _parse_catalog({"pairs": ["requests"]}, "t")

    @pytest.mark.parametrize("field", ["name", "description", "sides"])
    def test_required_field(self, field):
        entry = _pair()
        del entry[field]
        with pytest.raises(CatalogError, match=rf"missing required field\(s\): {field}"):
            _parse_catalog({"pairs": [entry]}, "t")

    def test_unknown_field(self):
        with pytest.raises(CatalogError, match=r"pairs\[0\]: unknown field\(s\): suggest"):
            _parse_catalog({"pairs": [_pair(suggest="x")]}, "t")

    @pytest.mark.parametrize(
        "sides",
        [
            [_side("a", ["a"])],
            [_side("a", ["a"]), _side("b", ["b"]), _side("c", ["c"])],
            {"a": ["a"]},
        ],
    )
    def test_exactly_two_sides(self, sides):
        with pytest.raises(CatalogError, match="'sides' must be an array of exactly two tables"):
            _parse_catalog({"pairs": [_pair(sides=sides)]}, "t")

    @pytest.mark.parametrize(
        ("sides", "message"),
        [
            (["a", _side("b", ["b"])], r"sides\[0\]: must be a table, got str"),
            (
                [_side("a", ["a"]), {"label": "b"}],
                r"sides\[1\]: missing required field\(s\): imports",
            ),
            ([_side("a", ["a"], suggest="x"), _side("b", ["b"])], r"unknown field\(s\): suggest"),
            ([_side("", ["a"]), _side("b", ["b"])], "'label' must be a non-empty string"),
            ([_side("a", []), _side("b", ["b"])], "'imports' must be a non-empty list"),
            ([_side("a", ["a"]), _side("a", ["b"])], "both sides have the label 'a'"),
            ([_side("os", ["os"]), _side("os.path", ["os.path"])], "sides overlap"),
            ([_side("x", ["x.y"]), _side("y", ["z", "x"])], "sides overlap"),
            (
                [_side("a", ["a"], no_substitute="a.b"), _side("b", ["b"])],
                "'no_substitute' must be a non-empty list",
            ),
            (
                [_side("a", ["a"], no_substitute=["b.c"]), _side("b", ["b"])],
                "no_substitute 'b.c' is not a name under the side's imports",
            ),
            (
                [_side("a", ["a"], no_substitute=["a"]), _side("b", ["b"])],
                "no_substitute 'a' is not a name under the side's imports",
            ),
        ],
    )
    def test_invalid_sides(self, sides, message):
        with pytest.raises(CatalogError, match=message):
            _parse_catalog({"pairs": [_pair(sides=sides)]}, "t")

    def test_no_substitute_is_parsed(self):
        sides = [_side("a", ["a"], no_substitute=["a.b"]), _side("b", ["b"])]
        (pair,) = _parse_catalog({"pairs": [_pair(sides=sides)]}, "t").pairs
        assert pair.sides[0].no_substitute == ("a.b",)
        assert pair.sides[0].covers("a.c")
        assert not pair.sides[0].covers("a.b.c")

    def test_duplicate_pair_names(self):
        with pytest.raises(CatalogError, match="duplicate pair name 'acme-http'"):
            _parse_catalog({"pairs": [_pair(), _pair()]}, "t")

    def test_a_pair_may_share_a_patterns_name(self):
        """Patterns and pairs are separate namespaces, as they drive separate checks."""
        catalog = _parse_catalog({"patterns": [_entry(name="x")], "pairs": [_pair(name="x")]}, "t")
        assert ([p.name for p in catalog.patterns], [p.name for p in catalog.pairs]) == (
            ["x"],
            ["x"],
        )


class TestParseUserPairs:
    def test_new_pair_is_accepted(self):
        (pair,) = parse_user_catalog({"pairs": [_pair()]}, "cfg catalog").pairs
        assert pair.name == "acme-http"

    @pytest.mark.parametrize("name", [p.name for p in shipped_catalog().pairs])
    def test_every_shipped_pair_name_is_refused(self, name):
        with pytest.raises(CatalogError, match=f"'{name}' is already a shipped catalog pair"):
            parse_user_catalog({"pairs": [_pair(name=name)]}, "cfg catalog")

    @pytest.mark.parametrize("reverse", [False, True])
    def test_shipped_sides_under_another_name_are_refused(self, reverse):
        sides = [_side("httpx", ["httpx"]), _side("requests", ["requests"])]
        if reverse:
            sides.reverse()
        with pytest.raises(CatalogError, match="same sides as the shipped pair 'requests-httpx'"):
            parse_user_catalog({"pairs": [_pair(name="our-http", sides=sides)]}, "cfg catalog")
