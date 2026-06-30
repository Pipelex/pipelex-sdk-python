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

## Out of scope for v0.1

- `/v1/build/*` helpers (the TS clients carry them; recorded as a conscious deferral).
- Organization *switch* (a WorkOS session operation, not a `/v1` route).
- A `~/.pipelex/config` file reader (env-only for now, matching the JS SDK).
- A synchronous client facade.
