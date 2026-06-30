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

## Error regimes

(To be detailed in Phase 1.) Two regimes, ported from the TS SDK:

- **Product routes** raise a typed `ApiResponseError` carrying the RFC 9457 `.code` discriminant — consumers branch on `err.code` (e.g. `"conflict"`, `"pipelex_api_key_limit_reached"`), never on the HTTP status.
- **Transport failures** (DNS/connect/TLS/timeout) raise `ApiUnreachableError`.
- **Inherited protocol routes** keep the base `mthds` `raise_for_status()` → `httpx.HTTPStatusError` behavior.

## Out of scope for v0.1

- `/v1/build/*` helpers (the TS clients carry them; recorded as a conscious deferral).
- Organization *switch* (a WorkOS session operation, not a `/v1` route).
- A `~/.pipelex/config` file reader (env-only for now, matching the JS SDK).
- A synchronous client facade.
