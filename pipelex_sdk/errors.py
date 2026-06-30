"""Pipelex SDK errors — the transport and response errors raised by `PipelexAPIClient`.

These are the error classes the Pipelex hosted client adds on top of the `mthds`
protocol base. Both derive from the protocol base `PipelineRequestError`
(`mthds.protocol.exceptions`), mirroring `pipelex-sdk-js/src/errors.ts`:

- `ApiUnreachableError` — the HTTP exchange never produced a response (DNS / connect
  / TLS / timeout). Distinguished from `ApiResponseError`, which represents a non-2xx
  response that *did* come back.
- `ApiResponseError` — a non-2xx response from the API, carrying the parsed
  problem-details and, for the product routes, the stable RFC 9457 `code` discriminant
  a consumer branches on (decoupled from the HTTP status).

The run-lifecycle errors (`RunFailedError`, `RunTimeoutError`,
`RunLifecycleUnavailableError`) are added here in Phase 2; `RunStillRunningError`
stays in `mthds` (it belongs to the protocol `execute()` 202-degrade path) and is
imported by consumers from there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mthds.protocol.exceptions import PipelineRequestError

if TYPE_CHECKING:
    from mthds.runners.api.models import ValidationErrorItem


class ApiUnreachableError(PipelineRequestError):
    """Raised when the Pipelex API host cannot be reached at all.

    DNS failure, connection refused, TLS handshake failure, or a request timeout —
    the HTTP exchange never produced a response. Distinguish from `ApiResponseError`,
    which represents a non-2xx response that did come back.

    `code` is the underlying transport-failure class when available (`ABORT_TIMEOUT`
    for a timeout, otherwise the httpx transport exception class name).
    """

    def __init__(self, message: str, api_url: str, code: str | None = None) -> None:
        super().__init__(message)
        self.api_url = api_url
        self.code = code


class ApiResponseError(PipelineRequestError):
    """A non-2xx response that DID come back from the API.

    Carries the parsed RFC 7807 problem-details (`error_type`, `server_message`) and,
    for the build routes' 422s, the structured `validation_errors` list.

    `code` is the product routes' stable RFC 9457 `problem+json` discriminant
    (`conflict`, `not_found`, `pipelex_api_key_limit_reached`, …) — the field a
    consumer branches on, decoupled from the HTTP status. `None` for any error body
    that carries no `code` (the protocol/build routes' `detail`-shaped problems).
    """

    def __init__(
        self,
        message: str,
        *,
        api_url: str,
        status: int,
        status_text: str,
        response_body: str,
        error_type: str | None = None,
        server_message: str | None = None,
        validation_errors: list[ValidationErrorItem] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.api_url = api_url
        self.status = status
        self.status_text = status_text
        self.response_body = response_body
        self.error_type = error_type
        self.server_message = server_message
        self.validation_errors = validation_errors
        self.code = code
