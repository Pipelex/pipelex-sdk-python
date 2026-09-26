"""Tests for the members `ApiResponseError` carries off a problem document, driven through a product route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_BASE_URL = "http://localhost:8081"

# A platform request-validation 422 as its error handler renders it: every standard member, the
# request id in the body and on the header, and the field-level `errors[]`.
_PLATFORM_422: dict[str, Any] = {
    "type": "https://pipelex.com/errors/validation_failed",
    "title": "Unprocessable entity",
    "status": 422,
    "code": "validation_failed",
    "detail": "Request validation failed.",
    "instance": "urn:pipelex:request:req-422",
    "request_id": "req-422",
    "errors": [{"field": "body.label", "code": "string_too_long", "detail": "String should have at most 64 characters"}],
}

# A runner-rendered problem (`ErrorReport.to_problem_document`) as the hosted API relays it: the
# standard slots plus the classification extension members, including a member this SDK does not name.
_RUNNER_PROBLEM: dict[str, Any] = {
    "type": "https://docs.pipelex.com/latest/errors/pipeline-input-error/",
    "title": "Pipeline input",
    "status": 422,
    "detail": "Input 'document' expects a Document, got an Image.",
    "request_id": "req-runner",
    "error_type": "PipelineInputError",
    "error_domain": "input",
    "error_category": "content",
    "retryable": False,
    "user_action": {"kind": "change_input", "detail": "Send a PDF for the 'document' input."},
    "location": "cv_screening.mthds:screen",
}


def _response(status_code: int, *, json_body: object | None = None, text: str | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, headers=headers or {}, request=request)
    return httpx.Response(status_code, text=text or "", headers=headers or {}, request=request)


class TestApiResponseError:
    def _raise_from(self, mocker: MockerFixture, response: httpx.Response) -> ApiResponseError:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))
        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.get_subscription())
        return exc_info.value

    def test_platform_problem_exposes_every_member(self, mocker: MockerFixture) -> None:
        err = self._raise_from(mocker, _response(422, json_body=_PLATFORM_422, headers={"X-Request-ID": "req-422"}))

        assert err.status == 422
        assert err.code == "validation_failed"
        assert err.type_uri == "https://pipelex.com/errors/validation_failed"
        assert err.title == "Unprocessable entity"
        assert err.server_message == "Request validation failed."
        assert err.request_id == "req-422"
        assert err.errors is not None
        assert [(item.field, item.code, item.detail) for item in err.errors] == [
            ("body.label", "string_too_long", "String should have at most 64 characters"),
        ]
        assert err.problem == _PLATFORM_422
        assert err.error_domain is None
        assert err.retryable is None
        assert err.user_action is None

    def test_runner_problem_exposes_the_classification_members(self, mocker: MockerFixture) -> None:
        err = self._raise_from(mocker, _response(422, json_body=_RUNNER_PROBLEM))

        assert err.type_uri == "https://docs.pipelex.com/latest/errors/pipeline-input-error/"
        assert err.title == "Pipeline input"
        assert err.error_type == "PipelineInputError"
        assert err.error_domain == "input"
        assert err.error_category == "content"
        assert err.retryable is False
        assert err.user_action is not None
        assert err.user_action.kind == "change_input"
        assert err.user_action.detail == "Send a PDF for the 'document' input."
        assert err.request_id == "req-runner"
        assert err.server_message == "Input 'document' expects a Document, got an Image."
        assert err.code is None
        # A member the SDK does not name stays reachable on the decoded document.
        assert err.problem is not None
        assert err.problem["location"] == "cv_screening.mthds:screen"

    def test_request_id_falls_back_to_the_header(self, mocker: MockerFixture) -> None:
        body = {key: value for key, value in _PLATFORM_422.items() if key != "request_id"}
        err = self._raise_from(mocker, _response(422, json_body=body, headers={"X-Request-ID": "req-from-header"}))

        assert err.request_id == "req-from-header"

    def test_request_id_from_the_header_when_the_body_is_not_a_problem(self, mocker: MockerFixture) -> None:
        err = self._raise_from(mocker, _response(502, text="Bad Gateway", headers={"X-Request-ID": "req-edge"}))

        assert err.request_id == "req-edge"
        assert err.problem is None
        assert err.response_body == "Bad Gateway"

    def test_body_request_id_wins_over_the_header(self, mocker: MockerFixture) -> None:
        err = self._raise_from(mocker, _response(422, json_body=_PLATFORM_422, headers={"X-Request-ID": "req-other"}))

        assert err.request_id == "req-422"

    @pytest.mark.parametrize(
        "body",
        [
            {"detail": "x", "retryable": "no", "user_action": "retry later", "errors": "none", "error_domain": 3, "type": None},
            {"detail": "x", "user_action": {"kind": 5}, "errors": [7]},
        ],
    )
    def test_members_of_the_wrong_shape_read_as_none(self, mocker: MockerFixture, body: dict[str, Any]) -> None:
        err = self._raise_from(mocker, _response(409, json_body=body))

        assert err.server_message == "x"
        assert err.retryable is None
        assert err.user_action is None
        assert err.errors is None
        assert err.error_domain is None
        assert err.type_uri is None
        assert err.request_id is None
        assert err.problem == body
