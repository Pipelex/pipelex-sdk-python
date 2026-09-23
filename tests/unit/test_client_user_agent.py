"""Tests that `PipelexAPIClient` sends the spec's `User-Agent` on real requests (httpx `MockTransport`)."""

import asyncio
import os
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.user_agent import AppInfo, build_user_agent
from pipelex_sdk.version import __version__

_BASE_URL = "http://localhost:8081"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class TestClientUserAgent:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {}, clear=True)

    @pytest.fixture
    def captured(self, mocker: MockerFixture) -> list[httpx.Request]:
        """Route every AsyncClient `start_client` builds through a MockTransport recording the requests."""
        requests: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok", "id": "u1"})

        def _client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(_handler), **kwargs)

        mocker.patch("pipelex_sdk.client.httpx.AsyncClient", side_effect=_client_factory)
        return requests

    @staticmethod
    async def _health_then_me(client: PipelexAPIClient) -> None:
        async with client:
            await client.health()
            await client._request_product("GET", "me")

    def test_authenticated_client_sends_user_agent_on_every_request(self, captured: list[httpx.Request]) -> None:
        client = PipelexAPIClient(api_key="pk-test", base_url=_BASE_URL)
        asyncio.run(self._health_then_me(client))
        assert [request.url.path for request in captured] == ["/health", "/v1/me"]
        for request in captured:
            assert request.headers["User-Agent"] == client.user_agent
            assert request.headers["Authorization"] == "Bearer pk-test"

    def test_anonymous_client_also_sends_user_agent(self, captured: list[httpx.Request]) -> None:
        client = PipelexAPIClient(api_key="", base_url=_BASE_URL)
        asyncio.run(self._health_then_me(client))
        assert len(captured) == 2
        for request in captured:
            assert request.headers["User-Agent"] == client.user_agent
            assert "Authorization" not in request.headers

    def test_header_carries_sdk_and_mthds_tokens(self, captured: list[httpx.Request]) -> None:
        client = PipelexAPIClient(base_url=_BASE_URL)
        asyncio.run(self._health_then_me(client))
        assert captured[0].headers["User-Agent"].startswith(f"pipelex-sdk-python/{__version__} mthds-python/{version('mthds')} python/")

    def test_app_info_leads_the_header(self, captured: list[httpx.Request]) -> None:
        app_info = AppInfo(name="acme-invoicer", version="1.4.0", details=["batch"], url="https://acme.example")
        client = PipelexAPIClient(base_url=_BASE_URL, app_info=app_info)
        asyncio.run(self._health_then_me(client))
        assert client.app_info == app_info
        assert client.user_agent == build_user_agent(app_info)
        assert captured[1].headers["User-Agent"].startswith(f"acme-invoicer/1.4.0 (batch; +https://acme.example) pipelex-sdk-python/{__version__} ")

    def test_over_long_app_info_fails_at_construction(self) -> None:
        with pytest.raises(ValueError, match="512-character ceiling"):
            PipelexAPIClient(base_url=_BASE_URL, app_info=AppInfo(name="a" * 600))
