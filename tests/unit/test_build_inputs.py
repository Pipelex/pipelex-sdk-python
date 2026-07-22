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
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.build_models import BuildInputsRequest, BuildInputsValidReport, CrateInvalidReport, MthdsFileItem
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

    def test_no_verdict_422_raises_api_response_error(self, mocker: MockerFixture) -> None:
        client = self._client()
        problem = {"detail": "Unknown pipe_ref", "error_type": "PipeNotFound"}
        self._mock_send(mocker, client, _response(422, json_body=problem))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.build_inputs(BuildInputsRequest(files=[MthdsFileItem(content="x")], pipe_ref="demo.nope")))
        assert exc_info.value.status == 422
