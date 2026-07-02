"""Tests for the `execute` override — the hosted gateway ~30s timeout translation (httpx mocked).

Mirrors `pipelex-sdk-js/tests/client.test.ts` "execute gateway 30s timeout": a 503/504 — or a
client-side request timeout — at/after the ~28s ceiling becomes a clear `PipelineExecuteTimeoutError`
pointing at start+poll, while a fast 503 stays the inherited `httpx.HTTPStatusError` (runner down,
not a timeout) and the 202 async-degrade stays the inherited `RunStillRunningError`.
"""

import asyncio

import httpx
import pytest
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import PipelineExecuteTimeoutError, RunStillRunningError

_BASE_URL = "http://localhost:8081"

_EXECUTE_BODY: dict[str, object] = {
    "pipeline_run_id": "run-x",
    "pipe_output": {"working_memory": {"root": {}, "aliases": {}}, "pipeline_run_id": "run-x"},
}


def _response(status_code: int, *, json: object | None = None) -> httpx.Response:
    request = httpx.Request("POST", f"{_BASE_URL}/v1/execute")
    if json is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=json, request=request)


class TestClientExecute:
    @pytest.fixture(autouse=True)
    def _mock_credentials(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "pipelex_sdk.client.load_config",
            return_value={"api_key": "", "base_url": "", "runner": "api"},
        )

    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def test_gateway_503_past_ceiling_translates_to_timeout(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(503)))
        # start = 0s, failure observed at 31s → over the ~30s gateway ceiling.
        mocker.patch("pipelex_sdk.client.monotonic", side_effect=[0.0, 31.0])

        with pytest.raises(PipelineExecuteTimeoutError) as exc_info:
            asyncio.run(client.execute(pipe_code="p"))

        error = exc_info.value
        assert error.elapsed_seconds == 31.0
        assert "30s" in str(error)
        assert "wait_for_result" in str(error)

    def test_gateway_504_past_ceiling_translates_to_timeout(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(504)))
        mocker.patch("pipelex_sdk.client.monotonic", side_effect=[0.0, 29.0])

        with pytest.raises(PipelineExecuteTimeoutError):
            asyncio.run(client.execute(pipe_code="p"))

    def test_client_timeout_past_ceiling_translates_to_timeout(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
        mocker.patch("pipelex_sdk.client.monotonic", side_effect=[0.0, 30.5])

        with pytest.raises(PipelineExecuteTimeoutError):
            asyncio.run(client.execute(pipe_code="p"))

    def test_fast_503_stays_inherited_http_status_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(503)))
        # Failure at 2s — under the ceiling: a genuinely-down runner, not a gateway timeout.
        mocker.patch("pipelex_sdk.client.monotonic", side_effect=[0.0, 2.0])

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(client.execute(pipe_code="p"))
        assert not isinstance(exc_info.value, PipelineExecuteTimeoutError)

    def test_success_passes_through_untranslated(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=_EXECUTE_BODY)))

        result = asyncio.run(client.execute(pipe_code="p"))
        assert result.pipeline_run_id == "run-x"

    def test_202_degrade_stays_run_still_running_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json={"pipeline_run_id": "run-x"})))

        with pytest.raises(RunStillRunningError):
            asyncio.run(client.execute(pipe_code="p"))
