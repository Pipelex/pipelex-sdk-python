# pipelex-sdk architecture

The design reference for the package. It covers the shipped surface and records where it stands against the TypeScript `@pipelex/sdk` reference, gaps included (see "Parity with `@pipelex/sdk`" at the end).

## What this is

`pipelex-sdk` (import package `pipelex_sdk`) is the Python client for the Pipelex hosted API. It is the Python counterpart of the TypeScript `@pipelex/sdk` (`PipelexApiClient`), built on top of `mthds` (the `mthds-python` package) exactly as `@pipelex/sdk` is built on the `mthds` npm package.

It is the **hosted superset** of the MTHDS Protocol:

- the five normative MTHDS Protocol routes — `POST /execute`, `POST /start`, `POST /validate`, `GET /models`, `GET /version` — built on the protocol base (`models` / `version` inherited as-is; `execute` / `start` / `validate` lightly overridden for Pipelex-API behaviors documented below);
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

MTHDS is the brand of the open standard (the language, the protocol). Pipelex is the brand of the hosted runtime/product. Artifacts that belong to the standard keep neutral, un-prefixed names; Pipelex branding is reserved for genuinely runtime/product-specific surfaces (the durable run lifecycle, the product routes, implementation envelopes). The five protocol routes and their neutral models stay in `mthds`; everything Pipelex-specific lives here.

The Pipelex narrowing of the `/v1/validate` verdict union is one such implementation envelope and lives here (`pipelex_sdk.validation_models`): `PipelexValidationReport` / `PipelexInvalidReport` / the `PipelexValidationResult` union, plus the supporting `ValidationErrorItem` / `ValidationErrorCategory` / `ValidatedPipeEntry` / `DryRunStatus` / `LiftablePipeEntry` / `SuggestedFix` / the `FixOp` variants / `FixOpKind` / `FixSafety`. They narrow the neutral `ValidationReport` / `InvalidValidationReport` / `ValidationResult` bases that `mthds` keeps (in `mthds.protocol.models`). The report/union types carry the `Pipelex` prefix; the supporting types stay neutrally named — branding the envelope, not the fields inside it. The brand-neutral `Dict*` wire concretes (`DictRunResultExecute` and friends) stay in `mthds` — they are a shared wire contract the `pipelex` runtime itself builds on — and this SDK reuses them by inheritance rather than redefining them (a deliberate divergence from `pipelex-sdk-js`, which duplicates both the `Dict*` and the `Pipelex*` types in its own `models.ts`).

## Credentials & configuration

Resolved at construction time, Pipelex-only — the SDK **never** consults the `mthds` resolver (`MTHDS_API_KEY` / `MTHDS_BASE_URL`, `~/.mthds/config`). That config stores a `(base_url, api_key)` credential pair for whatever runner the vendor-neutral `mthds` tooling targets; borrowing the key while ignoring the URL would send a credential configured for another runner to the hosted API (and `mthds`'s base-URL default, a local bare runner on `http://localhost:8081`, would preempt the hosted default). Both chains match the JS SDK exactly:

- **API key** — `api_key` argument → `PIPELEX_API_KEY` → anonymous. Presence, not truthiness, decides the argument layer, so an explicit empty string (`api_key=""`) is honored as an anonymous request rather than falling through to a configured env key (mirrors the JS SDK's `??` chain).
- **Base URL** — `base_url` argument → `PIPELEX_BASE_URL` → the hosted default `https://api.pipelex.com`. Presence semantics at both layers (also the JS `??` chain): an explicit empty string — a `base_url=""` argument or a set-but-empty `PIPELEX_BASE_URL` (e.g. an unfilled CI secret) — reaches the host-only validator and raises `PipelineRequestError` at construction, rather than silently targeting the hosted default with whatever API key is configured.

A token is **optional** (anonymous access is allowed; protocol routes work against anonymous bare runners, product routes return `401`). The base URL is validated host-only (no path/query/fragment/embedded credentials; http/https only).

`request_timeout_seconds` (constructor argument, default `1200.0` — 20 min) sets the per-instance blocking-execute ceiling the inherited protocol routes (`execute` / `start` / `validate` / `models` / `version`) read; the SDK's own poll and product GETs use the shorter `_POLL_REQUEST_TIMEOUT_SECONDS` instead.

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

Three regimes, ported faithfully from the TS SDK (the inherited-protocol vs product split is decision #5 — deliberately not unified):

- **Product routes** raise a typed `ApiResponseError` (subclass of `PipelineRequestError`) carrying the RFC 9457 `code` discriminant — consumers branch on `err.code` (e.g. `"conflict"`, `"pipelex_api_key_limit_reached"`), never on the HTTP status. It also carries `status`, `status_text`, `response_body`, `error_type`, `server_message`, and `validation_errors`.
- **Transport failures** (DNS/connect/TLS/timeout) raise `ApiUnreachableError` (subclass of `PipelineRequestError`) with `api_url` and `code`.
- **`health` / `_request_json`** raise the plainer `PipelineRequestError` on a non-2xx response. **(Checkpoint-5 decision: kept, not unified.)** Liveness is a binary up/down probe that needs no `code` taxonomy, and this already matches the JS `health` regime — bringing it under `ApiResponseError` would be over-engineering and a JS divergence. (Python's `PipelineRequestError` is already a typed improvement over the JS plain `Error`.)
- **Inherited protocol routes** (`execute` / `start` / `validate` / `models` / `version`) keep the base `mthds` `raise_for_status()` → `httpx.HTTPStatusError` behavior. The one typed addition is `PipelineExecuteTimeoutError` (below), raised by `execute` for the hosted gateway's synchronous cut-off.

## `execute` override (hosted gateway-timeout translation)

The protocol `execute` is **overridden** to add one Pipelex-API behavior the bare protocol route doesn't carry, while keeping the inherited error regime for everything else. Behind the hosted gateway, a synchronous request is cut off at ~30s; a blocking `execute` that exceeds that comes back as a gateway `503`/`504` (or, less commonly, a client-side request timeout). The override times the call and, when such a failure is observed **at or after ~28s elapsed**, translates it into a clear `PipelineExecuteTimeoutError` whose message points the caller at the durable start+poll path. The ~28s threshold guards against mislabeling a *fast* `503` (the runner genuinely down) as a timeout. This closes a JS-parity gap — the inherited base `execute` does no such translation (the Phase-1 flag, resolved at Checkpoint 5 by **implementing** it rather than deferring).

Everything else stays the inherited regime: the protocol's optional 202 async-degrade still raises `RunStillRunningError` (from the base `execute`), and every other non-2xx keeps `httpx.HTTPStatusError` — consistent with the other inherited protocol routes (decision #5). The `_execute_blocking` bare-runner fallback (below) calls this same overridden `execute`, so it inherits the translation; off-platform there is no gateway cap, so the `503/504`-after-28s condition effectively never fires there.

The override also **enriches the return type**: it re-validates the base result into a `PipelexExecuteResult` (`pipelex_sdk/execute_result.py`), a `DictRunResultExecute` subtype that adds a resolved `.main_stuff` accessor (dug out of `pipe_output`'s working memory via the response's `main_stuff_name`, raising `MissingMainStuffError` if unlocatable). This gives blocking and durable results the **same output accessor** — `result.main_stuff` — so callers never branch on which path ran. `_map_run_result_to_run_results` reads that accessor too, keeping the resolution single-sourced.

## Hosted run extensions (`method_id`)

The protocol's run args (`pipe_code`, `mthds_contents`, `inputs`, …) are the base client's named parameters and stay pure. The hosted API's *own* run args are named parameters **here**, on `execute` / `start` / `start_and_wait`. Today that is one, `method_id`, and the reasoning generalizes to every hosted-only argument that follows:

- **A named parameter, not an `extra` entry.** Typing its own platform's arguments is the one job a layer-3 client exists to do; `extra` stays the escape hatch for an extension this client does not know about (a third-party server, or a newer version of ours). That split, and the reserved-key guard behind it, follow the layered extension policy: a hosted client types its own platform's arguments and guards them per layer, and that guard must never be pushed down into the protocol package. It was previously passed as `extra={"method_id": …}`, which worked but documented nothing and validated nothing.
- **It reaches the wire through the base client's extension mechanism.** `_merge_hosted_run_extensions` folds it into the `extra` mapping handed to `super().execute` / `super().start`, which merges it into the body as a top-level property without knowing what it means. That is the layering working as designed, not a workaround: the protocol client stays catalog-agnostic while the hosted client owns the concept.
- **The guard is per layer.** `method_id` is rejected inside `extra` *here*, and must never become reserved in `mthds`: a protocol client talking to another vendor's server has no business rejecting that vendor's arguments.
- **Pure pass-through — nothing is expanded client-side.** The platform resolves the id against the org's catalog. Do not confuse this with `build_inputs(method_id=…)` and `prepare_inputs`, which are client-side *sugar* that resolves an id to inline files before the request is built, so no `method_id` ever reaches those wire bodies.
- **Alone it is a run source; alongside an inline source it is linkage.** With no inline source the platform resolves and runs the stored method. With one, the inline source is what RUNS (precedence) and the id is recorded as run-history linkage on the Run row — the index key `GET /v1/runs?method_id=` queries. The base client's "something to run" precondition is satisfied because the merged extension mapping is non-empty, so a `method_id`-only run is sent rather than refused client-side.
- **It rides the blocking fallback too.** `start_and_wait` degrades to `POST /v1/execute` against a runner with no run store, and the selector goes with it — so a bare `pipelex-api` answers the `422` that names the key instead of the client silently dropping it and running something else.
- **An empty string is treated as absent.** `method_id=""` selects no method and links nothing, so it is not sent and does not satisfy the precondition.
- **A non-string raises at the boundary.** A published client validates its request-option types where it names them, raising `PipelineRequestError` rather than dropping or forwarding a wrong-typed value — so one wrong value gets one answer. Without the guard the partition is arbitrary: a bare truthiness check drops the falsy wrong types (`0`, `[]`) and forwards the truthy ones (`123`, `["mt_1"]`) to a server `422`, which is a *different* partition than `@pipelex/sdk` makes for the same argument on the same wire. Both SDKs now make the same one.

## Run lifecycle (hosted extension)

The durable run lifecycle (`pipelex_sdk/runs.py` + the client's lifecycle methods) is a **hosted-API extension, not part of the MTHDS Protocol**. Long method runs outlive the hosted gateway's ~30s synchronous cap, so a caller submits a run (`POST /v1/start`), then polls a self-healing endpoint by bare `pipeline_run_id` until it reaches a terminal state. All state lives behind the id (DynamoDB + Temporal on the platform), so a caller can drop the poll loop and resume later with just the id. A bare runner has no run store and `404`s these routes, which the client translates into a clear `RunLifecycleUnavailableError`.

### Owned types (brand boundary)

`pipelex_sdk/runs.py` **owns** the lifecycle models — they are a Pipelex-branded surface, mirroring `pipelex-sdk-js/src/runs.ts`. They are not imported from `mthds`. During the transition the same shapes still exist in `mthds-python`; that duplication is deliberate (so this SDK is correct regardless of `mthds-python`'s state) and is removed from `mthds-python` later. While the base `MthdsAPIClient` still declares the same lifecycle methods, the client's overrides return this package's own types and so read as incompatible overrides to the type-checker — they carry a narrow `# type: ignore[override]`, which becomes unnecessary (harmless) once the base copies are stripped.

- `RunStatus` — the hosted status enum, with `is_terminal` / `is_success` predicates (exhaustive `match`).
- `RunRead` — a run record read through the self-healing status path (adds `degraded` + `retry_after_seconds`).
- `RunResults` — result artifacts. `main_stuff` (the resolved main output content) is always present for a completed run: on the hosted path it is the `main_stuff.json` S3 artifact; on the bare-runner blocking path the SDK resolves it from the returned working memory via the response's `main_stuff_name`, so both paths deliver the same shape. Consumers read `main_stuff` directly. The full working memory still rides `pipe_output` (blocking path only) for consumers that want it, and `graph_spec` rides the hosted path. A completed run that cannot deliver a main stuff raises `MissingMainStuffError`. Extension-open, so any other server artifact is preserved.
- `TokensUsageRecord` — one client-facing usage record per inference call, carried by `RunResults.tokens_usages` on both paths. A mirror of the wire contract specified in the MTHDS protocol spec, not a shape this SDK owns: every field is optional and the model is extension-open so pre-contract artifacts (relayed verbatim, never migrated) still parse. See [`run-usage.md`](./run-usage.md) for the field reference, the cost/null semantics, and the old-artifact rules.
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
- **Bare runner:** the blocking `POST /v1/execute` (`_execute_blocking`), which has no gateway cap off-platform and returns the native `pipe_output`, mapped onto `RunResults` — the SDK resolves `main_stuff` out of the working memory via the response's `main_stuff_name` and keeps the full working memory on `pipe_output`.
- **Self-heal:** a runner can look hosted yet lack the durable routes (`implementation` is an optional extension a compliant bare runner may omit). The client's `start` override translates a bare-runner missing-route `404` into `RunLifecycleUnavailableError` — raised **before any run is created** — so `start_and_wait` falls back to the blocking path without risking a double-run, and caches the negative so later calls skip the durable attempt.

### Run/lifecycle errors

`RunFailedError`, `RunTimeoutError`, and `RunLifecycleUnavailableError` are owned in `pipelex_sdk/errors.py`. `RunStillRunningError` — the protocol `execute()` 202-degrade error — stays owned by `mthds` and is re-exported from `pipelex_sdk/errors.py` so consumers have a single import home for all run/lifecycle errors.

## `validate` override (Pipelex-API presentation + sources)

The protocol `validate` is **overridden** (not inherited) to add the two Pipelex-API extensions the bare protocol route doesn't carry, while keeping the inherited protocol error regime (a no-verdict non-2xx surfaces as `httpx.HTTPStatusError`, not `ApiResponseError` — the verdict itself is always a 200 discriminated on `is_valid`):

- **Markdown render is always injected.** `validate(...)` adds `"markdown"` to the `render` list (de-duplicated, caller tokens first) so both a valid `PipelexValidationReport` and a produced `PipelexInvalidReport` carry `rendered_markdown`. Unknown render tokens are server-side lenient-ignored.
- **`mthds_sources`** is a named parameter (parallel to `mthds_contents`) threaded onto each diagnostic's `source`; sent only when provided.
- **`views`** is the opt-in for the server's structured views, on both `validate` and `validate_files`. `input_form` — named by the `VALIDATION_VIEW_INPUT_FORM` constant — is the only token today. Unlike `render`, the list travels **verbatim**: nothing is injected, nothing is de-duplicated, and an explicit `[]` is sent as `[]`, because the server resolves the tokens as a set and lenient-ignores the ones it does not know (never a `422`), so client-side normalization would only hide what the caller asked for. Left at `None` the key is not sent at all, which is what keeps the default response byte-identical for the consumers that discard views (hook pipelines, CI gates, agent loops). The constant is a constant rather than a closed enum on purpose: the request boundary is deliberately open, so a stale token never fails a call.
- **`validate_files(files, …)`** takes `MthdsFile(content, uri?)` records. When any file carries a URI, every content gets a parallel source label — the named file's URI, or a deterministic `inline://file-N.mthds` for an unnamed sibling — so the server never sees a length-mismatched `mthds_sources`.

The override reuses the inherited base transport seam `_post_validate` (which builds the body — passing `render` / `mthds_sources` through the protocol's `extra` extension passthrough — sends the request, and raises on a no-verdict non-2xx), then parses the raw 200 body into this SDK's own `PipelexValidationResult` via `PipelexValidationResultAdapter`. Body-building and transport stay shared with the base; only the Pipelex presentation/sources concerns and the branded narrowing live here. The validation models (`PipelexValidationResult` = `PipelexValidationReport | PipelexInvalidReport`, with `rendered_markdown`) are **owned by this SDK** (`pipelex_sdk.validation_models`); they narrow `mthds`'s neutral verdict bases, completing the brand boundary (the resolved follow-up #9). The base `MthdsAPIClient.validate()` returns the neutral `ValidationResult` instead.

**Checkpoint-5 decision (validate error regime):** the delegation keeps the inherited `httpx.HTTPStatusError` regime on a *no-verdict* non-2xx, where the JS `validate` raises `ApiResponseError`. Kept as-is (deferred parity), because in both SDKs `validate`'s error regime matches the *other* protocol routes of that SDK — JS routes all raise `ApiResponseError`, Python protocol routes all inherit `httpx.HTTPStatusError` (decision #5). Making Python's `validate` alone raise `ApiResponseError` would make it inconsistent with `execute`/`start`/`models`/`version`, which is worse than the JS divergence. The verdict itself (valid/invalid) is always a 200 either way — only the no-verdict failure *presentation* differs.

**What the report carries (0.17+).** A valid `PipelexValidationReport` adds three typed fields. `warnings: list[ValidationErrorItem]` are advisory lints on a bundle that is nonetheless valid — the same item type as `validation_errors[]`, so one parser serves both channels, but they never flip `is_valid` (this is where the `hint_*` error types ride). `liftable_pipes: list[LiftablePipeEntry]` inventories the pipes the runtime may skip when an optional slot resolves absent. `input_form: dict[str, Any] | None` carries the per-pipe input-form descriptors, keyed exactly like `pipe_io_contracts`; it is present only when the request named the `input_form` view, and it is kept **opaque** for the same reason as `bundle_blueprint` / `pipe_io_contracts` / `graph_spec` — the descriptor vocabulary is owned elsewhere and a second copy here would be free to drift. The two lists default empty and `input_form` defaults `None`, so a body from an older runner still parses; the empty default is also what a clean bundle yields, so no caller can tell the two apart. `PipelexInvalidReport` gains none of them: `warnings` and `input_form` derive from a crate that was never assembled.

`ValidationErrorItem` gains `missing_pipe_code` (symmetrical with `missing_concept_code`) and `suggested_fix: SuggestedFix | None` — a deterministic repair proposal with a `fix_code`, a `description`, a `FixSafety` (`safe` / `unsafe`, with an `is_safe` property), an optional `source`, and `ops`: a list discriminated on `kind` over the closed `FixOpKind` vocabulary (`set_key`, `ensure_table`, `delete_key`, `delete_table`, `rename_table_key`, `move_key`, `remap_value`), narrowed with an exhaustive `match op: case SetKeyOp(): …`. The ops are **reader** models here: `extra="allow"`, no `frozen`, none of the runtime's wildcard-refusing validators, because this SDK only reads fixes where the runtime plans them. A `kind` this SDK does not know fails the whole verdict parse, deliberately and consistently with `ValidationErrorCategory`. `error_type` stays an open `str`: the runtime union keeps gaining advisory members, and closing it here would turn every runtime addition into an SDK break.

One movement in the same release reaches no typed field here and is worth knowing anyway, because it changes what a consumer reads out of the opaque dicts: `PipeInputContract.optional` became `presence`, and the `fixed` multiplicity now carries an `item_count`. Both live inside `pipe_io_contracts`, which this SDK carries as `dict[str, Any]` on purpose — nobody should discover the new spellings by surprise.

## Pipelex product surface (hosted management routes)

The hosted catalog/account routes the webapp drives (`pipelex_sdk/product_models.py` + the client's product methods). Every route rides the same `{base}/v1/*` surface, `Authorization: Bearer`, org-from-JWT contract as the protocol routes, and goes through `_request_product`, which maps a non-2xx `problem+json` to a typed `ApiResponseError` — **consumers branch on `.code`, never the HTTP status**.

The wire models are snake_case Pydantic v2. Response models are extension-open (`extra="allow"`) so a newly-added server field is preserved, not rejected; input models name exactly what each route accepts. `PipelineRun.status` reuses the run-lifecycle `RunStatus`; `OrgRole`, `PipeStatus`, and the onboarding fields are `StrEnum`s.

- **User profile** — `get_me()` → `UserProfile` (`GET /v1/me`).
- **Methods catalog** — `list_methods()` / `iterate_methods()` / `get_method(id)` / `create_method(MethodWriteInput)` / `update_method(id, MethodWriteInput)` (a rename is a changed `name`) / `delete_method(id)`. The id is path-encoded; an absent `input_data` is dropped from the write body.

  **The index is paged.** `list_methods(q=…, limit=…, cursor=…)` returns one `MethodPage` — `items: list[MethodSummary]` plus an opaque `next_cursor` you pass straight back, `None` on the last page, and no total by design. A `MethodSummary` is deliberately **not** a `MethodData`: no `mthds`, no `python`, no `updated_at`, because none of them is in the index projection, and putting `mthds` back is exactly what restored the truncation bug paging removed. A method mid-deletion still appears in the list — so a UI can render "Deleting…" — while `get_method` refuses it with a `409`. Query parameters are kept on **presence**, never truthiness: an explicit empty `q` or `cursor` is bad input the API should reject, not something to drop silently into an unfiltered query that reads as working.

  **`iterate_methods(q=…, limit=…)` is the idiom for the whole catalog**, and there is deliberately no `list_all_methods()`: an all-at-once helper needs a cap, and a cap is the silent truncation paging removed. It **keeps going through an empty page that carries a live cursor**, because `q` is a post-read filter over a bounded index slice per request, so `{items: [], next_cursor: "…"}` means "keep going". It stops on a `None` cursor, and also when the server hands back the cursor it was just sent — checked *before* yielding, so a stuck cursor never double-counts a page. Past a runaway backstop set far beyond any real catalog it **raises** `PagingNotTerminatingError` rather than returning what it has, because a silently truncated list is the bug being fixed.

  **`MethodData.python` is a typed `list[MethodFile]`, converted at the boundary.** On the wire it is one string: the JSON text of a `[{name, content}]` array, or `""` for a method with no custom Python. `parse_method_files` / `serialize_method_files` (in `product_models.py`) carry the rules — a blank source or `"[]"` yields `[]`, blank-content entries are dropped in both directions, and an empty list serializes to `""` rather than `"[]"` because `""` is the platform's clear sentinel. `MethodData` applies the parser through a `field_validator(mode="before")` and `MethodWriteInput` the serializer through a `field_serializer`, which is what makes the platform's three-way write contract fall out of `exclude_none=True`: **`None` → key absent → the stored Python is preserved; `[]` → `""` → cleared; a non-empty list → replaced.** `MethodFile` is distinct from `MthdsFile` (the validate input) and `MthdsFileItem` (the build closure) — three shapes for three surfaces.

  **`delete_method` is asynchronous and its return type says so:** the route answers `202` with a `MethodDeletionAccepted` (`method_id`, `deletion_state`, `deletion_job_id`) the moment the platform has claimed the method and terminated its in-flight workflows — the rest of the cascade (runs, events, S3 objects) is enqueued. A returned value means "accepted", never "gone"; completion is the row disappearing from `list_methods`, not any field of that body. It used to be annotated `-> None` with a docstring promising an empty synchronous delete, which is a misleading contract around a destructive operation. The claim is a conditional write, so a double-clicked delete is a `409 conflict` rather than a second cascade over the same runs.
- **Organizations** — `list_memberships()` → `MembershipsResponse` (memberships + active-org feature flags); `create_organization(name)` / `rename_organization(org_id, name)` → `Membership`. Organization *switch* is out of scope (a WorkOS session op, not a `/v1` route).
- **Billing** — `get_subscription()`, `list_plans()`, `list_invoices()`, `create_checkout(plan)`. `change_plan(plan)` and `get_billing_portal()` surface a **409 `conflict`** (`ApiResponseError.code`) when there is no subscription yet — start one via `create_checkout` first.
- **Pipelex API keys** — `list_pipelex_api_keys()`; `create_pipelex_api_key(label)` and `rotate_pipelex_api_key(id)` return the plaintext `api_key` **once**; `revoke_pipelex_api_key(id)`. Creation surfaces a **409 `pipelex_api_key_limit_reached`** when the per-account limit is hit. Rotation sends no body.
- **Gateway (LLM inference) key** — `create_gateway_api_key(promo_code)` **always sends a JSON body** (even with `promo_code=None` → `{"promo_code": null}`); the server 422s an empty body. `get_gateway_api_key()` → status (`gateway_api_key` is `None` until provisioned).
- **Onboarding** — `submit_onboarding(OnboardingSubmission)` (`POST /v1/onboarding/submit`, empty 2xx body); absent optional fields are dropped.
- **Storage** — `resolve_storage_url(uri)` → presigned URL; `upload(UploadInput)` → the stored file handle. The higher-level `upload_file` / `prepare_inputs` preparation surface built on top of `upload` is now available — see [input-preparation.md](./input-preparation.md).
- **Run records** — `list_runs(method_id, …)` / `iterate_runs(method_id, …)` / `get_run_detail(run_id)` (the catalog-style reads, distinct from the lifecycle status/result routes); `update_run(run_id, UpdateRunInput)` (admin/manual status patch, empty 2xx body).

  **This list is paged too.** `list_runs(method_id, created_from=…, created_to=…, limit=…, cursor=…)` returns a `RunPage` with the same opaque-cursor contract as `MethodPage`. `created_from` / `created_to` are **instants** — ISO-8601 with a UTC offset — and inclusive; they are index key conditions rather than filters, so a bare date or a naive timestamp is a platform `400` surfaced as `ApiResponseError`. Worth knowing and not obvious from the route: every `/v1/runs*` product route sits behind the platform's surface-access gate, which for API-key auth demands the `ff_api_keys` feature flag and fails closed with a `403` — so a `403` here means "flag", not "wrong key".

  **The two iterators stop on different signals, and the difference is in the server.** `iterate_methods` continues through an empty page with a live cursor; `iterate_runs` treats an **empty page as the end**, because the run date bounds are index key conditions and so a run page is never empty-with-a-cursor. That stop rule also removes the need for a page ceiling on the run side — a server minting fresh cursors while returning nothing is already caught by it.

  **`PipelineRun` fields the platform genuinely serves as null are typed nullable.** `method_id` is `None` for an ad-hoc run from an inline bundle, which belongs to no stored method; `pipe_code` is `None` for a run that let the bundle's `main_pipe` decide. `org_id`, `created_by_user_id`, and a narrowed `error: RunErrorReport | None` (`message`, `error_type` — the two fields a consumer may rely on out of the runner's verbose report) join them. `RunDetail`, returned only by `get_run_detail`, adds `mthds_contents` and `inputs`: what the run actually executed, and the only record of it, since a method edited since the run no longer describes what happened. Both are left out of the list and the polled status on purpose — their cost scales with page size and poll rate respectively.

## Health probe

`health()` → `GET {origin}/health`. The one route served at the **origin**, NOT under the `/v1` prefix — the origin is derived from the base URL (`_origin_of`, exposed as `self.origin_url`), so a base URL of `https://api.pipelex.com/v1/...` still probes `https://api.pipelex.com/health`. It is **out-of-protocol**: the MTHDS Protocol defines no health route, and `/health` is neither a protocol nor a product surface. It rides `_request_json` (the plainer regime), so a non-2xx raises `PipelineRequestError` rather than the product `ApiResponseError` — liveness needs no `code` taxonomy. Transport failures still map to `ApiUnreachableError`. (Checkpoint-5 decision: kept the plainer regime — see "Error regimes" above.)

## Out of scope for v0.1

- The remaining `/v1/build/*` authoring helpers — `build_output`, `build_runner`, `concept`, `pipe_spec`. `build_inputs` shipped in 0.5.0 and is no longer deferred.
- Organization *switch* (a WorkOS session operation, not a `/v1` route).
- A `~/.pipelex/config` file reader (env-only for now, matching the JS SDK).
- A synchronous client facade.

## Versioning

`__version__` (`pipelex_sdk.version`) is read from the installed distribution metadata via `importlib.metadata`, so there is no hardcoded constant to drift from the `pyproject.toml` source of truth — the analogue of the JS SDK's `SDK_VERSION` guard, but with nothing to keep in sync by hand. `tests/unit/test_version.py` asserts the resolved value matches the version declared in `pyproject.toml`, catching a stale install (e.g. an editable tree whose metadata was not refreshed after a bump).

## Parity with `@pipelex/sdk`

This SDK is a port of the TypeScript `@pipelex/sdk` (`PipelexApiClient`) and tracks it closely, but it is **not surface-complete, and this section is where the gaps are named.** The Checkpoint-5 parity audit walked the JS `src/client.ts`, `src/index.ts` (the public barrel), and `docs/architecture.md`, plus a field-by-field sweep of `runs.ts` / `product-models.ts` / `models.ts`; it concluded surface-completeness, and that conclusion went stale as the JS SDK grew. The honest list of what has no Python counterpart today:

- **Tooling routes** — `lint`, `format`, `resolve`, `codegen`.
- **Authoring helpers** — `build_output`, `build_runner`, `concept`, `pipe_spec` (`build_inputs` shipped in 0.5.0 and is **not** a gap).
- **Offline helpers** — `run_codegen_check` (the codegen drift check) and `get_method_closure` (client-side sugar that parses the polymorphic `mthds` source into a run-ready closure).

None of them is moved by the releases this SDK last tracked, and each stays deferred rather than silently missing. Everything else — the protocol routes, the durable lifecycle, the whole product surface, and the errors — does have a Python equivalent.

**Methods** — everything outside the gap list above has a counterpart: protocol (`execute`, `start`, `validate`, `validate_files`, `models`, `version`), durable lifecycle (`get_run_status`, `get_run_result`, `wait_for_result`, `start_and_wait`, the private `_supports_run_lifecycle` / `_execute_blocking`), the whole product surface (profile, methods CRUD with paged listing and the two iterators, organizations, billing, Pipelex API keys, gateway key, onboarding, storage, run records with `get_run_detail`), `build_inputs`, the input-preparation surface (`upload_file` / `prepare_inputs`), and `health`.

**Models** — field-for-field across the run-lifecycle types and the product wire models. Deliberate idiomatic ports (not gaps): milliseconds → seconds (`interval_seconds` / `timeout_seconds` / `elapsed_seconds`); the JS `AbortSignal` → Python `asyncio` cancellation (no `signal` field); JS inline string-unions promoted to `StrEnum`s (`OrgRole`, `PipeStatus`, the onboarding fields) with identical wire values; response models are `extra="allow"` for forward-compat. The Pipelex validation narrowing is **owned here** (`pipelex_sdk.validation_models`), narrowing `mthds`'s neutral verdict bases (the resolved follow-up #9); the brand-neutral `Dict*` wire concretes (`DictRunResultExecute`) are reused from `mthds` by inheritance — they are a shared wire contract the `pipelex` runtime also builds on — rather than duplicated as `pipelex-sdk-js` does. Two divergences worth naming: the page envelopes keep the wire's snake_case `next_cursor`, where the JS mirror renamed it `nextCursor` for its own consumers; and the method-files catalog converter (`parse_method_files` / `serialize_method_files`) lives in this package, where the JS pair lives in `mthds-js` because `pipelex-mcp` consumes the same format and wanted one owner. There is no second Python consumer, and the catalog serialization is a Pipelex product concern rather than an MTHDS protocol one, so this SDK is a proper home for it. If `mthds-python` ever grows an owner for the format, this SDK adopts it then.

**Errors** — `ApiResponseError`, `ApiUnreachableError`, `PipelineExecuteTimeoutError`, `RunFailedError`, `RunTimeoutError`, `RunLifecycleUnavailableError`, `PagingNotTerminatingError` are owned here; `RunStillRunningError` is re-exported from `mthds`. `ClientAuthenticationError` is **not** ported: it is a dormant export in the JS barrel (defined and exported but never raised by the client), and in Python it already lives in `mthds.runners.api.exceptions` — importable directly if ever needed, with no barrel here to re-export it through.

**Resolved parity flags (Checkpoint 5):**

- **`PipelineExecuteTimeoutError` / `execute` gateway-timeout translation** (Phase-1 flag) — **implemented** (see "`execute` override" above), closing the one genuine feature gap rather than deferring it.
- **`health` error regime** (decision #5) — **kept** the plainer `PipelineRequestError` regime; already matches JS, needs no `code` taxonomy.
- **`validate` error regime** (Phase-3 flag) — **deferred**; keeps the inherited `httpx.HTTPStatusError` regime for consistency with the other Python protocol routes (see "`validate` override" above).

**Conscious exclusions:** the surfaces listed at the top of this section, and the organization *switch* (a WorkOS session op, not a `/v1` route).

**Intentional divergences from the JS SDK** (Python house style / clean inheritance): no barrel (`__init__.py` stays empty; import via full paths); inheritance on `MthdsAPIClient` rather than the JS composition-of-types; async-only; `__version__` derived from installed metadata rather than a hand-synced constant.
