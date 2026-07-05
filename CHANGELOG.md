# Changelog

## [v0.3.0] - 2026-07-05

### Changed

- **`RunResults.main_stuff` is now always present (Breaking).** `main_stuff` was optional (`Any = None`) and callers had to fall back to `pipe_output` and shape-guess the main output on the blocking path. Leaning on the pipelex >= 0.37 main-stuff invariant (every completed run delivers a main stuff), the SDK now delivers a resolved, non-null `main_stuff` on **both** paths: on the hosted path it is the `main_stuff.json` S3 artifact; on the blocking-execute path the SDK resolves it out of the returned working memory via the response's `main_stuff_name` extension field, so both paths carry the same content shape. Consumers read `results.main_stuff` directly — no `main_stuff or pipe_output` fallback, no shape-guessing. The full working memory still rides `pipe_output` (blocking path only) for consumers that want it. *(Migration: read `results.main_stuff` instead of `results.main_stuff or results.pipe_output`.)*

### Added

- **`MissingMainStuffError`.** A completed run that cannot deliver a main stuff now raises this typed error (derives from `PipelineRequestError`, carries `run_id`) instead of silently yielding a null output: the hosted results endpoint answered a `200` with a null `main_stuff`, or a blocking `execute` response named a `main_stuff_name` absent from its working-memory root. A falsy-but-present main stuff (empty list, `0`) is a valid output and does not raise.

## [v0.2.0] - 2026-07-02

### Added

- Added a `request_timeout_seconds` constructor parameter to `PipelexAPIClient`, setting a per-instance blocking-execute ceiling for the inherited protocol routes (`execute`, `start`, `validate`, `models`, `version`).

### Changed

- **BREAKING:** Renamed `PipelexAPIClient` constructor parameters and attributes to match the `mthds` base client and the `@pipelex/sdk` JavaScript counterpart: `api_token` → `api_key` and `api_base_url` → `base_url`. *(Migration: update all instantiations and property reads to the new names.)*
- **BREAKING:** Renamed the API URL environment variable for workspace-wide consistency: `PIPELEX_API_URL` → `PIPELEX_BASE_URL`. No read alias is kept for the old name.
- **BREAKING:** An empty base URL now raises `PipelineRequestError` instead of being treated as unset. Both layers of the `base_url` chain use presence semantics (matching the JS SDK's `??` chain): an explicit `base_url=""` argument or a set-but-empty `PIPELEX_BASE_URL` (e.g. an unfilled CI secret) fails fast at construction rather than silently targeting the hosted default with whatever API key is configured.
- **BREAKING:** `PipelexAPIClient` no longer reads the `mthds` resolver at all — `MTHDS_API_KEY`, `MTHDS_BASE_URL`, and `~/.mthds/config` are ignored. The mthds config stores a `(base_url, api_key)` credential pair for whatever runner the vendor-neutral `mthds` tooling targets; borrowing its key while ignoring its URL could send a key configured for another runner to `api.pipelex.com`. Resolution is now Pipelex-only, matching the JS SDK exactly: `api_key` argument → `PIPELEX_API_KEY` → anonymous, and `base_url` argument → `PIPELEX_BASE_URL` → the hosted default. *(Migration: set `PIPELEX_API_KEY` / pass `api_key`, and `PIPELEX_BASE_URL` / `base_url`, if you relied on `MTHDS_*` or `~/.mthds/config`.)*
- Bumped the `mthds` dependency from `>=0.6.1` to `>=0.7.1`.
- Updated documentation (`README.md`, `CLAUDE.md`, `docs/architecture.md`) and unit tests to reflect the new client signature, environment variables, and credential resolution.

### Fixed

- `PipelexAPIClient()` now targets the hosted API (`https://api.pipelex.com`) when nothing is configured, instead of leaking `mthds`'s local bare-runner default (`http://localhost:8081`) through the now-removed `mthds` resolver fallback.

## [v0.1.1] - 2026-07-01

### Fixed

- Ship a PEP 561 `py.typed` marker inside `pipelex_sdk`, so downstream type checkers (pyright/mypy) pick up the package's inline type hints. Without it the wheel's types were invisible to consumers despite the source being fully typed. Matches `mthds`'s `mthds/py.typed`.

## [v0.1.0] - 2026-07-01

The initial public surface of `pipelex-sdk` — the Python counterpart of `@pipelex/sdk`, built by inheritance on the `mthds` protocol base. Surface-complete against the TypeScript SDK (see `docs/architecture.md` → "Parity with `@pipelex/sdk`"); the `/v1/build/*` helpers and the WorkOS org-switch are consciously out of scope for this release.

### Added

- Initial repository scaffold: packaging (`pyproject.toml`), tooling (`Makefile`, ruff/pyright/mypy/pylint config mirroring `mthds-python`), and the empty `pipelex_sdk` package.
- GitHub Actions CI/CD mirroring `mthds-python`, adapted to the `Pipelex` org and the `pipelex-sdk` PyPI distribution: PR gates (`lint-check`, `tests-check`, `package-check`, `changelog-check`, `version-check`, `guard-branches`, `cla`) across the full Python matrix, plus `publish.yml` (build → PyPI Trusted Publishing → signed GitHub Release) on push to `main`. Root `CLA.md` and `docs/ci-cd.md` added alongside.
- `PipelexAPIClient` (subclass of `mthds`'s `MthdsAPIClient`): Pipelex-branded construction (resolves `PIPELEX_API_KEY` / `PIPELEX_API_URL`, falling back to the `mthds` resolver; token optional for anonymous access; host-only base-URL validation; origin URL for `health`).
- Transport extension layer: `_request_product` (typed `ApiResponseError` mapping, empty-body tolerant, PUT/PATCH/DELETE), `_request_json` (plainer error regime), transport-failure mapping to `ApiUnreachableError`, and the `problem+json` error-body parser.
- Errors: `ApiResponseError` (with the RFC 9457 `code` discriminant) and `ApiUnreachableError`, both deriving from the protocol-base `PipelineRequestError`.
- Durable run lifecycle (`pipelex_sdk/runs.py` + client methods): owned run-lifecycle models (`RunStatus`, `RunRead`, `RunResults`, the discriminated `RunResultState`, `WaitForResultOptions`, `PollInfo`) and the polling surface `get_run_status` / `get_run_result` / `wait_for_result`, mapping the platform's `202`/`503`/`200`/`409` results semantics to a typed union.
- `start_and_wait` self-heals across hosted and bare runners: a cached `GET /v1/version` handshake picks the durable start+poll path on the hosted API and falls back to the blocking `POST /v1/execute` on a bare runner (including the case where a base-only version response hides a missing run store — `start` then surfaces `RunLifecycleUnavailableError` before any run is created). This closes a gap versus `mthds-python` (whose `start_and_wait` raises on a bare runner).
- Lifecycle errors `RunFailedError`, `RunTimeoutError`, `RunLifecycleUnavailableError`; `RunStillRunningError` (the protocol `execute()` 202-degrade error) re-exported from `mthds` so all run/lifecycle errors share one import home.
- Pipelex product surface (`pipelex_sdk/product_models.py` + client methods): the hosted management routes — user profile (`get_me`), methods catalog CRUD (`list_methods` / `get_method` / `create_method` / `update_method` / `delete_method`), organizations (`list_memberships` / `create_organization` / `rename_organization`), billing (`get_subscription` / `list_plans` / `list_invoices` / `create_checkout` / `change_plan` / `get_billing_portal`), Pipelex API keys (`list_pipelex_api_keys` / `create_pipelex_api_key` / `revoke_pipelex_api_key` / `rotate_pipelex_api_key`), the gateway inference key (`create_gateway_api_key` / `get_gateway_api_key`), onboarding (`submit_onboarding`), storage (`resolve_storage_url` / `upload`), and run records (`list_runs` / `update_run`). Documented `409`-conflict behaviors surface through `ApiResponseError.code` (`change_plan` / `get_billing_portal` ⇒ `conflict`; `create_pipelex_api_key` ⇒ `pipelex_api_key_limit_reached`).
- Pipelex validation models (`pipelex_sdk/validation_models.py`): the SDK **owns** the Pipelex-branded narrowing of the `/v1/validate` 200-diagnostic union — `PipelexValidationReport`, `PipelexInvalidReport`, the `PipelexValidationResult` discriminated union + its `PipelexValidationResultAdapter`, and the supporting `ValidationErrorItem` / `ValidationErrorCategory` / `ValidatedPipeEntry` / `DryRunStatus`. These narrow the neutral protocol bases from `mthds.protocol.models`. (They previously lived in `mthds.runners.api.models`; moved here in lockstep with `mthds 0.7.0` to complete the brand boundary — `mthds`'s own `validate()` now returns the neutral `ValidationResult`. Mirrors `pipelex-sdk-js/src/models.ts`; resolves the brand-layering follow-up #9.)
- `validate` override + `validate_files`: the Pipelex-API `/v1/validate` surface — always injects `render: ["markdown"]` (so valid and invalid verdicts both carry `rendered_markdown`), accepts a parallel `mthds_sources` array, and `validate_files` synthesizes deterministic `inline://` source labels when any file carries a URI. Parses the 200 body into the SDK-owned `PipelexValidationResult` via the inherited `_post_validate` transport seam.
- `health()`: the origin-level liveness probe — `GET {origin}/health`, served at the origin (NOT under the `/v1` prefix) and out-of-protocol. Rides the plainer `_request_json` regime (`PipelineRequestError` on a non-2xx, `ApiUnreachableError` on transport failure), not the product `ApiResponseError`.
- `execute` override + `PipelineExecuteTimeoutError`: a blocking `POST /v1/execute` killed by the hosted gateway's ~30s synchronous ceiling (a `503`/`504`, or a client-side request timeout, observed at/after ~28s) is translated into a clear `PipelineExecuteTimeoutError` pointing at the durable start+poll path — closing a JS-parity gap (the inherited base `execute` does not do this). Every other non-2xx keeps the inherited `httpx.HTTPStatusError` regime, and the protocol's 202 async-degrade still raises `RunStillRunningError`.
- `__version__` (`pipelex_sdk.version`), derived from the installed distribution metadata so it cannot drift from the `pyproject.toml` source of truth, with a test asserting the two match.

### Changed

- Requires `mthds>=0.6.1` (protocol base floor).
- `CHANGELOG.md` version headers use the workspace-wide `## [vX.Y.Z]` convention (matching `mthds-python` / `pipelex-sdk-js`), which the changelog/publish workflows key off.

### Fixed

- `PipelexAPIClient` honors an explicit `api_token=""` as a request for anonymous access even when `PIPELEX_API_KEY` (or an mthds credential) is configured. Credential resolution tests `is not None` rather than truthiness, so the first *present* layer wins, restoring the documented "empty string = anonymous" contract and matching the JS SDK's `??` precedence chain.
- `guard-branches.yml` workflow-protection job checks out the PR head (`ref: pull_request.head.sha`) instead of the base branch, so its `git diff` actually detects fork edits to `.github/workflows/*`; it gates trust on the author's **effective repository permission** (resolved via `getCollaboratorPermissionLevel`, treating only `write`/`admin` as trusted, failing closed on non-404 API errors) rather than the spoofable `author_association` label, and drops to least-privilege `permissions: contents: read`. Matches `mthds-python`.
- `tests-check.yml` no longer grants `id-token: write` to the test matrix job, which runs untrusted PR code and never uses OIDC (least privilege).
