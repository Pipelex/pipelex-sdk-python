# Artifact download (`locate_artifacts` / `collect_artifacts` / `resolve_artifacts` / `fetch_artifact` / `download_artifacts`)

> **Status: implemented** (`pipelex_sdk/artifacts.py`, with its shapes in `pipelex_sdk/artifact_models.py`). This is the download twin of [input preparation](./input-preparation.md): where `prepare_inputs` turns local files into `pipelex-storage://` references before a run, these operations turn the references a run produced back into bytes on disk, or into a bounded stream, afterwards. They are layered so that each is usable without the next, and they are the Python twins of `@pipelex/sdk`'s `locateArtifacts` / `collectArtifacts` / `resolveArtifacts` / `fetchArtifact` / `downloadArtifacts`.
>
> **They need a platform that serves the bulk resolve route.** Everything below the pure walk mints its links through `POST /v1/resolve-storage-url/bulk`, a hosted-platform route. The public bare runner (`pipelex-api`) has no resolve route at all, single or bulk, and a hosted deployment that predates the route answers a `404`; in both cases the operation raises the existing `ApiResponseError` and nothing is downloaded. `resolve_storage_url`, the single-reference primitive, stays for the callers that have one link to mint.

## Why this exists

A run that produces an image, a PDF or a document does not embed the bytes in its results. The content carries the file's durable reference — a `pipelex-storage://` URI in its `url` field — beside a `public_url` the storage provider signed when the run wrote the file. That signed link is short-lived and must not be stored, so every consumer that wanted the file had to walk the result for references, mint a fresh link per reference, and stream each link to disk within sensible bounds. `download_artifacts` makes it one explicit operation, and the layers under it make each step reusable on its own.

Downloading is **explicit and separate from running**, the input-preparation rule in reverse: `execute` / `start` never silently upload, and `start_and_wait` never silently downloads. There is no `download` option on any run call. A download is its own gesture, inspectable and repeatable — by run id, days after the run.

Nothing here reads the embedded `public_url`. Every link is minted fresh by the platform, which is what makes a download work long after the embedded link died, and what keeps the tenant boundary where the platform enforces it.

## The operations

### `locate_artifacts(value)` and `collect_artifacts(value)` — the pure walk

```python
from pipelex_sdk.artifacts import collect_artifacts, locate_artifacts

locations = locate_artifacts(results.main_stuff)
# [ArtifactLocation(uri="pipelex-storage://org/runs/01J…/outputs/2325fcfe.png",
#                   found_at=["$.rooms[0].staged_photo.url"]), …]

uris = collect_artifacts(results.main_stuff)
# ["pipelex-storage://org/runs/01J…/outputs/2325fcfe.png", …] — the same walk, references only
```

Walks any JSON-shaped value and returns every string that **is** a `pipelex-storage://` reference — the whole string, scheme first, with something after the scheme. A string that merely contains a reference does not count; the bare scheme does not count; nothing else is looked at. The result is deduplicated and kept in discovery order, the order of each reference's first sighting. It is a contract rather than a heuristic, because the scheme is unambiguous: the runtime serializes a produced file as content carrying its reference in `url`, and nothing else on the wire starts that way. Mappings, lists and pydantic models are walked alike, a model by its `model_dump()` keys, so a parsed `main_stuff` and a whole `RunResults` both work.

`locate_artifacts` also says where each reference sits. It answers one `ArtifactLocation` per reference (in `pipelex_sdk.artifact_models`), whose `found_at` lists every path at which the reference occurs, in walk order, so a reference the output repeats is one entry with several paths, and `found_at[0]` is where it was first seen — the path a saved file is named after. `collect_artifacts` is the same walk's references alone.

**Path notation.** A path is rooted at `$`, the walked value itself. An object key matching `^[A-Za-z_][A-Za-z0-9_]*$` is written `.key`, any other key `["…"]` in JSON string escaping, and an array index `[n]`. So a nested field reads `$.rooms[3].staged_photo.url`, a list member `$.items[0].url` (a list output arrives as the `{"items": [...]}` envelope), a key that is not an identifier `$["a key"].url`, and an output that is itself a reference `$`. A path is the exact location of the string, the final `url` of a content object included, so a consumer can follow it into the JSON without guessing. The escaping is `JSON.stringify`'s — the quote, the backslash and the control characters are escaped, any other character is kept as typed, and a surrogate standing alone is written `\uXXXX` — so the same output yields the same paths from either SDK.

Both are pure — no network, no key — so a consumer can count, list or place a result's files without resolving any of them.

### `resolve_artifacts(uris)` — fresh links for a whole list

```python
resolved = await client.resolve_artifacts(uris)
for item in resolved:
    if item.error is None:
        ...  # item.url is fetchable now; item.expires_at says until when; item.content_type may be None
    else:
        ...  # item.error is {code, detail} — the route's own per-reference refusal
```

One call to the platform's bulk route for the whole list, chunked at the route's bound of `BULK_RESOLVE_MAX_URIS` references per request, answering one `ResolvedArtifact` per reference **in request order, duplicates included**. A resolved item carries `url`, `expires_at` (UTC, ISO 8601) and `content_type` (`str | None` — the platform's guess from the reference's extension, `None` when it has none) with `error` at `None`; a refused item carries `error` as an `ArtifactItemError` (`code`, `detail`) with the three link fields `None`. The codes are the route's: `invalid_storage_uri` for a malformed reference and `forbidden` for one belonging to another organization. A consumer branches on `error`, never on an HTTP status, because the request is a `200` whenever every reference got a verdict.

Only what is not about a reference raises: the route's whole-request refusals (a caller with no organization, a request over the bound or with an unknown field, a signing failure, a deployment without the route) as `ApiResponseError`, and an unreachable host as `ApiUnreachableError`. An empty list resolves to an empty list with no request made. A link lives about fifteen minutes; resolve close to the moment of use.

This is where every browser-side or server-rendering consumer stops: it mints links on the server and hands each one to a client that uses it immediately. `resolve_storage_urls_bulk` is the raw wire call underneath it (one request, at most the bound), the way `upload` sits under `upload_file`.

### `fetch_artifact(uri)` — one bounded stream

```python
from pipelex_sdk.artifacts import fetch_artifact

async with fetch_artifact(client, uri) as stream:
    # stream.status_code, stream.headers and stream.content_type are the store's own
    async for chunk in stream.aiter_bytes():
        ...
```

Resolves the reference fresh and hands the object store's response over as a bounded stream. It is an **async context manager**, not a returned response: an httpx stream is only live inside its own block, so the block is where the bytes are read and where the connection is released. `client.fetch_artifact(uri)` is the same thing as a method. The bounds:

- **A timeout** covering the connection, the headers and the whole body (`timeout_seconds`, default 120 s), applied twice: as a budget over the whole exchange and as httpx's own per-operation timeout, which is the per-stall bound a total budget alone does not give.
- **Redirects refused**: a presigned link has no reason to redirect, and one that does is refused rather than followed.
- **The byte cap enforced mid-stream** (`max_bytes`, default 1 GiB): a declared `Content-Length` over the cap is refused before a byte is read, and a body that crosses the cap while streaming raises out of the iteration — never buffered.
- **No credentials forwarded**: the request carries no headers of ours, and it runs on its own httpx client rather than the API client's. The link's authorization is in its query string, and nothing else may ride along to the store.
- **Plain `http:` refused** unless `allow_http=True`. A general-purpose library does not fetch over plain http silently; the local compose stack's object store hands out such links, and that is what the option opts into.

It is **header-neutral**: the status and headers are the store's own. The one change is that a `Content-Encoding` the installed httpx actually decoded — `gzip` and `deflate`, plus `br` and `zstd` only when their optional packages are installed, read from httpx's own decoder table — is dropped with the encoded `Content-Length`, since the body handed on is the decoded bytes; any other coding keeps both headers beside its still-encoded body. A proxy relaying it therefore owns the response hygiene — `X-Content-Type-Options: nosniff`, a sandboxing CSP on the asset response, a controlled `Content-Disposition`, private caching — and must set them itself.

Only a `2xx` is yielded. Anything else raises an `ArtifactFetchError` whose `code` says why, in the same closed vocabulary the download verdict uses per item: the route's `invalid_storage_uri` / `forbidden`, then `unsupported_url` (not a URL, not http(s), or carrying credentials), `plain_http_refused`, `redirect_refused`, `store_refused` (a 401/403 from the store — the link is freshly minted, so this is the store refusing a fresh signature, not an expired link), `not_found` (404/410), `store_error` (any other non-2xx, with `status`), `too_large`, `timeout` and `network`. Cancelling the awaiting task raises `asyncio.CancelledError` as-is. `download_artifacts` and a proxy share this boundary: the download is the same fetch followed by a write.

### `download_artifacts(run_id | results, dir_path, …)` — the files on disk

```python
from pipelex_sdk.artifact_models import ArtifactScope, DownloadArtifactsOptions
from pipelex_sdk.artifacts import download_artifacts

verdict = await download_artifacts(
    client,
    dir_path="./out/01J…",
    run_id="01J…",  # or: results=<a RunResults in hand>
    options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),  # default ArtifactScope.MAIN_STUFF
)

print(verdict.saved_paths)
if not verdict.all_saved:
    for artifact in verdict.artifacts:
        if artifact.error is not None:
            print(artifact.uri, artifact.error.code, artifact.error.detail)
```

`client.download_artifacts(dir_path=…, run_id=…)` is the same thing as a method.

**Where it reads from.** Exactly one of `run_id` and `results`. A `run_id` re-reads the results through `get_run_result`, so a completed run is downloadable days later from its id alone; a `RunResults` already in hand is read as it is, with no request. `scope` picks the artifact walked for references: `main_stuff` (the default) is the run's output, and `working_memory` is the opt-in that also brings down the echoed inputs and every intermediate stuff — it is read off `RunResults.working_memory`, the declared field both paths deliver ([`run-results.md`](./run-results.md#working_memory--every-named-stuff-of-the-run)).

**How it downloads.** The whole set is resolved through the bulk route ahead of the work, then an `asyncio.Semaphore` bounds how many references are in flight at once (`concurrency`, default 4) over the whole per-reference pipeline: fetch, create the file exclusively, stream the body in. Resolution is just-in-time where it matters: a link that has expired by the time its task reaches it — a large set downloaded a few at a time can outlive the fifteen-minute link — is resolved again for that reference alone, so no fetch ever runs on a stale signature.

**Filenames: each file is named after the field it fills.** The name comes from the first path in the reference's `found_at`, by the rule `artifact_filename(location, content_type, scope)` (exported) applies:

1. A final `url` key is dropped, since the runtime's image and document contents carry their reference there. A reference under any other key keeps that key, and a `url` key that is not final is kept.
2. Each key is reduced to `[A-Za-z0-9_]`, every other character becoming `_` — `-` and `.` included, since one is the separator and the other would fake an extension. An index stays its digits.
3. The segments are joined with `-`. A reference that is the walked value itself, or its `url`, has no segment left and takes the scope's name, so an output that is one image is saved as `main_stuff.png`.
4. A name over the length cap (128 characters, extension included) keeps its tail: whole leading segments are dropped first, since the last ones are the specific ones, and a single segment still too long is cut to fit.
5. A stem Windows reserves for a device — `con`, `prn`, `aux`, `nul`, `com0` to `com9` or `lpt0` to `lpt9`, in any case — gets a trailing `_`, so a field named `aux` is saved as `aux_.png`. Windows reserves those names whatever the extension, and a field name is the method author's to choose.
6. The extension is the one the storage key's last segment carries, reduced to `[A-Za-z0-9]`, when it has a short one; otherwise the content type's, for the types a run produces (`image/png` gives `.png`, `application/pdf` gives `.pdf`); otherwise there is none. The segment is percent-decoded the way `decodeURIComponent` decodes it, and read as typed where that would fail, so both SDKs find the same extension in the same key.

For example, a home-staging method whose output is

```json
{
  "rooms": [
    {
      "original_photo": { "url": "pipelex-storage://org/assets/53174b03.png", "public_url": "…" },
      "staged_photo": { "url": "pipelex-storage://org/runs/01J…/outputs/2325fcfe.png", "public_url": "…" }
    },
    { "original_photo": { "url": "…" }, "staged_photo": { "url": "…" } }
  ]
}
```

is saved as `rooms-0-original_photo.png`, `rooms-0-staged_photo.png`, `rooms-1-original_photo.png` and `rooms-1-staged_photo.png`, where the storage keys alone (`53174b03.png`, `2325fcfe.png`) would not say which picture is which. Each verdict item's `found_at` carries the unreduced path, `$.rooms[0].staged_photo.url`.

The result is always a bare filename — ASCII letters, digits, `_` and the `-` joins, then an optional extension — never empty, never starting with a dot and never a device name, so it can name nothing but a regular file directly inside `dir_path`. `artifact_filename` takes an `ArtifactLocation` from `locate_artifacts`, which is how a consumer predicts a name before downloading; a `DownloadedArtifact` is an `ArtifactLocation` too, so a verdict item can be passed as it is. It raises `ArtifactOperationError` for anything that is not an `ArtifactLocation`, for a location whose `found_at` is empty or whose `found_at[0]` is not a path in the notation above, and for an unknown scope.

Files are **never overwritten**: a name already on disk gets a numeric suffix (`report-1.pdf`, `report-2.pdf`), through exclusive creation (`os.O_EXCL`) rather than an exists-check, so two tasks cannot race for one name. Two references whose paths reduce to one name (`"staged photo"` and `"staged-photo"`) are told apart by the same suffix, and the verdict says which file is which. A reference found at several paths is saved once, under the name of the first. The directory is created if missing.

**Cleanup.** A failed or cancelled download unlinks its partial file; nothing truncated is ever left under a final name.

**The verdict.** A `DownloadArtifactsResult`, one entry per reference in discovery order: `scope`, `artifacts` (each a `DownloadedArtifact` with `uri`, `found_at`, `path`, `content_type`, `size` and `error`), `saved_paths` (the absolute paths of the saved ones, same order) and `all_saved`. `len(verdict.artifacts)` is the count of references walked, errors included. An empty walk over a present scope — an output that references no stored file — is a verdict with empty lists and `all_saved=True`, not an error, and it touches neither the network nor the disk. `found_at` is the reference's paths in the walked scope, exactly as `locate_artifacts` reports them, and `content_type` is the platform's guess from the reference, known before the fetch; both are on both arms, so an item that was not saved still says which field it would have filled.

Per-item `error.code` is the fetch vocabulary above plus the download's own: `resolve_failed` (an expired link could not be re-resolved, for a reason that is not the credential), `total_limit_exceeded` (the item that would take the call past `max_total_bytes`; an item refused only because files still in flight hold the room stops nothing else, since one of them may yet fail and give it back), `write_failed` (the file could not be created, written or closed) and `aborted` (not yet started when a credential failure stopped the call).

**What it raises.** Only conditions with no verdict, all typed:

- `RunStillRunningError` (with the retry hint) or `RunFailedError` — a `run_id` naming a run that has not completed;
- `FieldNotIncludedError` — the results read never carried the scope's key, so it is absent from `results.model_fields_set`. This is the Python reading of the JS `undefined`: ask for the key and read again;
- `ScopeUnavailableError` — the key WAS relayed and its value is `None`, which is the platform saying it has no such artifact for this run (`scope` and `run_id` on the error). Reading by `run_id`, a null `main_stuff` is already `MissingMainStuffError` from `get_run_result`;
- `ArtifactAuthenticationError` — the resolve route refused the credential (`401` / `403`), on the first resolve or on a re-resolve part-way through. It carries `verdict`, the result as it stood: the refusal stops the remaining references being taken but lets the fetches already running finish, since they are on presigned links that do not carry the credential, so every file saved is real and listed and the rest are marked `aborted` with a detail naming the credential failure;
- `ArtifactOperationError` — an unusable directory, both selectors or neither, an unknown `scope` (one that reached the call unvalidated, since `DownloadArtifactsOptions` refuses it at construction), or nonsense bounds;
- and the transport and lifecycle errors of the reads it makes, unchanged: `ApiResponseError` for a deployment without the bulk route, `RunLifecycleUnavailableError` for a bare runner asked by `run_id`, `ApiUnreachableError`.

Everything else that can go wrong with one reference is that reference's `error`.

**Options and defaults**, all on `DownloadArtifactsOptions` (`FetchArtifactOptions` is its first three):

| Option            | Default                  | What it bounds                                                                      |
| ----------------- | ------------------------ | ----------------------------------------------------------------------------------- |
| `scope`           | `ArtifactScope.MAIN_STUFF` | the artifact walked for references                                                  |
| `concurrency`     | `4`                      | artifacts in flight at once, held by an `asyncio.Semaphore`                          |
| `max_bytes`       | 1 GiB                    | one file, from `Content-Length` and again mid-stream                                 |
| `timeout_seconds` | 120 s                    | one file's whole exchange, and httpx's per-operation timeout with it                 |
| `max_total_bytes` | 4 GiB                    | the bytes the whole call saves, a file in flight counting its declared length        |
| `allow_http`      | `False`                  | whether a plain `http:` link is fetched                                              |

The caps are accident guards against filling a disk from a runaway output, not judgments about artifact size.

## What differs from `@pipelex/sdk`, and why

The contract is the JS one — the same operations, the same defaults, the same verdict shape, the same error taxonomy and the same safety rules. What differs is idiomatic, and is the same set of ports the SDK already makes elsewhere (see the parity section of [`architecture.md`](./architecture.md)):

- **Seconds, not milliseconds** (`timeout_seconds`), as everywhere else in this package.
- **`asyncio` cancellation, not an `AbortSignal`.** There is no `signal` option and no `aborted` field on the verdict: cancel the awaiting task, and `asyncio.CancelledError` comes out of `download_artifacts` with every partial file already unlinked. The item code `aborted` stays, for the references a credential failure stopped before they were taken.
- **An async context manager, not a returned response**, because an httpx stream is only live inside its own block.
- **`dir_path`, not `dir`**, which is a Python builtin.
- **Options as pydantic models**, so a caller passes `DownloadArtifactsOptions(...)` where the JS twin spreads keys into one request object.
- **A location is a model, not a shape.** Where the JS `artifactFilename` takes any object carrying `uri` and `found_at`, the Python one takes an `ArtifactLocation`, and `DownloadedArtifact` subclasses it so a verdict item passes as it is.
- **The shapes live in `artifact_models.py`**, beside `product_models` and `crate_models`, because `pipelex_sdk.errors` types two of its artifact errors with them and the operations module imports those errors — one home for the shapes keeps that from being an import cycle.

## Round trip

The two directions compose. A file uploaded by `prepare_inputs` is echoed in the run's working memory under the same reference, so a pass-through run brings it back byte for byte:

```python
prepared = await client.prepare_inputs(files=files, inputs={"doc": "./brief.pdf", "note": "hi"})
results = await client.start_and_wait(pipe_code=pipe_code, mthds_contents=[bundle], inputs=prepared.inputs)
verdict = await download_artifacts(
    client,
    dir_path="./out",
    run_id=results.pipeline_run_id,
    options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),
)
# verdict.artifacts finds prepared.uploads[0].uri among the saved files
```

That is also the live e2e leg (`tests/e2e/test_artifacts_e2e.py`, run by `make e2e-test`), which needs a platform carrying the bulk route and skips itself when `PIPELEX_E2E_BASE_URL` and `PIPELEX_API_KEY` are unset.
