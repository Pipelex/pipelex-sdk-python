# Updates warranted by pipelex-api 0.17.0 / 0.18.0, the pipelex-server bump, and `@pipelex/sdk` 0.14.0

A design for what this repo still owes after the three sources named in the title, written on the `feature/Typed-method-id-run-option` branch, which already carries the typed `method_id` run option and the honest `delete_method` contract. Every claim below was checked against the code it names; line numbers are as of 2026-08-25 and will drift.

## Verdict

Yes — three groups of work, in decreasing order of what the cited releases actually ask for:

1. **The `/v1/validate` surface moved, and this SDK has not followed.** pipelex-api 0.17.0 (via the `pipelex` 0.52.0 pin) added `warnings`, `liftable_pipes`, `input_form`, `missing_pipe_code` and `suggested_fix` to the report; 0.18.0 gated `input_form` behind a new `views` request list. `@pipelex/sdk` 0.14.0 mirrored all of it. Here, nothing crashes — every affected model is `extra="allow"`, so the new fields ride `model_extra` — but nothing is typed either, and there is no way to ask for the `input_form` view at all. This is the direct answer to the question and is purely additive. See §1.
2. **Two documentation corrections `@pipelex/sdk` 0.14.0 made apply verbatim here**: the `TokensUsageRecord` brand attribution, and citations of workspace-private paths from a public package. See §2.
3. **Found while checking, and more urgent than either: `list_methods` and `list_runs` crash against the deployed platform.** The platform reshaped both routes into `{items, next_cursor}` page envelopes (in prod since 2026-08-18 and 2026-08-11 respectively); this SDK still iterates a bare array, and its unit tests mock the old shape, which is why the suite is green. `PipelineRun` also declares `method_id` and `pipe_code` as required strings where the platform serves `str | None`. `@pipelex/sdk` fixed all of this in 0.10.0 / 0.11.0. This is a breaking fix and it is not optional. See §3.

A fourth item is a decision already taken rather than a release to mirror: the 2026-08-25 boundary-validation decision recorded in `pipelex-sdk-js/wip/boundary-option-type-validation.md` names this repo for its Phase 2. See §4.

What needs **no** change is listed in §5, so nobody re-derives it. The four design choices that were open in the first draft were decided on 2026-08-25 and are recorded in §7.

## 1. The validate surface

### 1.1 Where the Python SDK stands today

- `PipelexValidationReport` (`pipelex_sdk/validation_models.py:84`) inherits `extra="allow"` from `mthds.protocol.models.ValidationReport`, so a 0.17+ body's `warnings`, `liftable_pipes` and `input_form` land in `model_extra`. Untyped, but parsed.
- `ValidationErrorItem` (`validation_models.py:62`) inherits `extra="allow"` from `ValidationDiagnostic`, so `missing_pipe_code` and `suggested_fix` land in `model_extra` the same way.
- Every optional member of `ValidationErrorItem` is already `T | None = None`. The JS 0.14.0 breaking change (`T` → `T | null`, forced by the valid arm serializing unset locators as explicit `null` inside `warnings[]`) therefore has **no Python counterpart** — pydantic reads both a dropped key and an explicit `null` into the same `None`. That asymmetry still deserves a regression test here, because it is the one thing a future "tighten to required" edit would break; the workspace inbox item `wip/inbox/2026-08-25-workspace-validation-error-item-spec-gaps.md` explicitly asks for the Python mirror to be checked on this point, and this section is the answer.
- The hosted `/v1/validate` is the platform proxying to the runner (`pipelex-server/platform/src/pipelex_platform/routers/v1/tooling_proxy.py`), so once `feature/Bump-pipelex` (which moves `api-hosted` to the `pipelex-api` `v0.18.0` tag and the core to `pipelex==0.52.0`) is deployed, `api.pipelex.com` serves exactly the contract a bare 0.18.0 runner serves today, `views` gate included. Nothing in that branch changes any other route this SDK calls; its only non-pin edits are the Temporal dry-validate activity carrying `input_form` through to the route that gates it.

### 1.2 `views` — the structured-view opt-in on `validate` and `validate_files`

Add `views: list[str] | None = None` to `PipelexAPIClient.validate` (`pipelex_sdk/client.py:449`) after `render`, and `views: list[str] | None = None` to `validate_files` (`client.py:491`) after `render`. In Python this is not the breaking change it was in TypeScript: the parameter is appended last and callers pass it by keyword, so no existing positional call moves.

Semantics, mirroring `@pipelex/sdk` exactly:

- `None` (the default) → the `views` key is **not sent**. This is the invariant that keeps an opt-in view opt-in: the default response stays byte-identical for the consumers that discard it (hook pipelines, CI gates, agent loops).
- A list → sent **verbatim**, including an explicitly empty `[]`. Unlike `render`, nothing is injected and nothing is de-duplicated: the server resolves tokens as a set and lenient-ignores unknown ones (never a `422`), so client-side normalization would only hide what the caller asked for.
- Today `input_form` is the only supported token; a constant for it belongs in `validation_models.py` next to the field it gates, not a closed enum on the parameter — the spec deliberately keeps the request boundary open so a stale token never fails a call.

It reaches the wire through the base transport seam with no `mthds-python` change: `_post_validate` merges `extra` into the body as top-level keys, and its reserved set `_VALIDATE_REQUEST_ARGS` is only `{mthds_contents, allow_signatures}` (`mthds-python/mthds/runners/api/client.py:383`). `views`, like `render` and `mthds_sources` already, is a Pipelex-API carriage extension, so the protocol client stays unaware of it — the same layering as `method_id` on the run routes.

### 1.3 The valid arm's new fields

On `PipelexValidationReport`:

- `warnings: list[ValidationErrorItem] = Field(default_factory=…)` — advisory lints on a **valid** bundle. Same item type as `validation_errors[]`, so one parser serves both channels, but they never flip `is_valid`. This is where the `hint_*` error types ride.
- `liftable_pipes: list[LiftablePipeEntry] = Field(default_factory=…)` — pipes the runtime may skip when an optional slot resolves absent. New model `LiftablePipeEntry(extra="allow")` with `pipe_ref: str`, `within_pipe_ref: str`, `skipped_when_absent: list[str]` (default empty — the server model defaults it too), `absence_source: str`, mirroring `pipelex/pipelex/pipeline/liftable_pipes.py`.
- `input_form: dict[str, Any] | None = None` — per-pipe input-form descriptors, keyed exactly like `pipe_io_contracts`. **Optional on purpose**: it is present only when the request named the `input_form` view (0.18.0), and a 0.17.0 runner emitted it unconditionally, so `None`-by-default is the one typing that reads a body from either runner correctly. Kept **opaque** like `bundle_blueprint`, `pipe_io_contracts` and `graph_spec`, for the same reason `@pipelex/sdk` keeps it opaque: the descriptor vocabulary is owned elsewhere (the runtime's `PipeInputFormDescriptor`, the `@pipelex/mthds-form` kernel, the `docs/specs/mthds-input-form-descriptor.md` contract), and a second copy here would be free to drift.

Both list fields default to empty rather than being required, and that is a deliberate divergence from the runtime model, where they are always populated. This SDK is pointed at bare runners of whatever version a user runs; a pre-0.52 body with neither key must keep parsing, exactly as `bundle_blueprint` already defaults. The default is also what the runtime emits for a clean bundle, so no caller can tell the two apart — which is the point.

`PipelexInvalidReport` gains nothing: the invalid arm never carries `warnings` or `input_form` (they derive from a crate that was never assembled), and the e2e evidence on the JS side pins `"warnings" not in report`.

### 1.4 `ValidationErrorItem` additions and the fix vocabulary

On `ValidationErrorItem`: `missing_pipe_code: str | None = None` (symmetrical with the existing `missing_concept_code`) and `suggested_fix: SuggestedFix | None = None`.

New models in `validation_models.py`, mirroring `pipelex/pipelex/suggested_fix.py` and the OpenAPI artifact (`pipelex-api/docs/openapi/pipelex-api.openapi.yaml`, schemas `SuggestedFix`, `SetKeyOp` … `RemapValueOp`, `FixSafety`):

- `FixSafety(StrEnum)`: `SAFE = "safe"`, `UNSAFE = "unsafe"`, with an `is_safe` property (house style: never compare enum values inline).
- `FixOpKind(StrEnum)`: the seven kinds — `set_key`, `ensure_table`, `delete_key`, `delete_table`, `rename_table_key`, `move_key`, `remap_value`.
- `TomlScalar: TypeAlias = str | int | float | bool` and `TomlValue: TypeAlias = TomlScalar | dict[str, TomlScalar]` — what a `set_key` writes; deeper nesting is not modelled because the server does not emit it.
- One model per op, each `kind: Literal[FixOpKind.X]` and exactly its own members: `SetKeyOp(key, value)`, `EnsureTableOp()`, `DeleteKeyOp(key)`, `DeleteTableOp()`, `RenameTableKeyOp(key, new_key)`, `MoveKeyOp(key, new_table_path, new_key)`, `RemapValueOp(key, mapping: dict[str, str])`. All carry `table_path: list[str]` (empty for the document root; the OpenAPI artifact marks it `minItems: 1` on `ensure_table` / `delete_table`, which is worth a `Field(min_length=1)` since it costs nothing and mirrors the artifact).
- `FixOp: TypeAlias = Annotated[SetKeyOp | … | RemapValueOp, Field(discriminator="kind")]` — narrowing is `match op: case SetKeyOp(): …`, which is the Python spelling of the JS `kind` narrowing.
- `SuggestedFix(extra="allow")`: `fix_code: str`, `description: str`, `safety: FixSafety`, `source: str | None = None`, `ops: list[FixOp]`.

Two decisions inside that mirror:

- **Reader models, not runtime models.** The runtime declares these `frozen`, `extra="forbid"`, with wildcard-refusing validators, because it *plans* fixes. This SDK only reads them, so the ops follow the SDK's response-model convention (`extra="allow"`) and carry none of the validators. A new server-side member on an op must not break parsing here. The two runtime invariants a type cannot carry (`*` is the wildcard segment, refused as a `key` on every kind but `remap_value`; `ensure_table` / `delete_table` need a non-empty `table_path`) go in docstrings, as the JS mirror did.
- **The `kind` vocabulary is closed, and an unknown kind raises.** A pydantic discriminated union needs `Literal` tags, so a kind this SDK does not know fails the parse of the whole verdict. That is consistent with how `ValidationErrorCategory` already behaves (pinned by `test_unknown_category_is_rejected`): the vocabulary is a closed `StrEnum` upstream, a new kind is a `pipelex` release this SDK mirrors, and a loud failure beats a silently unnarrowable op. The alternative — a catch-all op with `kind: str` through a callable `Discriminator` — is more machinery for a repair proposal that is advisory in the first place; noted in §7 as the one place a reviewer might reasonably disagree.

`error_type` stays `str | None` — an open string, as in the JS mirror. The 0.17.0 changelog notes the union gained the advisory `HintLintErrorType` members; typing it as a closed enum here would turn every runtime enum addition into an SDK break for no consumer benefit.

Naming stays neutral (`SuggestedFix`, `FixOp`, `warnings`, `liftable_pipes`, `input_form`): fixes and lints are language-level concepts, and the runtime names them brand-neutrally too. The `Pipelex` prefix stays on the two envelope types only.

### 1.5 Tests

- `tests/unit/test_client_validate.py`: `views` sent verbatim when given; the key absent from the body when `None`; an explicit `[]` sent as `[]`; `validate_files` threads `views` through; `render` behaviour unchanged alongside it.
- `tests/unit/test_validation_contract.py`: a valid body carrying `warnings`, `liftable_pipes` and `input_form` parses into typed fields, with `input_form` keyed like `pipe_io_contracts`; the JS null-bearing warning fixture (`pipelex-sdk-js/tests/client.test.ts`, "carries advisory warnings on the VALID arm, with the valid arm's explicit nulls") parses with every explicit `null` reading as `None`; the existing pre-0.52 `VALID_BODY` still parses with both lists empty and `input_form` `None`; an invalid body carrying `missing_pipe_code` and a two-op `suggested_fix` parses, with `match`-narrowing reaching each op's own members; an unknown `kind` raises `ValidationError`; `FixSafety` / `FixOpKind` value sets are the locked vocabularies.
- The JS suite also pins the gate **live** (`tests/e2e/tools.e2e.ts`: absent by default, present when asked, unknown token lenient). This repo has no e2e suite at all (`tests/` holds only `unit/`), so that half is not reproducible here today. Not a blocker for this change; recorded in §6 as a known gap rather than silently skipped.

### 1.6 What stays opaque in the 0.17.0 contract move

The 0.17.0 changelog lists more `/v1/validate` movements than the ones above, and none of them reaches a typed field here: `PipeInputContract.optional` → `presence`, the `fixed` multiplicity with `item_count`, and the widened `inputs` map all live inside `pipe_io_contracts` / `bundle_blueprint`, which this SDK carries as `dict[str, Any]` on purpose. A consumer that reads those dicts should know the new spellings; the SDK's docs (`docs/architecture.md`, validate section) should name them in one sentence so nobody discovers `presence` by surprise, but no model changes.

## 2. Already on this branch, and the two documentation corrections still owed

**Done here, matching `@pipelex/sdk` 0.14.0 field-for-field** (commit `cdd8793`): `method_id` as a typed keyword on `execute` / `start` / `start_and_wait`; the run-source precondition satisfied by a `method_id`-only body; `extra` rejecting `method_id` (`_HOSTED_RUN_ARGS`, `client.py:139`); an empty string treated as absent; the selector forwarded on the blocking fallback; `delete_method` returning `MethodDeletionAccepted`. `tests/unit/test_client_method_id.py` pins every one of the JS cases. Nothing further is owed on those.

**Still owed** — the two prose fixes 0.14.0 shipped under "Fixed", which apply here for the same reason (`pipelex-sdk` is a public PyPI package):

- **`TokensUsageRecord` attribution.** `pipelex_sdk/runs.py:23` and `:128`, `docs/run-usage.md:5` and `docs/architecture.md:103` say the record is "specified in the MTHDS protocol spec". It is not: inference accounting is a Pipelex runtime extension the MTHDS Protocol does not model, and the hosted API is what pins the wire contract. Reword as the JS mirror did (`pipelex-sdk-js/src/runs.ts`, `docs/architecture.md`).
- **Citations a reader cannot open.** `client.py:138` (`docs/specs/pipelex-platform-api.md`), `validation_models.py:44` (`conformance/conformance/validation_contract.py`), and the test-module docstrings at `tests/unit/test_validation_contract.py:4-5` / `:167`, `tests/unit/test_runs.py:12`, `tests/unit/test_client_method_id.py:4`, plus `docs/architecture.md:84`. Each names a workspace-private path by bare relative reference, which resolves to nothing for anyone who clones this repo and reads as rot. Replace each with the rule it was citing (the layered extension policy; the locked category vocabulary; the shared conformance corpus), as 0.14.0 did. No behaviour change.

## 3. Found while checking: the product list routes are broken against the deployed platform

### 3.1 Evidence

- The platform serves `GET /v1/methods` as `MethodPage` — `{items: MethodSummary[], next_cursor: str | None}` — since `pipelex-server` commit `f4f8764` (2026-08-18, "paginate the method list, which was silently truncating"), and `GET /v1/runs?method_id=` as `RunPage` — `{items: RunPublic[], next_cursor}` — since `2c4e980` (2026-08-11). Both are ancestors of the latest `deploy(prod)` commit (`b9f9555`), so this is what `api.pipelex.com` answers today. Models: `pipelex-server/shared/src/pipelex_shared/schemas/method.py:265-309`, `schemas/run.py:180-236`.
- `list_methods` (`pipelex_sdk/client.py:767`) does `[MethodData.model_validate(item) for item in result]` over the JSON body. Iterating the envelope dict yields its **keys**, so the first call is `MethodData.model_validate("items")` → `pydantic.ValidationError` on every invocation. `list_runs` (`client.py:946`) fails identically.
- The unit tests mock the pre-paging bare arrays (`tests/unit/test_client_product.py:85`, `:379`), which is why nothing is red.
- `PipelineRun` (`product_models.py:369-370`) declares `method_id: str` and `pipe_code: str`; the platform's `RunPublic` declares both `str | None = None`, and both are genuinely null in practice (an ad-hoc run from an inline bundle; a pipe resolved from `main_pipe`). Once the envelope is fixed, the first such row raises.
- `@pipelex/sdk` took all three in 0.10.0 (`listRuns` → `RunPage`, `iterateRuns`, `getRunDetail`, nullable `PipelineRun` fields) and 0.11.0 (`listMethods` → `MethodPage`, `iterateMethods`, `MethodSummary`). `docs/architecture.md` here still claims full parity ("surface-complete, with no silent gaps"), which has been false since 2026-08-11.

### 3.2 Design

Breaking, and mirroring the JS shapes — with the wire kept snake_case, so the envelope field is `next_cursor` here where JS renamed it `nextCursor` for its own consumers.

**Models (`product_models.py`):**

- `MethodSummary(extra="allow")`: `method_id`, `name`, `description: str | None = None`, `created_at`, `deletion_state: MethodDeletionState | None = None`. Deliberately not a `MethodData`: no `mthds`, no `python`, no `updated_at`, because none is in the index projection and putting `mthds` back is what restored the truncation bug.
- `MethodPage(extra="allow")`: `items: list[MethodSummary]`, `next_cursor: str | None = None`. No total, by design.
- `RunPage(extra="allow")`: `items: list[PipelineRun]`, `next_cursor: str | None = None`.
- `RunErrorReport(extra="allow")`: `message: str | None = None`, `error_type: str | None = None` — the two fields a consumer may rely on out of the runner's verbose report.
- `PipelineRun`: `method_id: str | None = None`, `pipe_code: str | None = None`; add `org_id: str | None = None`, `created_by_user_id: str | None = None`, `error: RunErrorReport | None = None`. `pipe_statuses` stays as it is (the JS model keeps it optional; the platform's `RunPublic` no longer declares it, and `extra="allow"` covers either way).
- `RunDetail(PipelineRun)`: `mthds_contents: list[str] | None = None`, `inputs: dict[str, Any] | None = None` — the only read that carries what the run actually executed.
- `MethodData`: add `org_id: str`, `created_by_user_id: str` (required on the platform's `MethodPublic` and in the JS model), `description: str | None = None`, `deletion_state: MethodDeletionState | None = None`, and `python: list[MethodFile] = Field(default_factory=list)`. See the `python` decision below.
- `MethodWriteInput`: add `python: list[MethodFile] | None = None`. Because the write body is dumped with `exclude_none=True`, the platform's three-way contract falls out naturally: `None` → not sent → the stored Python is preserved; `[]` → serialized as `""` → clears it; a non-empty list → replaces it. Document that on the field.

**Client (`client.py`):**

- `list_methods(*, q: str | None = None, limit: int | None = None, cursor: str | None = None) -> MethodPage`. Query params are added on **presence** (`is not None`), never truthiness — an explicit empty `q` or cursor is bad input the API should reject, not something to silently drop into an unfiltered query that reads as working. Encode with `urllib.parse.urlencode` rather than string formatting; `q` is free text.
- `iterate_methods(*, q=None, limit=None) -> AsyncIterator[MethodSummary]` — an `async def` generator that follows the cursor. It must keep going **past empty pages** (`q` is a post-read filter over a bounded index slice per request, so `{items: [], next_cursor: "…"}` means "keep going"), stop on `next_cursor is None`, stop when the server hands back the cursor it was sent (checked before yielding, so rows are never double-counted), and **raise** rather than return past a runaway page ceiling set far beyond any real catalog. Deliberately not a `list_all_methods() -> list[…]`: an all-at-once helper needs a cap, and a cap means silently returning a truncated list — the exact bug paging removed.
- `list_runs(method_id: str, *, created_from: str | None = None, created_to: str | None = None, limit: int | None = None, cursor: str | None = None) -> RunPage`. `created_from` / `created_to` are instants (ISO-8601 with a UTC offset), not days; a naive timestamp is a platform `400`, surfaced as `ApiResponseError`. Same presence semantics.
- `iterate_runs(method_id, *, created_from=None, created_to=None, limit=None) -> AsyncIterator[PipelineRun]` — same loop, except an empty page **does** end it: the date bounds are index key conditions, so a run page is never empty-with-a-cursor. The difference is the server, not the client, and the docstring should say so.
- `get_run_detail(run_id: str) -> RunDetail` — `GET /v1/runs/{id}`, path-encoded like the other id routes.
- One thing to document that the JS mirror does not spell out: every `/v1/runs*` product route sits behind the platform's `require_surface_access()` gate (`pipelex-server/platform/src/pipelex_platform/deps.py:345`), which for API-key auth demands the `ff_api_keys` feature flag and fails closed with a `403`. That arrives here as an `ApiResponseError`, and a reader of `list_runs` should know a `403` means "flag", not "wrong key".

**The `python` field.** On the wire `MethodPublic.python` is one string: the JSON text of a `[{name, content}]` array, or `""` for a method with no custom Python (`pipelex-server/shared/src/pipelex_shared/schemas/method.py:209`, `:251`), and the write side is the same string three ways (omitted → preserve, `""` → clear, text → replace). `@pipelex/sdk` never shows that string to callers: it exposes `MethodFile[]` and converts at the client boundary (`pipelex-sdk-js/src/client.ts:281` on read, `:292` on write) with `parseMethodFiles` / `serializeMethodFiles` from `mthds-js/src/protocol/method_files.ts`. That module is small — parse the JSON, check every entry is `{name: str, content: str}`, drop blank-content entries, and serialize an empty list as `""` rather than `"[]"` because `""` is the platform's clear sentinel. It lives in `mthds/protocol` on the JS side because `pipelex-mcp` consumes the same format and wanted one owner.

The first draft of this document proposed exposing the raw wire string and asking `mthds-python` for the converter. That was over-engineered: with pydantic the whole converter is a `MethodFile` model plus a `TypeAdapter(list[MethodFile])` and the two sentinel rules, there is no second Python consumer that could drift, and the JS module's own docstring calls this the format "the hosted platform persists" — a Pipelex catalog concern, so this SDK is a proper home for it. **Decision: typed list, converter here.** `product_models.py` gains `MethodFile(name: str, content: str)` and a `parse_method_files(source: str | None) -> list[MethodFile]` / `serialize_method_files(files: list[MethodFile]) -> str` pair carrying the same rules as the JS pair (blank source or `"[]"` → `[]`; blank-content entries dropped on both directions; empty list → `""`). `MethodData` applies the parser through a `field_validator("python", mode="before")`, so `MethodData.model_validate(body)` keeps working unchanged at every call site; `MethodWriteInput` applies the serializer through a `field_serializer("python")`, so the write body still dumps with `exclude_none=True` and the three-way contract holds. Malformed wire text raises `ValueError` inside the validator and therefore surfaces as a `pydantic.ValidationError`, the same way any other malformed response body fails here. If the format ever gains a Python owner in `mthds-python`, this SDK adopts it then; nothing is filed to the inbox for it.

`get_method_closure` (JS-only client-side sugar that parses the polymorphic `mthds` source into a run-ready closure) stays deferred: it is not moved by any of the cited releases, and the 0.5.0 changelog already recorded it as "deferred and additive" alongside `prepare_inputs`.

### 3.3 Tests (`tests/unit/test_client_product.py`)

Replace the two bare-array fixtures with envelopes and add: query encoding for `q` / `limit` / `cursor` and for `created_from` / `created_to`, including that an explicit empty string is forwarded rather than dropped; a null `pipe_code` / `method_id` row parsing; `get_run_detail` returning `mthds_contents` and `inputs`; `MethodData` carrying the new fields, with `python` parsed from the wire string into `MethodFile` entries and `""` reading as an empty list; `MethodWriteInput.python` three-way serialization (`None` absent, `[]` sent as `""`, a list sent as the JSON text); the `parse_method_files` / `serialize_method_files` pair round-tripping, dropping blank-content entries, and rejecting a non-array or a malformed entry. For the iterators, in a dedicated module (one `TestClass` per module): `iterate_methods` continues through an empty page with a live cursor and stops on `None`; both iterators stop on an unchanged cursor without re-yielding; `iterate_runs` stops on an empty page; `iterate_methods` raises past the ceiling.

## 4. Boundary type validation for `method_id` (decision of 2026-08-25)

`pipelex-sdk-js/wip/boundary-option-type-validation.md` records the decision, taken by Louis, that a published client validates request-option types at its boundary and throws `PipelineRequestError` rather than dropping or forwarding a wrong-typed value. Its evidence section cites this repo directly: `client.py:1003` is a bare `if method_id:`, which drops falsy non-strings (`0`, `[]`) and forwards truthy ones (`123`, `["mt_1"]`) to a server `422` — a *different* partition of wrong values than the JS client makes for the same argument on the same wire. The plan's Phase 2 names the fix: an explicit `is not None` presence check followed by an `isinstance(method_id, str)` check that raises, with `None` and `""` still normalizing to absent.

The plan sequences the SDKs after the protocol packages so both inherit one behaviour for the protocol-level arguments. That ordering matters for `pipe_code` / `mthds_contents`, whose guards belong in `mthds-python`; it does not constrain `method_id`, which this layer owns outright and whose guard touches only `_merge_hosted_run_extensions` (`client.py:975`). **Recommendation: land the `method_id` guard in this update** — it is a few lines, this branch is already the `method_id` branch, and it closes the repo-specific finding in the JS wip doc — and take the protocol-argument guards later with the `mthds` floor bump once `mthds-python` ships its Phase 1. Decided 2026-08-25: it lands now (§7).

## 5. Checked, no change needed

- **pipelex-api 0.17.0's source-less `422` naming unhandled keys** (the `method_id`-at-a-bare-runner diagnosis): this SDK already forwards `method_id` on the blocking fallback precisely so that message reaches the caller; the `execute` docstring already describes it.
- **`storage_scope` / `callback_urls` / `orchestration_mode`** (0.15.0–0.16.0): layer-2 fields the hosted platform sends to the runner; not caller-facing and not an SDK concern.
- **The four OpenAPI schemas that went opaque, the `RunMetadata` split, the `.pipelex/` config schema, and the two authoring changes** (`required = true` + `default_value` rejected; unknown structure-field keys rejected): server-side and inside opaque dicts here; no wire field this SDK types moved.
- **`views` in `mthds-python`**: not needed. It is a Pipelex-API carriage extension exactly like `render`, and the base client's `extra` passthrough already carries it (§1.2). `mthds-js` likewise has no `views`.
- **Explicit-null locators** (JS 0.14.0's `T | null` widening): already `T | None = None` here (§1.1); only a regression test is owed.
- **`ValidationErrorItem.error_type` narrowing** to the new enum members: stays an open `str` by design (§1.4).
- **The 0.14.0 `validate()` positional break**: Python takes `views` by keyword after `render`; no positional call moves.
- **`method_id` typed option and `delete_method`**: already on this branch (§2).

## 6. Change plan

Four commits on this branch, each self-contained, each with its docs and changelog lines, each gated on `make agent-check` and `make agent-test`:

1. **Validate surface** (§1): `validation_models.py` (new fields, `LiftablePipeEntry`, the fix vocabulary), `client.py` (`views` on `validate` / `validate_files`), the two test modules, `docs/architecture.md` (the validate section and the brand-boundary field list gain `warnings` / `liftable_pipes` / `input_form`, and a sentence on the opaque `presence` / `fixed` spellings), `README.md` quickstart mention of `views`. Changelog: **Added** (`views`; the typed valid-arm fields; `missing_pipe_code` / `suggested_fix` and the `SuggestedFix` / `FixOp` / `FixSafety` vocabulary), with a note that an older runner's body still parses.
2. **Prose corrections** (§2): the attribution and citation edits in `runs.py`, `validation_models.py`, `client.py`, the three test docstrings, `docs/run-usage.md`, `docs/architecture.md`. Changelog: **Fixed**, two entries mirroring 0.14.0's wording.
3. **Product paging and nullability** (§3): `product_models.py`, `client.py` (`list_methods`, `iterate_methods`, `list_runs`, `iterate_runs`, `get_run_detail`), `tests/unit/test_client_product.py` plus a new iterator test module, `docs/architecture.md` (product surface section rewritten for pages, the "Parity with `@pipelex/sdk`" section corrected — it must stop claiming surface-completeness and list the conscious exclusions honestly), `README.md` if it gains a listing example. Changelog: **Changed (breaking)** for the two return types and the nullable `PipelineRun` fields, **Added** for the iterators, `get_run_detail`, `MethodSummary` / `MethodPage` / `RunPage` / `RunDetail` / `RunErrorReport`, `MethodFile` with `parse_method_files` / `serialize_method_files`, the `MethodData` fields and `MethodWriteInput.python`, **Fixed** naming the crash.
4. **`method_id` type guard** (§4): `_merge_hosted_run_extensions`, one wrong-type parametrized test in `test_client_method_id.py`. Changelog: **Changed**.

The version stays where it is under `## [Unreleased]` until `/release` cuts it; with the breaking items in commit 3 (and the ones already on the branch), that release is a minor bump.

**Known gaps this plan leaves open, on purpose:** no e2e suite exists in this repo, so the live `views` gate and the live paging envelope are pinned only by mocked bodies here (the JS suite pins both live); the remaining `@pipelex/sdk` surfaces without a Python counterpart — `lint`, `format`, `resolve`, `codegen`, `build_output` / `build_runner` / `concept` / `pipe_spec`, `run_codegen_check`, `get_method_closure` — are unchanged by the cited releases and stay the conscious exclusions `docs/architecture.md` already records.

## 7. Decisions (2026-08-25)

The four questions the first draft left open, each answered by Louis on 2026-08-25 with the reasoning that settled it.

1. **`input_form` stays opaque** — `dict[str, Any] | None = None`, matching the JS mirror and the ownership argument in §1.3. A `PipeInputFormDescriptor(fields: list[dict])` shell would type one level and still leave the field vocabulary opaque, which buys little.
2. **`MethodData.python` / `MethodWriteInput.python` are typed `list[MethodFile]`, with the converter in this repo.** The question was first posed as "raw wire string plus an inbox request to `mthds-python` for the parser", and the answer to "why do we need a parser at all?" dissolved that framing: the converter is a dozen lines of pydantic, the format is a Pipelex catalog concern rather than an MTHDS protocol one, and there is no second Python consumer to keep in step. Full design in §3.2; no inbox item is filed.
3. **The `method_id` type guard lands now**, in this update (§4). The protocol-argument guards for `pipe_code` / `mthds_contents` still wait for `mthds-python` to ship its Phase 1 and arrive here with the `mthds` floor bump.
4. **An unknown `FixOp.kind` raises** — closed `Literal` discriminator, `pydantic.ValidationError` on the whole verdict parse, consistent with the closed `ValidationErrorCategory`. The lenient catch-all alternative described in §1.4 was considered and not taken.
