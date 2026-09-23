"""Tests for `start_and_wait`'s hosted/bare self-healing — the version handshake + blocking fallback.

No equivalent exists in `mthds-python` (whose `start_and_wait` raises on a bare runner); this is the
SDK's own enhancement (`supports_run_lifecycle` + `execute_blocking`), mirroring `pipelex-sdk-js`.
"""

import asyncio
from typing import Any, cast

import httpx
import pytest
from mthds.protocol.pipe_io_contracts import PipeIOContract
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiUnreachableError, MissingMainStuffError, RunLifecycleUnavailableError
from pipelex_sdk.runs import TokensUsageRecord

_BASE_URL = "http://localhost:8081"

_HOSTED_VERSION = {"protocol_version": "0.6.0", "implementation": "pipelex-hosted", "runner_version": "0.9.0"}
_BARE_VERSION = {"protocol_version": "0.6.0", "implementation": "pipelex-api", "runner_version": "1.2.3"}
# A spec-compliant runner may report only the protocol base fields — `implementation` is an
# optional extension. Such a base-only response cannot be classified by name; the client must
# discover the missing lifecycle at runtime (start 404s) and self-heal to the blocking path.
_BASE_ONLY_VERSION = {"protocol_version": "0.6.0", "runner_version": "9.9.9"}

# A completed blocking-execute response: `main_stuff_name` (an extension field) names the
# working-memory root key of the main stuff, which the SDK resolves into `RunResults.main_stuff`.
_EXECUTE_BODY: dict[str, object] = {
    "pipeline_run_id": "run-x",
    "main_stuff_name": "result",
    "pipe_output": {
        "working_memory": {
            "root": {"result": {"concept": "native.Text", "content": {"text": "hello"}}},
            "aliases": {"main_stuff": "result"},
        },
        "pipeline_run_id": "run-x",
    },
}

# The executed graph as the runner returns it inside `pipe_output` — the same document a local run
# writes as `graphspec.json`.
_GRAPH_SPEC: dict[str, Any] = {
    "meta": {"format": "mthds", "mode": "live"},
    "nodes": [{"id": "pipe_1", "status": "COMPLETED"}],
    "edges": [],
}

# The runner's `pipe_io_artifacts` envelope: the three I/O artifacts together, since they share a
# key set and are built in one pass. The hosted results body relays them as three siblings instead.
_PIPE_IO_ARTIFACTS: dict[str, Any] = {
    "pipe_io_contracts": {
        "x.greet": {
            "inputs": {},
            "output": {
                "concept_ref": "native.Text",
                "multiplicity": "single",
                "item_count": None,
                "optional": False,
                "json_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        },
    },
    "input_form": {"x.greet": {"fields": []}},
    "output_form": {"x.greet": {"field": {"name": "text", "kind": "prose", "required": True}}},
}


def _execute_body_with(**pipe_output_extension_fields: object) -> dict[str, object]:
    """The completed blocking body with Pipelex extension fields added onto its `pipe_output`."""
    base_pipe_output = cast("dict[str, object]", _EXECUTE_BODY["pipe_output"])
    return {**_EXECUTE_BODY, "pipe_output": {**base_pipe_output, **pipe_output_extension_fields}}


def _response(status_code: int, *, json: object = None, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json is None:
        return httpx.Response(status_code, headers=headers or {}, request=request)
    return httpx.Response(status_code, json=json, headers=headers or {}, request=request)


def _urls(send_mock: Any) -> list[str]:
    """The URL (second positional arg) of every `_send` call, in order."""
    return [call.args[1] for call in send_mock.call_args_list]


class TestClientRunFallback:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    # ── Hosted (durable start + poll) ────────────────────────────

    def test_hosted_handshakes_then_starts_then_polls(self, mocker: MockerFixture) -> None:
        """Version (hosted) → start (202) → results (200); the durable path maps the result verbatim."""
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_HOSTED_VERSION),
                    _response(202, json={"pipeline_run_id": "run-1", "state": "STARTED", "created_at": "t0"}),
                    _response(200, json={"pipeline_run_id": "run-1", "main_stuff": {"answer": 42}, "graph_spec": {"n": 1}}),
                ]
            ),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.pipeline_run_id == "run-1"
        assert result.main_stuff == {"answer": 42}
        assert result.graph_spec == {"n": 1}
        assert _urls(send) == [f"{_BASE_URL}/v1/version", f"{_BASE_URL}/v1/start", f"{_BASE_URL}/v1/runs/run-1/results"]

    def test_caches_the_version_handshake_across_calls(self, mocker: MockerFixture) -> None:
        """The /v1/version handshake is performed once and cached for the client's lifetime."""
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_HOSTED_VERSION),
                    _response(202, json={"pipeline_run_id": "r1", "state": "STARTED", "created_at": "t0"}),
                    _response(200, json={"pipeline_run_id": "r1", "main_stuff": {}}),
                    _response(202, json={"pipeline_run_id": "r2", "state": "STARTED", "created_at": "t1"}),
                    _response(200, json={"pipeline_run_id": "r2", "main_stuff": {}}),
                ]
            ),
        )

        asyncio.run(client.start_and_wait(pipe_code="p"))
        asyncio.run(client.start_and_wait(pipe_code="p"))
        assert _urls(send).count(f"{_BASE_URL}/v1/version") == 1

    def test_hosted_completed_with_null_main_stuff_raises(self, mocker: MockerFixture) -> None:
        """A hosted 200 whose `main_stuff` is null is a completed run that delivered nothing — hard fail."""
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_HOSTED_VERSION),
                    _response(202, json={"pipeline_run_id": "run-1", "state": "STARTED", "created_at": "t0"}),
                    _response(200, json={"pipeline_run_id": "run-1", "main_stuff": None, "graph_spec": {"n": 1}}),
                ]
            ),
        )

        with pytest.raises(MissingMainStuffError):
            asyncio.run(client.start_and_wait(pipe_code="p"))

    # ── Bare runner (blocking execute fallback) ──────────────────

    def test_bare_runner_falls_back_to_blocking_execute(self, mocker: MockerFixture) -> None:
        """A bare runner (`implementation == pipelex-api`) skips start and runs the blocking POST /v1/execute."""
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.pipeline_run_id == "run-x"
        # The SDK resolves `main_stuff` out of the working memory via `main_stuff_name` ("result") —
        # its content, the same shape the hosted path relays; the full working memory rides pipe_output.
        assert result.main_stuff == {"text": "hello"}
        # `pipe_output` is the already-parsed protocol model, carried over without a dump round-trip.
        assert result.pipe_output is not None
        assert result.pipe_output.working_memory.root["result"].content == {"text": "hello"}
        assert _urls(send) == [f"{_BASE_URL}/v1/version", f"{_BASE_URL}/v1/execute"]

    def test_blocking_fallback_unpacks_usage_pair_from_pipe_output(self, mocker: MockerFixture) -> None:
        """The blocking execute response carries usage inside `pipe_output` (extension-open); the SDK
        unpacks it onto `RunResults.tokens_usages` / `.usage_assembly_error` so the accessor reads the
        same on both paths. A body without the pair (usage off) leaves both None.
        """
        client = self._client()
        tokens_usages = [
            {
                "model_type": "llm",
                "inference_model_name": "test-model",
                "inference_model_id": "test-model-2026-01-01",
                "pipe_code": "test_domain.summarize",
                "job_category": "llm_job",
                "unit_job_id": "llm_gen_text",
                "nb_tokens_by_category": {"input": 15, "output": 4},
                "cost": 0.000105,
                "started_at": "2026-06-20T10:00:01+00:00",
                "completed_at": "2026-06-20T10:00:03+00:00",
            }
        ]
        usage_body: dict[str, object] = {
            "pipeline_run_id": "run-x",
            "main_stuff_name": "result",
            "pipe_output": {
                "working_memory": {
                    "root": {"result": {"concept": "native.Text", "content": {"text": "hello"}}},
                    "aliases": {"main_stuff": "result"},
                },
                "pipeline_run_id": "run-x",
                "tokens_usages": tokens_usages,
                "usage_assembly_error": None,
            },
        }
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=usage_body)]),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.tokens_usages is not None
        record = result.tokens_usages[0]
        # Same typed record the durable path yields — the pair is validated, not passed through raw.
        assert isinstance(record, TokensUsageRecord)
        assert record.inference_model_name == "test-model"
        assert record.pipe_code == "test_domain.summarize"
        assert record.nb_tokens_by_category == {"input": 15, "output": 4}
        assert record.cost == 0.000105
        assert result.usage_assembly_error is None

    def test_blocking_fallback_without_usage_pair_defaults_to_none(self, mocker: MockerFixture) -> None:
        """A blocking response whose pipe_output carries no usage fields (usage off, or an older
        runner) maps to None on both fields — never a validation error.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.tokens_usages is None
        assert result.usage_assembly_error is None

    def test_blocking_fallback_lifts_the_working_memory_off_pipe_output(self, mocker: MockerFixture) -> None:
        """The standard declares `pipe_output.working_memory` required, so the SDK lifts it onto
        `RunResults.working_memory` and the field always carries a value on this path — set in
        `model_fields_set` like every other lifted field, where the hosted path may leave it unset.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.working_memory is not None
        assert "working_memory" in result.model_fields_set
        assert result.working_memory.root["result"].content == {"text": "hello"}
        assert result.working_memory.aliases == {"main_stuff": "result"}
        # The same memory the runner's own envelope carries — lifted, not copied or re-parsed.
        assert result.pipe_output is not None
        assert result.working_memory is result.pipe_output.working_memory

    def test_blocking_fallback_lifts_the_executed_graph_off_pipe_output(self, mocker: MockerFixture) -> None:
        """The runner returns the executed graph inside `pipe_output`; the SDK lifts it onto
        `RunResults.graph_spec` so the field carries the same document whichever path ran.
        Regression: this path used to write `graph_spec=None` and drop the graph the runner had
        already returned.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_BARE_VERSION),
                    _response(200, json=_execute_body_with(graph_spec=_GRAPH_SPEC, graph_assembly_error=None)),
                ]
            ),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.graph_spec == _GRAPH_SPEC
        assert result.graph_assembly_error is None

    def test_blocking_fallback_lifts_a_graph_assembly_failure_off_pipe_output(self, mocker: MockerFixture) -> None:
        """A broken assembly and a run with no graph both leave `graph_spec` None; the lifted
        `graph_assembly_error` is what separates them.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_BARE_VERSION),
                    _response(200, json=_execute_body_with(graph_spec=None, graph_assembly_error="failed to assemble the graph for the run")),
                ]
            ),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.graph_spec is None
        assert result.graph_assembly_error == "failed to assemble the graph for the run"

    def test_blocking_fallback_unwraps_the_io_artifacts_envelope_off_pipe_output(self, mocker: MockerFixture) -> None:
        """The runner carries the three I/O artifacts in one `pipe_io_artifacts` envelope; the SDK
        unwraps it onto the hosted shape's three sibling fields, typed from the standard, so each
        artifact has one accessor whichever path ran.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_BARE_VERSION),
                    _response(200, json=_execute_body_with(pipe_io_artifacts=_PIPE_IO_ARTIFACTS, pipe_io_artifacts_error=None)),
                ]
            ),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.pipe_io_contracts is not None
        assert isinstance(result.pipe_io_contracts["x.greet"], PipeIOContract)
        assert result.pipe_io_contracts["x.greet"].output.concept_ref == "native.Text"
        assert result.input_form is not None
        assert result.input_form["x.greet"].fields == []
        assert result.output_form is not None
        assert result.output_form["x.greet"].field.name == "text"
        assert result.pipe_io_artifacts_error is None
        # The dumped artifacts are the envelope's members, verbatim: unwrapping moved them, nothing else.
        dumped = result.model_dump(mode="json")
        assert dumped["pipe_io_contracts"] == _PIPE_IO_ARTIFACTS["pipe_io_contracts"]
        assert dumped["input_form"] == _PIPE_IO_ARTIFACTS["input_form"]
        # The envelope itself is not a field of `RunResults`: it stays on the runner's own output.
        assert "pipe_io_artifacts" not in dumped
        assert result.pipe_output is not None
        assert (result.pipe_output.model_extra or {})["pipe_io_artifacts"] == _PIPE_IO_ARTIFACTS

    def test_blocking_fallback_lifts_an_io_artifacts_build_failure_off_pipe_output(self, mocker: MockerFixture) -> None:
        """A null envelope leaves all three artifacts None, and the lifted `pipe_io_artifacts_error`
        is what separates a broken build from a run that described nothing.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_BARE_VERSION),
                    _response(
                        200,
                        json=_execute_body_with(pipe_io_artifacts=None, pipe_io_artifacts_error="failed to build the I/O artifacts for the run"),
                    ),
                ]
            ),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.pipe_io_contracts is None
        assert result.input_form is None
        assert result.output_form is None
        assert result.pipe_io_artifacts_error == "failed to build the I/O artifacts for the run"

    def test_blocking_fallback_without_graph_or_artifacts_sets_every_lifted_field_to_none(self, mocker: MockerFixture) -> None:
        """A blocking response whose `pipe_output` carries neither the graph pair nor the artifacts
        (tracing off, or an older runner) maps every lifted field to None — never a validation
        error — and, unlike a hosted body missing the keys, marks each as set: on the blocking path
        the SDK always answers, as the JS twin writes `null` there.
        """
        client = self._client()
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        result = asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))
        assert result.graph_spec is None
        assert result.graph_assembly_error is None
        assert result.pipe_io_contracts is None
        assert result.input_form is None
        assert result.output_form is None
        assert result.pipe_io_artifacts_error is None
        assert {
            "graph_spec",
            "graph_assembly_error",
            "pipe_io_contracts",
            "input_form",
            "output_form",
            "pipe_io_artifacts_error",
        } <= result.model_fields_set

    def test_blocking_fallback_raises_when_main_stuff_unlocatable(self, mocker: MockerFixture) -> None:
        """A completed blocking response whose `main_stuff_name` names no root stuff is a hard fail."""
        client = self._client()
        # `main_stuff_name` points at "answer", but the working-memory root has no such stuff.
        bad_body: dict[str, object] = {
            "pipeline_run_id": "run-y",
            "main_stuff_name": "answer",
            "pipe_output": {
                "working_memory": {"root": {"other": {"concept": "native.Text", "content": {}}}, "aliases": {}},
                "pipeline_run_id": "run-y",
            },
        }
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=bad_body)]),
        )

        with pytest.raises(MissingMainStuffError):
            asyncio.run(client.start_and_wait(pipe_code="p", mthds_contents=["x"]))

    def test_fallback_forwards_extra_extension_args(self, mocker: MockerFixture) -> None:
        """An `extra` extension arg rides the blocking execute body as a top-level field — not dropped."""
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json=_BARE_VERSION), _response(200, json=_EXECUTE_BODY)]),
        )

        asyncio.run(client.start_and_wait(inputs={"topic": "demo"}, extra={"some_vendor_selector": "sel_123"}))
        execute_call = send.call_args_list[1]
        assert execute_call.args[1] == f"{_BASE_URL}/v1/execute"
        assert '"some_vendor_selector":"sel_123"' in execute_call.kwargs["content"].decode("utf-8")

    def test_self_heals_when_base_only_version_hides_missing_run_store(self, mocker: MockerFixture) -> None:
        """A base-only version looks hosted → start 404s (no run created) → fall back; the negative is cached."""
        client = self._client()
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    _response(200, json=_BASE_ONLY_VERSION),
                    _response(404, json={"detail": "Not Found"}),
                    _response(200, json=_EXECUTE_BODY),
                    _response(200, json={**_EXECUTE_BODY, "pipeline_run_id": "run-x2"}),
                ]
            ),
        )

        first = asyncio.run(client.start_and_wait(pipe_code="p"))
        assert first.pipeline_run_id == "run-x"
        assert _urls(send) == [f"{_BASE_URL}/v1/version", f"{_BASE_URL}/v1/start", f"{_BASE_URL}/v1/execute"]

        # Second call: negative cached — no version re-handshake, no start retry, straight to execute.
        second = asyncio.run(client.start_and_wait(pipe_code="p"))
        assert second.pipeline_run_id == "run-x2"
        assert _urls(send) == [
            f"{_BASE_URL}/v1/version",
            f"{_BASE_URL}/v1/start",
            f"{_BASE_URL}/v1/execute",
            f"{_BASE_URL}/v1/execute",
        ]

    def test_handshake_failure_assumes_hosted(self, mocker: MockerFixture) -> None:
        """When the /v1/version handshake itself fails, assume hosted and let start surface the real error."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(500, json={"detail": "boom"})))

        # version 500 → assume hosted → start hits the same 500 → raise_for_status → HTTPStatusError.
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(client.start_and_wait(pipe_code="p"))

    def test_lifecycle_primitives_raise_unavailable_on_bare_404(self, mocker: MockerFixture) -> None:
        """The poll primitives surface a clear RunLifecycleUnavailableError on the bare-runner 404."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(404, json={"detail": "Not Found"})))

        with pytest.raises(RunLifecycleUnavailableError):
            asyncio.run(client.get_run_status("r"))
        with pytest.raises(RunLifecycleUnavailableError):
            asyncio.run(client.get_run_result("r"))
        with pytest.raises(RunLifecycleUnavailableError):
            asyncio.run(client.wait_for_result("r"))

    def test_unreachable_host_maps_to_api_unreachable_on_poll(self, mocker: MockerFixture) -> None:
        """A transport failure on a lifecycle GET maps to ApiUnreachableError (the richer transport layer)."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=httpx.ConnectError("refused")))

        with pytest.raises(ApiUnreachableError):
            asyncio.run(client.get_run_result("r"))
