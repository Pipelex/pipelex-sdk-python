"""Tests for `PipelexAPIClient`'s durable run-lifecycle surface (start/status/results/wait), httpx mocked."""

import asyncio
from typing import Any

import httpx
import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import (
    MissingMainStuffError,
    RunFailedError,
    RunLifecycleUnavailableError,
    RunStillRunningError,
    RunTimeoutError,
)
from pipelex_sdk.runs import (
    PollInfo,
    RunResultCompleted,
    RunResultFailed,
    RunResultRunning,
    RunResults,
    RunStatus,
    TokensUsageRecord,
    WaitForResultOptions,
)

_BASE_URL = "http://localhost:8081"

# A runner `ErrorReport` in its VERBOSE form, as the platform stores it on the run row and serves it
# on the status read and in the results read's 409 — here the gateway refusing a model, the case
# recorded live on the dev API: every inference field of the report is set.
_MODEL_REFUSED_REPORT: dict[str, Any] = {
    "error_type": "LLMCompletionError",
    "message": "Error code: 400 - model 'gpt-6-astra' is not enabled for this organization",
    "title": "LLM completion",
    "type_uri": "https://docs.pipelex.com/latest/errors/llm-completion-error/",
    "error_category": "configuration",
    "error_domain": "config",
    "retryable": False,
    "user_action": {"kind": "change_input", "detail": "The provider rejected the request — review the prompt, parameters, and inputs."},
    "model": "gpt-6-astra",
    "provider": "pipelex_gateway",
    "provider_metadata": {
        "provider": "pipelex_gateway",
        "sdk_exception_type": "BadRequestError",
        "message": "Error code: 400 - model 'gpt-6-astra' is not enabled for this organization",
        "status_code": 400,
        "request_id": "req_provider_1",
        "provider_error_code": "model_not_enabled",
    },
}

# A bundle fault, the report's other shape: caller-facing, with the structured validation items.
_BUNDLE_FAULT_REPORT: dict[str, Any] = {
    "error_type": "ValidateBundleError",
    "message": "Pipe 'summarize' names an unknown concept 'Sumary'.",
    "title": "Bundle validation",
    "type_uri": "https://docs.pipelex.com/latest/errors/validate-bundle-error/",
    "error_domain": "input",
    "retryable": False,
    "caller_facing_message": True,
    "validation_errors": [
        {
            "category": "pipe_validation",
            "message": "Pipe 'summarize' names an unknown concept 'Sumary'.",
            "error_type": "unknown_concept",
            "pipe_code": "summarize",
            "missing_concept_code": "Sumary",
        }
    ],
}


def _failed_results_problem(run_status: str, error: dict[str, Any] | None) -> dict[str, Any]:
    """The results read's 409 exactly as the platform renders it for a run that ended without completing."""
    if error is not None:
        detail = f"Run finished with status {run_status}: {error['message']}"
    else:
        detail = f"Run finished with status {run_status}; no result available"
    return {
        "type": "https://pipelex.com/errors/conflict",
        "title": "Conflict",
        "status": 409,
        "code": "conflict",
        "detail": detail,
        "instance": "urn:pipelex:request:req-409",
        "request_id": "req-409",
        "errors": [],
        "run_status": run_status,
        "error": error,
    }


def _response(status_code: int, *, json: object = None, headers: dict[str, str] | None = None) -> httpx.Response:
    """Build a constructed httpx.Response with a request attached (so raise_for_status works)."""
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json is None:
        return httpx.Response(status_code, headers=headers or {}, request=request)
    return httpx.Response(status_code, json=json, headers=headers or {}, request=request)


class TestClientLifecycle:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    # ── start (inherited body-building + bare-runner 404 translation) ──

    def test_start_targets_v1_url_and_returns_run_result_start(self, mocker: MockerFixture) -> None:
        """Start posts to <base>/v1/start; a 202 parses into RunResultStart with the authoritative id."""
        client = PipelexAPIClient(api_key="t", base_url=f"{_BASE_URL}/")
        body = {"pipeline_run_id": "run_1", "state": "RUNNING", "created_at": "2026-06-10T00:00:00Z"}
        send_mock = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=body)))

        started = asyncio.run(client.start(pipe_code="answer"))
        assert client.base_url == _BASE_URL
        assert send_mock.call_args.args[1] == f"{_BASE_URL}/v1/start"
        assert started.pipeline_run_id == "run_1"

    def test_start_request_prunes_absent_fields_and_carries_extra(self, mocker: MockerFixture) -> None:
        """Absent fields are pruned (exclude_none); extension args ride the body as top-level properties."""
        client = self._client()
        body = {"pipeline_run_id": "run_1", "state": "RUNNING", "created_at": "2026-06-10T00:00:00Z"}
        send_mock = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=body)))

        asyncio.run(client.start(pipe_code="answer", extra={"some_vendor_arg": {"nested": True}}))
        sent = send_mock.call_args.kwargs["content"].decode("utf-8")
        assert '"pipe_code":"answer"' in sent
        assert '"some_vendor_arg":{"nested":true}' in sent
        assert "output_name" not in sent

    def test_start_extra_rejects_protocol_args(self) -> None:
        """`extra` is for extension args only — a protocol arg inside it raises a clear client-side error
        (raised by the inherited body-builder, before any request, so the override passes it through).
        """
        client = self._client()
        with pytest.raises(PipelineRequestError, match="pipe_code"):
            asyncio.run(client.start(mthds_contents=['domain = "answer"'], extra={"pipe_code": "smuggled"}))

    def test_start_bare_runner_missing_route_404_is_lifecycle_unavailable(self, mocker: MockerFixture) -> None:
        """A bare-runner 404 with Starlette's default body (no `code`) becomes RunLifecycleUnavailableError."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(404, json={"detail": "Not Found"})))

        with pytest.raises(RunLifecycleUnavailableError) as exc_info:
            asyncio.run(client.start(pipe_code="answer"))
        assert exc_info.value.api_url == _BASE_URL

    def test_start_structured_404_stays_http_status_error(self, mocker: MockerFixture) -> None:
        """A structured platform 404 (carries `code`) is a normal HTTP error, not lifecycle-unavailable."""
        client = self._client()
        body = {"code": "NOT_FOUND", "detail": "The requested resource does not exist."}
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(404, json=body)))

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(client.start(pipe_code="answer"))

    # ── get_run_status ───────────────────────────────────────────

    def test_get_run_status_populates_degraded_and_retry_after(self, mocker: MockerFixture) -> None:
        """get_run_status hits /v1/runs/{id}/status, parses RunRead, and lifts Retry-After."""
        client = self._client()
        body = {"pipeline_run_id": "run_1", "status": "RUNNING", "created_at": "2026-06-10T00:00:00Z", "degraded": True}
        send_mock = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body, headers={"Retry-After": "7"})))

        run = asyncio.run(client.get_run_status("run_1"))
        assert send_mock.call_args.args[1] == f"{_BASE_URL}/v1/runs/run_1/status"
        assert run.degraded is True
        assert run.retry_after_seconds == 7

    def test_get_run_status_types_the_stored_report(self, mocker: MockerFixture) -> None:
        """A status read whose body carries `error` exposes the stored report on `RunRead.error`, typed whole."""
        client = self._client()
        body = {
            "pipeline_run_id": "run_1",
            "status": "FAILED",
            "created_at": "2026-06-10T00:00:00Z",
            "finished_at": "2026-06-10T00:01:00Z",
            "error": _BUNDLE_FAULT_REPORT,
            "degraded": False,
        }
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        run = asyncio.run(client.get_run_status("run_1"))
        assert run.status == RunStatus.FAILED
        assert run.error is not None
        assert run.error.error_type == "ValidateBundleError"
        assert run.error.error_domain == "input"
        assert run.error.caller_facing_message is True
        assert run.error.validation_errors is not None
        assert run.error.validation_errors[0].pipe_code == "summarize"
        assert run.error.validation_errors[0].missing_concept_code == "Sumary"
        assert run.error.model_dump(exclude_none=True) == _BUNDLE_FAULT_REPORT
        assert run.model_extra == {}

    def test_get_run_status_without_error_reads_none(self, mocker: MockerFixture) -> None:
        """A run that has not failed carries no report: `error` is None, whether absent or null."""
        client = self._client()
        body = {"pipeline_run_id": "run_1", "status": "RUNNING", "created_at": "2026-06-10T00:00:00Z", "error": None}
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        run = asyncio.run(client.get_run_status("run_1"))
        assert run.error is None

    def test_get_run_status_lifecycle_unavailable_on_missing_route(self, mocker: MockerFixture) -> None:
        """A bare-runner 404 on the status route becomes RunLifecycleUnavailableError."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(404, json={"detail": "Not Found"})))

        with pytest.raises(RunLifecycleUnavailableError):
            asyncio.run(client.get_run_status("run_1"))

    # ── get_run_result status mapping ────────────────────────────

    def test_get_run_result_completed_keeps_polymorphic_main_stuff(self, mocker: MockerFixture) -> None:
        """A 200 maps to RunResultCompleted; a list main_stuff stays a top-level array; graph_spec is parsed."""
        client = self._client()
        body: dict[str, object] = {
            "pipeline_run_id": "run_1",
            "main_stuff": [{"color": "red"}, {"color": "blue"}],
            "graph_spec": {"nodes": []},
        }
        send_mock = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert send_mock.call_args.args[1] == f"{_BASE_URL}/v1/runs/run_1/results"
        assert isinstance(state, RunResultCompleted)
        assert state.result.main_stuff == [{"color": "red"}, {"color": "blue"}]
        assert state.result.graph_spec == {"nodes": []}

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"pipeline_run_id": "run_1"}, id="main_stuff_key_omitted"),
            pytest.param({"pipeline_run_id": "run_1", "main_stuff": None}, id="main_stuff_null"),
        ],
    )
    def test_get_run_result_completed_without_main_stuff_raises_typed_error(self, mocker: MockerFixture, body: dict[str, object]) -> None:
        """A 200 that omits main_stuff (or sends it null) raises MissingMainStuffError, not a raw Pydantic error."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        with pytest.raises(MissingMainStuffError) as exc_info:
            asyncio.run(client.get_run_result("run_1"))
        assert exc_info.value.run_id == "run_1"

    def test_get_run_result_completed_keeps_falsy_main_stuff(self, mocker: MockerFixture) -> None:
        """A present-but-falsy main_stuff (an empty list) is a valid output and does NOT raise."""
        client = self._client()
        body: dict[str, object] = {"pipeline_run_id": "run_1", "main_stuff": []}
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultCompleted)
        assert state.result.main_stuff == []

    def test_get_run_result_completed_parses_usage_pair(self, mocker: MockerFixture) -> None:
        """A 200 carrying the hosted usage pair validates the relayed records into
        `TokensUsageRecord`s; a body without them (older platform / pre-artifact run) defaults both
        to None.
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
        body: dict[str, object] = {
            "pipeline_run_id": "run_1",
            "main_stuff": {"answer": "42"},
            "tokens_usages": tokens_usages,
            "usage_assembly_error": None,
        }
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(200, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultCompleted)
        assert state.result.tokens_usages is not None
        record = state.result.tokens_usages[0]
        assert isinstance(record, TokensUsageRecord)
        assert record.inference_model_name == "test-model"
        assert record.pipe_code == "test_domain.summarize"
        assert record.nb_tokens_by_category == {"input": 15, "output": 4}
        assert record.cost == 0.000105
        assert state.result.usage_assembly_error is None

        bare = RunResults(pipeline_run_id="run_1", main_stuff={"answer": "42"})
        assert bare.tokens_usages is None
        assert bare.usage_assembly_error is None

    def test_get_run_result_running_honors_retry_after(self, mocker: MockerFixture) -> None:
        """A 202 maps to RunResultRunning with the server's Retry-After hint."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, headers={"Retry-After": "3"})))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultRunning)
        assert state.retry_after_seconds == 3

    def test_get_run_result_degraded_503_defaults_retry(self, mocker: MockerFixture) -> None:
        """A 503 (DynamoDB/Temporal degraded) maps to running with the default retry, never a failure."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(503)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultRunning)
        assert state.retry_after_seconds == 5

    def test_get_run_result_failed_carries_the_typed_report(self, mocker: MockerFixture) -> None:
        """A 409 carrying the run's status and its stored report maps to RunResultFailed with the whole report, typed."""
        client = self._client()
        body = _failed_results_problem("FAILED", _MODEL_REFUSED_REPORT)
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultFailed)
        assert state.status == RunStatus.FAILED
        assert state.message == f"Run finished with status FAILED: {_MODEL_REFUSED_REPORT['message']}"
        report = state.error
        assert report is not None
        assert report.error_type == "LLMCompletionError"
        assert report.title == "LLM completion"
        assert report.type_uri == "https://docs.pipelex.com/latest/errors/llm-completion-error/"
        assert report.error_domain == "config"
        assert report.error_category == "configuration"
        assert report.retryable is False
        assert report.user_action is not None
        assert report.user_action.kind == "change_input"
        assert report.model == "gpt-6-astra"
        assert report.provider == "pipelex_gateway"
        assert report.provider_metadata is not None
        assert report.provider_metadata.status_code == 400
        assert report.provider_metadata.provider_error_code == "model_not_enabled"
        # Nothing is stripped: the typed report dumps back to exactly what the platform stored.
        assert report.model_dump(exclude_none=True) == _MODEL_REFUSED_REPORT

    @pytest.mark.parametrize("run_status", [RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TERMINATED, RunStatus.TIMED_OUT])
    def test_get_run_result_failed_without_a_report_reads_its_status(self, mocker: MockerFixture, run_status: RunStatus) -> None:
        """A 409 whose `error` is null is a report-less failure with the status the `run_status` member names."""
        client = self._client()
        body = _failed_results_problem(run_status, None)
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultFailed)
        assert state.status == run_status
        assert state.error is None
        assert state.message == f"Run finished with status {run_status}; no result available"

    def test_get_run_result_failed_reads_the_status_member_not_the_sentence(self, mocker: MockerFixture) -> None:
        """The status comes from `run_status`; the sentence is never parsed, so a detail naming another word does not win."""
        client = self._client()
        body = _failed_results_problem("CANCELLED", None)
        body["detail"] = "Run finished with status TIMED_OUT; no result available"
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultFailed)
        assert state.status == RunStatus.CANCELLED

    @pytest.mark.parametrize(
        "body",
        [
            {"code": "conflict", "detail": "Run finished with status TIMED_OUT; no result available"},
            {"code": "conflict", "detail": "refused", "run_status": "SOMETHING_NEW"},
            {"code": "conflict", "detail": "refused", "run_status": 7},
        ],
    )
    def test_get_run_result_failed_without_a_known_status_member_reads_failed(self, mocker: MockerFixture, body: dict[str, Any]) -> None:
        """A 409 without a `run_status` this SDK knows reads as FAILED, the status every such answer shares."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultFailed)
        assert state.status == RunStatus.FAILED
        assert state.message == body["detail"]
        assert state.error is None

    def test_get_run_result_failed_with_a_malformed_report_still_fails_with_its_reason(self, mocker: MockerFixture) -> None:
        """A report whose known fields do not fit their types reads as None; the failure and its detail survive."""
        client = self._client()
        body = _failed_results_problem("FAILED", {"message": "boom", "validation_errors": "not a list"})
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        state = asyncio.run(client.get_run_result("run_1"))
        assert isinstance(state, RunResultFailed)
        assert state.status == RunStatus.FAILED
        assert state.message == "Run finished with status FAILED: boom"
        assert state.error is None

    def test_get_run_result_lifecycle_unavailable_on_missing_route(self, mocker: MockerFixture) -> None:
        """A bare-runner 404 on the results route becomes RunLifecycleUnavailableError."""
        client = self._client()
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(404, json={"detail": "Not Found"})))

        with pytest.raises(RunLifecycleUnavailableError):
            asyncio.run(client.get_run_result("run_1"))

    # ── execute 202 degrade → re-exported RunStillRunningError ────

    def test_execute_202_raises_re_exported_still_running(self, mocker: MockerFixture) -> None:
        """A 202 on execute raises RunStillRunningError (re-exported from mthds) carrying run_id + hints."""
        client = self._client()
        body = {"pipeline_run_id": "run_1", "state": "RUNNING", "created_at": "2026-06-10T00:00:00Z"}
        headers = {"Retry-After": "10", "Location": "/v1/runs/run_1/results"}
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(202, json=body, headers=headers)))

        with pytest.raises(RunStillRunningError) as exc_info:
            asyncio.run(client.execute(pipe_code="answer"))
        assert exc_info.value.run_id == "run_1"
        assert exc_info.value.retry_after_seconds == 10

    # ── wait_for_result poll loop ────────────────────────────────

    def test_wait_for_result_polls_until_completed(self, mocker: MockerFixture) -> None:
        """The loop polls past a running state and returns the completed result; on_poll fires per wait."""
        client = self._client()
        result = RunResults(pipeline_run_id="run_1", main_stuff={"answer": "42"})
        mocker.patch.object(
            client,
            "get_run_result",
            mocker.AsyncMock(
                side_effect=[
                    RunResultRunning(pipeline_run_id="run_1", retry_after_seconds=0),
                    RunResultCompleted(pipeline_run_id="run_1", result=result),
                ]
            ),
        )
        mocker.patch("pipelex_sdk.client.asyncio.sleep", mocker.AsyncMock())
        polls: list[PollInfo] = []

        returned = asyncio.run(client.wait_for_result("run_1", WaitForResultOptions(interval_seconds=0.0, on_poll=polls.append)))
        assert returned.main_stuff == {"answer": "42"}
        assert len(polls) == 1
        assert polls[0].attempt == 1

    def test_wait_for_result_raises_run_failed(self, mocker: MockerFixture) -> None:
        """A terminal non-COMPLETED state raises RunFailedError carrying the typed status."""
        client = self._client()
        mocker.patch.object(
            client,
            "get_run_result",
            mocker.AsyncMock(return_value=RunResultFailed(pipeline_run_id="run_1", status=RunStatus.CANCELLED, message="cancelled")),
        )

        with pytest.raises(RunFailedError) as exc_info:
            asyncio.run(client.wait_for_result("run_1"))
        assert exc_info.value.run_id == "run_1"
        assert exc_info.value.status == RunStatus.CANCELLED

    def test_wait_for_result_raises_run_failed_with_the_report(self, mocker: MockerFixture) -> None:
        """Over the recorded 409, wait_for_result raises RunFailedError carrying the status, the detail and the whole report."""
        client = self._client()
        body = _failed_results_problem("FAILED", _MODEL_REFUSED_REPORT)
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=_response(409, json=body)))

        with pytest.raises(RunFailedError) as exc_info:
            asyncio.run(client.wait_for_result("run_1"))
        err = exc_info.value
        assert err.run_id == "run_1"
        assert err.status == RunStatus.FAILED
        assert str(err) == f"Run finished with status FAILED: {_MODEL_REFUSED_REPORT['message']}"
        assert err.error is not None
        assert err.error.error_domain == "config"
        assert err.error.user_action is not None
        assert err.error.user_action.detail == _MODEL_REFUSED_REPORT["user_action"]["detail"]
        assert err.error.model_dump(exclude_none=True) == _MODEL_REFUSED_REPORT

    def test_wait_for_result_times_out(self, mocker: MockerFixture) -> None:
        """When the run never terminates and the timeout elapses, RunTimeoutError is raised (run survives)."""
        client = self._client()
        mocker.patch.object(
            client,
            "get_run_result",
            mocker.AsyncMock(return_value=RunResultRunning(pipeline_run_id="run_1", retry_after_seconds=0)),
        )

        with pytest.raises(RunTimeoutError) as exc_info:
            asyncio.run(client.wait_for_result("run_1", WaitForResultOptions(timeout_seconds=0.0)))
        assert exc_info.value.run_id == "run_1"
        assert exc_info.value.timeout_seconds == 0.0

    def test_wait_for_result_propagates_cancellation(self, mocker: MockerFixture) -> None:
        """Cancellation surfaces as asyncio.CancelledError (the loop never swallows it; run stays resumable)."""
        client = self._client()
        mocker.patch.object(client, "get_run_result", mocker.AsyncMock(side_effect=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.wait_for_result("run_1"))
