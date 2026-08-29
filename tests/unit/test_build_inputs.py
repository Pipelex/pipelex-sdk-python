"""The `build_inputs` route — the signature source `prepare_inputs` reads. Pins the verb +
path + body, the 200-verdict discipline (branch on `is_valid`), and the no-verdict throw.

Ports the relevant slice of `pipelex-sdk-js/tests/build-routes.test.ts` for `/v1/build/inputs`.
`_send` is mocked; a produced verdict is a 200 discriminated on `is_valid`, a no-verdict
condition throws `ApiResponseError`.
"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.build_models import BuildInputsRequest, BuildInputsResponseAdapter, BuildInputsValidReport, CrateInvalidReport, MthdsFileItem
from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError

_BASE_URL = "http://localhost:8081"


def _response(status_code: int, *, json_body: object | None = None) -> httpx.Response:
    request = httpx.Request("POST", f"{_BASE_URL}/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, request=request)


class TestBuildInputs:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def _mock_send(self, mocker: MockerFixture, client: PipelexAPIClient, response: httpx.Response) -> MockType:
        return mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))

    def test_posts_files_and_flags_to_build_inputs(self, mocker: MockerFixture) -> None:
        client = self._client()
        valid = {
            "is_valid": True,
            "pipe_ref": "demo.main",
            "message": "ok",
            "format": "json",
            "explicit": True,
            "inputs": {"photo": {"concept": "demo.Photo", "content": {"url": "https://mock/p.png"}}},
        }
        send = self._mock_send(mocker, client, _response(200, json_body=valid))

        report = asyncio.run(
            client.build_inputs(BuildInputsRequest(files=[MthdsFileItem(content='domain = "demo"', source="b.mthds")], format="json", explicit=True))
        )

        call = send.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == f"{_BASE_URL}/v1/build/inputs"
        body = json.loads(call.kwargs["content"])
        assert body == {"files": [{"content": 'domain = "demo"', "source": "b.mthds"}], "format": "json", "explicit": True}
        assert isinstance(report, BuildInputsValidReport)
        assert report.pipe_ref == "demo.main"
        assert report.inputs is not None

    def test_invalid_closure_is_a_200_verdict(self, mocker: MockerFixture) -> None:
        client = self._client()
        invalid = {
            "is_valid": False,
            "message": "closure did not validate",
            "validation_errors": [{"category": "blueprint_validation", "message": "unknown pipe type"}],
        }
        self._mock_send(mocker, client, _response(200, json_body=invalid))

        report = asyncio.run(client.build_inputs(BuildInputsRequest(files=[MthdsFileItem(content="x")])))

        assert isinstance(report, CrateInvalidReport)
        assert report.validation_errors[0].message == "unknown pipe type"

    @pytest.mark.parametrize(
        "body",
        [
            # format=json but carrying no template at all — the flagged malformed-200 shape.
            {"is_valid": True, "pipe_ref": "demo.main", "message": "ok", "format": "json", "explicit": True},
            # format=json but carrying the toml template (mismatched/opposite shape).
            {"is_valid": True, "pipe_ref": "demo.main", "message": "ok", "format": "json", "explicit": True, "inputs_toml": "x = 1"},
            # format=toml but carrying no toml template.
            {"is_valid": True, "pipe_ref": "demo.main", "message": "ok", "format": "toml", "explicit": True},
        ],
    )
    def test_valid_report_without_matching_template_is_rejected(self, body: dict[str, object]) -> None:
        # A valid verdict must carry the template its `format` selects — the adapter's
        # malformed-200 guarantee, now honored for the template shape too.
        with pytest.raises(ValidationError):
            BuildInputsResponseAdapter.validate_python(body)

    def test_valid_toml_report_is_accepted(self) -> None:
        body = {"is_valid": True, "pipe_ref": "demo.main", "message": "ok", "format": "toml", "explicit": True, "inputs_toml": "photo = 1"}
        report = BuildInputsResponseAdapter.validate_python(body)
        assert isinstance(report, BuildInputsValidReport)
        assert report.inputs_toml == "photo = 1"

    def test_no_verdict_422_raises_api_response_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        problem = {"detail": "Unknown pipe_ref", "error_type": "PipeNotFound"}
        self._mock_send(mocker, client, _response(422, json_body=problem))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.build_inputs(BuildInputsRequest(files=[MthdsFileItem(content="x")], pipe_ref="demo.nope")))
        assert exc_info.value.status == 422

    # ── the closure selector (files XOR method_ref, NO method_id) ────

    def test_method_ref_closure_rides_the_body_with_the_fetch_budget(self, mocker: MockerFixture) -> None:
        """An address closure may make the server clone before answering, so the request gets
        the fetch-sized budget instead of the 30s management one.
        """
        client = self._client()
        valid: dict[str, object] = {
            "is_valid": True,
            "pipe_ref": "documents.summarize",
            "message": "ok",
            "format": "json",
            "explicit": False,
            "inputs": {},
        }
        send = self._mock_send(mocker, client, _response(200, json_body=valid))

        report = asyncio.run(client.build_inputs(BuildInputsRequest(method_ref="github.com/Pipelex/methods/documents@v0.1.0")))

        call = send.call_args
        body = json.loads(call.kwargs["content"])
        assert body == {"method_ref": "github.com/Pipelex/methods/documents@v0.1.0", "format": "json", "explicit": False}
        assert call.kwargs["request_timeout"] == 180.0
        assert isinstance(report, BuildInputsValidReport)

    def test_inline_files_keep_the_management_budget(self, mocker: MockerFixture) -> None:
        client = self._client()
        valid: dict[str, object] = {"is_valid": True, "pipe_ref": "demo.main", "message": "ok", "format": "json", "explicit": False, "inputs": {}}
        send = self._mock_send(mocker, client, _response(200, json_body=valid))

        asyncio.run(client.build_inputs(BuildInputsRequest(files=[MthdsFileItem(content="x")])))

        assert send.call_args.kwargs["request_timeout"] == 30.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"files": [MthdsFileItem(content="x")], "method_ref": "github.com/x/y@v1"},
        ],
    )
    def test_request_construction_enforces_files_xor_method_ref(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            BuildInputsRequest.model_validate(kwargs)

    def test_method_id_is_refused_with_a_teaching_error(self) -> None:
        """The `/v1/build/*` projections take no `method_id` — a teaching error beats pydantic
        silently ignoring the unknown key for a caller migrating off the by-id habit.
        """
        with pytest.raises(ValidationError, match="build_inputs takes no method_id"):
            BuildInputsRequest.model_validate({"method_id": "mt_1"})
