"""Tests for the method-files catalog converter — `parse_method_files` / `serialize_method_files`.

Mirrors `mthds-js/src/protocol/method_files.ts`. The at-rest catalog form is one wire string
holding a JSON `[{name, content}]` array, with `""` as the platform's "no source" sentinel;
these pin both sentinels, the blank-content drop, and the round-trip.
"""

from __future__ import annotations

import json

import pytest

from pipelex_sdk.product_models import MethodFile, parse_method_files, serialize_method_files


class TestMethodFiles:
    @pytest.mark.parametrize("blank_source", [None, "", "   ", "\n\t ", "[]"])
    def test_blank_source_and_empty_array_both_parse_to_no_files(self, blank_source: str | None) -> None:
        """A blank source and an explicit empty array both mean "this method has no Python"."""
        assert parse_method_files(blank_source) == []

    def test_parses_a_named_array(self) -> None:
        files = parse_method_files('[{"name": "funcs/price.py", "content": "def price(): ...\\n"}]')
        assert len(files) == 1
        assert files[0].name == "funcs/price.py"
        assert files[0].content == "def price(): ...\n"

    def test_drops_blank_content_entries_on_parse(self) -> None:
        """A zero-source file is not a file — dropped on the way in, mirroring serialization."""
        files = parse_method_files('[{"name": "a.py", "content": "x = 1"}, {"name": "empty.py", "content": "   "}]')
        assert [file.name for file in files] == ["a.py"]

    def test_tolerates_extra_keys_on_an_entry(self) -> None:
        """`MethodFile` is extension-open, so a newly-added server key must not fail the read."""
        files = parse_method_files('[{"name": "a.py", "content": "x = 1", "sha": "deadbeef"}]')
        assert files[0].name == "a.py"

    @pytest.mark.parametrize(
        "bad_source",
        [
            "not json at all",
            '{"name": "a.py", "content": "x"}',  # a JSON object, not an array
            '"just a string"',
            "42",
            '[{"name": "a.py"}]',  # entry missing `content`
            '[{"content": "x = 1"}]',  # entry missing `name`
            '[{"name": 1, "content": "x = 1"}]',  # `name` is not a string
            "[[]]",  # entry is not an object
        ],
    )
    def test_malformed_source_raises_naming_the_expected_shape(self, bad_source: str) -> None:
        with pytest.raises(ValueError, match=r"\{name, content\}"):
            parse_method_files(bad_source)

    def test_empty_list_serializes_to_the_clear_sentinel(self) -> None:
        """`""` is the platform's clear signal; the literal `"[]"` would not clear anything."""
        assert serialize_method_files([]) == ""

    def test_blank_only_list_serializes_to_the_clear_sentinel(self) -> None:
        assert serialize_method_files([MethodFile(name="empty.py", content="  \n")]) == ""

    def test_serializes_only_name_and_content(self) -> None:
        """Whatever an extension-open `MethodFile` picked up on the way in is not written back."""
        parsed = parse_method_files('[{"name": "a.py", "content": "x = 1", "sha": "deadbeef"}]')
        assert json.loads(serialize_method_files(parsed)) == [{"name": "a.py", "content": "x = 1"}]

    def test_serializes_mixed_list_dropping_the_blank_entry(self) -> None:
        files = [MethodFile(name="a.py", content="x = 1"), MethodFile(name="empty.py", content=""), MethodFile(name="b.py", content="y = 2")]
        assert json.loads(serialize_method_files(files)) == [{"name": "a.py", "content": "x = 1"}, {"name": "b.py", "content": "y = 2"}]

    def test_round_trip_is_stable(self) -> None:
        """Serialize → parse → serialize reaches a fixed point, blank entries and all."""
        files = [MethodFile(name="a.py", content="x = 1"), MethodFile(name="empty.py", content=" ")]
        once = serialize_method_files(files)
        twice = serialize_method_files(parse_method_files(once))
        assert once == twice
        assert parse_method_files(twice) == [MethodFile(name="a.py", content="x = 1")]
