"""Tests for `_parse_error_body` — the problem+json / HTTPException error-body parser."""

import pytest

from pipelex_sdk.client import _parse_error_body


class TestParseErrorBody:
    def test_detail_dict_extracts_error_type_and_message(self) -> None:
        parsed = _parse_error_body('{"detail": {"error_type": "ValidationError", "message": "bad bundle"}}')
        assert parsed.error_type == "ValidationError"
        assert parsed.server_message == "bad bundle"
        assert parsed.code is None
        assert parsed.validation_errors is None

    def test_detail_string_is_server_message(self) -> None:
        parsed = _parse_error_body('{"detail": "Not authenticated"}')
        assert parsed.server_message == "Not authenticated"
        assert parsed.error_type is None

    def test_top_level_error_type_and_message_fallback(self) -> None:
        parsed = _parse_error_body('{"error_type": "Boom", "message": "top level"}')
        assert parsed.error_type == "Boom"
        assert parsed.server_message == "top level"

    def test_extracts_rfc9457_code(self) -> None:
        parsed = _parse_error_body('{"code": "conflict", "detail": "already exists"}')
        assert parsed.code == "conflict"
        assert parsed.server_message == "already exists"

    def test_malformed_validation_errors_falls_back_to_none(self) -> None:
        parsed = _parse_error_body('{"validation_errors": [{"unexpected": 1}]}')
        assert parsed.validation_errors is None

    def test_absent_validation_errors_is_none(self) -> None:
        parsed = _parse_error_body('{"detail": "x"}')
        assert parsed.validation_errors is None

    @pytest.mark.parametrize("body", ["", "not json", "[]", "5", "null"])
    def test_non_object_bodies_are_empty(self, body: str) -> None:
        parsed = _parse_error_body(body)
        assert parsed.error_type is None
        assert parsed.server_message is None
        assert parsed.code is None
        assert parsed.validation_errors is None
