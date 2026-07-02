"""Tests for the transport extension layer — `_request_product` / `_request_json`, httpx mocked."""

import asyncio

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError, ApiUnreachableError

_BASE_URL = "http://localhost:8081"


def _response(status_code: int, *, json: object | None = None, content: bytes | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    """Build a constructed httpx.Response with a request attached."""
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json is not None:
        return httpx.Response(status_code, json=json, headers=headers or {}, request=request)
    if content is not None:
        return httpx.Response(status_code, content=content, headers=headers or {}, request=request)
    return httpx.Response(status_code, headers=headers or {}, request=request)


class TestClientTransport:
    @pytest.fixture(autouse=True)
    def _isolate(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "pipelex_sdk.client.load_config",
            return_value={"api_key": "", "base_url": _BASE_URL, "runner": "api"},
        )

    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="t", base_url=_BASE_URL)

    # ── _request_product ─────────────────────────────────────────────

    def test_request_product_parses_2xx_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json={"id": "u1"})))
        result = asyncio.run(client._request_product("GET", "me"))
        assert result == {"id": "u1"}
        assert send.call_args.args[0] == "GET"
        assert send.call_args.args[1] == f"{_BASE_URL}/v1/me"

    def test_request_product_empty_body_returns_none(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(204)))
        result = asyncio.run(client._request_product("DELETE", "methods/m1"))
        assert result is None

    def test_request_product_sends_body_and_verb(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json={"ok": True})))
        asyncio.run(client._request_product("PUT", "runs/r1", body={"name": "x"}))
        assert send.call_args.args[0] == "PUT"
        assert send.call_args.kwargs["content"] == b'{"name":"x"}'

    def test_request_product_non_2xx_raises_api_response_error_with_code(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"code": "conflict", "detail": {"error_type": "Conflict", "message": "no subscription"}}
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))
        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client._request_product("POST", "billing/change-plan", body={"plan": "pro"}))
        err = exc_info.value
        assert err.code == "conflict"
        assert err.status == 409
        assert err.server_message == "no subscription"
        assert err.error_type == "Conflict"
        assert err.api_url == _BASE_URL

    def test_request_product_connect_failure_maps_to_unreachable(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ConnectError("refused")))
        with pytest.raises(ApiUnreachableError) as exc_info:
            asyncio.run(client._request_product("GET", "me"))
        err = exc_info.value
        assert err.api_url == _BASE_URL
        assert err.code == "ConnectError"

    def test_request_product_timeout_maps_to_unreachable_abort(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ConnectTimeout("slow")))
        with pytest.raises(ApiUnreachableError) as exc_info:
            asyncio.run(client._request_product("GET", "me"))
        assert exc_info.value.code == "ABORT_TIMEOUT"

    # ── _request_json (plainer regime) ───────────────────────────────

    def test_request_json_parses_2xx(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json={"status": "ok"})))
        result = asyncio.run(client._request_json("GET", f"{client.origin_url}/health"))
        assert result == {"status": "ok"}

    def test_request_json_non_2xx_raises_pipeline_request_error_not_api_response_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(500, content=b"boom")))
        with pytest.raises(PipelineRequestError) as exc_info:
            asyncio.run(client._request_json("GET", f"{client.origin_url}/health"))
        assert not isinstance(exc_info.value, ApiResponseError)

    def test_request_json_transport_failure_maps_to_unreachable(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ReadError("reset")))
        with pytest.raises(ApiUnreachableError):
            asyncio.run(client._request_json("GET", f"{client.origin_url}/health"))
