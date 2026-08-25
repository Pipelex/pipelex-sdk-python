# TODOS — implementing `wip/updates.md`

This is the implementation tracker for the design in [`wip/updates.md`](wip/updates.md). The design answers *what* and *why*; this file is the *how*, broken into phases with checkboxes. Tick a box when the item is done and verified, not when it is started. Every design choice that was open has been decided (see `wip/updates.md` §7) and is treated here as settled: `input_form` stays opaque, `MethodData.python` is a typed `list[MethodFile]` with the converter in this repo, the `method_id` type guard lands now, and an unknown `FixOp.kind` raises.

Ground rules for every phase, from `CLAUDE.md`:

- Branch: `feature/Typed-method-id-run-option` (already carries the typed `method_id` option and the `delete_method` contract fix). The PR targets `dev`.
- Gate each phase on `make agent-check` **and** `make agent-test`; nothing is "done" before both pass. Run `make check` once at the end as well, since it adds pylint on top of the agent gate.
- Tests use `pytest-mock` only, one `TestClass` per module, no `__init__.py` under `tests/`. Mock at the httpx boundary (`_send`), as the existing modules do.
- Everything accumulates under `## [Unreleased]` in `CHANGELOG.md`. No version bump in this work: the `/release` skill cuts the version, and with the breaking items in Phase 3 (plus those already on the branch) that will be a minor bump.
- Docs move with the code, in the same commit. `docs/architecture.md` is the main one to keep truthful; its "Parity with `@pipelex/sdk`" section currently claims surface-completeness and is wrong.
- No hardcoded counts in code, docs, or commit messages. No volatile state in tracked files (this file records decisions and what landed, never whether the tree is clean or tests are green right now).
- Line numbers quoted below are as of the design date (2026-08-25) and will drift; they are anchors, not contracts.

Suggested order is the order below. Phase 3 is the most urgent fix (a crash against the deployed platform) but is also the largest breaking change, so the plan puts the purely additive Phase 1 first to keep each commit reviewable; reorder if the crash needs to ship alone.

## Phase 0 — preflight

- [x] `make install` and confirm the pinned `mthds` base is the one the code was written against (`uv pip show mthds`; the floor is `>=0.8.1` and the workspace copy is 0.8.2). Nothing in this plan needs a newer `mthds`. Confirmed: 0.8.2.
- [x] Run `make agent-check` and `make agent-test` before touching anything, so a later failure is attributable to this work and not to the starting point. Both green at the starting point.
- [x] Re-read `wip/updates.md` §6 and §7 once; if anything there contradicts this file, this file is the stale one. No contradiction found.

## Phase 1 — the `/v1/validate` surface (`wip/updates.md` §1)

Purely additive. Every affected response model is `extra="allow"`, so nothing parses differently for a body that lacks the new keys; the work is to type what already arrives and to add the one request knob (`views`) that has no way through today.

### 1.1 `pipelex_sdk/validation_models.py` — the fix vocabulary and the new fields

- [x] Add `FixSafety(StrEnum)` with `SAFE = "safe"`, `UNSAFE = "unsafe"`, and an `is_safe` property (house rule: never compare enum values inline; a `match` inside the property).
- [x] Add `FixOpKind(StrEnum)` with the kinds the runtime emits: `SET_KEY = "set_key"`, `ENSURE_TABLE = "ensure_table"`, `DELETE_KEY = "delete_key"`, `DELETE_TABLE = "delete_table"`, `RENAME_TABLE_KEY = "rename_table_key"`, `MOVE_KEY = "move_key"`, `REMAP_VALUE = "remap_value"`. Source of truth: `pipelex/pipelex/suggested_fix.py` and the OpenAPI artifact `pipelex-api/docs/openapi/pipelex-api.openapi.yaml`.
- [x] Add `TomlScalar: TypeAlias = str | int | float | bool` and `TomlValue: TypeAlias = TomlScalar | dict[str, TomlScalar]`. Deeper nesting is not modelled because the server does not emit it; say so in a comment.
- [x] Add a private `_FixOpBase(BaseModel)` with `model_config = ConfigDict(extra="allow")` and `table_path: list[str]` (empty list means the document root), then one subclass per kind, each with `kind: Literal[FixOpKind.X]` and exactly its own members: `SetKeyOp(key: str, value: TomlValue)`, `EnsureTableOp()`, `DeleteKeyOp(key: str)`, `DeleteTableOp()`, `RenameTableKeyOp(key: str, new_key: str)`, `MoveKeyOp(key: str, new_table_path: list[str], new_key: str)`, `RemapValueOp(key: str, mapping: dict[str, str])`. On `EnsureTableOp` and `DeleteTableOp` declare `table_path: list[str] = Field(min_length=1)`, mirroring the artifact's `minItems: 1`.
- [x] These are **reader** models: no `frozen`, no `extra="forbid"`, none of the runtime's wildcard-refusing validators. Put the two runtime invariants a type cannot carry in the docstrings (`*` is the wildcard segment and is refused as a `key` on every kind but `remap_value`; `ensure_table` / `delete_table` need a non-empty `table_path`).
- [x] Add `FixOp: TypeAlias = Annotated[SetKeyOp | EnsureTableOp | DeleteKeyOp | DeleteTableOp | RenameTableKeyOp | MoveKeyOp | RemapValueOp, Field(discriminator="kind")]`. Narrowing is `match op: case SetKeyOp(): …`, exhaustive, no `case _`.
- [x] **Verify** that pydantic accepts the raw wire string (`"set_key"`) against `Literal[FixOpKind.SET_KEY]` both in `validate_python` and `validate_json`, and that the discriminator resolves on it. If it does not on the pinned pydantic, use `Literal["set_key"]` on the models and keep `FixOpKind` as the documented vocabulary (with a test that the two sets agree).
- [x] Add `SuggestedFix(BaseModel, extra="allow")`: `fix_code: str`, `description: str`, `safety: FixSafety`, `source: str | None = None`, `ops: list[FixOp]` (typed default factory via `empty_list_factory_of` only if a default is warranted — the runtime always sends `ops`, so leaving it required is fine).
- [x] Add `LiftablePipeEntry(BaseModel, extra="allow")`: `pipe_ref: str`, `within_pipe_ref: str`, `skipped_when_absent: list[str] = Field(default_factory=list)`, `absence_source: str`. Mirrors `pipelex/pipelex/pipeline/liftable_pipes.py`.
- [x] Add the view token constant next to the field it gates: `VALIDATION_VIEW_INPUT_FORM: Final[str] = "input_form"`. A constant, not a closed enum — the request boundary is deliberately open so a stale token never fails a call.
- [x] On `ValidationErrorItem` add `missing_pipe_code: str | None = None` (symmetrical with `missing_concept_code`) and `suggested_fix: SuggestedFix | None = None`. Leave `error_type: str | None` as an open string; do not enum it.
- [x] On `PipelexValidationReport` add `warnings: list[ValidationErrorItem] = Field(default_factory=empty_list_factory_of(ValidationErrorItem))`, `liftable_pipes: list[LiftablePipeEntry] = Field(default_factory=empty_list_factory_of(LiftablePipeEntry))`, and `input_form: dict[str, Any] | None = None`, each with a docstring: `warnings` never flips `is_valid`; the two lists default empty so a pre-0.52 runner's body still parses; `input_form` is present only when the request named the `input_form` view (a 0.17.0 runner emitted it unconditionally, which `None`-by-default also reads correctly) and is opaque on purpose, keyed like `pipe_io_contracts`.
- [x] `PipelexInvalidReport` gains nothing; add one sentence to its docstring saying why (the invalid arm never carries `warnings` or `input_form` — they derive from a crate that was never assembled).
- [x] Update the module docstring's list of neutrally-named supporting types to include the new ones, and keep the brand rule stated there (the `Pipelex` prefix stays on the two envelopes only).

### 1.2 `pipelex_sdk/client.py` — `views` on `validate` and `validate_files`

- [x] `validate(...)`: append `views: list[str] | None = None` after `render`. When `views is not None`, set `extra["views"] = views` **verbatim** — no injection, no de-duplication, an explicit `[]` is sent as `[]`. When `None`, the key is absent from the body. It rides `_post_validate`'s `extra` exactly like `render` and `mthds_sources`; no `mthds-python` change.
- [x] `validate_files(...)`: append `views: list[str] | None = None` after `render` and thread it through to `validate`.
- [x] Docstrings: the sentence "differs from the inherited protocol `validate` in two Pipelex-API ways" becomes three (render injection, `mthds_sources`, `views`); document the `views` semantics (opt-in structured views; `input_form` is the only token today, named by `VALIDATION_VIEW_INPUT_FORM`; unknown tokens are lenient-ignored server-side, never a `422`; the default response stays byte-identical for consumers that discard views).
- [x] The `Returns:` section of `validate` should mention that a valid report now carries `warnings`, `liftable_pipes`, and (when asked) `input_form`.

### 1.3 Tests

- [x] `tests/unit/test_client_validate.py`: `views` sent verbatim when given; the `views` key absent from the body when the parameter is omitted; an explicit `[]` sent as `[]`; `validate_files` threads `views` through; `render` injection unchanged when `views` is also passed.
- [x] `tests/unit/test_validation_contract.py`: a valid body carrying `warnings`, `liftable_pipes`, and `input_form` parses into typed fields, with `input_form` keyed like `pipe_io_contracts`; the pre-0.52 `VALID_BODY` still parses with both lists empty and `input_form` `None`; the JS null-bearing warning fixture (`pipelex-sdk-js/tests/client.test.ts`, "carries advisory warnings on the VALID arm, with the valid arm's explicit nulls") parses with every explicit `null` reading as `None` — this is the regression guard against a future "tighten to required" edit, and it answers the inbox item `../wip/inbox/2026-08-25-workspace-validation-error-item-spec-gaps.md` for the Python mirror.
- [x] `tests/unit/test_validation_contract.py`: an invalid body carrying `missing_pipe_code` and a `suggested_fix` with at least two ops of different kinds parses, and `match`-narrowing reaches each op's own members; an `ensure_table` op with an empty `table_path` is rejected; an unknown `kind` raises `pydantic.ValidationError`; `FixSafety` and `FixOpKind` value sets are asserted as the locked vocabularies (same style as `test_category_vocabulary_is_the_locked_set`).
- [x] Keep the canonical bodies where the module already keeps them (module-level constants next to `VALID_BODY`); move to a `tests/unit/test_data.py` only if the module becomes unreadable.

### 1.4 Docs and changelog

- [x] `docs/architecture.md` → "`validate` override": add a `views` bullet beside the render bullet; list the typed valid-arm additions (`warnings`, `liftable_pipes`, `input_form`) and the `ValidationErrorItem` additions with the `SuggestedFix` / `FixOp` / `FixSafety` vocabulary; one sentence that `PipeInputContract.optional` became `presence` and that the `fixed` multiplicity carries `item_count` inside the opaque `pipe_io_contracts`, so nobody discovers the new spellings by surprise.
- [x] `docs/architecture.md` → "Brand boundary": the list of neutrally-named supporting types gains the new ones.
- [x] `README.md` quickstart: one line showing `views=[VALIDATION_VIEW_INPUT_FORM]` (or a comment that the input form is opt-in), so the knob is discoverable.
- [x] `CHANGELOG.md` `[Unreleased]` → **Added**: `views` on `validate` / `validate_files`; the typed valid-arm fields; `missing_pipe_code` / `suggested_fix` and the fix vocabulary; a note that a body from an older runner still parses (the lists default empty, `input_form` defaults `None`).

### 1.5 Gate and commit

- [x] `make agent-check` and `make agent-test` pass.
- [x] Commit (suggested message: "Type the pipelex-api 0.17/0.18 validate contract and add the views opt-in").

### Checkpoint 1

- [x] Update this file: tick what landed, record the SHA of the Phase 1 commit, note whether pydantic accepted the enum `Literal` tags or the string fallback was needed, and any deviation from §1 of the design with its reason.

**Landed in `434b2e3`.** Notes:

- **The enum `Literal` tags work as written; the string fallback was not needed.** Verified against the pinned pydantic (2.13.4) before writing the models: a raw wire `"set_key"` validates against `Literal[FixOpKind.SET_KEY]` in both `validate_python` and `validate_json`, the discriminator resolves on it, an unknown `kind` raises, and `Field(min_length=1)` on `EnsureTableOp.table_path` rejects an empty path.
- **`RemapValueOp.mapping` is left unconstrained**, where the runtime and the OpenAPI artifact both declare `minProperties: 1`. These are reader models: an empty mapping is an advisory no-op, not a parse hazard, and refusing it would fail a whole verdict over a harmless op. The `minItems: 1` on `ensure_table` / `delete_table` was kept because there the empty case is genuinely meaningless (the document root always exists, and cannot be deleted).
- **One Phase-2 item landed early**, because `validation_models.py` was rewritten wholesale here: the `conformance/conformance/validation_contract.py` citation on `ValidationErrorCategory` is already replaced with the rule it was citing. Phase 2 covers the rest.
- No other deviation from §1 of the design.

## Phase 2 — prose corrections (`wip/updates.md` §2)

No behaviour change. Two fixes `@pipelex/sdk` 0.14.0 shipped under "Fixed" that apply here for the same reason (`pipelex-sdk` is a public PyPI package).

- [x] **`TokensUsageRecord` attribution.** `pipelex_sdk/runs.py` (module docstring near line 23 and the class docstring near line 128), `docs/run-usage.md` (line 5), `docs/architecture.md` (the `TokensUsageRecord` bullet near line 103): the record is a Pipelex runtime extension the MTHDS Protocol does not model, and the hosted API pins the wire contract. Reword as `pipelex-sdk-js/src/runs.ts` and its `docs/architecture.md` did. `docs/run-usage.md` line 5 already says the right thing in its second sentence; make the first sentence agree with it.
- [x] **Citations a reader cannot open.** Replace each bare workspace-private path with the rule it was citing: `pipelex_sdk/client.py` near line 138 (`docs/specs/pipelex-platform-api.md` → "the layered extension policy: a hosted client types its own platform's arguments and guards them per layer"); `pipelex_sdk/validation_models.py` near line 44 (`conformance/conformance/validation_contract.py` → "the locked category vocabulary shared with the conformance corpus"); `tests/unit/test_validation_contract.py` module docstring and the docstring of `test_category_vocabulary_is_the_locked_set`; `tests/unit/test_runs.py` near line 12; `tests/unit/test_client_method_id.py` module docstring; `docs/architecture.md` near line 84. Keep the JS mirror references (`pipelex-sdk-js/...`) where they explain a port — those are a sibling public repo, not a private path.
- [x] `CHANGELOG.md` `[Unreleased]` → **Fixed**: two entries mirroring 0.14.0's wording.
- [x] `make agent-check` and `make agent-test` pass.
- [x] Commit (suggested message: "Correct the TokensUsageRecord attribution and drop unopenable citations").

## Phase 3 — product paging and nullability (`wip/updates.md` §3)

Breaking, and the most urgent fix in this plan: `list_methods` and `list_runs` crash against the deployed platform because both routes now answer a `{items, next_cursor}` envelope, and `PipelineRun` requires fields the platform serves as nullable. Wire fields stay snake_case (`next_cursor`), where the JS mirror renamed to `nextCursor` for its own consumers.

### 3.1 `pipelex_sdk/product_models.py` — models

- [x] Add `MethodFile(BaseModel, extra="allow")` with `name: str`, `content: str`, defined **before** `MethodData`. Docstring: the at-rest catalog form of one source file (`[{name, content}]`), distinct from `MthdsFile` (`client.py`, validate input) and `MthdsFileItem` (`build_models.py`, build closure) — three shapes for three surfaces, name the difference so nobody merges them.
- [x] Add `parse_method_files(source: str | None) -> list[MethodFile]`: blank source (`None`, `""`, whitespace) and `"[]"` both yield `[]`; a JSON array of `{name, content}` yields those files with blank-content entries dropped; anything else (non-array JSON, a malformed entry, unparseable text) raises `ValueError` with a message naming the expected shape. Implementation: `json.loads` then `TypeAdapter(list[MethodFile])` (built once at module level, TypeAdapter construction is expensive), wrapping `json.JSONDecodeError` / `pydantic.ValidationError` into the `ValueError`.
- [x] Add `serialize_method_files(files: list[MethodFile]) -> str`: drop blank-content entries; an empty result serializes to `""` (the platform's "no source / clear" sentinel), never `"[]"`; otherwise `json.dumps` of `[{name, content}]` only (no extras), stable key order.
- [x] `MethodData`: add `org_id: str`, `created_by_user_id: str`, `description: str | None = None`, `deletion_state: MethodDeletionState | None = None`, `python: list[MethodFile] = Field(default_factory=empty_list_factory_of(MethodFile))`, plus a `@field_validator("python", mode="before")` that applies `parse_method_files` when the incoming value is a `str` or `None` and passes a list through unchanged (so programmatic construction in tests still works). A `ValueError` from the parser surfaces as `pydantic.ValidationError` from `model_validate`, the same way any malformed response body fails here.
- [x] `MethodWriteInput`: add `python: list[MethodFile] | None = None` with a `@field_serializer("python")` returning `serialize_method_files(value)`. Docstring the three-way contract: `None` → key absent (the write body dumps with `exclude_none=True`) → the stored Python is preserved on `PUT`; `[]` → sent as `""` → clears it; a non-empty list → replaces it.
- [x] Add `MethodSummary(BaseModel, extra="allow")`: `method_id: str`, `name: str`, `description: str | None = None`, `created_at: str`, `deletion_state: MethodDeletionState | None = None`. Docstring: deliberately not a `MethodData` — no `mthds`, no `python`, no `updated_at` — because none is in the index projection and putting `mthds` back is what restored the truncation bug; a method mid-deletion stays in the list so a UI can render "Deleting…" while `get_method` refuses it with a `409`.
- [x] Add `MethodPage(BaseModel, extra="allow")`: `items: list[MethodSummary]`, `next_cursor: str | None = None`. Docstring: opaque cursor, pass it straight back; `None` means last page; no total by design.
- [x] Add `RunErrorReport(BaseModel, extra="allow")`: `message: str | None = None`, `error_type: str | None = None` — the two fields a consumer may rely on out of the runner's verbose report.
- [x] `PipelineRun`: `method_id: str | None = None` (an ad-hoc run from an inline bundle belongs to no stored method), `pipe_code: str | None = None` (resolved from the bundle's `main_pipe`); add `org_id: str | None = None`, `created_by_user_id: str | None = None`, `error: RunErrorReport | None = None`. Leave `pipe_statuses` as it is.
- [x] Add `RunDetail(PipelineRun)`: `mthds_contents: list[str] | None = None`, `inputs: dict[str, Any] | None = None`. Docstring: `mthds_contents` is what the run actually executed and the only record of it; both fields are absent from the list and the polled status read on purpose (size × page size, size × poll rate).
- [x] Add `RunPage(BaseModel, extra="allow")`: `items: list[PipelineRun]`, `next_cursor: str | None = None`.
- [x] Update the section comments in the module (`# ── Methods catalog`, `# ── Run records`) so the new models sit under the right banner.

### 3.2 `pipelex_sdk/errors.py` — the runaway-paging error

- [x] Add one error for `iterate_methods` refusing to keep paging past the ceiling (a name like `PagingNotTerminatingError`), extending whatever base the module's other product errors extend — check the existing hierarchy there first. Message mirrors the JS one: the iterator did not terminate after the ceiling; this is a server-side fault, not a coverage limit.

### 3.3 `pipelex_sdk/client.py` — list, iterate, detail

- [x] Add a module helper `_product_query(params: dict[str, str | int | None]) -> str` that keeps entries on **presence** (`is not None`, never truthiness — an explicit empty `q` or cursor is bad input the API should reject, not something to drop silently) and encodes with `urllib.parse.urlencode`, returning `""` or `?…`. The existing `test_list_runs_encodes_query_value` assertion (`method_id=m%2F1`) must stay green, so keep `/` percent-encoded.
- [x] Add `_MAX_LIST_PAGES: int = 10_000` beside the other module constants (the JS `MAX_PAGES`), with the comment that it is a runaway backstop set far beyond any real catalog, not a coverage cap.
- [x] `list_methods(self, *, q: str | None = None, limit: int | None = None, cursor: str | None = None) -> MethodPage` — `GET /v1/methods` with the query built by the helper; parse `MethodPage`. Docstring: `q` is a server-side case-insensitive substring match over name and description across the whole catalog; `limit` defaults to and is capped by the API; ordering is by creation, newest first.
- [x] `iterate_methods(self, *, q: str | None = None, limit: int | None = None) -> AsyncIterator[MethodSummary]` as an `async def` generator: request a page; **before yielding**, stop if `cursor is not None and page.next_cursor == cursor` (the server did not advance; yielding first would double-count); yield every item; stop when `page.next_cursor is None`; otherwise count the page and **raise** the new error once the count reaches `_MAX_LIST_PAGES`; continue **through empty pages** with a live cursor, because `q` is a post-read filter over a bounded index slice per request and `{items: [], next_cursor: "…"}` means "keep going". Docstring says why there is no `list_all_methods()`: an all-at-once helper needs a cap, and a cap is the silent truncation paging removed.
- [x] `list_runs(self, method_id: str, *, created_from: str | None = None, created_to: str | None = None, limit: int | None = None, cursor: str | None = None) -> RunPage` — `GET /v1/runs?method_id=…` plus the presence-kept query; parse `RunPage`. Docstring: `created_from` / `created_to` are instants (ISO-8601 with a UTC offset), inclusive, key conditions rather than filters; a bare date or naive timestamp is a platform `400` surfaced as `ApiResponseError`. Also document the gate the JS mirror does not spell out: every `/v1/runs*` product route sits behind the platform's `require_surface_access()`, which for API-key auth demands the `ff_api_keys` feature flag and fails closed with a `403` — a `403` here means "flag", not "wrong key".
- [x] `iterate_runs(self, method_id: str, *, created_from: str | None = None, created_to: str | None = None, limit: int | None = None) -> AsyncIterator[PipelineRun]` — same loop, except an **empty page ends it** (date bounds are index key conditions, so a run page is never empty-with-a-cursor; the difference is the server, not the client — say so in the docstring). No page ceiling needed: the empty-page stop already catches a server minting fresh cursors while returning nothing.
- [x] `get_run_detail(self, run_id: str) -> RunDetail` — `GET /v1/runs/{id}` via `_request_product` with the id path-encoded like the other id routes (`f"{_RUNS}/{quote(run_id, safe='')}"`). Distinct from `get_run_status` (`/status`) and `get_run_result` (`/results`).
- [x] Update the `PipelexAPIClient` class docstring's product-surface bullet and the import block (`MethodPage`, `MethodSummary`, `RunDetail`, `RunPage`, `AsyncIterator` from `collections.abc` under `TYPE_CHECKING` if only used in annotations — it is used at runtime as a return annotation with `from __future__ import annotations`, so `TYPE_CHECKING` is fine).

### 3.4 Tests

- [x] `tests/unit/test_client_product.py`: replace the bare-array fixtures at `test_list_methods` and `test_list_runs_encodes_query_value` with envelopes and assert `MethodPage` / `RunPage` come back with `next_cursor`; add query-encoding cases for `q` / `limit` / `cursor` and for `created_from` / `created_to`, including that an explicit empty string is forwarded rather than dropped and that an absent parameter leaves no key in the query; a run row with `null` `pipe_code` and `method_id` parses; `get_run_detail` hits `/v1/runs/{id}` with encoding and returns `mthds_contents` and `inputs`; `MethodData` parses the new fields with `python` read from the wire string into `MethodFile` entries and `""` reading as `[]`; `create_method` / `update_method` send `python` three ways (`None` absent, `[]` as `""`, a list as the JSON text).
- [x] New `tests/unit/test_method_files.py` (one `TestClass`): `parse_method_files` on blank / `"[]"` / a valid array / an array with a blank-content entry / a non-array / a malformed entry / unparseable text; `serialize_method_files` on empty / blank-only / mixed; a round-trip is stable.
- [x] New `tests/unit/test_client_paging.py` (one `TestClass`): `iterate_methods` continues through an empty page with a live cursor and stops on `None`; both iterators stop on an unchanged cursor without re-yielding the page; `iterate_runs` stops on an empty page; `iterate_methods` raises the new error past the ceiling (patch `_MAX_LIST_PAGES` down via `mocker.patch` rather than looping ten thousand times); the cursor sent on page N+1 is the `next_cursor` received on page N. Use `mocker.AsyncMock(side_effect=[...])` on `_send` to script the page sequence.
- [x] The `_response` / `_Sent` / `_mock_send` helpers live as private members of `tests/unit/test_client_product.py`. Rather than importing private test helpers across modules, promote a response builder and a `_send` spy to fixtures in a new `tests/unit/conftest.py` for the new modules to use (house rule: fixtures go in `conftest.py`). Migrating `test_client_product.py` onto those fixtures is optional and not part of this change.

### 3.5 Docs and changelog

- [x] `docs/architecture.md` → "Pipelex product surface": rewrite the **Methods catalog** bullet for `MethodPage` / `MethodSummary` / `iterate_methods` and the `python` three-way write contract with `MethodFile`; rewrite the **Run records** bullet for `RunPage` / `iterate_runs` / `get_run_detail`, the nullable `PipelineRun` fields and `error`, the instant-only date bounds, and the `ff_api_keys` `403`. State the two iterator stop rules and why they differ.
- [x] `docs/architecture.md` → "Parity with `@pipelex/sdk`": it must stop claiming "surface-complete, with no silent gaps". Rewrite it to list the conscious exclusions honestly: `lint`, `format`, `resolve`, `codegen`, `build_output` / `build_runner` / `concept` / `pipe_spec`, `run_codegen_check`, `get_method_closure` — unchanged by the cited releases and deferred. While there, fix the stale "Out of scope for v0.1" bullet that still lists `/v1/build/*` helpers as deferred even though `build_inputs` shipped in 0.5.0.
- [x] `README.md`: if the quickstart gains a listing example, use `iterate_methods` rather than a page loop, so the idiom people copy is the one that cannot truncate.
- [x] `CHANGELOG.md` `[Unreleased]`: **Changed (breaking)** — `list_methods` returns `MethodPage` (items are `MethodSummary`, not `MethodData`), `list_runs` returns `RunPage`, `PipelineRun.method_id` / `pipe_code` are nullable, `MethodData.python` is `list[MethodFile]`; **Added** — `iterate_methods`, `iterate_runs`, `get_run_detail`, `MethodSummary` / `MethodPage` / `RunPage` / `RunDetail` / `RunErrorReport`, `MethodFile` with `parse_method_files` / `serialize_method_files`, the new `MethodData` fields, `MethodWriteInput.python`, the paging error; **Fixed** — name the crash plainly (iterating the envelope dict yielded its keys, so the first call was `MethodData.model_validate("items")`), and that the unit tests mocked the pre-paging shape.

### 3.6 Gate and commit

- [x] `make agent-check` and `make agent-test` pass.
- [x] Commit (suggested message: "Follow the platform's paged method and run lists and stop requiring nullable run fields").

### Checkpoint 2

- [x] Update this file: tick what landed, record the Phase 2 and Phase 3 commit SHAs, and note any place the Python shapes deliberately diverge from the JS mirror beyond snake_case (there should be none besides the `python` converter living here).

**Phase 2 landed in `2a8c589`, Phase 3 in `5e01c1b`.** Notes:

- **Divergences from the JS mirror beyond snake_case:** the `python` converter lives here rather than in `mthds-python` (the decision of `wip/updates.md` §7.2), and `parse_method_files` raises `ValueError` where the JS pair raises `PipelineRequestError` — because the Python parser is reached through a pydantic `field_validator`, where a `ValueError` is the idiomatic signal and surfaces to the caller as a `pydantic.ValidationError` like any other malformed response body. Nothing else diverges.
- **`MethodPage.items` / `RunPage.items` are required**, not defaulted empty. A page body with no `items` is malformed, and failing loudly is the whole point of this phase — the previous shape failed silently in the tests and loudly in production.
- **The shared test fixtures landed in a new `tests/unit/conftest.py`** (`api_client`, `wire_response`, `patch_send`), used by the two new modules. Migrating `test_client_product.py` onto them was left out as the plan allowed; it keeps its own equivalent private helpers.

## Phase 4 — `method_id` boundary type guard (`wip/updates.md` §4)

The 2026-08-25 decision in `pipelex-sdk-js/wip/boundary-option-type-validation.md`: a published client validates request-option types at its boundary and raises `PipelineRequestError` rather than dropping or forwarding a wrong-typed value. Its evidence names this repo's bare `if method_id:` in `_merge_hosted_run_extensions`, which drops falsy non-strings and forwards truthy ones to a server `422`.

- [x] `_merge_hosted_run_extensions` (`client.py` near line 975): replace `if method_id:` with an explicit presence check (`if method_id is not None`) followed by `if not isinstance(method_id, str): raise PipelineRequestError(msg)` naming the received type, then the existing empty-string-is-absent normalization. `None` and `""` still contribute nothing.
- [x] Update the docstring's `Raises:` and the "An absent or empty `method_id`" paragraph.
- [x] `tests/unit/test_client_method_id.py`: one parametrized test over wrong-typed values (`0`, `123`, `[]`, `["mt_1"]`, `{}`) asserting `PipelineRequestError` on `execute` and `start` before any request is sent; keep `test_empty_method_id_is_absent` green.
- [x] `docs/architecture.md` → "Hosted run extensions (`method_id`)": add a bullet that a non-string raises at the boundary, and why (one partition of wrong values across both SDKs).
- [x] `CHANGELOG.md` `[Unreleased]` → **Changed**: the guard, with the JS decision as the reason.
- [x] `make agent-check` and `make agent-test` pass.
- [x] Commit (suggested message: "Reject a non-string method_id at the client boundary").

## Phase 5 — wrap-up

- [x] `make check` (adds pylint to the agent gate) and `make agent-test` pass on the final tree.
- [x] Re-read `docs/architecture.md` end to end for any remaining claim the code no longer supports (parity, `Out of scope`, the validate section, the product section). Three further corrections beyond the ones §3.5 named: the intro line claiming "the full `0.1.0` surface", the parity section's "**Methods** — full coverage" (which contradicted the gap list directly above it), and the "**Models** — full field-for-field match" claim; the Models paragraph now also names the two deliberate divergences (snake_case `next_cursor`, the converter's home).
- [x] Re-read `CHANGELOG.md` `[Unreleased]`: every breaking item is labelled "breaking", no counts, no WIP-doc mentions, the version line untouched (still `0.5.0`).
- [x] `wip/updates.md` stays where it is, with its §7 decisions; this file stays too. Neither is deleted or emptied as part of finishing the work.
- [ ] Open the PR against `dev`, then follow `/review-pr-agents` for the Greptile / Codex loop (compare SHAs, not notifications; reply and resolve each thread in one pass).

### Checkpoint 3

- [ ] Update this file with the final commit SHAs, the PR number, and anything deferred out of the plan with the reason.

## Known gaps left open on purpose

- No e2e suite exists in this repo (`tests/` holds only `unit/`), so the live `views` gate and the live paging envelope are pinned only by mocked bodies here; the JS suite pins both live. Adding an e2e suite is separate work.
- The remaining `@pipelex/sdk` surfaces without a Python counterpart (`lint`, `format`, `resolve`, `codegen`, `build_output` / `build_runner` / `concept` / `pipe_spec`, `run_codegen_check`, `get_method_closure`) are untouched by the cited releases and stay deferred; Phase 3 makes `docs/architecture.md` say so.
- The protocol-argument type guards (`pipe_code`, `mthds_contents`) belong in `mthds-python` and arrive here with the `mthds` floor bump once that package ships its Phase 1.
