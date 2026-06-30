# Changelog

All notable changes to `pipelex-sdk` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GitHub Actions CI/CD mirroring `mthds-python`, adapted to the `Pipelex` org and the `pipelex-sdk` PyPI distribution: PR gates (`lint-check`, `tests-check`, `package-check`, `changelog-check`, `version-check`, `guard-branches`, `cla`) across the full Python matrix, plus `publish.yml` (build → PyPI Trusted Publishing → signed GitHub Release) on push to `main`. Root `CLA.md` and `docs/ci-cd.md` added alongside.

### Changed

- `CHANGELOG.md` version headers now use the workspace-wide `## [vX.Y.Z]` convention (matching `mthds-python` / `pipelex-sdk-js`), which the changelog/publish workflows key off.

## [v0.1.0] - 2026-06-30

The initial public surface of `pipelex-sdk` — the Python counterpart of `@pipelex/sdk`, built by inheritance on the `mthds` protocol base. Surface-complete against the TypeScript SDK (see `docs/architecture.md` → "Parity with `@pipelex/sdk`"); the `/v1/build/*` helpers and the WorkOS org-switch are consciously out of scope for this release.

### Added

- Initial repository scaffold: packaging (`pyproject.toml`), tooling (`Makefile`, ruff/pyright/mypy/pylint config mirroring `mthds-python`), and the empty `pipelex_sdk` package.
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
