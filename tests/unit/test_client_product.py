"""Tests for the Pipelex product surface — verb + path + body per route, model round-trips, `.code` branching.

Ports `pipelex-sdk-js/tests/product.test.ts`. `_send` is mocked; bodies use complete valid
shapes (Pydantic validates on the way back, unlike the TS interfaces).
"""

import asyncio
import json

import httpx
import pytest
from pytest_mock import MockerFixture, MockType

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError
from pipelex_sdk.product_models import (
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


def _response(status_code: int, *, json_body: object | None = None, content: bytes | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{_BASE_URL}/x")
    if json_body is not None:
        return httpx.Response(status_code, json=json_body, request=request)
    if content is not None:
        return httpx.Response(status_code, content=content, request=request)
    return httpx.Response(status_code, request=request)


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

    def test_list_methods(self, mocker: MockerFixture) -> None:
        client = self._client()
        methods = [{"method_id": "m1", "name": "M", "mthds": "...", "created_at": "t", "updated_at": "t"}]
        send = self._mock_send(mocker, client, _response(200, json_body=methods))

        result = asyncio.run(client.list_methods())

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods"
        assert result[0].method_id == "m1"
        assert result[0].name == "M"

    def test_get_method_encodes_id(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"method_id": "a/b", "name": "M", "mthds": "...", "created_at": "t", "updated_at": "t"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.get_method("a/b"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/methods/a%2Fb"

    def test_create_method_posts_write_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"method_id": "m1", "name": "M", "mthds": "src", "created_at": "t", "updated_at": "t"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.create_method(MethodWriteInput(name="M", mthds="src", input_data={"a": 1})))

        sent = self._sent(send)
        assert sent.method == "POST"
        assert sent.url == f"{_BASE_URL}/v1/methods"
        assert sent.body == {"name": "M", "mthds": "src", "input_data": {"a": 1}}

    def test_update_method_puts_and_drops_absent_input_data(self, mocker: MockerFixture) -> None:
        client = self._client()
        body = {"method_id": "m1", "name": "Renamed", "mthds": "src", "created_at": "t", "updated_at": "t"}
        send = self._mock_send(mocker, client, _response(200, json_body=body))

        asyncio.run(client.update_method("m1", MethodWriteInput(name="Renamed", mthds="src")))

        sent = self._sent(send)
        assert sent.method == "PUT"
        assert sent.url == f"{_BASE_URL}/v1/methods/m1"
        # input_data is None → dropped from the wire (matches the JS undefined-drop).
        assert sent.body == {"name": "Renamed", "mthds": "src"}

    def test_delete_method_tolerates_empty_204(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(204))

        result = asyncio.run(client.delete_method("m1"))

        sent = self._sent(send)
        assert result is None
        assert sent.method == "DELETE"
        assert sent.url == f"{_BASE_URL}/v1/methods/m1"

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
        runs = [{"pipeline_run_id": "r1", "method_id": "m/1", "pipe_code": "p", "status": "RUNNING", "created_at": "t"}]
        send = self._mock_send(mocker, client, _response(200, json_body=runs))

        result = asyncio.run(client.list_runs("m/1"))

        assert self._sent(send).url == f"{_BASE_URL}/v1/runs?method_id=m%2F1"
        assert result[0].pipeline_run_id == "r1"
        assert result[0].status is RunStatus.RUNNING

    def test_update_run_drops_absent_finished_at_and_tolerates_empty_body(self, mocker: MockerFixture) -> None:
        client = self._client()
        send = self._mock_send(mocker, client, _response(204))

        result = asyncio.run(client.update_run("r1", UpdateRunInput(status="COMPLETED", result_url="https://x")))

        sent = self._sent(send)
        assert result is None
        assert sent.method == "PUT"
        assert sent.url == f"{_BASE_URL}/v1/runs/r1"
        assert sent.body == {"status": "COMPLETED", "result_url": "https://x"}
