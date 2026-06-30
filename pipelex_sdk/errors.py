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
`RunLifecycleUnavailableError`) are owned here (ported from `mthds-python` in
HANDOFF Phase 2, and removed from `mthds-python` in Phase 6). `RunStillRunningError`
stays in `mthds` — it belongs to the protocol `execute()` 202-degrade path, not the
lifecycle — and is re-exported here so consumers have a single import home.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mthds.protocol.exceptions import PipelineRequestError

# Explicit re-export (PEP 484 `as` self-alias): the protocol 202-degrade error stays owned by
# `mthds`, surfaced here so consumers have a single import home for the run/lifecycle errors.
from mthds.runners.api.exceptions import RunStillRunningError as RunStillRunningError  # noqa: PLC0414

if TYPE_CHECKING:
    from mthds.runners.api.models import ValidationErrorItem

    from pipelex_sdk.runs import RunStatus


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


class RunFailedError(PipelineRequestError):
    """Raised when a run reaches a terminal state that is not `COMPLETED`.

    Surfaced from `wait_for_result` / `get_run_result` when the platform answers a
    result lookup with HTTP 409 (`FAILED`, `CANCELLED`, `TERMINATED`,
    `TIMED_OUT`). `run_id` and `status` let callers report the outcome precisely;
    `status` stays the typed `RunStatus` enum so callers can match/case on it.
    """

    def __init__(self, message: str, run_id: str, status: RunStatus) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.status = status


class RunTimeoutError(PipelineRequestError):
    """Raised when `wait_for_result` exceeds its timeout before the run is terminal.

    The run is NOT cancelled — it keeps executing server-side and can be resumed
    later by `run_id` (the poll loop just stopped waiting).
    """

    def __init__(self, message: str, run_id: str, timeout_seconds: float) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds


class RunLifecycleUnavailableError(PipelineRequestError):
    """Raised when the durable run lifecycle (`/v1/runs/*`) is not served by the
    configured `PIPELEX_API_URL`.

    Run polling is a hosted-API extension, not part of the MTHDS Protocol: the
    open-source `pipelex-api` runner executes methods but has no run store, so it
    404s those routes; only a deployment that includes the platform block (the
    Pipelex Hosted API) serves status/results. Distinguished from a genuine
    run-not-found 404, which carries the platform's structured error envelope.
    """

    def __init__(self, message: str, api_url: str) -> None:
        super().__init__(message)
        self.api_url = api_url
