# Typing the descriptor and the contracts by import (`mthds.protocol`)

This is this repo's tracker for **Stage 3.4** of the workspace input-form program (`../../wip/input-form/plan.md`), carried by ledger item `L-260826-c9b76b`. The program plan holds the *why* and the sequence; this file holds what the change is here, the decisions taken while making it, and what a reader needs to know afterwards.

## The instruction

Stage 3.4 applies decision **D-1**: the wire types of the input-form descriptor and of the pipe I/O contracts belong to the standard's clients — `mthds/protocol` in TypeScript, `mthds.protocol` in Python. Every SDK therefore narrows its opaque field **by import** rather than by restating the shape. Here that means two fields of `PipelexValidationReport` stop being bare mappings and start being the standard's own models, and the `mthds` floor moves to the version that publishes them.

## What this retires, and why it is not a reversal

The first program ruled (its D4) that `input_form` stays opaque, and the reason it gave was ownership plus drift: the descriptor vocabulary is owned elsewhere, so a second copy inside this SDK would be free to drift from it. That reasoning was sound and its conclusion is now obsolete, because the premise changed. When D4 was taken, no published Python package declared the descriptor, so "type it here" could only mean "restate it here" — a copy, and therefore drift. Since `mthds` 0.9.0 the standard's own client declares both artifacts, so typing them here means importing them: one declaration per language, nothing to drift from. D-1 supersedes D4 on that basis, and the principle D4 was protecting — this SDK is transport and does not own these types — is exactly what an import preserves and a restatement would have broken.

## Where the boundary now sits

Two fields of the valid arm are typed by import: `pipe_io_contracts: PipeIOContracts` (from `mthds.protocol.pipe_io_contracts`) and `input_form: InputForm | None` (from `mthds.protocol.input_form`). Two remain opaque, and for the reason that used to cover all four: `bundle_blueprint` and `graph_spec` have no published declaration to import, so a type here could only be a copy. When one of them gets a standard page and a client model, it moves the same way.

The types are imported and used, never re-exported from `pipelex_sdk`. Re-exporting them would put this package's name on a vocabulary it does not own and would give consumers a second import path to drift against; a consumer that wants to name a node's type imports it from `mthds.protocol.input_form` directly, which is also how it reaches the per-kind models for narrowing.

## Strictness: closed artifacts inside an open envelope

This is the one thing worth getting exactly right, because getting it wrong turns a strictness improvement into a regression.

The standard's models are **closed** shapes (`extra="forbid"`, decision D-5): a member this version of `mthds` does not define is version drift and fails the parse. The validate report itself is **extension-open** per the protocol's extension policy, and stays that way — `PipelexValidationReport` inherits `model_config = ConfigDict(extra="allow")` from `mthds`'s `ValidationReport`, and declaring two typed fields on a subclass does not touch that config. So the two closures compose the way the standard intends and nest rather than spread:

- An unrelated field the server adds to the **report** — a new artifact, a cost estimate, another opt-in view — still parses and still rides `model_extra`, exactly as before this change. The `input-form-does-not-close-the-report` test pins that, and it is the regression guard against a future edit that reaches for `extra="forbid"` on the envelope.
- An undefined member **inside** a contract or a field descriptor now fails the parse. That is deliberate, it is the standard's own rule rather than this SDK's invention, and it is scoped to the artifact.

## The break

A valid report whose `pipe_io_contracts` predates the reshape — an input contract carrying the boolean `optional` instead of `presence`, or missing `multiplicity` / `item_count` — no longer parses, where before it rode through untyped. The hosted plane emits the reshaped contracts, so this is a break against runners older than that reshape and not against the API this SDK targets. No compatibility shim: an artifact that does not conform to the version of the standard this package pins is version drift, and reporting it at the parse is the whole point of D-5.

## Checklist

- [x] `mthds` floor moved to `>=0.9.0` in `pyproject.toml`, lockfile refreshed.
- [x] `pipe_io_contracts` and `input_form` typed by import in `pipelex_sdk/validation_models.py`, with the module docstring stating which members are typed by import, which stay opaque, and why.
- [x] Wire fixtures in `tests/unit/test_validation_contract.py` updated to conformant payloads — they were written for the opaque era and state neither the reshaped contract members nor the descriptor's pipe-slot facts.
- [x] Tests: the artifacts read as typed members; the report stays extension-open around them; drift inside an artifact and a violated cross-field invariant both fail the parse; a pre-reshape contract no longer parses.
- [x] `docs/architecture.md` — the validate section says what is typed and what stays opaque, and the paragraph that told a reader to go read the spellings out of an opaque mapping is retired.
- [x] `README.md` — the import map names where the descriptor and contract types come from.
- [x] `CHANGELOG.md` under `## [Unreleased]`.

## Release

None from this item. Decision **D-7** of the program plan: every Stage 3 repo lands on `dev` and records its warrant under `## [Unreleased]`; the versions are cut together at the plan's release cascade (item 4.0), when Stage 4 needs published artifacts. Where the plan or the ledger item says "minor bump" for this item, that means the changelog warrant, not a version cut.
