"""Tests for `method_source_to_contents` — the adapter over a stored method's polymorphic source.

Mirrors `pipelex-sdk-js/tests/method-source.test.ts` case for case, so the two SDKs read one
stored method the same way, and adds the cases where this side deliberately does not: an array
entry whose `content` is not a string, and a source the Python decoder cannot take.

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
            '[{"name": 1, "content": "domain = \\"demo\\""}]',
        ],
    )
    def test_an_array_of_malformed_entries_is_a_raw_bundle(self, source: str) -> None:
        assert method_source_to_contents(source) == [source]

    def test_a_partly_malformed_catalog_array_is_a_raw_bundle_not_a_partial_read(self) -> None:
        """This SDK's reading, where three implementations of one field disagree.

        `@pipelex/sdk` and the platform's own resolver — the one that expands a `method_id` run —
        both recognize an entry by key presence alone, so both keep `"x = 1"` and silently drop the
        sibling whose `content` is a number. This side takes the array as the catalog form only when
        every entry is `{name: str, content: str}`, the rule `parse_method_files` applies to the same
        shape, so a partly malformed array is not the catalog form at all. Which reading is right is
        an open product question rather than a settled rule; this pins the behaviour as it ships, so
        that a ruling either way shows up here as a failing test rather than as silent drift.
        """
        source = '[{"name": "a.mthds", "content": "x = 1"}, {"name": "b.mthds", "content": 123}]'

        assert method_source_to_contents(source) == [source]

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
