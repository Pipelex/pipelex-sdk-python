"""Tests for `PipelexAPIClient` construction — credential resolution and base-URL validation."""

import os

import pytest
from mthds.protocol.exceptions import PipelineRequestError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient

_MTHDS_DEFAULT_CREDENTIALS = {"api_key": "", "api_url": "https://api.pipelex.com", "runner": "api", "telemetry": "0"}


class TestClientConstruction:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, mocker: MockerFixture) -> None:
        """Hermetic construction — no real env vars, mthds resolver returns defaults."""
        mocker.patch.dict(os.environ, {}, clear=True)
        mocker.patch("pipelex_sdk.client.load_credentials", return_value=dict(_MTHDS_DEFAULT_CREDENTIALS))

    def test_defaults_to_hosted_base_and_anonymous(self) -> None:
        client = PipelexAPIClient()
        assert client.api_base_url == "https://api.pipelex.com"
        assert client.origin_url == "https://api.pipelex.com"
        assert client.api_token == ""

    def test_pipelex_env_takes_precedence_over_mthds(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {"PIPELEX_API_KEY": "pk-live", "PIPELEX_API_URL": "http://localhost:8081"}, clear=True)
        mocker.patch(
            "pipelex_sdk.client.load_credentials",
            return_value={"api_key": "mthds-key", "api_url": "https://mthds.example.com", "runner": "api", "telemetry": "0"},
        )
        client = PipelexAPIClient()
        assert client.api_token == "pk-live"
        assert client.api_base_url == "http://localhost:8081"

    def test_falls_back_to_mthds_credentials(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "pipelex_sdk.client.load_credentials",
            return_value={"api_key": "mthds-key", "api_url": "https://mthds.example.com", "runner": "api", "telemetry": "0"},
        )
        client = PipelexAPIClient()
        assert client.api_token == "mthds-key"
        assert client.api_base_url == "https://mthds.example.com"

    def test_explicit_args_override_env_and_credentials(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {"PIPELEX_API_KEY": "pk-env", "PIPELEX_API_URL": "http://env.example.com"}, clear=True)
        client = PipelexAPIClient(api_token="arg-token", api_base_url="https://arg.example.com")
        assert client.api_token == "arg-token"
        assert client.api_base_url == "https://arg.example.com"

    def test_strips_trailing_slash(self) -> None:
        client = PipelexAPIClient(api_base_url="https://api.pipelex.com/")
        assert client.api_base_url == "https://api.pipelex.com"
        assert client.origin_url == "https://api.pipelex.com"

    def test_origin_includes_port(self) -> None:
        client = PipelexAPIClient(api_base_url="http://localhost:8081")
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
        ],
    )
    def test_rejects_non_host_only_base_url(self, bad_url: str) -> None:
        with pytest.raises(PipelineRequestError):
            PipelexAPIClient(api_base_url=bad_url)
