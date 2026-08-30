"""The crate routes — `resolve` and `codegen` — and their three-form closure selector.

Ports the relevant slice of `pipelex-sdk-js/tests/crate-routes.test.ts`: verb + path + body,
the 200-verdict discipline (branch on `is_valid`), the strict three-way XOR at request
construction, the hosted `method_id` pass-through, and the fetch-sized budget a
`method_ref` closure gets (the server may have to clone before it answers).
"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.crate_models import CodegenRequest, CodegenValidReport, CrateInvalidReport, MthdsFileItem, ResolveRequest, ResolveValidReport
from pipelex_sdk.errors import ApiResponseError

_BASE_URL = "http://localhost:8081"
_METHOD_REF = "github.com/Pipelex/methods/documents@v0.1.0"

_RESOLVE_VALID: dict[str, object] = {"is_valid": True, "crate": {"concepts": {}, "pipes": {}, "domains": {}, "fingerprint": "abc"}, "message": "ok"}
_CODEGEN_VALID = {
    "is_valid": True,
    "kind": "types",
    "target": "python-pydantic",
    "crate_fingerprint": "abc",
    "engine_version": "0.55.0",
    "artifacts": [{"path": "models.py", "content": "# stamped\n"}],
    "lock": 'lock_version = 1\ncrate_fingerprint = "abc"\n',
    "lock_filename": "codegen.lock",
    "message": "ok",
}
_INVALID = {
    "is_valid": False,
    "message": "closure did not validate",
    "validation_errors": [{"category": "blueprint_validation", "message": "unknown pipe type"}],
}


def _response(status_code: int, *, json_body: object | None = None) -> httpx.Response:
    request = httpx.Request("POST", f"{_BASE_URL}/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    return httpx.Response(status_code, request=request)


class TestCrateRoutes:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def _mock_send(self, mocker: MockerFixture, client: PipelexAPIClient, response: httpx.Response) -> MockType:
        return mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))

    # ── resolve ──────────────────────────────────────────────────────

    def test_resolve_posts_files_and_returns_the_crate(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_RESOLVE_VALID))

        report = asyncio.run(client.resolve(ResolveRequest(files=[MthdsFileItem(content='domain = "demo"', source="b.mthds")])))

        call = send.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == f"{_BASE_URL}/v1/resolve"
        assert json.loads(call.kwargs["content"]) == {"files": [{"content": 'domain = "demo"', "source": "b.mthds"}]}
        assert isinstance(report, ResolveValidReport)
        assert report.crate["fingerprint"] == "abc"

    def test_resolve_method_id_is_a_pure_pass_through(self, mocker: MockerFixture) -> None:
        """Nothing is expanded client-side: the id rides the body alone and the platform
        resolves it (a bare runner rejects the request as carrying no source it understands).
        """
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_RESOLVE_VALID))

        asyncio.run(client.resolve(ResolveRequest(method_id="mt_1")))

        assert json.loads(send.call_args.kwargs["content"]) == {"method_id": "mt_1"}

    def test_resolve_invalid_closure_is_a_200_verdict(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(200, json_body=_INVALID))

        report = asyncio.run(client.resolve(ResolveRequest(method_ref=_METHOD_REF)))

        assert isinstance(report, CrateInvalidReport)
        assert report.validation_errors[0].message == "unknown pipe type"

    def test_resolve_no_verdict_raises_api_response_error(self, mocker: MockerFixture) -> None:
        """A selector-resolution failure (no package at the address, an unknown id) is a
        non-2xx — never an `is_valid: false` verdict.
        """
        client = self._client()
        self._mock_send(mocker, client, _response(404, json_body={"detail": "Unknown method", "code": "not_found"}))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.resolve(ResolveRequest(method_id="mt_ghost")))
        assert exc_info.value.status == 404

    # ── codegen ──────────────────────────────────────────────────────

    def test_codegen_posts_axes_and_returns_artifacts_with_lock(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_CODEGEN_VALID))

        report = asyncio.run(client.codegen(CodegenRequest(files=[MthdsFileItem(content="x")], target="python-pydantic")))

        call = send.call_args
        assert call.args[1] == f"{_BASE_URL}/v1/codegen"
        assert json.loads(call.kwargs["content"]) == {"files": [{"content": "x"}], "kind": "types", "target": "python-pydantic"}
        assert isinstance(report, CodegenValidReport)
        assert report.artifacts[0].path == "models.py"
        assert report.lock_filename == "codegen.lock"

    # ── the strict three-way XOR ─────────────────────────────────────

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"files": [MthdsFileItem(content="x")], "method_ref": _METHOD_REF},
            {"files": [MthdsFileItem(content="x")], "method_id": "mt_1"},
            {"method_ref": _METHOD_REF, "method_id": "mt_1"},
            {"files": [MthdsFileItem(content="x")], "method_ref": _METHOD_REF, "method_id": "mt_1"},
        ],
    )
    def test_request_construction_enforces_exactly_one_selector(self, kwargs: dict[str, object]) -> None:
        """The tooling routes are stateless, so there is no linkage exception: zero selectors
        and every pairing fail at construction, mirroring the server's request-shape 422.
        """
        with pytest.raises(ValidationError, match="exactly one"):
            ResolveRequest.model_validate(kwargs)
        with pytest.raises(ValidationError, match="exactly one"):
            CodegenRequest.model_validate({**kwargs, "target": "python-pydantic"})

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"files": []},
            {"method_ref": ""},
            {"method_ref": "   "},
            {"method_id": ""},
            {"method_id": "  \t"},
            {"files": [], "method_ref": "", "method_id": ""},
        ],
    )
    def test_empty_selectors_are_absent_and_fail_the_xor(self, kwargs: dict[str, object]) -> None:
        """`files=[]` and blank strings select nothing — the same empty-as-absent rule as the
        run routes — so an empty selector never counts as the sole one and never reaches the
        wire as an unusable value: alone it is zero selectors, refused at construction.
        """
        with pytest.raises(ValidationError, match="exactly one"):
            ResolveRequest.model_validate(kwargs)
        with pytest.raises(ValidationError, match="exactly one"):
            CodegenRequest.model_validate({**kwargs, "target": "python-pydantic"})

    def test_empty_selector_beside_a_real_one_is_simply_absent(self, mocker: MockerFixture) -> None:
        """An empty selector beside a real one is absent, not a conflict — exactly-one
        semantics stay coherent with the run boundary, and the empty key is not sent.
        """
        request = ResolveRequest(files=[MthdsFileItem(content="x")], method_ref="", method_id="  ")
        assert request.method_ref is None
        assert request.method_id is None

        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_RESOLVE_VALID))
        asyncio.run(client.resolve(request))
        assert json.loads(send.call_args.kwargs["content"]) == {"files": [{"content": "x"}]}

    # ── the fetch-sized budget ───────────────────────────────────────

    def test_method_ref_closure_gets_the_fetch_budget(self, mocker: MockerFixture) -> None:
        """Resolving an address can make the server clone a repository before it answers; the
        30s management budget would abort a legitimate cold-cache clone and blame the network.
        """
        client = self._client()
        codegen_valid = {**_CODEGEN_VALID, "target": "ts-zod"}
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(side_effect=[_response(200, json_body=_RESOLVE_VALID), _response(200, json_body=codegen_valid)]),
        )

        asyncio.run(client.resolve(ResolveRequest(method_ref=_METHOD_REF)))
        assert send.call_args.kwargs["request_timeout"] == 180.0

        asyncio.run(client.codegen(CodegenRequest(method_ref=_METHOD_REF, target="ts-zod")))
        assert send.call_args.kwargs["request_timeout"] == 180.0

    def test_inline_and_by_id_closures_keep_the_management_budget(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_RESOLVE_VALID))

        asyncio.run(client.resolve(ResolveRequest(files=[MthdsFileItem(content="x")])))
        assert send.call_args.kwargs["request_timeout"] == 30.0

        asyncio.run(client.resolve(ResolveRequest(method_id="mt_1")))
        assert send.call_args.kwargs["request_timeout"] == 30.0
