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
- `PipelineExecuteTimeoutError` — a blocking `execute()` killed by the hosted gateway's
  ~30s synchronous-request ceiling; points the caller at the durable start+poll path.
- `PagingNotTerminatingError` — a paged-list iterator hit its runaway backstop, meaning
  the server never stopped handing out cursors.

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
from mthds.runners.api.exceptions import RunStillRunningError as RunStillRunningError  # ruff: ignore[useless-import-alias]

if TYPE_CHECKING:
    from pipelex_sdk.runs import RunStatus
    from pipelex_sdk.validation_models import ValidationErrorItem


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


class PipelineExecuteTimeoutError(PipelineRequestError):
    """Raised when a blocking `execute()` (`POST /v1/execute`) is killed by the hosted
    gateway's ~30s synchronous-request ceiling.

    The blocking path cannot run methods longer than ~30s behind the hosted gateway — use
    the durable run lifecycle (`start` + `wait_for_result`, or `start_and_wait`) instead,
    which survives long runs and client disconnects. `elapsed_seconds` is how long the
    request ran before the gateway cut it off.
    """

    def __init__(self, message: str, elapsed_seconds: float) -> None:
        super().__init__(message)
        self.elapsed_seconds = elapsed_seconds


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


class MissingMainStuffError(PipelineRequestError):
    """Raised when a completed run cannot deliver its main stuff.

    Every completed run delivers a main stuff (the pipelex >= 0.37 wire invariant), so the SDK
    hands consumers a non-null `RunResults.main_stuff`. This surfaces the contract violation when it
    cannot: the hosted results endpoint answered a `200` with a null `main_stuff`, or a blocking
    `execute` response named a `main_stuff_name` whose stuff is absent from the returned working
    memory. `run_id` locates the run. (A falsy-but-present main stuff — an empty list, `0` — is a
    valid output and does NOT raise; only a genuinely absent one does.)
    """

    def __init__(self, message: str, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class RunLifecycleUnavailableError(PipelineRequestError):
    """Raised when the durable run lifecycle (`/v1/runs/*`) is not served by the
    configured `PIPELEX_BASE_URL`.

    Run polling is a hosted-API extension, not part of the MTHDS Protocol: the
    open-source `pipelex-api` runner executes methods but has no run store, so it
    404s those routes; only a deployment that includes the platform block (the
    Pipelex Hosted API) serves status/results. Distinguished from a genuine
    run-not-found 404, which carries the platform's structured error envelope.
    """

    def __init__(self, message: str, api_url: str) -> None:
        super().__init__(message)
        self.api_url = api_url


class PagingNotTerminatingError(PipelineRequestError):
    """Raised when a paged-list iterator refuses to keep following cursors.

    The ceiling sits far beyond any real catalog, so reaching it is a server-side fault —
    an endpoint minting a fresh cursor forever — not a coverage limit the caller can raise.
    Raising beats returning, because a silently truncated list is exactly the bug paging
    was introduced to remove.
    """

    def __init__(self, message: str, page_limit: int) -> None:
        super().__init__(message)
        self.page_limit = page_limit


class InputPreparationError(PipelineRequestError):
    """Base class for every failure raised by input preparation (`upload_file` /
    `prepare_inputs`).

    Catch this to handle any preparation failure; catch a subclass to branch on the
    semantic category. All preparation failures are raised BEFORE any run is created —
    a run never triggers a hidden upload. Mirrors `pipelex-sdk-js`'s
    `InputPreparationError` family.
    """


class InvalidLocalSourceError(InputPreparationError):
    """A local asset could not be turned into bytes — a missing or unreadable path.
    `source` is the offending path.
    """

    def __init__(self, message: str, source: str) -> None:
        super().__init__(message)
        self.source = source


class RejectedAssetError(InputPreparationError):
    """The server refused the asset — most commonly a `413` past the service-defined
    size cap. The SDK imposes no client-side cap; it surfaces the server's rejection.
    `filename` and `status` locate it.
    """

    def __init__(self, message: str, filename: str, status: int) -> None:
        super().__init__(message)
        self.filename = filename
        self.status = status


class UnsupportedUploadCapabilityError(InputPreparationError):
    """The configured deployment does not support upload (no `/v1/upload` route, seen
    as a `404`). Upload is a hosted Pipelex-product capability even though the SDK can
    be pointed at other base URLs.
    """


class UploadAuthenticationError(InputPreparationError):
    """Upload was not authorized — a `401`/`403` from the upload route."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class UploadTransportError(InputPreparationError):
    """A network or server fault reaching the upload route (unreachable host, `5xx`)."""
