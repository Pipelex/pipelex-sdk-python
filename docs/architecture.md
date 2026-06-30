# pipelex-sdk architecture

This document grows phase by phase as the SDK is built. It is the design reference for the package.

## What this is

`pipelex-sdk` (import package `pipelex_sdk`) is the Python client for the Pipelex hosted API. It is the Python counterpart of the TypeScript `@pipelex/sdk` (`PipelexApiClient`), built on top of `mthds` (the `mthds-python` package) exactly as `@pipelex/sdk` is built on the `mthds` npm package.

It is the **hosted superset** of the MTHDS Protocol:

- the five normative MTHDS Protocol routes — `POST /execute`, `POST /start`, `POST /validate`, `GET /models`, `GET /version` — inherited from the protocol base;
- **plus** the durable run lifecycle (`get_run_status`, `get_run_result`, `wait_for_result`, `start_and_wait`);
- **plus** the Pipelex product surface (methods catalog, organizations, billing, API keys, onboarding, storage, run records).

## Dependency direction

One-way: `pipelex-sdk → mthds`. The SDK depends on `mthds` for the protocol/transport base and never the reverse. This mirrors the TypeScript `@pipelex/sdk → mthds` edge.

The client is built by **inheritance**:

```
mthds.runners.api.client.MthdsAPIClient   (protocol-only base: transport, body-builders, the protocol routes)
    └── pipelex_sdk.client.PipelexAPIClient   (adds lifecycle + product + health on top)
```

`PipelexAPIClient` reuses the base transport (`_send`, `_url`), the request-body builders, the reusable protocol methods, `runner_type`, and the async context-manager; it adds the lifecycle, product, and health surfaces plus a richer error/transport layer.

## Brand boundary (MTHDS vs Pipelex)

MTHDS is the brand of the open standard (the language, the protocol). Pipelex is the brand of the hosted runtime/product. Artifacts that belong to the standard keep neutral, un-prefixed names; Pipelex branding is reserved for genuinely runtime/product-specific surfaces (the durable run lifecycle, the product routes, implementation envelopes). The five protocol routes and their models stay in `mthds`; everything Pipelex-specific lives here.

## Credentials & configuration

Resolved at construction time:

- `PIPELEX_API_KEY` / `PIPELEX_API_URL` first (brand + JS parity);
- falling back to the `mthds` resolver (`MTHDS_API_KEY` / `MTHDS_API_URL`, `~/.mthds/config`) as a secondary source.

A token is **optional** (anonymous access is allowed; protocol routes work against anonymous bare runners, product routes return `401`). The default base URL is `https://api.pipelex.com`. The base URL is validated host-only (no path/query/fragment/embedded credentials; http/https only).

## Conventions

- **Async-only** — httpx `AsyncClient`, `async def` throughout. No sync facade in v0.1.
- **No barrel** — package `__init__.py` files stay empty; consumers import via full paths (`from pipelex_sdk.client import PipelexAPIClient`). The public import paths are documented in the README.
- **Wire format** — snake_case JSON fields on Pydantic v2 models.

## Transport layer

The client inherits `mthds`'s `_send` (one raw HTTP request, no status interpretation) and `_url` (`{base}/v1/{endpoint}`), and layers three helpers on top (`pipelex_sdk/client.py`):

- **`_send_or_unreachable`** — wraps `_send`, mapping httpx transport failures to `ApiUnreachableError` (a `httpx.TimeoutException` → `code="ABORT_TIMEOUT"`; any other `httpx.TransportError` → `code=<exception class name>`). Non-2xx interpretation stays with the caller.
- **`_request_product`** — the product-route path. Serializes the body with `pydantic_core.to_json` (supporting PUT/PATCH/DELETE as well as GET/POST), uses the management-call timeout, maps a non-2xx response to `ApiResponseError`, and is **empty-body tolerant** (a 2xx with no body — DELETE / onboarding / update — returns `None`).
- **`_request_json`** — the plainer path for `health` (and, if ever added, the build extensions). Takes an absolute URL, raises `PipelineRequestError` on a non-2xx response. Transport failures still map to `ApiUnreachableError`.

`start_client` is overridden so the `Authorization` header is sent only when a token is configured — anonymous access (empty token) omits it.

The `problem+json` / `HTTPException` error body is parsed by `_parse_error_body` into `(error_type, server_message, validation_errors, code)`, handling both `{"detail": {...}}` and `{"detail": "..."}` shapes plus top-level `error_type` / `message` / `code`, and falling through to empty on a non-JSON or non-object body. `validation_errors` is parsed leniently (best-effort error-path enrichment; only reachable via the out-of-scope build-route 422s).

## Error regimes

Two regimes, ported faithfully from the TS SDK (decision #5 — not unified yet):

- **Product routes** raise a typed `ApiResponseError` (subclass of `PipelineRequestError`) carrying the RFC 9457 `code` discriminant — consumers branch on `err.code` (e.g. `"conflict"`, `"pipelex_api_key_limit_reached"`), never on the HTTP status. It also carries `status`, `status_text`, `response_body`, `error_type`, `server_message`, and `validation_errors`.
- **Transport failures** (DNS/connect/TLS/timeout) raise `ApiUnreachableError` (subclass of `PipelineRequestError`) with `api_url` and `code`.
- **`health` / `_request_json`** raise the plainer `PipelineRequestError` on a non-2xx response (decision #5 revisits whether to bring this under `ApiResponseError` at Checkpoint 5).
- **Inherited protocol routes** (`execute` / `start` / `validate` / `models` / `version`) keep the base `mthds` `raise_for_status()` → `httpx.HTTPStatusError` behavior.

## Run lifecycle (hosted extension)

The durable run lifecycle (`pipelex_sdk/runs.py` + the client's lifecycle methods) is a **hosted-API extension, not part of the MTHDS Protocol**. Long method runs outlive the hosted gateway's ~30s synchronous cap, so a caller submits a run (`POST /v1/start`), then polls a self-healing endpoint by bare `pipeline_run_id` until it reaches a terminal state. All state lives behind the id (DynamoDB + Temporal on the platform), so a caller can drop the poll loop and resume later with just the id. A bare runner has no run store and `404`s these routes, which the client translates into a clear `RunLifecycleUnavailableError`.

### Owned types (brand boundary)

`pipelex_sdk/runs.py` **owns** the lifecycle models — they are a Pipelex-branded surface, mirroring `pipelex-sdk-js/src/runs.ts`. They are not imported from `mthds`. During the transition the same shapes still exist in `mthds-python`; that duplication is deliberate (so this SDK is correct regardless of `mthds-python`'s state) and is removed from `mthds-python` later. While the base `MthdsAPIClient` still declares the same lifecycle methods, the client's overrides return this package's own types and so read as incompatible overrides to the type-checker — they carry a narrow `# type: ignore[override]`, which becomes unnecessary (harmless) once the base copies are stripped.

- `RunStatus` — the hosted status enum, with `is_terminal` / `is_success` predicates (exhaustive `match`).
- `RunRead` — a run record read through the self-healing status path (adds `degraded` + `retry_after_seconds`).
- `RunResults` — result artifacts. Hosted runs carry `main_stuff` (+ `graph_spec`); the bare-runner blocking fallback carries `pipe_output` (the runner's native execute response). Consumers read `main_stuff or pipe_output` (the documented hosted/bare output-shape difference). Extension-open, so any other server artifact is preserved.
- `RunResultState` — the single-shot result outcome, a union discriminated on `state` (`running` / `completed` / `failed`).
- `WaitForResultOptions` / `PollInfo` — poll-loop tuning and progress info. Async-native cancellation is via `asyncio.CancelledError` (cancel the awaiting task), so there is no `signal` field.

### Polling surface

- **`get_run_status(run_id)`** — `GET /v1/runs/{id}/status` → `RunRead`. Lifts the `Retry-After` header onto `retry_after_seconds`.
- **`get_run_result(run_id)`** — `GET /v1/runs/{id}/results`, mapping the platform's poll semantics to the `RunResultState` union: `202`/`503` → `running` (in-flight / degraded — never fail a poller), `200` → `completed`, `409` → `failed` (terminal status parsed from the message).
- **`wait_for_result(run_id, options)`** — polls `get_run_result` to a terminal state, honoring `Retry-After` and the deadline. Resolves on `COMPLETED`; raises `RunFailedError` on any other terminal status and `RunTimeoutError` if the budget elapses (the run keeps executing server-side — resume later by id).

These poll GETs go through `_send_or_unreachable`, so a transport failure surfaces as `ApiUnreachableError` (consistent with the product layer), while a missing-route `404` surfaces as `RunLifecycleUnavailableError` and any other non-2xx as `httpx.HTTPStatusError`.

### `start_and_wait` — hosted ↔ bare self-healing

`start_and_wait` runs the whole lifecycle in one call and self-heals across runner kinds (the one place this SDK intentionally exceeds `mthds-python`, whose `start_and_wait` raises on a bare runner):

- A cached `GET /v1/version` handshake (`_supports_run_lifecycle`) classifies the runner. `VersionInfo.implementation == "pipelex-api"` ⇒ a bare runner (no run store); anything else ⇒ assumed hosted. The outcome is cached for the client's lifetime; a failed handshake assumes hosted and lets `start` surface the real error.
- **Hosted:** durable `start` (202 ack) → `wait_for_result` (poll to terminal).
- **Bare runner:** the blocking `POST /v1/execute` (`_execute_blocking`), which has no gateway cap off-platform and returns the native `pipe_output`, mapped onto `RunResults` (`main_stuff = None`).
- **Self-heal:** a runner can look hosted yet lack the durable routes (`implementation` is an optional extension a compliant bare runner may omit). The client's `start` override translates a bare-runner missing-route `404` into `RunLifecycleUnavailableError` — raised **before any run is created** — so `start_and_wait` falls back to the blocking path without risking a double-run, and caches the negative so later calls skip the durable attempt.

### Run/lifecycle errors

`RunFailedError`, `RunTimeoutError`, and `RunLifecycleUnavailableError` are owned in `pipelex_sdk/errors.py`. `RunStillRunningError` — the protocol `execute()` 202-degrade error — stays owned by `mthds` and is re-exported from `pipelex_sdk/errors.py` so consumers have a single import home for all run/lifecycle errors.

## `validate` override (Pipelex-API presentation + sources)

The protocol `validate` is **overridden** (not inherited) to add the two Pipelex-API extensions the bare protocol route doesn't carry, while keeping the inherited protocol error regime (a no-verdict non-2xx surfaces as `httpx.HTTPStatusError`, not `ApiResponseError` — the verdict itself is always a 200 discriminated on `is_valid`):

- **Markdown render is always injected.** `validate(...)` adds `"markdown"` to the `render` list (de-duplicated, caller tokens first) so both a valid `PipelexValidationReport` and a produced `PipelexInvalidReport` carry `rendered_markdown`. Unknown render tokens are server-side lenient-ignored.
- **`mthds_sources`** is a named parameter (parallel to `mthds_contents`) threaded onto each diagnostic's `source`; sent only when provided.
- **`validate_files(files, …)`** takes `MthdsFile(content, uri?)` records. When any file carries a URI, every content gets a parallel source label — the named file's URI, or a deterministic `inline://file-N.mthds` for an unnamed sibling — so the server never sees a length-mismatched `mthds_sources`.

The override delegates the wire call to the inherited base `validate` (passing `render` / `mthds_sources` through the protocol's `extra` extension passthrough), so the body-building and transport stay shared; only the Pipelex presentation/sources concerns live here. The validation models (`PipelexValidationResult` = `PipelexValidationReport | PipelexInvalidReport`, with `rendered_markdown`) are reused from `mthds` for now (the brand-layering follow-up #9 — they would eventually migrate here to fully mirror the JS boundary).

## Pipelex product surface (hosted management routes)

The hosted catalog/account routes the webapp drives (`pipelex_sdk/product_models.py` + the client's product methods). Every route rides the same `{base}/v1/*` surface, `Authorization: Bearer`, org-from-JWT contract as the protocol routes, and goes through `_request_product`, which maps a non-2xx `problem+json` to a typed `ApiResponseError` — **consumers branch on `.code`, never the HTTP status**.

The wire models are snake_case Pydantic v2. Response models are extension-open (`extra="allow"`) so a newly-added server field is preserved, not rejected; input models name exactly what each route accepts. `PipelineRun.status` reuses the run-lifecycle `RunStatus`; `OrgRole`, `PipeStatus`, and the onboarding fields are `StrEnum`s.

- **User profile** — `get_me()` → `UserProfile` (`GET /v1/me`).
- **Methods catalog** — `list_methods()` / `get_method(id)` / `create_method(MethodWriteInput)` / `update_method(id, MethodWriteInput)` (a rename is a changed `name`) / `delete_method(id)`. The id is path-encoded; an absent `input_data` is dropped from the write body.
- **Organizations** — `list_memberships()` → `MembershipsResponse` (memberships + active-org feature flags); `create_organization(name)` / `rename_organization(org_id, name)` → `Membership`. Organization *switch* is out of scope (a WorkOS session op, not a `/v1` route).
- **Billing** — `get_subscription()`, `list_plans()`, `list_invoices()`, `create_checkout(plan)`. `change_plan(plan)` and `get_billing_portal()` surface a **409 `conflict`** (`ApiResponseError.code`) when there is no subscription yet — start one via `create_checkout` first.
- **Pipelex API keys** — `list_pipelex_api_keys()`; `create_pipelex_api_key(label)` and `rotate_pipelex_api_key(id)` return the plaintext `api_key` **once**; `revoke_pipelex_api_key(id)`. Creation surfaces a **409 `pipelex_api_key_limit_reached`** when the per-account limit is hit. Rotation sends no body.
- **Gateway (LLM inference) key** — `create_gateway_api_key(promo_code)` **always sends a JSON body** (even with `promo_code=None` → `{"promo_code": null}`); the server 422s an empty body. `get_gateway_api_key()` → status (`gateway_api_key` is `None` until provisioned).
- **Onboarding** — `submit_onboarding(OnboardingSubmission)` (`POST /v1/onboarding/submit`, empty 2xx body); absent optional fields are dropped.
- **Storage** — `resolve_storage_url(uri)` → presigned URL; `upload(UploadInput)` → the stored file handle.
- **Run records** — `list_runs(method_id)` → `list[PipelineRun]` (the catalog-style list, distinct from the lifecycle status/result routes); `update_run(run_id, UpdateRunInput)` (admin/manual status patch, empty 2xx body).

## Out of scope for v0.1

- `/v1/build/*` helpers (the TS clients carry them; recorded as a conscious deferral).
- Organization *switch* (a WorkOS session operation, not a `/v1` route).
- A `~/.pipelex/config` file reader (env-only for now, matching the JS SDK).
- A synchronous client facade.
