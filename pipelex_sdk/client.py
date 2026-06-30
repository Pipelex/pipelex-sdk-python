"""`PipelexAPIClient` — the Python client for the Pipelex hosted API.

Built by inheritance on `mthds`'s protocol base (`MthdsAPIClient`): the protocol
routes (`models` / `version` reused as-is; `execute` / `start` / `validate` overridden),
the transport (`_send`, `_url`), and the request-body builders are reused; this client
adds the Pipelex branding (env resolution, optional token, host-only base-URL
validation), the richer transport/error layer, the durable run lifecycle, the product
surface, and `health`.

This module holds construction, the transport extension helpers (`_request_product`,
`_request_json`, `_send_or_unreachable`), the `problem+json` error-body parser, the
`execute` override (hosted gateway-timeout translation), the durable run lifecycle, the
`validate` override (markdown-render injection + `validate_files`), the Pipelex product
surface (methods, organizations, billing, API keys, onboarding, storage, run records),
and the origin-level `health` probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from time import monotonic
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, cast
from urllib.parse import quote, urlparse

import httpx
from mthds.config.credentials import load_credentials
from mthds.protocol.exceptions import PipelineRequestError
from mthds.runners.api.client import MthdsAPIClient
from mthds.runners.api.models import ValidationErrorItem
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_json
from typing_extensions import override

from pipelex_sdk.errors import (
    ApiResponseError,
    ApiUnreachableError,
    PipelineExecuteTimeoutError,
    RunFailedError,
    RunLifecycleUnavailableError,
    RunTimeoutError,
)
from pipelex_sdk.product_models import (
    BillingPortalResponse,
    ChangePlanResponse,
    CheckoutResponse,
    GatewayApiKey,
    GatewayApiKeyStatus,
    InvoiceView,
    Membership,
    MembershipsResponse,
    MethodData,
    PipelexApiKeyCreated,
    PipelexApiKeyList,
    PipelineRun,
    PlanView,
    ResolvedStorageUrl,
    SubscriptionResponse,
    UploadedFile,
    UserProfile,
)
from pipelex_sdk.runs import (
    PollInfo,
    RunRead,
    RunResultCompleted,
    RunResultFailed,
    RunResultRunning,
    RunResults,
    RunStatus,
    WaitForResultOptions,
)

if TYPE_CHECKING:
    from mthds.protocol.models import RunResultStart
    from mthds.protocol.pipe_output import VariableMultiplicity
    from mthds.protocol.pipeline_inputs import PipelineInputs
    from mthds.protocol.stuff import StuffType
    from mthds.protocol.working_memory import WorkingMemoryAbstract
    from mthds.runners.api.models import DictRunResultExecute, PipelexValidationResult

    from pipelex_sdk.product_models import (
        MethodWriteInput,
        OnboardingSubmission,
        UpdateRunInput,
        UploadInput,
    )
    from pipelex_sdk.runs import RunResultState

# The client composes every endpoint from one origin (PIPELEX_API_URL): `{base}/v1/{endpoint}`.
# The same paths are served by the Pipelex Hosted API (api.pipelex.com) and by a bare
# OSS pipelex-api runner (localhost:8081) — the protocol surface is identical; only the
# hosted extensions (e.g. run polling) differ, detectable via GET /v1/version.
_API_PREFIX = "v1"
_RUNS = "runs"

#: Hosted default — the client composes every endpoint as `{base}/v1/{endpoint}`.
DEFAULT_API_BASE_URL = "https://api.pipelex.com"

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 1200.0  # 20 min — matches the runner's blocking-execute ceiling.
_POLL_REQUEST_TIMEOUT_SECONDS = 30.0  # single status/result/product GETs; the hosted gateway caps responses at ~30s.
_DEFAULT_DEGRADED_RETRY_SECONDS = 5  # matches the platform's `_DEGRADE_RETRY_AFTER_SECONDS`.

# The hosted gateway caps synchronous requests at ~30s. A blocking-`execute` failure at/after
# this elapsed threshold is the gateway cut-off, not a transient outage — the threshold guards
# against mislabeling a fast 503 (runner genuinely down) as a timeout.
_GATEWAY_TIMEOUT_THRESHOLD_SECONDS = 28.0

_PIPELEX_API_KEY_ENV = "PIPELEX_API_KEY"
_PIPELEX_API_URL_ENV = "PIPELEX_API_URL"

# `VersionInfo.implementation` of the bare open-source runner (no run store). Anything
# else — the hosted implementation first — is assumed to serve the durable run-lifecycle
# extension; a wrong guess still fails with a clear `RunLifecycleUnavailableError` on the
# first poll (or self-heals through `start_and_wait`'s blocking-execute fallback).
_BARE_RUNNER_IMPLEMENTATION = "pipelex-api"

# `validate` always asks the Pipelex API for the Markdown view so both a valid result and a
# produced validation-error verdict carry `rendered_markdown`; callers may add more tokens.
_VALIDATE_MARKDOWN_RENDER_FORMAT = "markdown"


class MthdsFile(BaseModel):
    """One MTHDS file submitted to `validate_files` — content plus an optional provenance URI.

    The URI is threaded into validation diagnostics so cross-file errors name the owning
    file; an absent URI yields `source: null` for that content (unless any sibling file
    carries one, in which case a deterministic `inline://` label is synthesized).
    """

    #: File contents to validate.
    content: str
    #: Optional provenance URI threaded into validation diagnostics.
    uri: str | None = None


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
        #: Cached `/v1/version` handshake outcome — whether the durable lifecycle is served.
        self._lifecycle_available: bool | None = None

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

    def _raise_if_lifecycle_unavailable(self, response: httpx.Response, url: str) -> None:
        """Translate a "route absent" 404 (a bare pipelex-api with no platform block) into a clear
        `RunLifecycleUnavailableError`. The platform's own 404s (run not found / cross-org) carry a
        structured problem+json envelope (a `code` field) and are left for normal handling.
        """
        if response.status_code != 404:
            return
        if _is_missing_route_404(response):
            msg = (
                f"The durable run lifecycle is not available: {url} returned 404. Run polling is a "
                f"hosted-API extension (/{_API_PREFIX}/{_RUNS}/*), not part of the MTHDS Protocol; "
                "PIPELEX_API_URL points at a bare runner that does not serve it."
            )
            raise RunLifecycleUnavailableError(msg, api_url=self.api_base_url)

    # ── Protocol surface: `execute` override (gateway-timeout translation) ──

    @override
    async def execute(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> DictRunResultExecute:
        """Execute a method synchronously and wait for its completion — `POST /v1/execute`.

        Identical to the inherited protocol `execute`, except a failure consistent with the
        hosted gateway's ~30s synchronous ceiling — a gateway `503`/`504`, or a client-side
        request timeout, after at least ~28s have elapsed — is translated into a clear
        `PipelineExecuteTimeoutError` pointing at the durable start+poll path, matching the JS
        SDK. The protocol's optional 202 async-degrade still raises `RunStillRunningError`
        (from the inherited `execute`), and every other non-2xx keeps the inherited
        `httpx.HTTPStatusError` regime (consistent with the other inherited protocol routes).

        Raises:
            PipelineExecuteTimeoutError: The blocking request hit the hosted gateway's ~30s
                synchronous ceiling — use `start_and_wait` (or `start` + `wait_for_result`).
            RunStillRunningError: The server answered 202 (the protocol's optional async
                degrade) — the run continues server-side; resume by `pipeline_run_id`.
            httpx.HTTPStatusError: Any other non-2xx response (the inherited regime).
        """
        started_at = monotonic()
        try:
            return await super().execute(
                pipe_code=pipe_code,
                mthds_contents=mthds_contents,
                inputs=inputs,
                output_name=output_name,
                output_multiplicity=output_multiplicity,
                dynamic_output_concept_ref=dynamic_output_concept_ref,
                extra=extra,
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            elapsed_seconds = monotonic() - started_at
            if _is_gateway_timeout(exc, elapsed_seconds):
                raise PipelineExecuteTimeoutError(_execute_timeout_message(elapsed_seconds), elapsed_seconds=elapsed_seconds) from exc
            raise

    # ── Protocol surface: `start` override (bare-runner 404 → typed error) ──

    @override
    async def start(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RunResultStart:
        """Start a method asynchronously — `POST /v1/start` (202: `pipeline_run_id` only).

        Identical to the inherited protocol `start`, except a bare-runner missing-route 404
        (no run store) is translated into a clear `RunLifecycleUnavailableError` instead of a
        raw `httpx.HTTPStatusError` — matching the JS SDK and letting `start_and_wait` self-heal
        to the blocking-execute fallback. The platform's structured 404s (run not found) keep
        their normal `httpx.HTTPStatusError` behavior.
        """
        try:
            return await super().start(
                pipe_code=pipe_code,
                mthds_contents=mthds_contents,
                inputs=inputs,
                output_name=output_name,
                output_multiplicity=output_multiplicity,
                dynamic_output_concept_ref=dynamic_output_concept_ref,
                extra=extra,
            )
        except httpx.HTTPStatusError as exc:
            self._raise_if_lifecycle_unavailable(exc.response, str(exc.request.url))
            raise

    @override
    async def validate(  # type: ignore[override]
        self,
        mthds_contents: list[str],
        allow_signatures: bool = False,
        mthds_sources: list[str] | None = None,
        render: list[str] | None = None,
    ) -> PipelexValidationResult:
        """Parse, validate, and dry-run an MTHDS bundle — `POST /v1/validate`.

        `/validate` is 200-diagnostic: a produced verdict — valid or invalid — rides a 200
        body discriminated on `is_valid`, returned verbatim as the `PipelexValidationResult`
        union (an invalid bundle is NOT raised; the caller match/cases `is_valid`). A non-2xx
        means no verdict could be produced (request shape, auth, server fault) and surfaces as
        `httpx.HTTPStatusError` (the inherited protocol error regime).

        This override differs from the inherited protocol `validate` in two Pipelex-API ways:
        it always injects `render: ["markdown"]` (so both valid and invalid verdicts carry
        `rendered_markdown`), and it accepts `mthds_sources` as a named parameter.

        Args:
            mthds_contents: MTHDS contents to load (always a list, even for one file).
            allow_signatures: Tolerate unimplemented pipe signatures (strict by default).
            mthds_sources: Optional per-content source names, parallel to `mthds_contents`,
                threaded onto each diagnostic's `source` (an unnamed content yields
                `source: null`). The server 422s a length mismatch.
            render: Optional Pipelex-API presentation hints; `"markdown"` is always added.
                Unknown tokens are server-side lenient-ignored (never a 422).

        Returns:
            The 200-diagnostic union: `PipelexValidationReport` (`is_valid: true`) or
            `PipelexInvalidReport` (`is_valid: false`, with `validation_errors`), each
            carrying `rendered_markdown`.
        """
        extra: dict[str, Any] = {"render": _with_validate_markdown_render(render)}
        if mthds_sources is not None:
            extra["mthds_sources"] = mthds_sources
        return await super().validate(mthds_contents, allow_signatures, extra=extra)

    async def validate_files(
        self,
        files: list[MthdsFile],
        allow_signatures: bool = False,
        render: list[str] | None = None,
    ) -> PipelexValidationResult:
        """Validate paired MTHDS files while preserving URI attribution for diagnostics.

        Decomposes the files into the low-level `validate(...)` payload. When any file carries
        a URI, every content gets a parallel source label (a deterministic `inline://` label
        for the ones without), so the server never sees a length-mismatched `mthds_sources`.

        Raises:
            PipelineRequestError: If `files` is empty.
        """
        if not files:
            msg = "At least one MTHDS file must be provided to validate_files()."
            raise PipelineRequestError(msg)

        mthds_contents = [mthds_file.content for mthds_file in files]
        has_any_uri = any(mthds_file.uri is not None for mthds_file in files)
        mthds_sources: list[str] | None
        if has_any_uri:
            mthds_sources = [
                mthds_file.uri if mthds_file.uri is not None else f"inline://file-{index + 1}.mthds" for index, mthds_file in enumerate(files)
            ]
        else:
            mthds_sources = None

        return await self.validate(mthds_contents, allow_signatures, mthds_sources, render)

    # ── Hosted extension: durable run lifecycle (NOT part of the protocol) ──
    #
    # These four methods are OWNED by this SDK and return this package's own `runs`
    # types (a Pipelex-branded surface). The protocol base `MthdsAPIClient` is
    # protocol-only — it declares no run lifecycle — so these are plain methods, not
    # overrides (no `@override`, no override suppressions).

    async def get_run_status(self, run_id: str) -> RunRead:
        """Fetch a run's status by bare id — `GET /v1/runs/{run_id}/status`.

        Self-healing: a finished-but-unrecorded run resolves to its true terminal status on read.
        `degraded=True` means Temporal was unreachable and `status` is the last-known value;
        `retry_after_seconds` carries the server's `Retry-After` hint when present.

        Raises:
            RunLifecycleUnavailableError: If the lifecycle routes are absent (a bare runner).
            ApiUnreachableError: If the host cannot be reached (DNS / connect / TLS / timeout).
            httpx.HTTPStatusError: For a genuine run-not-found 404 or any other non-2xx response.
        """
        url = self._url(f"{_RUNS}/{quote(run_id, safe='')}/status")
        response = await self._send_or_unreachable("GET", url, content=None, request_timeout=_POLL_REQUEST_TIMEOUT_SECONDS)
        self._raise_if_lifecycle_unavailable(response, url)
        response.raise_for_status()
        run = RunRead.model_validate(response.json())
        retry_after = _parse_retry_after(response.headers)
        if retry_after is not None:
            run = run.model_copy(update={"retry_after_seconds": retry_after})
        return run

    async def get_run_result(self, run_id: str) -> RunResultState:
        """Single-shot result lookup — `GET /v1/runs/{run_id}/results`.

        Maps the platform's poll semantics to a discriminated union:
        - HTTP 202 → `running` (in-flight, with the `Retry-After` hint)
        - HTTP 503 → `running` (DynamoDB/Temporal degraded — retry, never fail a poller)
        - HTTP 200 → `completed` (with the result artifacts)
        - HTTP 409 → `failed` (terminal non-`COMPLETED`)

        Raises:
            RunLifecycleUnavailableError: If the lifecycle routes are absent (a bare runner).
            ApiUnreachableError: If the host cannot be reached (DNS / connect / TLS / timeout).
            httpx.HTTPStatusError: For a genuine run-not-found 404 or any other non-2xx response.
        """
        url = self._url(f"{_RUNS}/{quote(run_id, safe='')}/results")
        response = await self._send_or_unreachable("GET", url, content=None, request_timeout=_POLL_REQUEST_TIMEOUT_SECONDS)
        status_code = response.status_code

        if status_code in {202, 503}:
            retry_after = _parse_retry_after(response.headers)
            return RunResultRunning(
                pipeline_run_id=run_id,
                retry_after_seconds=retry_after if retry_after is not None else _DEFAULT_DEGRADED_RETRY_SECONDS,
            )
        if status_code == 409:
            message = _parse_error_message(response) or "Run finished without a result."
            return RunResultFailed(
                pipeline_run_id=run_id,
                status=_extract_run_status_from_message(message),
                message=message,
            )

        self._raise_if_lifecycle_unavailable(response, url)
        response.raise_for_status()
        result = RunResults.model_validate(response.json())
        return RunResultCompleted(pipeline_run_id=run_id, result=result)

    async def wait_for_result(self, run_id: str, options: WaitForResultOptions | None = None) -> RunResults:
        """Poll a run to a terminal state and return its result.

        Resolves on `COMPLETED`, raises `RunFailedError` on any other terminal status, and raises
        `RunTimeoutError` if `timeout_seconds` elapses first (the run keeps executing server-side —
        resume later by `run_id`). Honors the server's `Retry-After`. Async-native: cancelling the
        awaiting task raises `asyncio.CancelledError` out of this loop, leaving the run resumable.
        """
        opts = options or WaitForResultOptions()
        started_at = monotonic()
        attempt = 0

        while True:
            elapsed = monotonic() - started_at
            remaining = opts.timeout_seconds - elapsed
            if remaining <= 0:
                raise RunTimeoutError(_timeout_message(run_id, opts.timeout_seconds), run_id=run_id, timeout_seconds=opts.timeout_seconds)

            try:
                state = await asyncio.wait_for(self.get_run_result(run_id), timeout=remaining)
            except asyncio.TimeoutError as exc:  # noqa: UP041 — on Python 3.10 asyncio.TimeoutError is its own class, distinct from builtin TimeoutError.
                raise RunTimeoutError(_timeout_message(run_id, opts.timeout_seconds), run_id=run_id, timeout_seconds=opts.timeout_seconds) from exc

            if isinstance(state, RunResultCompleted):
                return state.result
            if isinstance(state, RunResultFailed):
                msg = state.message
                raise RunFailedError(msg, run_id=run_id, status=state.status)

            # state is RunResultRunning — decide whether to keep waiting.
            attempt += 1
            elapsed = monotonic() - started_at
            if elapsed >= opts.timeout_seconds:
                raise RunTimeoutError(_timeout_message(run_id, opts.timeout_seconds), run_id=run_id, timeout_seconds=opts.timeout_seconds)
            if opts.on_poll is not None:
                opts.on_poll(PollInfo(attempt=attempt, elapsed_seconds=elapsed))

            retry_seconds = state.retry_after_seconds if state.retry_after_seconds is not None else 0
            wait_seconds = min(max(opts.interval_seconds, retry_seconds), opts.timeout_seconds - elapsed)
            await asyncio.sleep(wait_seconds)

    async def _supports_run_lifecycle(self) -> bool:
        """Whether the configured server serves the durable run lifecycle, decided via the
        `GET /v1/version` handshake and cached for the client's lifetime. A bare `pipelex-api`
        runner has no run store; anything else is assumed hosted. When the handshake itself fails,
        assume hosted (the SDK default) and let the start call surface the real error.
        """
        if self._lifecycle_available is None:
            try:
                info = await self.version()
            except (httpx.HTTPError, ValidationError):
                self._lifecycle_available = True
            else:
                implementation = (info.model_extra or {}).get("implementation")
                self._lifecycle_available = not (isinstance(implementation, str) and implementation == _BARE_RUNNER_IMPLEMENTATION)
        return self._lifecycle_available

    async def start_and_wait(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
        wait_options: WaitForResultOptions | None = None,
    ) -> RunResults:
        """Start a run and wait for its result — the whole lifecycle in one call, self-healing
        across hosted and bare runners.

        - **Hosted** (per the `/v1/version` handshake): durable `start` + poll, the path that
          survives the gateway's ~30s synchronous ceiling and client disconnects.
        - **Bare runner** (no run store): the blocking `POST /v1/execute`, which has no gateway
          cap off-platform and returns the native `pipe_output`.

        A runner can look hosted yet lack the durable routes (`implementation` is an extension
        field a compliant bare runner may omit). Such a runner raises `RunLifecycleUnavailableError`
        from `start`, BEFORE any run is created, so the blocking fallback cannot double-run; the
        negative is cached so later calls skip the durable attempt.

        Raises:
            RunFailedError: If the run reaches a terminal status other than COMPLETED.
            RunTimeoutError: If the poll budget elapses (the run keeps executing — resume by id).
        """
        if await self._supports_run_lifecycle():
            try:
                started = await self.start(
                    pipe_code=pipe_code,
                    mthds_contents=mthds_contents,
                    inputs=inputs,
                    output_name=output_name,
                    output_multiplicity=output_multiplicity,
                    dynamic_output_concept_ref=dynamic_output_concept_ref,
                    extra=extra,
                )
            except RunLifecycleUnavailableError:
                self._lifecycle_available = False
                return await self._execute_blocking(
                    pipe_code=pipe_code,
                    mthds_contents=mthds_contents,
                    inputs=inputs,
                    output_name=output_name,
                    output_multiplicity=output_multiplicity,
                    dynamic_output_concept_ref=dynamic_output_concept_ref,
                    extra=extra,
                )
            return await self.wait_for_result(started.pipeline_run_id, options=wait_options)

        return await self._execute_blocking(
            pipe_code=pipe_code,
            mthds_contents=mthds_contents,
            inputs=inputs,
            output_name=output_name,
            output_multiplicity=output_multiplicity,
            dynamic_output_concept_ref=dynamic_output_concept_ref,
            extra=extra,
        )

    async def _execute_blocking(
        self,
        *,
        pipe_code: str | None,
        mthds_contents: list[str] | None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None,
        output_name: str | None,
        output_multiplicity: VariableMultiplicity | None,
        dynamic_output_concept_ref: str | None,
        extra: dict[str, Any] | None,
    ) -> RunResults:
        """Blocking `POST /v1/execute` adapted onto `RunResults` — the bare-runner path.

        Forwards every protocol field PLUS the `extra` extension passthrough: an extension-only
        call (`{extra}` with no pipe_code/bundle) or a vendor selector riding `extra` must survive
        this path, not just the durable one.
        """
        result = await self.execute(
            pipe_code=pipe_code,
            mthds_contents=mthds_contents,
            inputs=inputs,
            output_name=output_name,
            output_multiplicity=output_multiplicity,
            dynamic_output_concept_ref=dynamic_output_concept_ref,
            extra=extra,
        )
        return _map_run_result_to_run_results(result)

    # ── Pipelex product surface (hosted management routes) ─────────────────
    #
    # The hosted catalog/account routes the webapp drives. Every one rides the same
    # `{base}/v1/*` surface, `Authorization: Bearer`, org-from-JWT contract as the protocol
    # routes, and goes through `_request_product`, which maps a non-2xx `problem+json` to a
    # typed `ApiResponseError` — branch on `.code`, never the HTTP status.

    async def get_me(self) -> UserProfile:
        """The authenticated user's profile — `GET /v1/me`."""
        return UserProfile.model_validate(await self._request_product("GET", "me"))

    async def list_methods(self) -> list[MethodData]:
        """List the caller's saved methods — `GET /v1/methods`."""
        result = await self._request_product("GET", "methods")
        return [MethodData.model_validate(item) for item in result]

    async def get_method(self, method_id: str) -> MethodData:
        """Fetch one method by id — `GET /v1/methods/{id}`."""
        return MethodData.model_validate(await self._request_product("GET", f"methods/{quote(method_id, safe='')}"))

    async def create_method(self, write_input: MethodWriteInput) -> MethodData:
        """Create a method — `POST /v1/methods`."""
        body = write_input.model_dump(mode="json", exclude_none=True)
        return MethodData.model_validate(await self._request_product("POST", "methods", body=body))

    async def update_method(self, method_id: str, write_input: MethodWriteInput) -> MethodData:
        """Replace a method (a rename is a changed `name`) — `PUT /v1/methods/{id}`."""
        body = write_input.model_dump(mode="json", exclude_none=True)
        return MethodData.model_validate(await self._request_product("PUT", f"methods/{quote(method_id, safe='')}", body=body))

    async def delete_method(self, method_id: str) -> None:
        """Delete a method — `DELETE /v1/methods/{id}` (empty body)."""
        await self._request_product("DELETE", f"methods/{quote(method_id, safe='')}")

    async def list_memberships(self) -> MembershipsResponse:
        """The caller's org memberships + active-org feature flags — `GET /v1/organizations/memberships`."""
        return MembershipsResponse.model_validate(await self._request_product("GET", "organizations/memberships"))

    async def create_organization(self, name: str) -> Membership:
        """Create an organization — `POST /v1/organizations`."""
        return Membership.model_validate(await self._request_product("POST", "organizations", body={"name": name}))

    async def rename_organization(self, org_id: str, name: str) -> Membership:
        """Rename an organization — `PATCH /v1/organizations/{org_id}`."""
        return Membership.model_validate(await self._request_product("PATCH", f"organizations/{quote(org_id, safe='')}", body={"name": name}))

    async def get_subscription(self) -> SubscriptionResponse:
        """The active org's subscription state — `GET /v1/billing/subscription`."""
        return SubscriptionResponse.model_validate(await self._request_product("GET", "billing/subscription"))

    async def list_plans(self) -> list[PlanView]:
        """Available plans (with `is_current`) — `GET /v1/billing/plans`."""
        result = await self._request_product("GET", "billing/plans")
        return [PlanView.model_validate(item) for item in result]

    async def list_invoices(self) -> list[InvoiceView]:
        """Past invoices — `GET /v1/billing/invoices`."""
        result = await self._request_product("GET", "billing/invoices")
        return [InvoiceView.model_validate(item) for item in result]

    async def create_checkout(self, plan: str) -> CheckoutResponse:
        """Open a Stripe checkout for a plan — `POST /v1/billing/checkout`."""
        return CheckoutResponse.model_validate(await self._request_product("POST", "billing/checkout", body={"plan": plan}))

    async def change_plan(self, plan: str) -> ChangePlanResponse:
        """Switch the existing subscription's plan — `POST /v1/billing/change-plan`.

        A 409 `conflict` (`ApiResponseError.code`) means there is no subscription to change —
        start one via `create_checkout` first.
        """
        return ChangePlanResponse.model_validate(await self._request_product("POST", "billing/change-plan", body={"plan": plan}))

    async def get_billing_portal(self) -> BillingPortalResponse:
        """A Stripe billing-portal session URL — `GET /v1/billing/portal`.

        A 409 `conflict` (`ApiResponseError.code`) means there is no subscription yet.
        """
        return BillingPortalResponse.model_validate(await self._request_product("GET", "billing/portal"))

    async def list_pipelex_api_keys(self) -> PipelexApiKeyList:
        """List the caller's Pipelex API keys — `GET /v1/pipelex-api-keys`."""
        return PipelexApiKeyList.model_validate(await self._request_product("GET", "pipelex-api-keys"))

    async def create_pipelex_api_key(self, label: str) -> PipelexApiKeyCreated:
        """Mint a Pipelex API key — `POST /v1/pipelex-api-keys`.

        The plaintext `api_key` is returned ONCE. A 409 `pipelex_api_key_limit_reached`
        (`ApiResponseError.code`) means the per-account key limit is hit.
        """
        return PipelexApiKeyCreated.model_validate(await self._request_product("POST", "pipelex-api-keys", body={"label": label}))

    async def revoke_pipelex_api_key(self, key_id: str) -> None:
        """Revoke a Pipelex API key — `DELETE /v1/pipelex-api-keys/{id}` (empty body)."""
        await self._request_product("DELETE", f"pipelex-api-keys/{quote(key_id, safe='')}")

    async def rotate_pipelex_api_key(self, key_id: str) -> PipelexApiKeyCreated:
        """Rotate a Pipelex API key — `POST /v1/pipelex-api-keys/{id}/rotate` (no body).

        Returns the new plaintext `api_key` once; the old key stops working.
        """
        return PipelexApiKeyCreated.model_validate(await self._request_product("POST", f"pipelex-api-keys/{quote(key_id, safe='')}/rotate"))

    async def create_gateway_api_key(self, promo_code: str | None) -> GatewayApiKey:
        """Provision the gateway (LLM inference) API key — `POST /v1/gateway-api-key`.

        The JSON body is ALWAYS sent (even with `promo_code=None`) — the server 422s an empty body.
        """
        return GatewayApiKey.model_validate(await self._request_product("POST", "gateway-api-key", body={"promo_code": promo_code}))

    async def get_gateway_api_key(self) -> GatewayApiKeyStatus:
        """The gateway key status (`None` until provisioned) — `GET /v1/gateway-api-key`."""
        return GatewayApiKeyStatus.model_validate(await self._request_product("GET", "gateway-api-key"))

    async def submit_onboarding(self, submission: OnboardingSubmission) -> None:
        """Submit the onboarding questionnaire — `POST /v1/onboarding/submit` (empty body)."""
        body = submission.model_dump(mode="json", exclude_none=True)
        await self._request_product("POST", "onboarding/submit", body=body)

    async def resolve_storage_url(self, uri: str) -> ResolvedStorageUrl:
        """Resolve a storage URI to a presigned URL — `POST /v1/resolve-storage-url`."""
        return ResolvedStorageUrl.model_validate(await self._request_product("POST", "resolve-storage-url", body={"uri": uri}))

    async def upload(self, upload_input: UploadInput) -> UploadedFile:
        """Upload a base64 file — `POST /v1/upload`."""
        body = upload_input.model_dump(mode="json", exclude_none=True)
        return UploadedFile.model_validate(await self._request_product("POST", "upload", body=body))

    async def list_runs(self, method_id: str) -> list[PipelineRun]:
        """List a method's runs — `GET /v1/runs?method_id={methodId}`."""
        result = await self._request_product("GET", f"{_RUNS}?method_id={quote(method_id, safe='')}")
        return [PipelineRun.model_validate(item) for item in result]

    async def update_run(self, run_id: str, update_input: UpdateRunInput) -> None:
        """Patch a run's status (admin/manual) — `PUT /v1/runs/{id}` (empty body)."""
        body = update_input.model_dump(mode="json", exclude_none=True)
        await self._request_product("PUT", f"{_RUNS}/{quote(run_id, safe='')}", body=body)

    # ── Health ─────────────────────────────────────────────────────────────
    #
    # The origin-level liveness probe. `/health` is served at the origin, NOT under the
    # `/v1` prefix, and is out-of-protocol — the MTHDS Protocol defines no health route.
    # It rides `_request_json`, the plainer regime: a non-2xx raises `PipelineRequestError`,
    # not the product `ApiResponseError`, since liveness needs no `code` taxonomy.

    async def health(self) -> dict[str, Any]:
        """Origin-level liveness probe — `GET {origin}/health` (NOT under the `/v1` prefix)."""
        result = await self._request_json("GET", f"{self.origin_url}/health")
        return cast("dict[str, Any]", result)


# ── Module helpers ──────────────────────────────────────────────────────


_KNOWN_RUN_STATUS_NAMES: frozenset[str] = frozenset(RunStatus.__members__)


def _with_validate_markdown_render(render: list[str] | None) -> list[str]:
    """Ensure `"markdown"` rides the `/validate` render list, preserving order and de-duplicating.

    Mirrors the JS `withValidateMarkdownRender` (a `Set`): the caller's tokens come first, then
    `"markdown"` if not already present, so both valid results and produced validation-error
    verdicts carry `rendered_markdown`.
    """
    return list(dict.fromkeys([*(render or []), _VALIDATE_MARKDOWN_RENDER_FORMAT]))


def _timeout_message(run_id: str, timeout_seconds: float) -> str:
    """The shared `RunTimeoutError` message — the run survives and is resumable by id."""
    return f"Run {run_id} did not reach a terminal state within {timeout_seconds}s; it is still executing server-side and can be resumed by id."


def _is_gateway_timeout(exc: httpx.HTTPStatusError | httpx.TimeoutException, elapsed_seconds: float) -> bool:
    """Whether a failed blocking `execute` is the hosted gateway's ~30s synchronous cut-off.

    The elapsed threshold guards against mislabeling a fast `503` (the runner genuinely down)
    as a timeout: a gateway `503`/`504`, or a client-side request timeout, only counts once the
    request has run at least ~28s. Mirrors the JS `isGatewayTimeout`.
    """
    if elapsed_seconds < _GATEWAY_TIMEOUT_THRESHOLD_SECONDS:
        return False
    if isinstance(exc, httpx.TimeoutException):
        return True
    return exc.response.status_code in {503, 504}


def _execute_timeout_message(elapsed_seconds: float) -> str:
    """The `PipelineExecuteTimeoutError` message — point the caller at the durable start+poll path."""
    seconds = round(elapsed_seconds)
    return (
        f"The Pipelex Hosted API times out synchronous requests after ~30s — this run took {seconds}s. "
        "The blocking execute path can't run methods longer than 30s behind the gateway. "
        "Start the run and poll for its result instead: `start()` then `wait_for_result(run_id)` (or `start_and_wait`)."
    )


def _is_missing_route_404(response: httpx.Response) -> bool:
    """Whether a 404 is an unmatched-route 404 (no platform deployed) rather than the platform's
    structured run-not-found 404. The platform wraps its 404s in RFC 7807 problem+json with a stable
    `code`; a bare runner returns Starlette's default `{"detail": "Not Found"}` (no `code`).
    """
    try:
        body = response.json()
    except ValueError:
        return True
    if not isinstance(body, dict):
        return True
    return "code" not in body


def _parse_retry_after(headers: httpx.Headers) -> int | None:
    """Parse the `Retry-After` header (integer-seconds form, which the platform uses)."""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _parse_error_message(response: httpx.Response) -> str | None:
    """Extract a human message from an error body — handles the platform's problem+json (`detail`
    string) and the runner's `{"detail": {"message": ...}}` / `{"message": ...}` shapes.
    """
    try:
        raw = response.json()
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    body = cast("dict[str, Any]", raw)
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        message = cast("dict[str, Any]", detail).get("message")
        if isinstance(message, str):
            return message
    top_message = body.get("message")
    return top_message if isinstance(top_message, str) else None


def _extract_run_status_from_message(message: str) -> RunStatus:
    """Pull the status word out of a 409 detail ("Run finished with status FAILED; ..."), defaulting
    to FAILED if the shape ever changes.
    """
    match = re.search(r"status\s+([A-Z_]+)", message)
    if match and match.group(1) in _KNOWN_RUN_STATUS_NAMES:
        return RunStatus(match.group(1))
    return RunStatus.FAILED


def _map_run_result_to_run_results(response: DictRunResultExecute) -> RunResults:
    """Map the protocol's blocking `POST /v1/execute` response onto the lifecycle's `RunResults`.

    The bare-runner path returns `pipe_output` (native runner shape); `main_stuff` and `graph_spec`
    are hosted-durable artifacts and stay `None` here. Consumers read `main_stuff or pipe_output`
    (the documented hosted/bare output-shape difference).
    """
    return RunResults(
        pipeline_run_id=response.pipeline_run_id,
        main_stuff=None,
        graph_spec=None,
        pipe_output=response.pipe_output.model_dump(),
    )


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
