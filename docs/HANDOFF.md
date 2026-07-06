# Handoff: split the Python SDKs along the same line as the TS SDKs

This repo (`pipelex-sdk-python`, package TBD — mirror `@pipelex/sdk` → likely `pipelex-sdk` on PyPI) was just created empty. It should become the Python counterpart of `pipelex-sdk-js`, exactly as `mthds-python` is the Python counterpart of `mthds-js`.

**Run the work from a session rooted at the workspace root** (`/Users/lchoquel/repos/Pipelex/`), not from inside either repo — it touches two repos as targets and two more as references, and per-repo `CLAUDE.md`/skills only auto-load for repos under the session root. Write the detailed plan there.

## Goal

Apply the TS philosophy in Python:

- **`mthds-python`** = generic MTHDS-protocol client only. It should **stop carrying the durable run lifecycle**.
- **`pipelex-sdk-python`** (this repo) = the protocol surface **plus** the durable run lifecycle **plus** the Pipelex-hosted product surface.

This mirrors the TS split exactly: `mthds-js`/`MthdsApiClient` is protocol-only; `pipelex-sdk-js`/`PipelexApiClient` is the hosted superset.

## Two phases (do in this order)

1. **Build `pipelex-sdk-python`** — port `pipelex-sdk-js` to Python. Reuse `mthds-python` for the protocol/transport base (one-way dep `pipelex-sdk-python → mthds`, mirroring `@pipelex/sdk → mthds`). Add the lifecycle + product surface on top.
2. **Strip the durable run lifecycle out of `mthds-python`** — only after Phase 1 has parity, so nothing is lost.

Phase 1 first so the strip in Phase 2 is informed by exactly what the new repo now owns.

## Exact surface to move / port

Source of truth for the Python lifecycle code being moved: `mthds-python/mthds/runners/api/client.py` + `runs.py`.

**Durable run lifecycle — REMOVE from `mthds-python`, OWN here:**
- `get_run_status(run_id)` → `GET /v1/runs/{id}/status`
- `get_run_result(run_id)` → `GET /v1/runs/{id}/results` (202 running / 200 done / 409 failed / 503 running)
- `wait_for_result(...)` (polls results)
- `start_and_wait(...)` (start + poll; keep the blocking-`execute` fallback for bare runners)
- the `RunLifecycleUnavailableError` path

**Pipelex-hosted product surface — NEW here** (port from `pipelex-sdk-js/src/client.ts`, product block ~`:738-891`; types from its `product-models.ts`):
- methods catalog CRUD `/v1/methods`, `getMe` `/v1/me`
- organizations `/v1/organizations/*`
- billing `/v1/billing/*`
- API keys `/v1/pipelex-api-keys/*`, `/v1/gateway-api-key`
- onboarding `/v1/onboarding/submit`
- storage `/v1/resolve-storage-url`, `/v1/upload`
- run records `/v1/runs` (list, by method_id), `PUT /v1/runs/{id}`

These need the `PUT/PATCH/DELETE` verbs and a `problem+json` → typed error mapping (`.code` discriminant), like the TS client.

## What stays in `mthds-python` (the protocol boundary)

Keep exactly the five normative routes + their typed models: `execute` / `start` / `validate` / `models` / `version`. These are the **entire** normative MTHDS Protocol per `mthds/docs/spec/` (OpenAPI `mthds-protocol.openapi.yaml` is authoritative). Everything else — lifecycle, product, build helpers, health — is out-of-protocol by the spec's own words ("keeps no run store and owns no user, billing, or catalog concepts"). The lifecycle is an out-of-protocol *hosted extension*, which is why it belongs here, not in the protocol client.

Note: `mthds-python` does **not** currently carry the `/v1/build/*` helpers (the TS clients do) — decide separately whether this repo should; not required for the split.

## Reference map

- Port target / philosophy reference: `pipelex-sdk-js/` (`PipelexApiClient`, `product-models.ts`).
- Protocol base to depend on: `mthds-python/` (`MthdsAPIClient`, `protocol/`).
- Normative boundary: `mthds/docs/spec/protocol.md` + `openapi/mthds-protocol.openapi.yaml`.
- Python coding standards: `mthds-python/CLAUDE.md` (target 3.11+, Pydantic v2, StrEnum rules, etc.) — apply the same here.
