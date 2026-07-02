"""Tests for the origin-level `health()` probe — httpx mocked."""

import asyncio

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError, ApiUnreachableError

_BASE_URL = "http://localhost:8081"


def _response(status_code: int, *, json: object | None = None, content: bytes | None = None) -> httpx.Response:
    """Build a constructed httpx.Response with a request attached."""
    request = httpx.Request("GET", f"{_BASE_URL}/health")
    if json is not None:
        return httpx.Response(status_code, json=json, request=request)
    if content is not None:
        return httpx.Response(status_code, content=content, request=request)
    return httpx.Response(status_code, request=request)


class TestClientHealth:
    @pytest.fixture(autouse=True)
    def _isolate(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "pipelex_sdk.client.load_config",
            return_value={"api_key": "", "base_url": _BASE_URL, "runner": "api"},
        )

    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="t", base_url=_BASE_URL)

    def test_health_hits_origin_level_path_outside_v1(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json={"status": "ok"})))
        result = asyncio.run(client.health())
        assert result == {"status": "ok"}
        assert send.call_args.args[0] == "GET"
        # `/health` lives at the origin, NOT under `/v1`.
        assert send.call_args.args[1] == f"{_BASE_URL}/health"
        assert "/v1/" not in send.call_args.args[1]

    def test_health_non_2xx_raises_plain_pipeline_request_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(503, content=b"unavailable")))
        with pytest.raises(PipelineRequestError) as exc_info:
            asyncio.run(client.health())
        # The plainer regime — not the product `ApiResponseError` with its `code` taxonomy.
        assert not isinstance(exc_info.value, ApiResponseError)

    def test_health_transport_failure_maps_to_unreachable(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ConnectError("refused")))
        with pytest.raises(ApiUnreachableError):
            asyncio.run(client.health())
