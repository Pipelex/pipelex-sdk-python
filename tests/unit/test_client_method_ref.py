"""Tests for the `method_ref` run option — the layer-2 Pipelex-API run source the runner resolves.

Mirrors `pipelex-sdk-js/tests/client.test.ts` "method_ref run source". The doctrine the
assertions pin: a `method_ref` is a complete run source, so it pairs with nothing (the
client-side guards mirror the server's 422s), provenance comes back typed on both run paths,
and the selector survives the blocking-execute fallback.
"""

import asyncio

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.runs import PipelexRunResultStart

_BASE_URL = "http://localhost:8081"
_METHOD_REF = "github.com/Pipelex/methods/documents@v0.1.0"

_BARE_VERSION = {"protocol_version": "0.6.0", "implementation": "pipelex-api", "runner_version": "1.2.3"}
_PROVENANCE = {"address": "github.com/Pipelex/methods/documents", "tag": "v0.1.0", "commit_sha": "23dda75deadbeef"}
_START_BODY = {"pipeline_run_id": "run_1", "state": "RUNNING", "created_at": "2026-08-29T00:00:00Z", "method_provenance": _PROVENANCE}
_EXECUTE_BODY: dict[str, object] = {
    "pipeline_run_id": "run-x",
    "main_stuff_name": "result",
    "method_provenance": _PROVENANCE,
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


class TestMethodRefRunOption:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def test_method_ref_rides_the_body_and_provenance_comes_back_typed(self, mocker: MockerFixture) -> None:
        """The typed option reaches the wire as a top-level field, and the 202 ack narrows to
        `PipelexRunResultStart` with the `{address, tag, commit_sha}` provenance typed.
        """
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        started = asyncio.run(client.start(method_ref=_METHOD_REF))

        sent = send.call_args.kwargs["content"].decode("utf-8")
        assert f'"method_ref":"{_METHOD_REF}"' in sent
        assert '"extra"' not in sent
        assert isinstance(started, PipelexRunResultStart)
        assert started.method_provenance is not None
        assert started.method_provenance.address == "github.com/Pipelex/methods/documents"
        assert started.method_provenance.tag == "v0.1.0"
        assert started.method_provenance.commit_sha == "23dda75deadbeef"

    def test_start_without_provenance_types_none(self, mocker: MockerFixture) -> None:
        """An inline-source ack has no provenance; the typed field is honestly `None`."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json={"pipeline_run_id": "run_2"})))

        started = asyncio.run(client.start(pipe_code="answer"))

        assert started.method_provenance is None

    def test_execute_surfaces_provenance_on_the_blocking_path(self, mocker: MockerFixture) -> None:
        """`PipelexExecuteResult` declares `method_provenance` too, so the blocking path reads
        it the same way as the durable ack.
        """
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=_EXECUTE_BODY)))

        result = asyncio.run(client.execute(method_ref=_METHOD_REF))

        assert result.method_provenance is not None
        assert result.method_provenance.commit_sha == "23dda75deadbeef"

    def test_pipe_code_beside_method_ref_is_legal(self, mocker: MockerFixture) -> None:
        """`pipe_code` overrides the fetched manifest's `main_pipe` — it is a selector WITHIN
        the run source, not a second source.
        """
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(pipe_code="documents.summarize", method_ref=_METHOD_REF))

        sent = send.call_args.kwargs["content"].decode("utf-8")
        assert '"pipe_code":"documents.summarize"' in sent
        assert f'"method_ref":"{_METHOD_REF}"' in sent

    def test_method_ref_and_inline_contents_are_mutually_exclusive(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        with pytest.raises(PipelineRequestError, match="method_ref and inline mthds_contents are mutually exclusive"):
            asyncio.run(client.start(mthds_contents=['domain = "x"'], method_ref=_METHOD_REF))
        with pytest.raises(PipelineRequestError, match="method_ref and inline mthds_contents are mutually exclusive"):
            asyncio.run(client.execute(mthds_contents=['domain = "x"'], method_ref=_METHOD_REF))

        send.assert_not_called()

    def test_method_ref_and_method_id_are_mutually_exclusive(self, mocker: MockerFixture) -> None:
        """An address run carries its own provenance, so it takes no run-history linkage id —
        there is NO linkage exception for `method_ref` (that exception is inline+method_id's).
        """
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        with pytest.raises(PipelineRequestError, match="method_ref and method_id are mutually exclusive"):
            asyncio.run(client.start(method_ref=_METHOD_REF, method_id="mt_1"))
        with pytest.raises(PipelineRequestError, match="method_ref and method_id are mutually exclusive"):
            asyncio.run(client.execute(method_ref=_METHOD_REF, method_id="mt_1"))

        send.assert_not_called()

    def test_extra_rejects_a_smuggled_method_ref(self) -> None:
        """`extra` merges last into the body, so a smuggled copy would overwrite the validated
        named option and bypass the selector-exclusivity checks — the key is reserved.
        """
        client = self._client()
        with pytest.raises(PipelineRequestError, match="method_ref"):
            asyncio.run(client.start(pipe_code="p", extra={"method_ref": _METHOD_REF}))
        with pytest.raises(PipelineRequestError, match="method_ref"):
            asyncio.run(client.execute(pipe_code="p", extra={"method_ref": _METHOD_REF}))

    def test_empty_method_ref_is_absent(self, mocker: MockerFixture) -> None:
        """`method_ref=""` selects nothing, so it is neither sent nor a run source."""
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        asyncio.run(client.start(pipe_code="p", method_ref=""))
        assert "method_ref" not in send.call_args.kwargs["content"].decode("utf-8")

        with pytest.raises(PipelineRequestError):
            asyncio.run(client.start(method_ref=""))

    @pytest.mark.parametrize("wrong_typed_method_ref", [0, 123, [], ["github.com/x/y"], {}, 1.5, True])
    def test_non_string_method_ref_raises_before_any_request(self, mocker: MockerFixture, wrong_typed_method_ref: object) -> None:
        """Same boundary rule as `method_id`: one wrong value, one answer, before the wire."""
        client = self._client()
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=_START_BODY)))

        with pytest.raises(PipelineRequestError, match="method_ref must be a string"):
            asyncio.run(client.execute(pipe_code="p", method_ref=wrong_typed_method_ref))  # type: ignore[arg-type]
        with pytest.raises(PipelineRequestError, match="method_ref must be a string"):
            asyncio.run(client.start(pipe_code="p", method_ref=wrong_typed_method_ref))  # type: ignore[arg-type]

        send.assert_not_called()

    def test_blocking_fallback_forwards_method_ref(self, mocker: MockerFixture) -> None:
        """A `method_ref` run must run the same fetched package on the blocking fallback —
        dropping the selector there would silently run nothing (or something else).
        """
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        asyncio.run(client.start_and_wait(method_ref=_METHOD_REF))

        execute_call = send.call_args_list[1]
        assert execute_call.args[1] == f"{_BASE_URL}/v1/execute"
        assert f'"method_ref":"{_METHOD_REF}"' in execute_call.kwargs["content"].decode("utf-8")
