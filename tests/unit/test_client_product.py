"""Tests for the Pipelex product surface — verb + path + body per route, model round-trips, `.code` branching.

Ports `pipelex-sdk-js/tests/product.test.ts`. `_send` is mocked; bodies use complete valid
shapes (Pydantic validates on the way back, unlike the TS interfaces).
"""

import asyncio
import json
from typing import cast

import httpx
import pytest
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError
from pipelex_sdk.product_models import (
    MethodDeletionState,
    MethodFile,
    MethodWriteInput,
    OnboardingCurrentTool,
    OnboardingHeardFrom,
    OnboardingInputType,
    OnboardingRole,
    OnboardingSubmission,
    OrgRole,
    UpdateRunInput,
    UploadInput,
)
from pipelex_sdk.runs import RunStatus

_BASE_URL = "http://localhost:8081"

# The platform's `MethodPublic` shape: `org_id` and `created_by_user_id` are required, and
# `python` crosses the wire as one string holding a JSON `[{name, content}]` array.
_METHOD_BODY: dict[str, object] = {
    "method_id": "m1",
    "name": "M",
    "mthds": "src",
    "org_id": "org_1",
    "created_by_user_id": "u1",
    "python": "",
    "created_at": "t",
    "updated_at": "t",
}


def _response(status_code: int, *, json_body: object | None = None, content: bytes | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    if content is not None:
        return httpx.Response(status_code, content=content, request=request)
    return httpx.Response(status_code, request=request)


def _sent_body(send: MockType) -> dict[str, object]:
    """The decoded JSON request body of the single recorded `_send` call."""
    content = send.call_args.kwargs["content"]
    return cast("dict[str, object]", json.loads(content))


class _Sent:
    """The single `_send` call recorded by the spy, decoded for assertions."""

    def __init__(self, method: str, url: str, body: object | None) -> None:
        self.method = method
        self.url = url
        self.body = body


class TestClientProduct:
    def _client(self) -> PipelexAPIClient:
        return PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)

    def _mock_send(self, mocker: MockerFixture, client: PipelexAPIClient, response: httpx.Response) -> MockType:
        return mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=response))

    @staticmethod
    def _sent(send: MockType) -> _Sent:
        call = send.call_args
        content = call.kwargs["content"]
        body = json.loads(content) if content is not None else None
        return _Sent(method=call.args[0], url=call.args[1], body=body)

    # ── User profile ─────────────────────────────────────────────────

    def test_get_me(self, mocker: MockerFixture) -> None:
        client = self._client()
        profile = {"email": "a@b.com", "user_id": "u1", "full_name": "A B", "onboarding_completed_at": None}
        send = self._mock_send(mocker, client, _response(200, json_body=profile))

        result = asyncio.run(client.get_me())

        sent = self._sent(send)
        assert sent.method == "GET"
        assert sent.url == f"{_BASE_URL}/v1/me"
        assert result.email == "a@b.com"
        assert result.user_id == "u1"
        assert result.onboarding_completed_at is None

    # ── Methods catalog ──────────────────────────────────────────────

    def test_list_methods_returns_the_page_envelope(self, mocker: MockerFixture) -> None:
        """The route answers `{items, next_cursor}`; the rows are index projections, not full methods."""
        client = self._client()
        page = {
            "items": [{"method_id": "m1", "name": "M", "description": "d", "created_at": "t", "deletion_state": "pending"}],
            "next_cursor": "c1",
        }
        send = self._mock_send(mocker, client, _response(200, json_body=page))

        result = asyncio.run(client.list_methods())

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods"
        assert result.next_cursor == "c1"
        assert result.items[0].method_id == "m1"
        assert result.items[0].description == "d"
        # A method mid-deletion stays listed, so a UI can render "Deleting…".
        assert result.items[0].deletion_state is MethodDeletionState.PENDING

    def test_list_methods_keeps_query_params_on_presence(self, mocker: MockerFixture) -> None:
        """An explicit empty `q` is forwarded — bad input the API should reject, not something to drop."""
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"items": [], "next_cursor": None}))

        asyncio.run(client.list_methods(q="", limit=50, cursor="c/1"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods?q=&limit=50&cursor=c%2F1"

    def test_list_methods_omits_absent_query_params_entirely(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"items": [], "next_cursor": None}))

        asyncio.run(client.list_methods(limit=5))

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods?limit=5"

    def test_method_data_parses_the_new_fields_and_the_python_wire_string(self, mocker: MockerFixture) -> None:
        """`python` is one wire string; the boundary converts it so callers never see it."""
        client = self._client()
        body = {**_METHOD_BODY, "description": "d", "python": '[{"name": "a.py", "content": "x = 1"}]'}
        self._mock_send(mocker, client, _response(200, json_body=body))

        method = asyncio.run(client.get_method("m1"))

        assert method.org_id == "org_1"
        assert method.created_by_user_id == "u1"
        assert method.description == "d"
        assert method.deletion_state is None
        assert method.python == [MethodFile(name="a.py", content="x = 1")]

    def test_method_data_reads_the_clear_sentinel_as_no_files(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(200, json_body=_METHOD_BODY))

        assert asyncio.run(client.get_method("m1")).python == []

    def test_write_input_sends_python_three_ways(self, mocker: MockerFixture) -> None:
        """`None` omits the key (preserve), `[]` sends the clear sentinel, a list sends the JSON text."""
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=_METHOD_BODY))

        asyncio.run(client.update_method("m1", MethodWriteInput(name="M", mthds="src")))
        assert "python" not in _sent_body(send)

        send = self._mock_send(mocker, client, _response(200, json_body=_METHOD_BODY))
        asyncio.run(client.update_method("m1", MethodWriteInput(name="M", mthds="src", python=[])))
        assert _sent_body(send)["python"] == ""

        send = self._mock_send(mocker, client, _response(200, json_body=_METHOD_BODY))
        asyncio.run(client.create_method(MethodWriteInput(name="M", mthds="src", python=[MethodFile(name="a.py", content="x = 1")])))
        assert json.loads(cast("str", _sent_body(send)["python"])) == [{"name": "a.py", "content": "x = 1"}]

    def test_get_method_encodes_id(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {**_METHOD_BODY, "method_id": "a/b"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.get_method("a/b"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods/a%2Fb"

    def test_create_method_posts_write_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = _METHOD_BODY
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.create_method(MethodWriteInput(name="M", mthds="src", input_data={"a": 1})))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/methods"
        assert sent.body == {"name": "M", "mthds": "src", "input_data": {"a": 1}}

    def test_update_method_puts_and_drops_absent_input_data(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {**_METHOD_BODY, "name": "Renamed"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.update_method("m1", MethodWriteInput(name="Renamed", mthds="src")))

        sent = self._sent(send)
        assert sent.method == "PUT"
        assert sent.url == f"{_BASE_URL}/v1/methods/m1"
        # input_data is None → dropped from the wire (matches the JS undefined-drop).
        assert sent.body == {"name": "Renamed", "mthds": "src"}

    def test_delete_method_returns_the_202_acceptance(self, mocker: MockerFixture) -> None:
        """The erasure is asynchronous: the caller gets the CLAIM, never a "it's gone" signal.

        Completion is the row disappearing from `list_methods`, so the honest return value is the
        acceptance body — a `deletion_job_id` to log or correlate, and the state it started in.
        """
        client = self._client()
        body = {"method_id": "m1", "deletion_state": "pending", "deletion_job_id": "job-1"}
        send = self._mock_send(mocker, client, _response(202, json_body=body))

        accepted = asyncio.run(client.delete_method("m/1"))

        sent = self._sent(send)
        assert sent.method == "DELETE"
        assert sent.url == f"{_BASE_URL}/v1/methods/m%2F1"
        assert accepted.method_id == "m1"
        assert accepted.deletion_state is MethodDeletionState.PENDING
        assert accepted.deletion_job_id == "job-1"

    # ── Organizations ────────────────────────────────────────────────

    def test_list_memberships(self, mocker: MockerFixture) -> None:
        client = self._client()
        membership = {"org_id": "o1", "workos_organization_id": None, "name": "Acme", "is_personal": True, "role_in_org": "admin"}
        body = {"memberships": [membership], "active_org_feature_flags": ["flag"]}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        result = asyncio.run(client.list_memberships())

        assert self._sent(send).url == f"{_BASE_URL}/v1/organizations/memberships"
        assert result.active_org_feature_flags == ["flag"]
        assert result.memberships[0].role_in_org is OrgRole.ADMIN

    def test_create_organization(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"org_id": "o1", "workos_organization_id": None, "name": "Acme", "is_personal": False, "role_in_org": "admin"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.create_organization("Acme"))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/organizations"
        assert sent.body == {"name": "Acme"}

    def test_rename_organization(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"org_id": "o1", "workos_organization_id": None, "name": "Beta", "is_personal": False, "role_in_org": "member"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.rename_organization("o1", "Beta"))

        sent = self._sent(send)
        assert sent.method == "PATCH"
        assert sent.url == f"{_BASE_URL}/v1/organizations/o1"
        assert sent.body == {"name": "Beta"}

    # ── Billing ───────────────────────────────────────────────────────

    def test_get_subscription(self, mocker: MockerFixture) -> None:
        client = self._client()
        sub = {"plan": "pro", "status": "active", "can_use_service": True}
        send = self._mock_send(mocker, client, _response(200, json_body=sub))

        result = asyncio.run(client.get_subscription())

        assert self._sent(send).url == f"{_BASE_URL}/v1/billing/subscription"
        assert result.plan == "pro"
        assert result.can_use_service is True

    def test_list_plans_and_invoices_paths(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body=[]))

        asyncio.run(client.list_plans())
        assert self._sent(send).url == f"{_BASE_URL}/v1/billing/plans"

        send.reset_mock()
        asyncio.run(client.list_invoices())
        assert self._sent(send).url == f"{_BASE_URL}/v1/billing/invoices"

    def test_create_checkout(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"checkout_url": "https://stripe"}))

        result = asyncio.run(client.create_checkout("pro"))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/billing/checkout"
        assert sent.body == {"plan": "pro"}
        assert result.checkout_url == "https://stripe"

    def test_change_plan_409_conflict_surfaces_code(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(409, json_body={"code": "conflict", "message": "No subscription to change."}))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.change_plan("pro"))

        err = exc_info.value
        assert err.status == 409
        assert err.code == "conflict"
        assert err.server_message == "No subscription to change."

    def test_billing_portal_409_conflict_surfaces_code(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(409, json_body={"code": "conflict"}))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.get_billing_portal())

        assert exc_info.value.code == "conflict"

    # ── Pipelex API keys ─────────────────────────────────────────────

    def test_list_pipelex_api_keys(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"keys": []}))

        result = asyncio.run(client.list_pipelex_api_keys())

        assert self._sent(send).url == f"{_BASE_URL}/v1/pipelex-api-keys"
        assert result.keys == []

    def test_create_pipelex_api_key_returns_once_only_plaintext(self, mocker: MockerFixture) -> None:
        client = self._client()
        created = {"api_key": "plx_sk_secret", "id": "k1", "label": "L", "prefix": "plx_sk", "created_at": "t"}
        send = self._mock_send(mocker, client, _response(201, json_body=created))

        result = asyncio.run(client.create_pipelex_api_key("L"))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.body == {"label": "L"}
        assert result.api_key == "plx_sk_secret"

    def test_create_pipelex_api_key_409_limit_surfaces_code(self, mocker: MockerFixture) -> None:
        client = self._client()
        self._mock_send(mocker, client, _response(409, json_body={"code": "pipelex_api_key_limit_reached", "message": "Limit reached."}))

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(client.create_pipelex_api_key("L"))

        assert exc_info.value.code == "pipelex_api_key_limit_reached"

    def test_revoke_pipelex_api_key_encodes_id(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(204))

        asyncio.run(client.revoke_pipelex_api_key("a/b"))

        sent = self._sent(send)
        assert sent.method == "DELETE"
        assert sent.url == f"{_BASE_URL}/v1/pipelex-api-keys/a%2Fb"

    def test_rotate_pipelex_api_key_sends_no_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        created = {"api_key": "plx_sk_new", "id": "k1", "label": "L", "prefix": "plx_sk", "created_at": "t"}
        send = self._mock_send(mocker, client, _response(200, json_body=created))

        asyncio.run(client.rotate_pipelex_api_key("k1"))

        sent = self._sent(send)
        assert sent.url == f"{_BASE_URL}/v1/pipelex-api-keys/k1/rotate"
        assert sent.method == "POST"
        assert sent.body is None

    # ── Gateway API key ──────────────────────────────────────────────

    def test_create_gateway_api_key_always_sends_body_even_when_promo_none(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"gateway_api_key": "gw"}))

        asyncio.run(client.create_gateway_api_key(None))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/gateway-api-key"
        assert sent.body == {"promo_code": None}

    def test_get_gateway_api_key_null_until_provisioned(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"gateway_api_key": None}))

        result = asyncio.run(client.get_gateway_api_key())

        assert self._sent(send).url == f"{_BASE_URL}/v1/gateway-api-key"
        assert result.gateway_api_key is None

    # ── Onboarding ───────────────────────────────────────────────────

    def test_submit_onboarding_drops_absent_optionals(self, mocker: MockerFixture) -> None:
        client = self._client()
        submission = OnboardingSubmission(
            role=OnboardingRole.DEVELOPER,
            use_case="automate document review for the team",
            process_to_transform="manual review",
            input_types=[OnboardingInputType.DOCUMENTS],
            material_domain="legal",
            current_tool=OnboardingCurrentTool.NONE,
            heard_from=OnboardingHeardFrom.TWITTER,
        )
        send = self._mock_send(mocker, client, _response(204))

        result = asyncio.run(client.submit_onboarding(submission))

        sent = self._sent(send)
        assert result is None
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/onboarding/submit"
        assert sent.body == {
            "role": "developer",
            "use_case": "automate document review for the team",
            "process_to_transform": "manual review",
            "input_types": ["documents"],
            "material_domain": "legal",
            "current_tool": "none",
            "heard_from": "twitter",
        }

    # ── Storage ──────────────────────────────────────────────────────

    def test_resolve_storage_url(self, mocker: MockerFixture) -> None:
        client = self._client()
        resolved = {"url": "https://s3", "expires_at": "t", "content_type": "application/pdf"}
        send = self._mock_send(mocker, client, _response(200, json_body=resolved))

        result = asyncio.run(client.resolve_storage_url("s3://bucket/key"))

        sent = self._sent(send)
        assert sent.url == f"{_BASE_URL}/v1/resolve-storage-url"
        assert sent.body == {"uri": "s3://bucket/key"}
        assert result.url == "https://s3"
        assert result.content_type == "application/pdf"

    def test_upload_base64_payload(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"uri": "s3://x", "filename": "f.pdf"}))

        result = asyncio.run(client.upload(UploadInput(filename="f.pdf", data="Zm9v", content_type="application/pdf")))

        sent = self._sent(send)
        assert sent.url == f"{_BASE_URL}/v1/upload"
        assert sent.body == {"filename": "f.pdf", "data": "Zm9v", "content_type": "application/pdf"}
        assert result.uri == "s3://x"

    # ── Runs list / update ───────────────────────────────────────────

    def test_list_runs_encodes_query_value(self, mocker: MockerFixture) -> None:
        client = self._client()
        page = {
            "items": [{"pipeline_run_id": "r1", "method_id": "m/1", "pipe_code": "p", "status": "RUNNING", "created_at": "t"}],
            "next_cursor": "c1",
        }
        send = self._mock_send(mocker, client, _response(200, json_body=page))

        result = asyncio.run(client.list_runs("m/1"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/runs?method_id=m%2F1"
        assert result.next_cursor == "c1"
        assert result.items[0].pipeline_run_id == "r1"
        assert result.items[0].status is RunStatus.RUNNING

    def test_list_runs_keeps_date_bounds_and_paging_params_on_presence(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(200, json_body={"items": [], "next_cursor": None}))

        asyncio.run(client.list_runs("m1", created_from="2026-08-01T00:00:00+00:00", created_to="", limit=10, cursor="c1"))

        # Instants are percent-encoded; an explicit empty bound is forwarded, not dropped.
        assert self._sent(send).url == (
            f"{_BASE_URL}/v1/runs?method_id=m1&created_from=2026-08-01T00%3A00%3A00%2B00%3A00&created_to=&limit=10&cursor=c1"
        )

    def test_run_row_parses_with_null_method_id_and_pipe_code(self, mocker: MockerFixture) -> None:
        """An ad-hoc run belongs to no stored method, and a `main_pipe` run names no pipe."""
        client = self._client()
        row = {
            "pipeline_run_id": "r1",
            "method_id": None,
            "pipe_code": None,
            "status": "FAILED",
            "created_at": "t",
            "error": {"message": "boom", "error_type": "PipeExecutionError"},
        }
        self._mock_send(mocker, client, _response(200, json_body={"items": [row], "next_cursor": None}))

        result = asyncio.run(client.list_runs("m1"))

        pipeline_run = result.items[0]
        assert pipeline_run.method_id is None
        assert pipeline_run.pipe_code is None
        assert pipeline_run.error is not None
        assert pipeline_run.error.message == "boom"
        assert pipeline_run.error.error_type == "PipeExecutionError"

    def test_get_run_detail_encodes_id_and_returns_what_ran(self, mocker: MockerFixture) -> None:
        """The detail read is the only one carrying `mthds_contents` and `inputs`."""
        client = self._client()
        body = {
            "pipeline_run_id": "r/1",
            "method_id": "m1",
            "pipe_code": "p",
            "status": "COMPLETED",
            "created_at": "t",
            "mthds_contents": ["domain = 'x'"],
            "inputs": {"topic": "quantum"},
        }
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        detail = asyncio.run(client.get_run_detail("r/1"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/runs/r%2F1"
        assert detail.mthds_contents == ["domain = 'x'"]
        assert detail.inputs == {"topic": "quantum"}
        assert detail.status is RunStatus.COMPLETED

    def test_update_run_drops_absent_finished_at_and_tolerates_empty_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(204))

        result = asyncio.run(client.update_run("r1", UpdateRunInput(status="COMPLETED", result_url="https://x")))

        sent = self._sent(send)
        assert result is None
        assert sent.method == "PUT"
        assert sent.url == f"{_BASE_URL}/v1/runs/r1"
        assert sent.body == {"status": "COMPLETED", "result_url": "https://x"}
