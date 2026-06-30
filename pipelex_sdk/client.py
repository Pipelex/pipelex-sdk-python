"""`PipelexAPIClient` — the Python client for the Pipelex hosted API.

Built by inheritance on `mthds`'s protocol base (`MthdsAPIClient`): the protocol
routes (`execute` / `start` / `validate` / `models` / `version`), the transport
(`_send`, `_url`), and the request-body builders are reused; this client adds the
Pipelex branding (env resolution, optional token, host-only base-URL validation),
the richer transport/error layer the product and lifecycle phases build on, and —
in later phases — the durable run lifecycle, the product surface, and `health`.

This module currently holds Phase 1: construction, the transport extension helpers
(`_request_product`, `_request_json`, `_send_or_unreachable`), and the `problem+json`
error-body parser. Lifecycle and product methods land in Phases 2-4.
"""

from __future__ import annotations

import json
import os
from typing import Any, NamedTuple, NoReturn, cast
from urllib.parse import urlparse

import httpx
from mthds.config.credentials import load_credentials
from mthds.protocol.exceptions import PipelineRequestError
from mthds.runners.api.client import MthdsAPIClient
from mthds.runners.api.models import ValidationErrorItem
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_json
from typing_extensions import override

from pipelex_sdk.errors import ApiResponseError, ApiUnreachableError

# The client composes every endpoint from one origin (PIPELEX_API_URL): `{base}/v1/{endpoint}`.
# The same paths are served by the Pipelex Hosted API (api.pipelex.com) and by a bare
# OSS pipelex-api runner (localhost:8081) — the protocol surface is identical; only the
# hosted extensions (e.g. run polling) differ, detectable via GET /v1/version.
_API_PREFIX = "v1"

#: Hosted default — the client composes every endpoint as `{base}/v1/{endpoint}`.
DEFAULT_API_BASE_URL = "https://api.pipelex.com"

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 1200.0  # 20 min — matches the runner's blocking-execute ceiling.
_POLL_REQUEST_TIMEOUT_SECONDS = 30.0  # single status/result/product GETs; the hosted gateway caps responses at ~30s.
_DEFAULT_DEGRADED_RETRY_SECONDS = 5  # matches the platform's `_DEGRADE_RETRY_AFTER_SECONDS`.

_PIPELEX_API_KEY_ENV = "PIPELEX_API_KEY"
_PIPELEX_API_URL_ENV = "PIPELEX_API_URL"


class PipelexAPIClient(MthdsAPIClient):
    """Client for the Pipelex hosted API — and any MTHDS-compliant runner.

    One base URL (`PIPELEX_API_URL`); every endpoint is `<base>/v1/<endpoint>`:
    - **protocol** (`execute` / `start` / `validate` / `models` / `version`) — inherited
      from `MthdsAPIClient`; works against any MTHDS-compliant runner, hosted or bare.
    - **run lifecycle** (`get_run_status` / `get_run_result` / `wait_for_result`) — the
      durable polling extension (added in Phase 2).
    - **product** (`/v1/me`, `/v1/methods`, `/v1/billing/*`, …) — the hosted product
      surface (added in Phase 3), reached through `_request_product` so callers branch
      on the structured `ApiResponseError.code`, not the HTTP status.

    Construction resolves credentials Pipelex-first (`PIPELEX_API_KEY` /
    `PIPELEX_API_URL`), falling back to the `mthds` resolver (`MTHDS_API_KEY` /
    `MTHDS_API_URL`, `~/.mthds/config`). The token is optional — anonymous access works
    against the protocol routes; product routes return `401`. The base URL is validated
    host-only (no path/query/fragment/credentials; http/https only).
    """

    def __init__(self, api_token: str | None = None, api_base_url: str | None = None) -> None:
        credentials = load_credentials()

        # Pipelex-primary, mthds fallback. `credentials` already layers env (MTHDS_*) >
        # file (~/.mthds/config) > default, so this `or` chain gives the full precedence:
        # explicit arg > PIPELEX_* env > MTHDS_* env > file > default. Empty string ("")
        # means anonymous — the token is optional.
        self.api_token: str = api_token or os.environ.get(_PIPELEX_API_KEY_ENV) or credentials["api_key"]

        resolved_base_url = api_base_url or os.environ.get(_PIPELEX_API_URL_ENV) or credentials["api_url"] or DEFAULT_API_BASE_URL
        normalized_base_url = resolved_base_url.rstrip("/")
        # The base URL must be host-only: a path-prefixed value (e.g. `.../v1`) would
        # compose as `/v1/v1/...` and fail with a misleading endpoint error instead of a
        # clear base-URL one. Trailing slashes are stripped first; any remaining
        # path/query/fragment/credentials is rejected.
        if not _is_valid_base_url(normalized_base_url):
            msg = (
                f'Invalid API base URL "{normalized_base_url}": must be host-only '
                "(http/https, no path, query, fragment, or credentials). "
                "Endpoints compose as {base}/v1/{endpoint}."
            )
            raise PipelineRequestError(msg)
        self.api_base_url: str = normalized_base_url
        #: Origin root derived from the base URL — `/health` lives here, not under `/v1`.
        self.origin_url: str = _origin_of(normalized_base_url)
        self.client: httpx.AsyncClient | None = None

    @override
    def start_client(self) -> PipelexAPIClient:
        """Initialize the HTTP client. The Authorization header is sent only when a token
        is configured — anonymous access (empty token) omits it, matching the JS SDK.
        """
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        self.client = httpx.AsyncClient(headers=headers)
        return self

    # ── Transport extensions (layered on the inherited `_send`) ──────────

    async def _send_or_unreachable(self, method: str, url: str, *, content: bytes | None, request_timeout: float) -> httpx.Response:
        """Issue one request via the inherited `_send`, mapping transport failures
        (DNS / connect / TLS / timeout) to `ApiUnreachableError`. Non-2xx interpretation
        stays the caller's — `_send` returns the raw response without raising on status.
        """
        try:
            return await self._send(method, url, content=content, request_timeout=request_timeout)
        except httpx.TimeoutException as exc:
            msg = f"Could not reach Pipelex API at {self.api_base_url} (timeout)"
            raise ApiUnreachableError(msg, api_url=self.api_base_url, code="ABORT_TIMEOUT") from exc
        except httpx.TransportError as exc:
            code = type(exc).__name__
            msg = f"Could not reach Pipelex API at {self.api_base_url} ({code})"
            raise ApiUnreachableError(msg, api_url=self.api_base_url, code=code) from exc

    async def _request_product(self, method: str, endpoint: str, *, body: object | None = None) -> Any:
        """Issue a Pipelex-product request (`/v1/me`, `/v1/methods`, `/v1/billing/*`, …)
        and parse its JSON body, mapping a non-2xx response to the typed `ApiResponseError`
        so callers branch on the structured `code` discriminant, not the HTTP status.

        Empty-body tolerant — DELETE / onboarding / update routes answer 2xx with no body,
        returned as `None`. Uses the management-call timeout, not the blocking ceiling.
        """
        content = to_json(body) if body is not None else None
        response = await self._send_or_unreachable(method, self._url(endpoint), content=content, request_timeout=_POLL_REQUEST_TIMEOUT_SECONDS)
        if not 200 <= response.status_code < 300:
            self._raise_api_response_error(method=method, endpoint=endpoint, response=response)
        if not response.content:
            return None
        return response.json()

    async def _request_json(self, method: str, url: str, *, body: object | None = None) -> Any:
        """Issue a request to an absolute URL and parse the JSON body, raising the plainer
        `PipelineRequestError` on a non-2xx response. Used by `health` (origin-level) and
        the build extensions — surfaces that don't need the product `code` taxonomy.
        Transport failures still map to `ApiUnreachableError`.
        """
        content = to_json(body) if body is not None else None
        response = await self._send_or_unreachable(method, url, content=content, request_timeout=_POLL_REQUEST_TIMEOUT_SECONDS)
        if not 200 <= response.status_code < 300:
            detail = response.text or response.reason_phrase
            msg = f"API {method} {url} failed ({response.status_code}): {detail}"
            raise PipelineRequestError(msg)
        return response.json()

    def _raise_api_response_error(self, *, method: str, endpoint: str, response: httpx.Response) -> NoReturn:
        """Parse an error response and raise the typed `ApiResponseError`."""
        body_text = response.text
        parsed = _parse_error_body(body_text)
        detail = parsed.server_message or body_text or response.reason_phrase
        msg = f"API {method} /{_API_PREFIX}/{endpoint} failed ({response.status_code}): {detail}"
        raise ApiResponseError(
            msg,
            api_url=self.api_base_url,
            status=response.status_code,
            status_text=response.reason_phrase,
            response_body=body_text,
            error_type=parsed.error_type,
            server_message=parsed.server_message,
            validation_errors=parsed.validation_errors,
            code=parsed.code,
        )


# ── Module helpers ──────────────────────────────────────────────────────


def _is_valid_base_url(value: str) -> bool:
    """Whether a base URL is host-only — http/https, no path, query, fragment, or
    embedded credentials (auth travels in the Authorization header, never the URL).
    Endpoints compose as `{base}/v1/{endpoint}`, so a path-prefixed base would double
    the prefix.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    if parsed.path not in {"", "/"}:
        return False
    if parsed.username or parsed.password:
        return False
    return not parsed.query and not parsed.fragment


def _origin_of(base_url: str) -> str:
    """Derive the origin (`scheme://host[:port]`) from a validated host-only base URL.

    `/health` is served at the origin, not under the `/v1` prefix.
    """
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


class _ParsedErrorBody(NamedTuple):
    """The fields pulled out of a `problem+json` / `HTTPException` error body."""

    error_type: str | None
    server_message: str | None
    validation_errors: list[ValidationErrorItem] | None
    code: str | None


_EMPTY_ERROR_BODY = _ParsedErrorBody(error_type=None, server_message=None, validation_errors=None, code=None)

# The build routes' 422s carry a top-level `validation_errors[]`. Validated leniently
# (best-effort error-path enrichment) so an odd shape never masks the underlying failure.
_VALIDATION_ERRORS_ADAPTER: TypeAdapter[list[ValidationErrorItem]] = TypeAdapter(list[ValidationErrorItem])


def _parse_error_body(body: str) -> _ParsedErrorBody:
    """Extract `error_type` / `message` / `validation_errors` / `code` from an error body.

    The API serializes errors as `{"detail": {"error_type": ..., "message": ...}}`
    (HTTPException with dict detail) or `{"detail": "..."}` (auth 401s and RFC 7807
    problems); both shapes are handled, with top-level `error_type` / `message`
    fallbacks. The product routes' RFC 9457 `problem+json` adds a stable top-level
    `code` discriminant. Falls through to empty on a non-JSON or non-object body.
    """
    if not body:
        return _EMPTY_ERROR_BODY
    try:
        parsed = json.loads(body)
    except ValueError:
        return _EMPTY_ERROR_BODY
    if not isinstance(parsed, dict):
        return _EMPTY_ERROR_BODY
    root = cast("dict[str, Any]", parsed)

    error_type: str | None = None
    server_message: str | None = None
    detail = root.get("detail")
    if isinstance(detail, dict):
        detail_dict = cast("dict[str, Any]", detail)
        raw_error_type = detail_dict.get("error_type")
        if isinstance(raw_error_type, str):
            error_type = raw_error_type
        raw_message = detail_dict.get("message")
        if isinstance(raw_message, str):
            server_message = raw_message
    elif isinstance(detail, str):
        server_message = detail
    if error_type is None:
        top_error_type = root.get("error_type")
        if isinstance(top_error_type, str):
            error_type = top_error_type
    if server_message is None:
        top_message = root.get("message")
        if isinstance(top_message, str):
            server_message = top_message

    validation_errors: list[ValidationErrorItem] | None = None
    raw_validation_errors = root.get("validation_errors")
    if isinstance(raw_validation_errors, list):
        try:
            validation_errors = _VALIDATION_ERRORS_ADAPTER.validate_python(raw_validation_errors)
        except ValidationError:
            # Best-effort error-path enrichment: an odd validation_errors shape (only
            # reachable via the out-of-scope /v1/build/* 422s) must not mask the
            # underlying API failure — server_message still carries the problem.
            validation_errors = None

    code: str | None = None
    raw_code = root.get("code")
    if isinstance(raw_code, str):
        code = raw_code

    return _ParsedErrorBody(error_type=error_type, server_message=server_message, validation_errors=validation_errors, code=code)
