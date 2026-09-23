"""Tests for `pipelex_sdk.user_agent` — `AppInfo` rendering and refusals, and the header's composition."""

import re
from importlib.metadata import PackageNotFoundError, version

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from pipelex_sdk.user_agent import MAX_USER_AGENT_LENGTH, AppInfo, build_user_agent, is_token
from pipelex_sdk.version import __version__

# The spec's runtime token: `python/<major.minor.micro> (<os>; <arch>)`.
_RUNTIME_PATTERN = r"python/\d+\.\d+\.\d+ \([^;()]+; [^;()]+\)"


class TestUserAgent:
    # ── is_token ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("value", ["acme-invoicer", "1.4.0", "0.2.16-rc.01", "a", "!#$%&'*+-.^_`|~"])
    def test_is_token_accepts_tchar_strings(self, value: str) -> None:
        assert is_token(value) is True

    @pytest.mark.parametrize("value", ["", "acme invoicer", "a/b", "a(b", "a;b", "a=b", "café", "a\tb", 'a"b', "a,b"])
    def test_is_token_refuses_non_tchar_strings(self, value: str) -> None:
        assert is_token(value) is False

    # ── AppInfo rendering ────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("app_info", "expected"),
        [
            (AppInfo(name="acme-invoicer"), "acme-invoicer"),
            (AppInfo(name="acme-invoicer", version="1.4.0"), "acme-invoicer/1.4.0"),
            (AppInfo(name="acme-invoicer", url="https://acme.example"), "acme-invoicer (+https://acme.example)"),
            (
                AppInfo(name="pipelex-mcp", version="0.17.0", details=["workshop", "host=claude-code/2.1.4"]),
                "pipelex-mcp/0.17.0 (workshop; host=claude-code/2.1.4)",
            ),
            (
                AppInfo(name="acme", version="2", details=["console", "host=openai"], url="https://acme.example/bot"),
                "acme/2 (console; host=openai; +https://acme.example/bot)",
            ),
        ],
    )
    def test_app_info_renders_name_version_details_url(self, app_info: AppInfo, expected: str) -> None:
        assert app_info.render() == expected

    def test_app_info_details_default_is_empty(self) -> None:
        assert AppInfo(name="acme").details == []

    # ── AppInfo refusals ─────────────────────────────────────────────

    @pytest.mark.parametrize("name", ["", "acme invoicer", "acme/1", "acme(x)", "acmé"])
    def test_app_info_refuses_invalid_name(self, name: str) -> None:
        with pytest.raises(ValueError, match=r"app_info\.name"):
            AppInfo(name=name)

    @pytest.mark.parametrize("app_version", ["", "1 4", "1/4", "1;4"])
    def test_app_info_refuses_invalid_version(self, app_version: str) -> None:
        with pytest.raises(ValueError, match=r"app_info\.version"):
            AppInfo(name="acme", version=app_version)

    @pytest.mark.parametrize("url", ["", "https://acme.example/a b", "https://acme.example/(x)", "https://acme.example;x", "https://acme.example\n"])
    def test_app_info_refuses_invalid_url(self, url: str) -> None:
        with pytest.raises(ValueError, match=r"app_info\.url"):
            AppInfo(name="acme", url=url)

    @pytest.mark.parametrize("detail", ["", "two words", "a;b", "=value", "key=", "key=a b", "key=a/b/c", "(x)"])
    def test_app_info_refuses_invalid_detail(self, detail: str) -> None:
        with pytest.raises(ValueError, match=r"app_info\.details"):
            AppInfo(name="acme", details=["ok", detail])

    def test_app_info_refusal_is_a_validation_error_subclassing_value_error(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppInfo(name="bad name")
        assert isinstance(exc_info.value, ValueError)

    def test_app_info_refuses_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            AppInfo.model_validate({"name": "acme", "extra": "x"})

    def test_app_info_is_frozen(self) -> None:
        app_info = AppInfo(name="acme")
        with pytest.raises(ValidationError):
            app_info.name = "other"  # type: ignore[misc]

    # ── build_user_agent ─────────────────────────────────────────────

    def test_default_header_lists_sdk_mthds_then_runtime(self) -> None:
        expected_prefix = f"pipelex-sdk-python/{__version__} mthds-python/{version('mthds')} "
        user_agent = build_user_agent()
        assert user_agent.startswith(expected_prefix)
        assert re.fullmatch(re.escape(expected_prefix) + _RUNTIME_PATTERN, user_agent)

    def test_app_info_is_placed_first(self) -> None:
        user_agent = build_user_agent(AppInfo(name="acme-invoicer", version="1.4.0"))
        assert user_agent.startswith(f"acme-invoicer/1.4.0 pipelex-sdk-python/{__version__} mthds-python/")

    def test_runtime_token_reads_interpreter_and_platform(self, mocker: MockerFixture) -> None:
        mocker.patch("pipelex_sdk.user_agent.platform.system", return_value="Linux")
        mocker.patch("pipelex_sdk.user_agent.platform.machine", return_value="x86_64")
        mocker.patch("pipelex_sdk.user_agent.sys.version_info", (3, 12, 4, "final", 0))
        mocker.patch("pipelex_sdk.user_agent.version", return_value="0.15.0")
        mocker.patch("pipelex_sdk.user_agent.__version__", "0.11.0")
        user_agent = build_user_agent(AppInfo(name="acme-invoicer", version="1.4.0"))
        assert user_agent == "acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.15.0 python/3.12.4 (linux; x86_64)"

    def test_unreadable_platform_drops_the_comment(self, mocker: MockerFixture) -> None:
        mocker.patch("pipelex_sdk.user_agent.platform.system", return_value="")
        user_agent = build_user_agent()
        assert re.search(r" python/\d+\.\d+\.\d+$", user_agent)

    def test_missing_mthds_metadata_omits_the_mthds_token(self, mocker: MockerFixture) -> None:
        mocker.patch("pipelex_sdk.user_agent.version", side_effect=PackageNotFoundError("mthds"))
        user_agent = build_user_agent()
        assert "mthds-python" not in user_agent
        assert user_agent.startswith(f"pipelex-sdk-python/{__version__} python/")

    def test_over_long_header_is_refused(self) -> None:
        app_info = AppInfo(name="a" * MAX_USER_AGENT_LENGTH)
        with pytest.raises(ValueError, match="512-character ceiling"):
            build_user_agent(app_info)
