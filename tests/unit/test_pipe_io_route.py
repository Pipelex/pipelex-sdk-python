"""`pipe_io` — `POST /v1/pipe-io`, the crate route that returns a method's three I/O artifacts.

The route's own slice of the crate family: verb, path and body; the two 200 arms and the standard's
artifacts typed on the valid one; what raises; and the fetch-sized budget a `method_ref` closure gets.
The selector XOR it shares with `resolve` and `codegen` is pinned for the whole family in
`test_crate_routes.py`.
"""

import asyncio
import json

import httpx
import pytest
from mthds.protocol.input_form import ObjectField, PipeInputFormDescriptor
from mthds.protocol.output_form import PipeOutputFormDescriptor
from mthds.protocol.pipe_io_contracts import PipeIOContract, PresenceMarker
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.crate_models import CrateInvalidReport, MthdsFileItem, PipeIORequest, PipeIOValidReport
from pipelex_sdk.errors import ApiResponseError
from tests.unit.test_data import PipeIOBodies

_BASE_URL = "http://localhost:8081"
_METHOD_REF = "github.com/Pipelex/methods/documents@v0.1.0"
_FILES = [MthdsFileItem(content='domain = "smoke"', source="smoke.mthds")]


def _response(status_code: int, *, json_body: object | None = None) -> httpx.Response:
    request = httpx.Request("POST", f"{_BASE_URL}/v1/pipe-io")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, request=request)


class TestPipeIORoute:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def _mock_send(self, mocker: MockerFixture, client: PipelexAPIClient, response: httpx.Response) -> MockType:
        return mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))

    # ── The request ──────────────────────────────────────────────────

    def test_posts_the_closure_to_the_route(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        asyncio.run(client.pipe_io(PipeIORequest(files=_FILES)))

        call = send.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == f"{_BASE_URL}/v1/pipe-io"
        # The opt-ins ride at their defaults; an absent `pipe_ref` is not sent, so the server's chain selects.
        assert json.loads(call.kwargs["content"]) == {
            "files": [{"content": 'domain = "smoke"', "source": "smoke.mthds"}],
            "all_pipes": False,
            "include_files": False,
        }

    def test_the_pipe_selector_and_the_opt_ins_ride_the_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        asyncio.run(client.pipe_io(PipeIORequest(method_ref=_METHOD_REF, pipe_ref="smoke.echo", all_pipes=True, include_files=True)))

        assert json.loads(send.call_args.kwargs["content"]) == {
            "method_ref": _METHOD_REF,
            "pipe_ref": "smoke.echo",
            "all_pipes": True,
            "include_files": True,
        }

    def test_method_id_is_a_pure_pass_through(self, mocker: MockerFixture) -> None:
        """Nothing is expanded client-side: the id rides the body alone and the platform resolves it."""
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        asyncio.run(client.pipe_io(PipeIORequest(method_id="mt_1")))

        assert json.loads(send.call_args.kwargs["content"]) == {"method_id": "mt_1", "all_pipes": False, "include_files": False}

    # ── The valid arm ────────────────────────────────────────────────

    def test_the_valid_arm_types_the_standards_artifacts(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        report = asyncio.run(client.pipe_io(PipeIORequest(files=_FILES)))

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref == PipeIOBodies.PIPE_REF
        assert report.default_pipe_ref == PipeIOBodies.PIPE_REF
        assert report.pending_signatures == []
        assert report.is_runnable is True
        assert report.files is None
        contract = report.pipe_io_contracts[PipeIOBodies.PIPE_REF]
        assert isinstance(contract, PipeIOContract)
        assert contract.inputs["doc"].presence == PresenceMarker.PLAIN
        descriptor = report.input_form[PipeIOBodies.PIPE_REF]
        assert isinstance(descriptor, PipeInputFormDescriptor)
        assert [field.name for field in descriptor.fields] == ["doc", "note", "dossier"]
        dossier = descriptor.fields[2]
        assert isinstance(dossier, ObjectField)
        assert [(field.name, field.required) for field in dossier.fields] == [("title", True), ("cover", False)]
        output = report.output_form[PipeIOBodies.PIPE_REF]
        assert isinstance(output, PipeOutputFormDescriptor)
        assert output.field.name == "output"

    def test_the_files_echo_and_stated_nulls_parse(self, mocker: MockerFixture) -> None:
        """Under `all_pipes` with no entry pipe, `pipe_ref` and `default_pipe_ref` are stated `null`s, and
        the `include_files` echo keeps a file's absent `source` absent.
        """
        client = self._client()
        body = {
            **PipeIOBodies.VALID,
            "pipe_ref": None,
            "default_pipe_ref": None,
            "pending_signatures": ["smoke.draft"],
            "is_runnable": False,
            "files": [{"content": 'domain = "smoke"', "source": "smoke.mthds"}, {"content": "x"}],
        }
        self._mock_send(mocker, client, _response(200, json_body=body))

        report = asyncio.run(client.pipe_io(PipeIORequest(files=_FILES, all_pipes=True, include_files=True)))

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref is None
        assert report.default_pipe_ref is None
        assert report.pending_signatures == ["smoke.draft"]
        assert report.is_runnable is False
        assert report.files == [MthdsFileItem(content='domain = "smoke"', source="smoke.mthds"), MthdsFileItem(content="x")]

    def test_the_valid_arm_is_extension_open(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(200, json_body={**PipeIOBodies.VALID, "future_member": 1}))

        report = asyncio.run(client.pipe_io(PipeIORequest(files=_FILES)))

        assert isinstance(report, PipeIOValidReport)
        assert report.model_extra == {"future_member": 1}

    # ── The invalid arm and the no-verdict conditions ───────────────

    def test_an_invalid_closure_is_a_200_verdict(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.INVALID))

        report = asyncio.run(client.pipe_io(PipeIORequest(files=_FILES, include_files=True)))

        assert isinstance(report, CrateInvalidReport)
        assert report.validation_errors[0].pipe_code == "echo"

    @pytest.mark.parametrize(
        ("status", "body", "error_type"),
        [
            (422, PipeIOBodies.UNKNOWN_PIPE_REFUSAL, "EntryPipeNotFoundError"),
            (422, PipeIOBodies.AMBIGUOUS_PIPE_REFUSAL, "EntryPipeAmbiguousError"),
            (422, PipeIOBodies.REQUEST_SHAPE_REFUSAL, "ValidationError"),
            (404, {"detail": "Unknown method", "code": "not_found"}, None),
        ],
    )
    def test_a_no_verdict_answer_raises_api_response_error(
        self, mocker: MockerFixture, status: int, body: dict[str, object], error_type: str | None
    ) -> None:
        """A refused selection is a request-shape `422`, never an `is_valid: false` verdict; the route
        itself does not translate it, so the runner's `error_type` stays readable on the error.
        """
        client = self._client()
        self._mock_send(mocker, client, _response(status, json_body=body))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.pipe_io(PipeIORequest(files=_FILES, pipe_ref="smoke.absent")))
        assert exc_info.value.status == status
        assert exc_info.value.error_type == error_type

    # ── The fetch-sized budget ───────────────────────────────────────

    def test_a_method_ref_closure_gets_the_fetch_budget(self, mocker: MockerFixture) -> None:
        """Resolving an address can make the server clone a repository before it answers."""
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        asyncio.run(client.pipe_io(PipeIORequest(method_ref=_METHOD_REF)))

        assert send.call_args.kwargs["request_timeout"] == 180.0

    @pytest.mark.parametrize("request_body", [PipeIORequest(files=_FILES), PipeIORequest(method_id="mt_1")])
    def test_inline_and_by_id_closures_keep_the_management_budget(self, mocker: MockerFixture, request_body: PipeIORequest) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=PipeIOBodies.VALID))

        asyncio.run(client.pipe_io(request_body))

        assert send.call_args.kwargs["request_timeout"] == 30.0
