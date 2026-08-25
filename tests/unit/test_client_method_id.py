"""Tests for the hosted `method_id` run option — the layer-3 extension this SDK names itself.

Mirrors `pipelex-sdk-js/tests/client.test.ts` "hosted method_id option". The doctrine the
assertions pin is the layered extension policy: a hosted client types its own platform's
arguments and guards them per layer, and `extra` stays the escape hatch for the extensions it
does not know about.
"""

import asyncio

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient

_BASE_URL = "http://localhost:8081"

_BARE_VERSION = {"protocol_version": "0.6.0", "implementation": "pipelex-api", "runner_version": "1.2.3"}
_START_BODY = {"pipeline_run_id": "run_1", "state": "RUNNING", "created_at": "2026-08-24T00:00:00Z"}
_EXECUTE_BODY: dict[str, object] = {
    "pipeline_run_id": "run-x",
    "main_stuff_name": "result",
    "pipe_output": {
        "working_memory": {
            "root": {"result": {"concept": "native.Text", "content": {"text": "hi"}}},
            "aliases": {"main_stuff": "result"},
        },
        "pipeline_run_id": "run-x",
    },
}


def _response(status_code: int, *, json: object | None = None) -> httpx.Response:
    request = httpx.Request("POST", f"{_BASE_URL}/v1/start")
    if json is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=json, request=request)


class TestHostedMethodIdOption:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def test_method_id_rides_the_body_as_a_top_level_field(self, mocker: MockerFixture) -> None:
        """The typed option reaches the wire exactly where the `extra` passthrough used to put it."""
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(pipe_code="answer", method_id="mt_1"))

        sent = send.call_args.kwargs["content"].decode("utf-8")
        assert '"method_id":"mt_1"' in sent
        # The wire body is flat — the option is not nested under an `extra` key.
        assert '"extra"' not in sent

    def test_method_id_only_run_is_accepted(self, mocker: MockerFixture) -> None:
        """A stored method IS something to run: the platform resolves its source server-side."""
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(method_id="mt_1"))

        assert '"method_id":"mt_1"' in send.call_args.kwargs["content"].decode("utf-8")

    def test_method_id_alongside_inline_source_is_linkage_not_a_conflict(self, mocker: MockerFixture) -> None:
        """Inline source wins as the thing to RUN; the id rides along as run-history linkage.

        Refusing the combination once orphaned every unsaved-buffer run from its method — the Run
        row's `method_id` is what writes the index key `GET /v1/runs?method_id=` queries.
        """
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(mthds_contents=['domain = "answer"'], method_id="mt_1"))

        sent = send.call_args.kwargs["content"].decode("utf-8")
        assert '"method_id":"mt_1"' in sent
        assert '"mthds_contents"' in sent

    def test_start_extra_rejects_a_smuggled_method_id(self) -> None:
        """One argument, one path: the layer that NAMES a key must also guard it on `extra`."""
        client = self._client()
        with pytest.raises(PipelineRequestError, match="method_id"):
            asyncio.run(client.start(pipe_code="p", extra={"method_id": "mt_1"}))

    def test_execute_extra_rejects_a_smuggled_method_id(self) -> None:
        """The guard is on the shared merge helper, so both run routes reject identically."""
        client = self._client()
        with pytest.raises(PipelineRequestError, match="method_id"):
            asyncio.run(client.execute(pipe_code="p", extra={"method_id": "mt_1"}))

    def test_empty_method_id_is_absent(self, mocker: MockerFixture) -> None:
        """`method_id=""` selects nothing and links nothing, so it is neither sent nor a run source."""
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(pipe_code="p", method_id=""))
        assert "method_id" not in send.call_args.kwargs["content"].decode("utf-8")

        with pytest.raises(PipelineRequestError):
            asyncio.run(client.start(method_id=""))

    def test_blocking_fallback_forwards_method_id(self, mocker: MockerFixture) -> None:
        """A bare runner must SEE the selector, so it can answer the 422 that names it.

        Dropping it on the fallback would silently run something else instead of surfacing that
        the deployment has no catalog.
        """
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        asyncio.run(client.start_and_wait(pipe_code="p", method_id="mt_1"))

        execute_call = send.call_args_list[1]
        assert execute_call.args[1] == f"{_BASE_URL}/v1/execute"
        assert '"method_id":"mt_1"' in execute_call.kwargs["content"].decode("utf-8")
