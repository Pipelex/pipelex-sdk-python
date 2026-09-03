# Input preparation (`upload_file` / `prepare_inputs`)

> **Status: implemented** (`pipelex_sdk/upload.py`, `pipelex_sdk/prepare_inputs.py`). `upload_file` and `prepare_inputs` are the Python counterpart of `@pipelex/sdk`'s `uploadFile` / `prepareInputs`, built on the raw `upload()` wire call. The design of record for the current shape is `pipelex-sdk-js/wip/prepare-inputs-selectors/design.md` in the sibling repo; the two SDKs are kept semantically identical.
>
> **Current scope.** `prepare_inputs` names the method three ways — inline `files`, a `method_ref` address, or a stored `method_id` — and reads the target pipe's signature from the standard's input-form descriptor. One piece is deliberately deferred and additive (it does not change this contract): the opt-in ingest of `http(s)` URLs into storage — for now an `http(s)` URL at a file position always passes through unchanged.

## Why this exists

A hosted run cannot see the caller's filesystem. Turning caller-local assets into run-ready inputs is therefore the SDK's job, not the runner's — the SDK process is the only component that can read the local file or hold the bytes. Today that work is re-implemented by every consumer (read file → base64 → `POST /v1/upload` → rewrite the input to the returned URI); `fenix-pipelex` is an early real-world example. `prepare_inputs` makes it one reusable, explicit operation.

Preparation is **explicit and separate from running.** `execute` / `start` never silently upload local files. The payoff: file-access errors happen *before* a run exists, prepared inputs are inspectable and reusable across a model sweep or retries without re-uploading, and `start` keeps a deterministic JSON-input contract.

## Parity note

This is the Python side of one cross-language contract. The behavior matrix, pass-through rules, `Dynamic` handling, dedup, and failure categories are identical to the JS SDK; only the accepted source types differ per language. The two SDKs must agree semantically. See the JS counterpart's `docs/input-preparation.md` for the mirror.

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
client.prepare_inputs(files=…,      pipe_ref=…, inputs=…) → PreparedInputs
client.prepare_inputs(method_ref=…, pipe_ref=…, inputs=…) → PreparedInputs
client.prepare_inputs(method_id=…,  pipe_ref=…, inputs=…) → PreparedInputs
```

Takes the **method** as exactly one of three selectors, the optional target **pipe**, and the caller's `inputs`; resolves the pipe's declared input signature; interprets the inputs top-down against it; uploads the file-bearing values; and returns `PreparedInputs`. Per input, the caller may submit **either** the compact value **or** the explicit `{concept, content}` envelope — see "[Compact or explicit-envelope inputs](#compact-or-explicit-envelope-inputs)" below:

- `inputs` — a **copy** of the caller's inputs with each asset reference replaced by the canonical content shape carrying `pipelex-storage://` in its `url` field (see "Rewritten-input shape" below). Copy-on-write: the caller's original object is never mutated.
- `uploads` — one `UploadRecord` per prepared asset, exposing `uri` so callers can log which source became which reference without reverse-engineering the rewritten object.

The prepared `inputs` are passed to the existing run lifecycle unchanged.

#### The three selectors

Exactly one per call. **Empty is absent** — `files=[]`, `method_ref=""`, `method_id="  "` — mirroring the run options' rule, so an empty selector may sit beside a real one without tripping the exclusivity check. None or several raises `InputPreparationError` naming the three forms, before any request leaves the process.

| Selector | What it is | Who resolves it |
| --- | --- | --- |
| `files` | the inline MTHDS closure (`MthdsFileItem` entries: `content` plus an optional `source` label) | nobody — inline |
| `method_ref` | a published method's address, `github.com/<owner>/<repo>[/<selector>][@<tag>]` | the runner, server-side (pipelex-api >= 0.21.0 fetches the repository at the tag) |
| `method_id` | a stored method's catalog id (`mt_…`) | the hosted platform, which injects the stored source before the runner sees the request |

Nothing is expanded client-side: `method_id` here is a **pass-through**, the same rule every other id-taking operation in this SDK follows.

#### Where the signature comes from

One `POST /v1/validate` per call, whatever the selector, asking for the **input-form descriptor**:

```python
await client.validate(<contents or selector>, True, …, views=[VALIDATION_VIEW_INPUT_FORM])
```

`allow_signatures=True` is deliberate. Preparation needs a pipe's *declared* inputs, and a bundle mid-authoring with an unresolved signature somewhere else must not be refused inputs for a pipe whose inputs are declared — whether the bundle runs is the run's verdict, not preparation's. An `is_valid: false` verdict still means the closure does not load, which is a preparation failure.

A `method_ref` makes the server clone a repository first; `validate` needs no special budget for it, because the route already defaults to the 20-minute execute ceiling.

**A valid report that carries no descriptor is an error, never a silent "no uploads".** The descriptor rides `views: ["input_form"]` on pipelex-api >= 0.18.0; pointed at an older runner, `prepare_inputs` says so rather than returning inputs whose local paths would travel to the runner verbatim.

#### Pipe selection

`validate` has no pipe selector — its report describes every pipe, keyed by qualified `pipe_ref` — so the helper picks one, in this order:

1. **`pipe_ref` when given.** Qualified-only: `domain.pipe_code`. A bare code, or a ref the method does not declare, is an `InputPreparationError` listing the qualified refs — one step to fix. The helper never grows a searched `pipe_code`: search is a run-route affordance, and the descriptor is keyed by qualified refs.
2. **The report's typed resolved default** (`default_pipe_ref`), once the runner serves it: the ref a caller gets by omitting the selector, manifest-aware for a fetched package. Read when present; a server that predates it sends nothing.
3. **The bundle's declared `main_pipe`**, read defensively from the opaque `bundle_blueprint` and qualified by its `domain`.
4. **The single pipe**, when the method declares exactly one.
5. Otherwise an `InputPreparationError` naming the candidates and asking for `pipe_ref`.

> **The manifest-only `main_pipe` gap.** A published package may name its entry pipe in `METHODS.toml` alone — `github.com/Pipelex/methods/documents` and `.../image_generation` do — and the validate report never carries a manifest. Until step 2's field ships, such a package needs an explicit `pipe_ref`; the error lists the candidates, so the fix is one line.

## Compact or explicit-envelope inputs

Each input may be submitted in **either** of two shapes, and preparation treats them equivalently:

- **Compact** — the bare value: a source string / `bytes` / `Path` / canonical `{"url": …}` content (e.g. `photo="…/p.png"`).
- **Explicit envelope** — the `{"concept", "content"}` shape (e.g. `photo={"concept": "native.Image", "content": {"url": "…"}}`). This is the template shape the hosted console and MCP hand agents to fill, so an agent that fills a template can hand it straight back.

When a value is an envelope (a dict whose keys are **exactly** `concept` and `content`, matching the runtime's `_is_explicit` in `input_shaper.py`), preparation unwraps `content`, interprets it exactly as the compact value would be, and **re-wraps** the result — so the concept annotation rides through to the run. The envelope's `content` may itself be a scalar, canonical file content, a list, or a structured object nesting file fields; the same top-down walk applies underneath.

## Signature-driven asset identification

The SDK **must not** guess that every string resembling a path is an asset — that would make ordinary text inputs environment-dependent and could upload unintended files. Interpretation comes from the method's **declared signature**, never from a value's shape alone. This mirrors the runtime's own top-down interpretation (`pipelex`'s `InputShaper`) combined with the file-reference resolution of `input_normalizer`, so local and hosted execution read the same compact inputs the same way.

The signature is the **input-form descriptor** (`InputForm` from `mthds.protocol.input_form`), and the walk is discriminated on each node's declared `kind`:

| Node kind | What the walk does |
| --- | --- |
| `document`, `image` | a **file position**, whatever the value's shape — resolved per the pass-through rules below |
| `object` | walks the declared `fields` by name against a dict value; keys the descriptor does not name are copied through untouched |
| `list` | walks `item` against each element of a list value |
| `text`, `prose`, `date`, `number`, `boolean`, `enum`, `unknown` | passes through at any depth |

An **optional** field (`required: false`) is walked when the caller supplies it. A caller value whose shape disagrees with the node — a scalar at an `object`, a non-list at a `list` — passes through for the run to reject; preparation never second-guesses the signature.

`unknown` is the standard's escape hatch for a `Dynamic` or `Composite` input, and it is **not** entered: the signature declares no file there. A caller with such an input uploads with `upload_file` first and passes the resulting `pipelex-storage://` URI.

### Why the descriptor, and why not the inputs template

Earlier releases read the signature from the explicit inputs template (`POST /v1/build/inputs`), which marked a file position by rendering a `{"url": …}` dict. That is a side effect of a field being **named** `url`, not of its concept being an Image or a Document, and two positions were misread as a result:

- an **optional nested file field** was never rendered by the required-only template, so its position was invisible and the caller's local path travelled to the runner as a literal string;
- a **text field merely named `url`** was read from disk and uploaded.

The descriptor states the resolved kind at every depth and includes optional fields, so both are gone. It is also the standard's own artifact, derived from authored facts rather than from a rendered shape, and `/v1/validate` resolves all three method selectors server-side — which is what made the uniform selector surface possible at no server cost.

**Known limit.** A class-backed concept (`structure = "SomeClass"`) whose reflection cannot map a field annotation collapses to `kind: "unknown"` in the descriptor, so a file field beneath one is invisible to this walk. That is a fidelity bug in the runtime's `build_input_form`, tracked separately; pass such a value as an already-uploaded storage URI until it is fixed.

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
