"""Tests for `PipelexAPIClient` construction — credential resolution and base-URL validation."""

import os

import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient


class TestClientConstruction:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, mocker: MockerFixture) -> None:
        """Hermetic construction — no real env vars."""
        mocker.patch.dict(os.environ, {}, clear=True)

    def test_defaults_to_hosted_base_and_anonymous(self) -> None:
        client = PipelexAPIClient()
        assert client.base_url == "https://api.pipelex.com"
        assert client.origin_url == "https://api.pipelex.com"
        assert client.api_key == ""

    def test_reads_pipelex_env_vars(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {"PIPELEX_API_KEY": "pk-live", "PIPELEX_BASE_URL": "http://localhost:8081"}, clear=True)
        client = PipelexAPIClient()
        assert client.api_key == "pk-live"
        assert client.base_url == "http://localhost:8081"

    def test_mthds_resolver_is_never_consulted(self, mocker: MockerFixture) -> None:
        """Regression: this SDK is Pipelex-only. `MTHDS_API_KEY` / `MTHDS_BASE_URL` are a
        credential pair for whatever runner the vendor-neutral mthds tooling targets — an
        unconfigured client must stay anonymous against the hosted default instead of
        borrowing a key configured for another runner.
        """
        mocker.patch.dict(os.environ, {"MTHDS_API_KEY": "mthds-key", "MTHDS_BASE_URL": "http://localhost:8081"}, clear=True)
        client = PipelexAPIClient()
        assert client.api_key == ""
        assert client.base_url == "https://api.pipelex.com"

    def test_explicit_args_override_env(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {"PIPELEX_API_KEY": "pk-env", "PIPELEX_BASE_URL": "http://env.example.com"}, clear=True)
        client = PipelexAPIClient(api_key="arg-token", base_url="https://arg.example.com")
        assert client.api_key == "arg-token"
        assert client.base_url == "https://arg.example.com"

    def test_explicit_empty_token_forces_anonymous_over_env(self, mocker: MockerFixture) -> None:
        """An explicit `api_key=""` means anonymous and must win over a configured env token."""
        mocker.patch.dict(os.environ, {"PIPELEX_API_KEY": "pk-env"}, clear=True)
        client = PipelexAPIClient(api_key="")
        assert client.api_key == ""

    def test_strips_trailing_slash(self) -> None:
        client = PipelexAPIClient(base_url="https://api.pipelex.com/")
        assert client.base_url == "https://api.pipelex.com"
        assert client.origin_url == "https://api.pipelex.com"

    def test_origin_includes_port(self) -> None:
        client = PipelexAPIClient(base_url="http://localhost:8081")
        assert client.origin_url == "http://localhost:8081"

    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://api.pipelex.com/v1",  # path
            "https://api.pipelex.com?x=1",  # query
            "https://api.pipelex.com#frag",  # fragment
            "https://user:pass@api.pipelex.com",  # embedded credentials
            "ftp://api.pipelex.com",  # non-http(s) scheme
            "api.pipelex.com",  # no scheme
            "not a url",  # garbage
            "",  # explicit empty string — presence semantics: it must fail, not fall through
        ],
    )
    def test_rejects_non_host_only_base_url(self, bad_url: str) -> None:
        with pytest.raises(PipelineRequestError):
            PipelexAPIClient(base_url=bad_url)

    def test_set_but_empty_base_url_env_raises(self, mocker: MockerFixture) -> None:
        """A set-but-empty `PIPELEX_BASE_URL` (e.g. an unfilled CI secret) must fail fast
        instead of silently targeting the hosted default with whatever API key is configured.
        """
        mocker.patch.dict(os.environ, {"PIPELEX_BASE_URL": "", "PIPELEX_API_KEY": "pk-live"}, clear=True)
        with pytest.raises(PipelineRequestError):
            PipelexAPIClient()

    def test_default_request_timeout(self) -> None:
        """With no override, the blocking-execute ceiling defaults to 20 minutes."""
        client = PipelexAPIClient()
        assert client.request_timeout_seconds == 1200.0

    @pytest.mark.parametrize("timeout_seconds", [30.0, 0.0])
    def test_request_timeout_seconds_override(self, timeout_seconds: float) -> None:
        """An explicit `request_timeout_seconds` sets the per-instance ceiling — including a falsy `0.0`."""
        client = PipelexAPIClient(request_timeout_seconds=timeout_seconds)
        assert client.request_timeout_seconds == timeout_seconds
