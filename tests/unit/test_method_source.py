"""Tests for `method_source_to_contents` — the adapter over a stored method's polymorphic source.

Mirrors `pipelex-sdk-js/tests/method-source.test.ts` case for case, so the two SDKs read one
stored method the same way, and adds the cases the JS twin leaves untested: an array entry whose
`content` is not a string — where the reading is the platform's and is pinned here because it was
ruled rather than inherited — and a source the Python decoder cannot take, which has no JS twin
at all because `JSON.parse` iterates where `json.loads` recurses.

`MethodData.mthds` is either the catalog file-array or a bare `.mthds` bundle, and the reader
cannot ask which. Every case here is therefore about telling the two apart — above all the
degenerate ones, where reading a sentinel as a bundle yields a method that runs the string
`"[]"` as MTHDS source.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from pipelex_sdk import product_models
from pipelex_sdk.product_models import method_source_to_contents

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


class TestMethodSourceToContents:
    def test_a_raw_bundle_is_one_content(self) -> None:
        """A `.mthds` source is not JSON, and the whole of it is the bundle."""
        source = 'domain = "demo"\nmain_pipe = "main"'

        assert method_source_to_contents(source) == [source]

    def test_a_catalog_array_yields_each_content_in_order(self) -> None:
        source = json.dumps(
            [
                {"name": "bundle.mthds", "content": 'domain = "demo"'},
                {"name": "pipes.mthds", "content": 'main_pipe = "main"'},
            ]
        )

        assert method_source_to_contents(source) == ['domain = "demo"', 'main_pipe = "main"']

    def test_blank_contents_are_dropped_from_a_catalog_array(self) -> None:
        """A zero-source file is not a bundle file — it would fail the MTHDS parse downstream."""
        source = json.dumps(
            [
                {"name": "empty.mthds", "content": ""},
                {"name": "blank.mthds", "content": "  \n\t"},
                {"name": "bundle.mthds", "content": 'domain = "demo"'},
            ]
        )

        assert method_source_to_contents(source) == ['domain = "demo"']

    def test_an_all_blank_catalog_array_is_no_source(self) -> None:
        source = json.dumps([{"name": "empty.mthds", "content": ""}])

        assert method_source_to_contents(source) == []

    def test_the_empty_catalog_array_is_no_source_not_a_bundle(self) -> None:
        """`"[]"` is what the webapp editor writes for a method with no files.

        Read as a bundle it would send the two characters `[]` to the runner as MTHDS source.
        """
        assert method_source_to_contents("[]") == []

    @pytest.mark.parametrize("blank_source", ["", "   \n", "\t "])
    def test_a_blank_source_is_no_source(self, blank_source: str) -> None:
        assert method_source_to_contents(blank_source) == []

    def test_a_contract_violating_none_is_no_source_not_a_crash(self) -> None:
        """`MethodData` types the field `str`; a server that sends `null` must not raise here."""
        assert method_source_to_contents(None) == []

    @pytest.mark.parametrize(
        "source",
        [
            "42",
            '{"name": "x", "content": "y"}',
            '"just a string"',
            "null",
        ],
    )
    def test_non_array_json_is_a_raw_bundle(self, source: str) -> None:
        """Valid JSON that is not the catalog form IS the source — a bundle may open with a digit or a brace."""
        assert method_source_to_contents(source) == [source]

    def test_a_json_array_of_non_entries_is_a_raw_bundle(self) -> None:
        assert method_source_to_contents('["a", "b"]') == ['["a", "b"]']

    @pytest.mark.parametrize(
        "source",
        [
            '[{"name": "a.mthds"}]',
            '[{"content": "domain = \\"demo\\""}]',
        ],
    )
    def test_an_array_missing_a_catalog_key_is_a_raw_bundle(self, source: str) -> None:
        """The catalog gate is key presence, so an entry lacking either key fails the whole array."""
        assert method_source_to_contents(source) == [source]

    def test_a_non_string_name_does_not_disqualify_the_catalog_form(self) -> None:
        """Neither reference checks the `name`'s type, only that the key is there."""
        source = '[{"name": 1, "content": "domain = \\"demo\\""}]'

        assert method_source_to_contents(source) == ['domain = "demo"']

    def test_a_partly_malformed_catalog_array_keeps_its_valid_siblings(self) -> None:
        """The ruled reading: reproduce the server's reading of its own field.

        `@pipelex/sdk` and the platform's own resolver — the one that expands a `method_id` run,
        and therefore the one that decides whether a stored method runs — both recognize an entry
        by key presence alone, keep `"x = 1"` and drop the sibling whose `content` is a number.
        This SDK now does the same, so one stored method has one observable reading wherever it is
        read. An earlier strict reading took the whole array as a bundle instead; it refused to
        lose a file silently, but it disagreed with the code that actually runs the method, which
        is the property the ruling chose. The silent drop is a defect of the shared format and is
        fixed in the platform, not worked around here.
        """
        source = '[{"name": "a.mthds", "content": "x = 1"}, {"name": "b.mthds", "content": 123}]'

        assert method_source_to_contents(source) == ["x = 1"]

    def test_a_source_too_deep_for_the_decoder_is_a_raw_bundle_not_an_escape(self, mocker: MockerFixture) -> None:
        """Python's JSON decoder recurses where `JSON.parse` iterates, and `RecursionError` is not a `ValueError`.

        Unconverted it escapes a function whose whole contract is that it never raises. The rule has
        one owner — `parse_method_files` turns the decoder's `RecursionError` into the `ValueError`
        its contract promises — so this pins the through-path rather than a mock of the delegate. The
        depth that trips the real decoder is an interpreter build constant that moved by an order of
        magnitude in CPython 3.14, so the decoder is made to raise instead of a nesting literal being
        pinned: the conversion is what is under test, not the threshold.
        """
        # A source that parses cleanly unpatched, so the assertion below fails if the patch or the
        # conversion is absent: without them it reads as the catalog form and yields `["x = 1"]`.
        source = '[{"name": "a.mthds", "content": "x = 1"}]'
        mocker.patch.object(product_models.json, "loads", side_effect=RecursionError)

        assert method_source_to_contents(source) == [source]
