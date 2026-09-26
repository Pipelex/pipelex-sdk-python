"""Pipelex SDK errors — the transport and response errors raised by `PipelexAPIClient`.

These are the error classes the Pipelex hosted client adds on top of the `mthds`
protocol base. They derive from the protocol base `PipelineRequestError`
(`mthds.protocol.exceptions`), mirroring `pipelex-sdk-js/src/errors.ts`:

- `ApiUnreachableError` — the HTTP exchange never produced a response (DNS / connect
  / TLS / timeout). Distinguished from `ApiResponseError`, which represents a non-2xx
  response that *did* come back.
- `ApiResponseError` — a non-2xx response from the API, carrying the members of its
  RFC 9457 problem document: the branch fields `error_domain` and `type_uri` (the
  problem's `type`), the surface-native `code` / `error_type`, the request id, and the
  rest (decoupled from the HTTP status).
- `PipelineExecuteTimeoutError` — a blocking `execute()` killed by the hosted gateway's
  ~30s synchronous-request ceiling; points the caller at the durable start+poll path.
- `PagingNotTerminatingError` — a paged-list iterator hit its runaway backstop, meaning
  the server never stopped handing out cursors.

The run-lifecycle errors (`RunFailedError`, `RunTimeoutError`,
`RunLifecycleUnavailableError`) are owned here (ported from `mthds-python` in
HANDOFF Phase 2, and removed from `mthds-python` in Phase 6). `RunStillRunningError`
stays in `mthds` — it belongs to the protocol `execute()` 202-degrade path, not the
lifecycle — and is re-exported here so consumers have a single import home.

The artifact errors (`ArtifactOperationError` and its subclasses, plus `FieldNotIncludedError`)
are the download twin of the input-preparation family: they are raised only where the artifact
operations can produce no verdict at all, per-reference failure being a value on the verdict's
item. See `docs/artifact-download.md`.

The codegen tree errors (`CodegenError`, `CodegenLockError`) are not request errors at all: they are
raised by `pipelex_sdk.codegen_writer`, `pipelex_sdk.codegen_check`, `pipelex_sdk.codegen_lock` and
`pipelex_sdk.codegen_stamp` over
bytes and a directory, so they derive from `Exception` rather than from the protocol base.

`FieldNotIncludedError` is raised by `pipelex_sdk.usage` over an already-validated `RunResults`
whose body did not carry a key the operation needs. Like `MissingMainStuffError`, it reports a
results read that did not deliver what the caller reads, so it stays under the protocol base.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mthds.protocol.exceptions import PipelineRequestError

# Explicit re-export (PEP 484 `as` self-alias): the protocol 202-degrade error stays owned by
# `mthds`, surfaced here so consumers have a single import home for the run/lifecycle errors.
from mthds.runners.api.exceptions import RunStillRunningError as RunStillRunningError  # ruff: ignore[useless-import-alias]

if TYPE_CHECKING:
    from typing import Any

    from pipelex_sdk.artifact_models import ArtifactScope, DownloadArtifactsResult
    from pipelex_sdk.error_models import FieldError, RunErrorReport, UserAction
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
    """A non-2xx response that DID come back from the API, with its problem document parsed.

    Every error the hosted API answers is an RFC 9457 `application/problem+json` document, and this
    error carries its members as typed attributes, each `None` when the document did not carry it:

    - **The branch fields.** `error_domain` is the coarse class a consumer branches on — `input` (the
      caller can fix it), `config` (a configuration change is needed), `runtime` (a failure during
      execution) — and `type_uri` (the problem's `type`) is the stable URI naming the error class.
      `retryable` says whether a blind retry can succeed, `None` meaning unknown. Branch on these,
      never on the HTTP status or on the wording of a message.
    - **The native codes.** `code` is the platform's own closed code (`conflict`, `not_found`,
      `pipelex_api_key_limit_reached`, …) and `error_type` the runner's open exception class name.
      Each is finer than `error_domain` and specific to the surface that emits it.
    - **For a person.** `title` is the stable label of the error class, `server_message` the
      per-occurrence `detail`, `user_action` the advised next step, and `error_category` a finer
      classification of an inference failure.
    - **For support.** `request_id` correlates the response with the server's logs; it is read from
      the body, or from the `X-Request-ID` response header when the body has none.
    - **Per-item failures.** `errors` is the platform's field-level list (`field`, `code`, `detail`),
      and `validation_errors` the structured diagnostics of a bundle that failed validation.

    `problem` is the decoded document whole, so a member this SDK does not name — `instance`, or the
    `run_status` and `error` of a failed run's results read — stays reachable; `response_body` is the
    raw text, and `status` / `status_text` the transport's. `problem` is `None` when the body was not
    a JSON object.
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
        request_id: str | None = None,
        type_uri: str | None = None,
        title: str | None = None,
        error_domain: str | None = None,
        error_category: str | None = None,
        retryable: bool | None = None,
        user_action: UserAction | None = None,
        errors: list[FieldError] | None = None,
        problem: dict[str, Any] | None = None,
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
        self.request_id = request_id
        self.type_uri = type_uri
        self.title = title
        self.error_domain = error_domain
        self.error_category = error_category
        self.retryable = retryable
        self.user_action = user_action
        self.errors = errors
        self.problem = problem


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

    Surfaced by `wait_for_result`, `start_and_wait` and `download_artifacts` when the platform
    answers the results read with HTTP 409 (`FAILED`, `CANCELLED`, `TERMINATED`, `TIMED_OUT`).

    - `status` is the run's terminal status, the typed `RunStatus` enum, read from the problem's
      `run_status` member — so callers can match/case on it.
    - `error` is the run's stored error report, typed whole as `RunErrorReport`: the runner's
      `error_type`, `message`, `title`, `type_uri`, `error_domain`, `error_category`, `retryable`,
      `user_action`, `model`, `provider`, `provider_metadata`, `validation_errors` and anything newer
      on `model_extra`. Branch on `error.error_domain`, `error.type_uri` and `error.retryable`; show
      `error.user_action` as the next step. It is the runner's VERBOSE report, so `message` and
      `provider_metadata` can hold a provider's raw text — deciding what a person sees is yours.
      `None` when the run ended with no stored report (a cancelled, terminated or timed-out run, or
      one the platform finalized itself).
    - The exception's own message is the problem's `detail`, which names the status and then the
      report's message (`Run finished with status FAILED: <message>`), so printing the error already
      tells the reason.
    - `run_id` locates the run, for a status read or a support request.
    """

    def __init__(self, message: str, run_id: str, status: RunStatus, error: RunErrorReport | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.status = status
        self.error = error


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


class CodegenError(Exception):
    """A codegen tree this SDK refuses to write or read.

    Raised before the first byte is written when a `/v1/codegen` response, or the directory it is
    headed for, is unsafe: a `lock_filename` other than `codegen.lock`, an artifact path that could
    leave the output root or name a file type codegen never emits (absolute or drive-prefixed, a `..`
    or empty component, a backslash, a control character, an unstampable suffix, a duplicate), a lock
    that cannot be read or does not track exactly the artifacts, a symbolic link or a regular file on
    the way to a destination, a symbolic link on the way to a previously tracked path about to be pruned,
    a destination or such a path that is not a regular file, or a file already at an artifact's path that
    codegen does not own.
    """


class CodegenLockError(CodegenError):
    """A `codegen.lock` that cannot be read: malformed TOML, a shape the format does not define,
    bytes that are not UTF-8, or a `lock_version` this SDK does not know.

    It is also the offline check's one no-verdict class, raised where that check can reach no verdict at
    all rather than find a drift — including a file or directory under the output root the process cannot
    read, so a CI caller has a single thing to catch.

    An unsafe artifact path inside an otherwise well-formed lock is deliberately NOT this error but a
    plain `CodegenError`: it is a containment violation, not corrupt state a writer may recover from
    by replacing the lock.
    """


class ArtifactOperationError(PipelineRequestError):
    """Base class for the failures the artifact operations raise on their own
    (`fetch_artifact` / `download_artifacts`) — the download twin of `InputPreparationError`.

    Catch this to handle any artifact failure; catch a subclass to branch on the category. A
    per-reference failure inside a `download_artifacts` verdict is a **value on the item**, never
    one of these: the operation throws only when it can produce no verdict at all. The transport
    failures of the resolve route (`ApiResponseError`, `ApiUnreachableError`) and the run-lifecycle
    errors propagate unchanged, so they are not subclasses. Mirrors `pipelex-sdk-js`'s
    `ArtifactOperationError` family.
    """


class ScopeUnavailableError(ArtifactOperationError):
    """The scope `download_artifacts` was asked to walk is `None` on the run's results.

    The key WAS relayed — it is in `results.model_fields_set` — and its value is `None`, which is
    the platform saying it has no such artifact for this run. Distinct from a key the results read
    never carried, which is `FieldNotIncludedError`, and from an empty walk over a present scope,
    which is a produced verdict with no artifacts. `scope` names the scope, `run_id` the run.
    """

    def __init__(self, scope: ArtifactScope, run_id: str) -> None:
        msg = f'Run "{run_id}" carries no "{scope}" artifact to walk for produced files — the results relayed it as null.'
        super().__init__(msg)
        self.scope = scope
        self.run_id = run_id


class ArtifactFetchError(ArtifactOperationError):
    """One reference could not be turned into a bounded stream by `fetch_artifact`.

    `code` says why, in a closed vocabulary the download verdict shares for its per-item errors:
    the resolve route's own per-reference codes (`invalid_storage_uri`, `forbidden`), then the
    fetch boundary's — `unsupported_url`, `plain_http_refused`, `redirect_refused`, `store_refused`
    (a 401/403 from the object store), `not_found` (404/410), `store_error` (any other non-2xx),
    `too_large`, `timeout`, `network`. `status` is the store's HTTP status when one was received.
    `download_artifacts` never lets this escape: it becomes the item's `error`.
    """

    def __init__(self, message: str, uri: str, code: str, status: int | None = None) -> None:
        super().__init__(message)
        self.uri = uri
        self.code = code
        self.status = status


class ArtifactAuthenticationError(ArtifactOperationError):
    """The resolve route refused the caller's credential (`401` / `403`) during a download.

    No further reference can be resolved with it, so the download stops — but the files already
    saved are real, and `verdict` carries the result as it stood: every item saved before the
    refusal, and the rest marked `aborted`. `status` is the route's status; the wrapped
    `ApiResponseError` is reachable through `__cause__`.
    """

    def __init__(self, message: str, status: int, verdict: DownloadArtifactsResult) -> None:
        super().__init__(message)
        self.status = status
        self.verdict = verdict


class FieldNotIncludedError(PipelineRequestError):
    """A `RunResults` field this operation needs was not carried by the results body it was read from.

    Raised when the field is absent from `results.model_fields_set` — the body did not carry the key —
    as opposed to relayed as `None`, which is a value. Carries the field's name in `field_name`.
    """

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name
        msg = f"RunResults field `{field_name}` was not in the results body: the read did not carry it"
        super().__init__(msg)
