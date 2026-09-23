"""Tests for `pipelex_sdk.user_agent` and the `User-Agent` `PipelexAPIClient` builds through the `mthds` seam."""

import os
import re

import pytest
from mthds.runners.api import user_agent as mthds_user_agent
from mthds.version import __version__ as mthds_version
from pydantic import ValidationError
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.user_agent import SDK_TOKEN_NAME, AppInfo, pipelex_sdk_token
from pipelex_sdk.version import __version__

_BASE_URL = "http://localhost:8081"
# The spec's runtime token: `python/<major.minor.micro> (<os>; <arch>)`.
_RUNTIME_PATTERN = r"python/\d+\.\d+\.\d+ \([^;()]+; [^;()]+\)"


class TestUserAgent:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, mocker: MockerFixture) -> None:
        mocker.patch.dict(os.environ, {}, clear=True)

    # ── The re-exported AppInfo ──────────────────────────────────────

    def test_app_info_is_the_mthds_class(self) -> None:
        assert AppInfo is mthds_user_agent.AppInfo

    @pytest.mark.parametrize("details", [["batch", "host=openai"], ("batch", "host=openai")])
    def test_app_info_details_accept_a_list_or_a_tuple(self, details: list[str] | tuple[str, ...]) -> None:
        # A list is typed as a tuple but still accepted at run time (the model is not strict).
        assert AppInfo.model_validate({"name": "acme", "details": details}).details == ("batch", "host=openai")

    def test_app_info_empty_optional_fields_count_as_absent(self) -> None:
        app_info = AppInfo(name="acme", version="", url="", details=())
        assert app_info.version is None
        assert app_info.url is None
        assert app_info.details == ()

    @pytest.mark.parametrize(
        "fields",
        [
            {"name": "acme invoicer"},
            {"name": "acme", "version": "1 4"},
            {"name": "acme", "url": "https://café.example"},
            {"name": "acme", "details": ["a;b"]},
            {"name": "acme", "extra": "x"},
        ],
    )
    def test_app_info_refuses_an_invalid_field_with_a_value_error(self, fields: dict[str, object]) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppInfo.model_validate(fields)
        assert isinstance(exc_info.value, ValueError)

    # ── This SDK's token ─────────────────────────────────────────────

    def test_sdk_token_is_the_registered_name_and_package_version(self) -> None:
        assert SDK_TOKEN_NAME == "pipelex-sdk-python"
        assert pipelex_sdk_token() == f"pipelex-sdk-python/{__version__}"

    def test_sdk_tokens_put_this_sdk_before_mthds(self) -> None:
        assert PipelexAPIClient.user_agent_sdk_tokens() == (f"pipelex-sdk-python/{__version__}", f"mthds-python/{mthds_version}")

    # ── The whole header ─────────────────────────────────────────────

    def test_header_without_app_info_starts_with_the_sdk_tokens(self) -> None:
        user_agent = PipelexAPIClient(base_url=_BASE_URL).user_agent
        expected_prefix = f"pipelex-sdk-python/{__version__} mthds-python/{mthds_version} "
        assert re.fullmatch(re.escape(expected_prefix) + _RUNTIME_PATTERN, user_agent)

    def test_header_is_exactly_the_spec_shape(self, mocker: MockerFixture) -> None:
        mocker.patch("mthds.runners.api.user_agent.platform.system", return_value="Linux")
        mocker.patch("mthds.runners.api.user_agent.platform.machine", return_value="x86_64")
        mocker.patch("mthds.runners.api.user_agent.sys.version_info", _VersionInfo(3, 12, 4))
        mocker.patch("mthds.runners.api.user_agent.__version__", "0.16.0")
        mocker.patch("pipelex_sdk.user_agent.__version__", "0.11.0")
        client = PipelexAPIClient(base_url=_BASE_URL, app_info=AppInfo(name="acme-invoicer", version="1.4.0"))
        assert client.user_agent == "acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.16.0 python/3.12.4 (linux; x86_64)"

    def test_over_long_header_fails_at_construction(self) -> None:
        with pytest.raises(ValueError, match="512-character limit"):
            PipelexAPIClient(base_url=_BASE_URL, app_info=AppInfo(name="a" * 600))


class _VersionInfo:
    """A stand-in for `sys.version_info`, which `runtime_token` reads by attribute."""

    def __init__(self, major: int, minor: int, micro: int) -> None:
        self.major = major
        self.minor = minor
        self.micro = micro
