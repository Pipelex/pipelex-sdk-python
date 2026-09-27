"""Tests for a run the plane refuses: `start`, `execute` and `start_and_wait` raise the typed `ApiResponseError`.

Each case replays a refusal the dev plane answered, byte for byte (`RefusedRunBodies`), and checks that the
error's message and members carry the reason, the failing pipe and the next step — where the inherited
`httpx.HTTPStatusError` regime said only "Client error '422 Unprocessable Entity'". The SDK's error is
`mthds`'s own `ApiResponseError` narrowed, so a handler written against the standard's client catches it too.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from mthds.runners.api.exceptions import ApiResponseError as MthdsApiResponseError

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError
from pipelex_sdk.validation_models import ValidationErrorCategory, ValidationErrorItem
from tests.unit.test_data import RefusedRunBodies

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from pytest_mock import MockerFixture, MockType

_BASE_URL = "http://localhost:8081"

_HOSTED_VERSION = {"protocol_version": "0.6.0", "implementation": "pipelex-hosted", "runner_version": "0.9.0"}


def _start(client: PipelexAPIClient) -> Coroutine[Any, Any, object]:
    return client.start(pipe_code="pitch_product", mthds_contents=['domain = "sales_copy"'])


def _execute(client: PipelexAPIClient) -> Coroutine[Any, Any, object]:
    return client.execute(pipe_code="pitch_product", mthds_contents=['domain = "sales_copy"'])


# Each run route, called with arguments that name something to run: (route, call).
_RUN_ROUTE_CALLS = [pytest.param("start", _start, id="start"), pytest.param("execute", _execute, id="execute")]

_COMBINE_FAILURE_DETAIL: str = json.loads(RefusedRunBodies.COMBINE_FAILURE_AT_RUN)["detail"]

# Each refusal: (body, the reason, the next step, what the message says after the status). The next
# step rides its own line — unless the reason already ends with it, as the combine failure's does, in
# which case the message says it once.
_REFUSALS = [
    pytest.param(
        RefusedRunBodies.UNKNOWN_MODEL_AT_LOAD,
        RefusedRunBodies.UNKNOWN_MODEL_DETAIL,
        RefusedRunBodies.UNKNOWN_MODEL_NEXT_STEP,
        f"{RefusedRunBodies.UNKNOWN_MODEL_DETAIL}\nNext step: {RefusedRunBodies.UNKNOWN_MODEL_NEXT_STEP}",
        id="unknown-model-at-load",
    ),
    pytest.param(
        RefusedRunBodies.COMBINE_FAILURE_AT_RUN,
        _COMBINE_FAILURE_DETAIL,
        RefusedRunBodies.COMBINE_FAILURE_NEXT_STEP,
        _COMBINE_FAILURE_DETAIL,
        id="combine-failure-at-run",
    ),
    pytest.param(
        RefusedRunBodies.UNSERVED_MODEL_AT_RUN,
        RefusedRunBodies.UNSERVED_MODEL_DETAIL,
        RefusedRunBodies.UNSERVED_MODEL_NEXT_STEP,
        f"{RefusedRunBodies.UNSERVED_MODEL_DETAIL}\nNext step: {RefusedRunBodies.UNSERVED_MODEL_NEXT_STEP}",
        id="unserved-model-at-run",
    ),
]


def _response(status_code: int, *, text: str | None = None, json_body: object | None = None) -> httpx.Response:
    """A wire response: the refusal's raw bytes as `text`, or a JSON body."""
    request = httpx.Request("POST", f"{_BASE_URL}/v1/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, text=text or "", headers={"content-type": "application/problem+json"}, request=request)


class TestClientRefusedRun:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def _patch_send(self, mocker: MockerFixture, client: PipelexAPIClient, *responses: httpx.Response) -> MockType:
        return mocker.patch.object(client, "_send", mocker.AsyncMock(side_effect=list(responses)))

    def _raised(self, mocker: MockerFixture, body: str, call: Callable[[PipelexAPIClient], Coroutine[Any, Any, object]]) -> ApiResponseError:
        client = self._client()
        self._patch_send(mocker, client, _response(422, text=body))
        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(call(client))
        return exc_info.value

    @pytest.mark.parametrize(("route", "call"), _RUN_ROUTE_CALLS)
    @pytest.mark.parametrize(("body", "detail", "next_step", "said"), _REFUSALS)
    def test_a_refused_run_raises_the_typed_error_with_its_reason_and_next_step(
        self,
        mocker: MockerFixture,
        route: str,
        call: Callable[[PipelexAPIClient], Coroutine[Any, Any, object]],
        body: str,
        detail: str,
        next_step: str,
        said: str,
    ) -> None:
        exc = self._raised(mocker, body, call)

        assert isinstance(exc, MthdsApiResponseError)
        assert not isinstance(exc, httpx.HTTPStatusError)
        assert str(exc) == f"API POST /v1/{route} failed (422): {said}"
        assert exc.status == 422
        assert exc.status_text == "Unprocessable Entity"
        assert exc.api_url == _BASE_URL
        assert exc.request_url == f"{_BASE_URL}/v1/{route}"
        assert exc.headers["content-type"] == "application/problem+json"
        assert exc.server_message == detail
        assert exc.user_action is not None
        assert exc.user_action.detail == next_step
        assert exc.error_domain == "input"
        assert exc.response_body == body
        assert exc.problem == json.loads(body)

    def test_a_bundle_refused_at_load_names_the_failing_pipe_in_typed_diagnostics(self, mocker: MockerFixture) -> None:
        exc = self._raised(mocker, RefusedRunBodies.UNKNOWN_MODEL_AT_LOAD, _start)

        assert exc.type_uri == "https://docs.pipelex.com/latest/errors/validate-bundle-error/"
        assert exc.title == "Validate bundle"
        assert exc.instance == "/v1/execute"
        assert exc.request_id == "req_a3dd6900-7909-48d3-b551-0140e73ac7fc"
        assert exc.error_type == "ValidateBundleError"
        assert exc.error_category == "configuration"
        assert exc.retryable is False
        assert exc.user_action is not None
        assert exc.user_action.kind == "change_input"
        assert exc.code is None
        assert exc.errors is None
        assert exc.validation_errors is not None
        assert len(exc.validation_errors) == 1
        item = exc.validation_errors[0]
        assert isinstance(item, ValidationErrorItem)
        assert item.category == ValidationErrorCategory.PIPE_VALIDATION
        assert item.message == RefusedRunBodies.UNKNOWN_MODEL_DETAIL
        assert item.error_type == "unknown_model"
        assert item.pipe_code == "draft_pitch"
        assert item.domain_code == "sales_copy"
        assert item.field_path == "pipe.draft_pitch.model"
        assert item.field_name == "model"
        # The runner's locators this SDK does not declare stay on the item's extras.
        assert item.model_extra == {
            "model_reference": "gpt-5.1",
            "model_type": "llm",
            "suggestions": ["gpt-5.5", "gpt-5.4", "gpt-5.6-sol", "gpt-5.4-pro", "gpt-5.6-luna"],
        }

    def test_a_run_failed_at_a_combine_step_names_the_pipe_and_leaves_absent_members_none(self, mocker: MockerFixture) -> None:
        exc = self._raised(mocker, RefusedRunBodies.COMBINE_FAILURE_AT_RUN, _execute)

        assert exc.error_type == "StuffFactoryError"
        assert exc.type_uri == "https://docs.pipelex.com/latest/errors/stuff-factory-error/"
        assert exc.request_id == "req_d4212542-63a6-4ddb-87c9-4b968785c8c5"
        assert exc.server_message is not None
        assert exc.server_message.startswith("Pipe 'analyze_topics' failed (review_topics → analyze_topics)")
        assert exc.user_action is not None
        assert exc.user_action.kind == "change_input"
        # The document carries no classification of an inference failure, no retry verdict and no itemized list.
        assert exc.error_category is None
        assert exc.retryable is None
        assert exc.validation_errors is None

    def test_a_run_failed_on_an_unserved_model_advises_changing_the_model(self, mocker: MockerFixture) -> None:
        exc = self._raised(mocker, RefusedRunBodies.UNSERVED_MODEL_AT_RUN, _execute)

        assert exc.error_type == "ModelNotFoundError"
        assert exc.title == "Model not found"
        assert exc.error_category == "configuration"
        assert exc.retryable is False
        assert exc.user_action is not None
        assert exc.user_action.kind == "change_model"
        assert exc.validation_errors is None

    def test_start_and_wait_on_a_refused_method_raises_the_typed_error_without_falling_back(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._patch_send(
            mocker, client, _response(200, json_body=_HOSTED_VERSION), _response(422, text=RefusedRunBodies.UNKNOWN_MODEL_AT_LOAD)
        )

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.start_and_wait(pipe_code="pitch_product", mthds_contents=['domain = "sales_copy"']))

        exc = exc_info.value
        assert str(exc) == (
            f"API POST /v1/start failed (422): {RefusedRunBodies.UNKNOWN_MODEL_DETAIL}\nNext step: {RefusedRunBodies.UNKNOWN_MODEL_NEXT_STEP}"
        )
        assert exc.validation_errors is not None
        assert exc.validation_errors[0].pipe_code == "draft_pitch"
        # A refusal is no missing run store: the durable path stops here, with no blocking retry of the same method.
        assert [call.args[1] for call in send.call_args_list] == [f"{_BASE_URL}/v1/version", f"{_BASE_URL}/v1/start"]

    @pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy], ids=["copy", "deepcopy"])
    def test_the_error_survives_copying_with_its_pipelex_members(
        self, mocker: MockerFixture, duplicate: Callable[[ApiResponseError], ApiResponseError]
    ) -> None:
        """Copying goes through the base's `__reduce__`, as pickling across a process boundary does."""
        exc = self._raised(mocker, RefusedRunBodies.UNKNOWN_MODEL_AT_LOAD, _start)

        clone = duplicate(exc)

        assert type(clone) is ApiResponseError
        assert str(clone) == str(exc)
        assert clone.error_category == "configuration"
        assert clone.validation_errors == exc.validation_errors
        assert clone.user_action == exc.user_action
        assert clone.request_url == exc.request_url
