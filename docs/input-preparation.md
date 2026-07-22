# Input preparation (`upload_file` / `prepare_inputs`)

> **Status: implemented** (`pipelex_sdk/upload.py`, `pipelex_sdk/prepare_inputs.py`, `pipelex_sdk/build_models.py`). This document records the contract (design source: `wip/upload/README.md` in the workspace, tracked in `TODOS.md`). `upload_file` and `prepare_inputs` are the Python counterpart of `@pipelex/sdk`'s `uploadFile` / `prepareInputs`, built on the raw `upload()` wire call. This work also added the `build_inputs` route (the signature source), which the Python SDK previously lacked.
>
> **Current scope.** `prepare_inputs` takes the method closure as inline `files` (the signature source). Two pieces are deliberately deferred and additive (they do not change this contract): resolving a closure from a catalog `method_id`, and the opt-in ingest of `http(s)` URLs into storage — for now an `http(s)` URL at a file position always passes through unchanged. Kept in parity with `@pipelex/sdk`.

## Why this exists

A hosted run cannot see the caller's filesystem. Turning caller-local assets into run-ready inputs is therefore the SDK's job, not the runner's — the SDK process is the only component that can read the local file or hold the bytes. Today that work is re-implemented by every consumer (read file → base64 → `POST /v1/upload` → rewrite the input to the returned URI); `fenix-pipelex` is an early real-world example. `prepare_inputs` makes it one reusable, explicit operation.

Preparation is **explicit and separate from running.** `execute` / `start` never silently upload local files. The payoff: file-access errors happen *before* a run exists, prepared inputs are inspectable and reusable across a model sweep or retries without re-uploading, and `start` keeps a deterministic JSON-input contract.

## Parity note

This is the Python side of one cross-language contract. The behavior matrix, pass-through rules, `Dynamic` handling, dedup, and failure categories are identical to the JS SDK; only the accepted source types differ per language. The two SDKs must agree semantically. See the JS counterpart's `docs/input-preparation.md` for the mirror.

The Python SDK previously had **no `/v1/build/*` coverage** — this change added the `build_inputs` counterpart `prepare_inputs` needs to resolve the declared signature (the JS SDK already exposes `buildInputs`).

## The two operations

### `upload_file` — single-asset convenience

Uploads one asset and returns its upload record. It is the language-native convenience over the raw `upload()` wire call (base64 JSON body), assembling the record client-side.

- **Accepted sources:** `str` and `pathlib.Path` filesystem paths, and raw `bytes`. `upload_file` treats every string as a **filesystem path** — a URL handed directly to it is read as a local file and fails.
- The URL / storage-URI **pass-through** (leaving **HTTP(S) URLs** and existing **`pipelex-storage://` URIs** untouched) is a `prepare_inputs`-level behavior (see pass-through rules below), not a feature of `upload_file` itself: `prepare_inputs` classifies each string (URL vs local path) against the declared signature *before* it reaches `upload_file`.
- Open file objects and streams are **deferred** — they can be added later without removing anything.

The returned **upload record** guarantees, beyond the source identity:

| Field | Guarantee |
| --- | --- |
| `uri` | The `pipelex-storage://` reference for the uploaded asset. |
| content type (MIME) | Known client-side at upload time. |
| size (bytes) | Known client-side at upload time. |
| filename | Already in the wire model. |

The MIME type and size are known client-side, so the record is assembled without extending the `/v1/upload` response. There is deliberately **no checksum field**: within-preparation dedup keys on source identity (not hashing), and cross-preparation dedup is a hosted storage-policy concern (Phase 5).

### `prepare_inputs` — signature-driven input preparation

```
prepare_inputs(method_ref, pipe, inputs) → PreparedInputs
```

Takes the **method reference** (bundle files or catalog `method_id`) plus the target **pipe**, resolves the pipe's declared input signature, interprets the caller's compact `inputs` top-down against that signature, uploads the file-bearing values, and returns `PreparedInputs`:

- `inputs` — a **copy** of the caller's inputs with each asset reference replaced by the canonical content shape carrying `pipelex-storage://` in its `url` field (see "Rewritten-input shape" below). Copy-on-write: the caller's original object is never mutated.
- `uploads` — one upload record per prepared asset (the `upload_file` record shape), exposing `uri` so callers can log which source became which reference without reverse-engineering the rewritten object.

The prepared `inputs` are passed to the existing run lifecycle unchanged.

## Signature-driven asset identification

The SDK **must not** guess that every string resembling a path is an asset — that would make ordinary text inputs environment-dependent and could upload unintended files. Interpretation comes from the method's **declared signature**, never from a value's shape alone. This mirrors the runtime's own top-down interpretation (`pipelex/pipelex/core/memory/input_shaper.py`, `InputShaper`) combined with the file-reference resolution of `pipelex/pipelex/pipeline/input_normalizer.py`, so local and hosted execution read the same compact inputs the same way.

The declared signature is resolved via the explicit inputs template (`build_inputs` with `explicit=True`), which carries concept identity, canonical content shape, and multiplicity per input.

Interpretation per declared input:

- A bare string, `Path`, or `bytes` value at an **Image/Document-declared** input is a **file reference**: local paths, data URLs, and bytes are uploaded and rewritten to `pipelex-storage://` URIs; HTTP(S) URLs and existing `pipelex-storage://` URIs pass through unchanged.
- The **identical** bare string at a **Text-declared** input is text and is never touched.
- **Canonical image/document content structures** are recognized by their URL-bearing fields wherever they appear, including nested in structured objects and lists — exactly as the runtime normalizer walks them. The refining case matters: a concept refining `Image`/`PDF` is classified by the **canonical content shape**, not by the concept ref alone.
- Inputs declared **`Dynamic`** are not path-interpreted (the signature genuinely cannot guide them); they accept canonical content structures or already-prepared references only.
- A repeated reference to the **same source** within one preparation is uploaded once and rewritten consistently (within-preparation dedup by source identity).

### Pass-through rules

| Source at a file-bearing input | Action |
| --- | --- |
| Local path (`str`/`Path`) / data URL / `bytes` | Upload → rewrite to `pipelex-storage://` |
| Existing `pipelex-storage://` URI | Already prepared — pass through unchanged |
| HTTP(S) URL | Pass through unchanged, **unless** the caller explicitly asks to ingest it into Pipelex storage |

## Rewritten-input shape: `url` carries the URI

The runtime's canonical image/document content stores its reference in a **`url`** field. Preparation emits inputs the runtime interprets natively, so a rewritten input keeps the canonical content shape with `url` holding the `pipelex-storage://` value — exactly what the runtime's `input_normalizer` writes.

The "uploaded reference is named `uri`" decision applies to the **upload surface**: the raw upload result and each upload record expose the storage reference as `uri`. Preparation must **not** invent a `uri` field inside rewritten inputs — that would produce inputs the runtime does not recognize.

## Error and capability behavior

Upload is a **hosted Pipelex-product capability**, even though the SDK can be pointed at other base URLs. A deployment that does not support upload must raise a specific, actionable exception — preparation must never silently leave a local path in place and let a later run fail obscurely.

The contract distinguishes at least these semantic outcomes (exact typed exception classes are settled during implementation):

- **invalid local source** — missing or unreadable path;
- **rejected asset** — the server refused it (e.g. a `413` past the service-defined size cap — see "Storage policy" — surfaced as a clear rejection, not a raw transport error);
- **unsupported server capability** — the configured deployment has no upload route;
- **authentication / authorization failure** — `401` / `403`;
- **transport failure** — network / server fault.

All preparation failures are raised **before any run is created**.

## Storage policy (inherited, Phase 1)

The SDK ships against **today's route behavior**: a service-defined size cap (hosted default 50 MiB via `MAX_UPLOAD_MIB`, rejected with `413`), auth required, per-user keys, and nothing else — no MIME validation, retention, quotas, dedup, or cleanup. The SDK documents limits as **service-defined** and surfaces server rejections as clear "rejected asset" errors; it does **not** hardcode a client-side cap. Real storage policy (retention, quotas, org scoping, cleanup) is a later hosted-owner deliverable.

## Stability across the future endpoint move

The public abstraction sits deliberately **above** the HTTP route. Callers depend on `upload_file` / `prepare_inputs`, the `uri` result field, and the `pipelex-storage://` scheme — never on which backend service owns the route. The current transport is `POST /v1/upload` on `pipelex-api`; when hosted storage upload later moves to `pipelex-platform` (together with its paired resolution route, as one storage domain), the public path and wire shape are kept compatible so released SDK versions keep working, and any wire-protocol change is absorbed inside the SDK's upload transport. `upload_file`, `prepare_inputs`, and the prepared run-input shape stay stable across that move.
