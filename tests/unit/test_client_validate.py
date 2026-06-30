"""Tests for the `validate` override + `validate_files` — render injection, `mthds_sources`, the union round-trip."""

import asyncio
import json
from typing import cast

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.client import MthdsFile, PipelexAPIClient
from pipelex_sdk.validation_models import PipelexInvalidReport, PipelexValidationReport

_BASE_URL = "http://localhost:8081"

_VALID_BODY = {"is_valid": True, "rendered_markdown": "## ok"}
_INVALID_BODY = {
    "is_valid": False,
    "is_runnable": False,
    "message": "bundle failed",
    "validation_errors": [{"category": "blueprint_validation", "message": "boom"}],
    "rendered_markdown": "## errors",
}


class TestClientValidate:
    @pytest.fixture(autouse=True)
    def _isolate(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "pipelex_sdk.client.load_credentials",
            return_value={"api_key": "", "api_url": _BASE_URL, "runner": "api", "telemetry": "0"},
        )

    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_token="t", api_base_url=_BASE_URL)

    def _mock_send(self, mocker: MockerFixture, client: PipelexAPIClient, *, json_body: object) -> MockType:
        response = httpx.Response(200, json=json_body, request=httpx.Request("POST", f"{_BASE_URL}/v1/validate"))
        return mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))

    @staticmethod
    def _sent_body(send: MockType) -> dict[str, object]:
        content = send.call_args.kwargs["content"]
        return cast("dict[str, object]", json.loads(content))

    def test_injects_markdown_render_by_default(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, json_body=_VALID_BODY)

        asyncio.run(client.validate(["bundle"]))

        body = self._sent_body(send)
        assert send.call_args.args[1] == f"{_BASE_URL}/v1/validate"
        assert body["mthds_contents"] == ["bundle"]
        assert body["allow_signatures"] is False
        assert body["render"] == ["markdown"]
        assert "mthds_sources" not in body

    def test_merges_and_dedupes_caller_render(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, json_body=_VALID_BODY)

        asyncio.run(client.validate(["bundle"], render=["html", "markdown"]))

        # Caller tokens first, markdown not duplicated (mirrors the JS Set semantics).
        assert self._sent_body(send)["render"] == ["html", "markdown"]

    def test_sends_mthds_sources_when_provided(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, json_body=_VALID_BODY)

        asyncio.run(client.validate(["a", "b"], allow_signatures=True, mthds_sources=["x.mthds", "y.mthds"]))

        body = self._sent_body(send)
        assert body["allow_signatures"] is True
        assert body["mthds_sources"] == ["x.mthds", "y.mthds"]
        assert body["render"] == ["markdown"]

    def test_returns_valid_report_with_rendered_markdown(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, json_body=_VALID_BODY)

        result = asyncio.run(client.validate(["bundle"]))

        assert isinstance(result, PipelexValidationReport)
        assert result.is_valid is True
        assert result.rendered_markdown == "## ok"

    def test_returns_invalid_report_union_arm(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, json_body=_INVALID_BODY)

        result = asyncio.run(client.validate(["bundle"]))

        assert isinstance(result, PipelexInvalidReport)
        assert result.is_valid is False
        assert result.rendered_markdown == "## errors"
        assert result.validation_errors[0].message == "boom"

    # ── validate_files ───────────────────────────────────────────────

    def test_validate_files_no_uri_omits_mthds_sources(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, json_body=_VALID_BODY)

        asyncio.run(client.validate_files([MthdsFile(content="a"), MthdsFile(content="b")]))

        body = self._sent_body(send)
        assert body["mthds_contents"] == ["a", "b"]
        assert "mthds_sources" not in body
        assert body["render"] == ["markdown"]

    def test_validate_files_synthesizes_inline_labels_when_any_uri(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, json_body=_VALID_BODY)

        asyncio.run(client.validate_files([MthdsFile(content="a", uri="file://a.mthds"), MthdsFile(content="b")]))

        body = self._sent_body(send)
        assert body["mthds_contents"] == ["a", "b"]
        # Named file keeps its URI; the unnamed sibling gets a deterministic inline label.
        assert body["mthds_sources"] == ["file://a.mthds", "inline://file-2.mthds"]

    def test_validate_files_empty_raises(self) -> None:
        client = self._client()
        with pytest.raises(PipelineRequestError):
            asyncio.run(client.validate_files([]))
