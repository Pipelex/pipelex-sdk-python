---
status: draft
item: L-260829-8a25d5
---

# `prepare_inputs`: three selectors, signature from the input-form descriptor

The Python half of the workspace campaign retiring `/v1/build/*` (epic `L-260829-848001`, `wip/build-retirement/` at the workspace root).

## The design of record is the JS one

This repo writes no second design. `pipelex-sdk-js/wip/prepare-inputs-selectors/design.md` holds the investigation, Louis's ruling of 2026-08-29, the surface, the walk, the pipe-selection ladder, the error wordings, the alternatives rejected and the known limits — and it names this item's mandate explicitly: `prepare_inputs` lands the same surface, and because `build_inputs` and `BuildInputsRequest` exist here only to back it, this item deletes them.

The JS twin (`L-260829-300c50`) landed as `pipelex-sdk-js` PR #42 (`bea4632`) and is the reference implementation. Divergence from it is a bug unless recorded below.

## What this repo did

- `prepare_inputs(client, *, files=None, method_ref=None, method_id=None, pipe_ref=None, inputs)` — keyword parameters rather than JS's `never`-pinned discriminated union, matching how `validate` already takes its selectors here. Empty-as-absent and the exactly-one check run before any request, raising `InputPreparationError`.
- One `validate(..., allow_signatures=True, views=["input_form"])` per call; the walk is a `match` over the descriptor's item classes rather than over `kind`, because each `*Field` derives from its `*Item` — one set of patterns covers the named layer (top level, `object.fields`) and the nameless one (`list.item`), and it narrows for pyright where matching on `node.kind` would not.
- `PipelexValidationReport.default_pipe_ref` added ahead of the server (`L-260829-0208c7`), as JS did.
- `build_inputs` and the `BuildInputs*` models deleted. `build_models.py` deleted with them: the three survivors it also held — `MthdsFileItem`, `CrateRequestBase`, `CrateInvalidReport` — moved to `crate_models.py`, beside the routes that still use them.
- The explicit `{concept, content}` envelope is now accepted. This was a **pre-existing parity gap**, not part of the item's letter: JS gained it in an earlier release and Python never did, so the two SDKs would not have been identical after the fix. Ruled in scope with the user on 2026-08-30.

## Decisions taken here

| Decision | Why |
|---|---|
| Keyword selectors, not a request model | The repo's own `validate` idiom; `architecture.md` already records the JS-vs-Python signature-shape divergence as idiomatic per language. |
| `build_inputs` deleted, where JS kept `buildInputs` | JS has a wrapper family (`buildOutput`, `buildRunner`, `concept`, `pipeSpec`) retiring together under `L-260829-eefc3f`. Python only ever had this one, added in 0.5.0 solely to back `prepare_inputs`. |
| `build_models.py` folded into `crate_models.py` | A module named for the build routes cannot go on owning the crate envelope after those routes leave. |
| A local `_non_empty_string`, not `client._normalized_selector` | That helper is private to the client boundary and raises `PipelineRequestError`; every failure of this module owes an `InputPreparationError`. |
| No fetch budget on the signature call | `validate` already rides the 20-minute ceiling; the 3-minute budget exists to *raise* the ~30s poll-ceiling routes. JS implemented this and reverted it — do not re-add. |

## What this supersedes

`wip/pr-11-review-notes.md` recorded a nested-file limitation of the old template walk: a top-level `url` key caused an early return, so a sibling file field went un-uploaded, and the note explained that shape refinement was ambiguous because the walk dropped the envelope's `concept`. The descriptor walk removes that class of problem structurally — position and kind are stated, never inferred — so the note is history, not open work.

## Release

None from this item directly. The change lands on `dev` and records its warrant under `## [Unreleased]`; `/release` cuts the version. `L-260826-ddd843` (the two misclassifications) closes only when **both** SDKs have shipped a release carrying the fix — the JS half was still unreleased when this landed.
