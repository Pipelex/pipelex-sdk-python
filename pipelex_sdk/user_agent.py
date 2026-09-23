"""The `User-Agent` this SDK sends on every request to the Pipelex API.

The header follows the workspace spec `docs/specs/client-identification.md`: product tokens,
outermost first — the integrator's `app_info`, then this SDK, then the `mthds` library whose
transport it inherits, then the Python runtime and its `(<os>; <arch>)` comment:

    acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.15.0 python/3.12.4 (linux; x86_64)

The header is self-declared and unauthenticated: the platform reads it for analytics only.

The builder lives here rather than in `mthds` because the pinned `mthds` does not ship one yet;
the public shape (`AppInfo`, `ValueError` on an invalid token) matches the one `mthds` will expose.
"""

from __future__ import annotations

import platform
import re
import sys
from importlib.metadata import PackageNotFoundError, version

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pipelex_sdk.version import __version__

#: This SDK's product-token name in the spec's closed registry (the repo name, not the PyPI name).
SDK_TOKEN_NAME = "pipelex-sdk-python"
#: The token name of the `mthds` library on this SDK's transport path.
MTHDS_TOKEN_NAME = "mthds-python"
#: The PyPI distribution whose installed version the `mthds-python` token carries.
MTHDS_DISTRIBUTION_NAME = "mthds"
#: The spec's ceiling on the whole header.
MAX_USER_AGENT_LENGTH = 512

# RFC 9110 `token` = 1*tchar; tchar = "!" / "#" / "$" / "%" / "&" / "'" / "*" / "+" / "-" / "." / "^" / "_" / "`" / "|" / "~" / DIGIT / ALPHA
_TOKEN_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# A comment parameter value is a `token` or a `name/version` product.
_PARAM_VALUE_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+(/[!#$%&'*+\-.^_`|~0-9A-Za-z]+)?$")
# A URL rendered as `+url` inside a comment: no whitespace or control character, and none of the
# characters that would close the comment or start a new parameter.
_URL_PATTERN = re.compile(r"^[^\s()\\;\x00-\x1f\x7f]+$")


def is_token(value: str) -> bool:
    """Whether `value` is a non-empty RFC 9110 `token` (only `tchar` characters)."""
    return _TOKEN_PATTERN.fullmatch(value) is not None


def _is_comment_param(value: str) -> bool:
    """Whether `value` is a comment parameter: a `token`, or `token=value` with a token or `name/version` value."""
    name, separator, param_value = value.partition("=")
    if not separator:
        return is_token(value)
    return is_token(name) and _PARAM_VALUE_PATTERN.fullmatch(param_value) is not None


class AppInfo(BaseModel):
    """The integrator's own name, placed before this SDK's tokens in the `User-Agent` (Stripe's `appInfo`).

    It renders as `name/version (<details>; +url)`, dropping the `/version` and the comment when
    they are empty. An invalid field raises `ValueError` (pydantic's `ValidationError`, a
    `ValueError` subclass) at construction — it is never silently dropped or rewritten.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    version: str | None = None
    url: str | None = None
    details: list[str] = Field(default_factory=list[str])

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not is_token(value):
            msg = f"app_info.name {value!r} must be an RFC 9110 token (letters, digits and !#$%&'*+-.^_`|~ only)"
            raise ValueError(msg)
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str | None) -> str | None:
        if value is not None and not is_token(value):
            msg = f"app_info.version {value!r} must be an RFC 9110 token (letters, digits and !#$%&'*+-.^_`|~ only)"
            raise ValueError(msg)
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is not None and _URL_PATTERN.fullmatch(value) is None:
            msg = f"app_info.url {value!r} must be a non-empty URL without whitespace, parentheses, backslashes or semicolons"
            raise ValueError(msg)
        return value

    @field_validator("details")
    @classmethod
    def _validate_details(cls, value: list[str]) -> list[str]:
        for detail in value:
            if not _is_comment_param(detail):
                msg = f"app_info.details entry {detail!r} must be a token or token=value, the value a token or name/version"
                raise ValueError(msg)
        return value

    def render(self) -> str:
        """The product token (and optional comment) this app info contributes to the header."""
        product = f"{self.name}/{self.version}" if self.version is not None else self.name
        params = list(self.details)
        if self.url is not None:
            params.append(f"+{self.url}")
        if not params:
            return product
        return f"{product} ({'; '.join(params)})"


def _mthds_version() -> str | None:
    """The installed `mthds` distribution's version, or `None` when its metadata is unavailable."""
    try:
        return version(MTHDS_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return None


def _runtime_token() -> str:
    """`python/<major.minor.micro> (<os>; <arch>)`, dropping the comment when the platform is not readable as tokens."""
    major, minor, micro = sys.version_info[:3]
    runtime = f"python/{major}.{minor}.{micro}"
    os_name = platform.system().lower()
    arch = platform.machine()
    if is_token(os_name) and is_token(arch):
        return f"{runtime} ({os_name}; {arch})"
    return runtime


def build_user_agent(app_info: AppInfo | None = None) -> str:
    """Build the spec's `User-Agent`: `[app_info] pipelex-sdk-python/<v> [mthds-python/<v>] python/<x.y.z> (<os>; <arch>)`.

    Raises:
        ValueError: when the rendered header exceeds the spec's 512-character ceiling.
    """
    parts: list[str] = []
    if app_info is not None:
        parts.append(app_info.render())
    parts.append(f"{SDK_TOKEN_NAME}/{__version__}")
    mthds_version = _mthds_version()
    if mthds_version is not None:
        parts.append(f"{MTHDS_TOKEN_NAME}/{mthds_version}")
    parts.append(_runtime_token())
    user_agent = " ".join(parts)
    if len(user_agent) > MAX_USER_AGENT_LENGTH:
        msg = f"The User-Agent would be {len(user_agent)} characters, over the {MAX_USER_AGENT_LENGTH}-character ceiling; shorten app_info"
        raise ValueError(msg)
    return user_agent
