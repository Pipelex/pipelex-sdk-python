# Changelog

## [v0.5.0] - 2026-07-22

### Added

- **Input preparation: `upload_file` and `prepare_inputs` (hosted upload capability).** The Python counterpart of `@pipelex/sdk`'s `uploadFile` / `prepareInputs`, in parity. `client.upload_file(source, *, filename=None, content_type=None)` uploads one local asset — a filesystem path (`str`/`Path`) or raw `bytes` — and returns an `UploadRecord` (`uri`, `content_type`, `size`, `filename`) assembled client-side. `client.prepare_inputs(*, files, pipe_ref=None, inputs)` resolves the target pipe's declared signature from the explicit inputs template, interprets the caller's compact `inputs` top-down against it (the file signal is the canonical Image/Document content shape — a `{"url": …}` dict — mirroring the runtime's `input_normalizer`), uploads the file-bearing values, and returns `PreparedInputs`: a copy-on-write rewrite of `inputs` with each asset reference replaced by canonical content carrying `pipelex-storage://` in `url`, plus one `UploadRecord` per prepared asset. HTTP(S) URLs and existing `pipelex-storage://` URIs pass through unchanged; data URLs and local/byte sources are uploaded; the same source referenced twice uploads once (dedup by source identity); all failures are raised before any run is created. Failures are typed per category: `InvalidLocalSourceError`, `RejectedAssetError`, `UnsupportedUploadCapabilityError`, `UploadAuthenticationError`, `UploadTransportError` (all extend `InputPreparationError`). `prepare_inputs` takes the method closure as inline `files`; catalog `method_id` resolution and opt-in `http(s)` ingest are deferred and additive. See [`docs/input-preparation.md`](./docs/input-preparation.md).
- **`build_inputs` route (`POST /v1/build/inputs`).** Closes the `/v1/build/*` parity gap the Python SDK had — `client.build_inputs(BuildInputsRequest)` projects a pipe's declared inputs as a fill-in template, returning a 200 verdict discriminated on `is_valid` (`BuildInputsValidReport` | `CrateInvalidReport`); a no-verdict condition throws `ApiResponseError`. It is the signature source `prepare_inputs` reads (with `explicit=True`). Models live in `pipelex_sdk/build_models.py`.
- **Typed run usage: `RunResults.tokens_usages` + `RunResults.usage_assembly_error`.** The per-call usage records a run produces — token counts by category, the server-computed `cost` in USD, model name and id, the pipe that made the call, job-kind fields and timing, for LLM and img-gen/extract/search calls alike — are now first-class typed fields instead of riding `model_extra`. Records validate into a new `TokensUsageRecord` model (`pipelex_sdk/runs.py`) mirroring the wire contract specified in the MTHDS protocol spec. Both paths populate the pair: the hosted durable path reads it off `GET /v1/runs/{id}/results` (which unpacks the runner's `tokens_usages.json` artifact), and the blocking fallback lifts the same pair out of the execute response's extension-open `pipe_output` — so `result.tokens_usages` reads the same regardless of which path ran.

  Note that the rate table (`unit_costs`) no longer crosses the wire: a record now carries the computed `cost` for the call instead, which is `None` when the model has no rate table at all (own-GPU, mock, dry run) and `0` when a rate table priced it at zero. There is no run-level aggregate — sum the records.

  `tokens_usages` is `None` whenever usage assembly produced no list (it was off, it broke, or the run was delivered before the artifact existed) and `[]` when assembly ran and no inference happened; `usage_assembly_error` is the only field separating a broken assembly from an off one. `TokensUsageRecord` keeps every field optional and is extension-open, so durable artifacts written before the contract shipped — relayed verbatim, never migrated — still parse: `cost` and `pipe_code` come back `None`, and the legacy `job_metadata` / `unit_costs` survive in `model_extra`. Enum-ish fields (`model_type`, `job_category`, `unit_job_id`) are open sets typed as plain `str`, so runtime enum churn stays non-breaking.

### Changed

- **`RunResults.pipe_output` is now `DictPipeOutputAbstract | None`, was `dict[str, Any] | None` (breaking).** The blocking path already parsed the protocol model and then threw the types away with a `.model_dump()` round-trip; it now carries the parsed model straight through. Read the working memory as attributes — `result.pipe_output.working_memory.root["out"].content` — rather than nested dict keys. The durable path still leaves it `None`.

## [v0.4.0] - 2026-07-06

### Changed

- **Dropped Python 3.10 support (breaking).** The minimum supported Python is now 3.11, matching the `mthds>=0.8.1` base. `requires-python` is now `>=3.11,<3.15`, and the 3.10 leg is removed from the CI lint/test matrices. Deleted the `pipelex_sdk._compat` StrEnum/`Self` backport shim and dropped the `backports.strenum` dependency (and its mypy override) — `StrEnum` now imports directly from the stdlib `enum`. *(Migration: run on Python 3.11 or newer.)*
- Bumped the `mthds` dependency from `>=0.7.1` to `>=0.8.1`.
- Bumped the dev-only `pytest` constraint to `>=9.0.3` (from `>=8.0.0,<9.0.0`) and moved its `pyproject.toml` config from `[tool.pytest.ini_options]` to the newer `[tool.pytest]` table with `minversion = "9.0"`, matching `mthds-python`.

## [v0.3.0] - 2026-07-05

### Changed

- **One output accessor across both execution modes: `result.main_stuff` (Breaking).** Reading a run's output no longer depends on which path ran. Both the durable result (`RunResults`, from `wait_for_result` / `start_and_wait`) and the blocking result (now a `PipelexExecuteResult`, from `execute`) expose a resolved, non-null `.main_stuff` — so a caller writes `result = await client.<run>(...); output = result.main_stuff` uniformly, with no `main_stuff or pipe_output` fallback and no working-memory spelunking.
  - `RunResults.main_stuff` is now **required and non-null** for a completed run (was `Any = None`). On the hosted path it is the `main_stuff.json` S3 artifact; on the blocking path the SDK resolves it out of the returned working memory via the response's `main_stuff_name` extension field. The full working memory still rides `pipe_output` (blocking path only) for consumers that want it.
  - `execute()` now returns a `PipelexExecuteResult` — the protocol's raw execute response enriched with the same resolved `.main_stuff` accessor. It remains a `DictRunResultExecute` subtype, so existing field access (`pipeline_run_id`, `pipe_output`) is unchanged.
  - *(Migration: read `result.main_stuff` instead of digging through `pipe_output` / falling back from `main_stuff` to `pipe_output`.)*

### Added

- **`MissingMainStuffError`.** A completed run that cannot deliver a main stuff now raises this typed error (derives from `PipelineRequestError`, carries `run_id`) instead of silently yielding a null output: the hosted results endpoint answered a `200` that omits `main_stuff` or sends it null, or a blocking `execute` response named a `main_stuff_name` absent from its working-memory root. The results path checks the decoded payload before validating into the now-required `RunResults` model, so an omitted key surfaces as `MissingMainStuffError` rather than a raw Pydantic validation error. A falsy-but-present main stuff (empty list, `0`) is a valid output and does not raise.

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
