"""The artifact stack — the download twin of `prepare_inputs`, in layers so each operation is
usable without the next:

- `locate_artifacts(value)` — a pure walk of any JSON-shaped value for the strings that ARE
  `pipelex-storage://` references, each with every `$`-rooted path it sits at. `collect_artifacts`
  is the same walk's bare references. No network, no key.
- `resolve_artifacts(client, uris)` — the platform's bulk resolve route over a whole list,
  chunked at the route's bound, one verdict per reference.
- `fetch_artifact(client, uri)` — an async context manager yielding a bounded stream for one
  reference: resolved fresh, timed out, redirects refused, the byte cap enforced mid-stream, no
  credentials forwarded, headers neutral.
- `download_artifacts(client, ...)` — a run's produced files saved under a directory by a bounded
  pool of tasks, each named after the field it fills (`artifact_filename`), as a produced verdict.

A produced file is never embedded in a run's results: the content carries its durable
`pipelex-storage://` reference beside a signed `public_url` that expires on the provider's
schedule. Nothing here reads that embedded link — every link is minted fresh by the platform, and
re-minted when it has expired by the time a task reaches it. See `docs/artifact-download.md`.

Python counterpart of `pipelex-sdk-js`'s `src/artifacts.ts`, with the idiomatic ports the SDK
already makes elsewhere: seconds instead of milliseconds, an `asyncio.Semaphore` instead of a
worker pool, `asyncio` cancellation instead of an `AbortSignal`, and an async context manager
instead of a returned `Response` (an httpx stream is only live inside its own block).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, TypeAlias, cast
from urllib.parse import unquote, urlsplit

import httpx
from httpx._decoders import SUPPORTED_DECODERS  # ruff: ignore[import-private-name]
from mthds.protocol.exceptions import PipelineRequestError
from pydantic import BaseModel, ValidationError

from pipelex_sdk.artifact_models import (
    BULK_RESOLVE_MAX_URIS,
    PIPELEX_STORAGE_SCHEME,
    ArtifactItemError,
    ArtifactLocation,
    ArtifactScope,
    BulkResolvedStorageUrls,
    DownloadArtifactsOptions,
    DownloadArtifactsResult,
    DownloadedArtifact,
    FetchArtifactOptions,
    ResolvedArtifact,
)
from pipelex_sdk.errors import (
    ApiResponseError,
    ArtifactAuthenticationError,
    ArtifactFetchError,
    ArtifactOperationError,
    FieldNotIncludedError,
    RunFailedError,
    RunStillRunningError,
    ScopeUnavailableError,
)
from pipelex_sdk.runs import RunResultCompleted, RunResultFailed, RunResultRunning, RunResults

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence
    from contextlib import AbstractAsyncContextManager

    from pipelex_sdk.runs import RunResultState

# ── Constants ────────────────────────────────────────────────────────

#: A link this close to its `expires_at` is re-resolved rather than fetched: the object store checks
#: the signature when the request arrives, and a few seconds of clock skew between the platform and
#: this process must not turn a link the platform still considers live into a `403`.
_EXPIRY_MARGIN_SECONDS = 10.0

#: Longest filename `download_artifacts` writes, extension included.
_MAX_FILENAME_LENGTH = 128

#: Longest extension taken from a storage key, dot excluded. Anything longer after the key's last
#: dot is read as part of a name rather than as an extension, and the content type's extension is
#: used instead.
_MAX_EXTENSION_LENGTH = 10

#: Stems Windows reserves for a device, in any case and whatever the extension: `aux.png` there
#: names the auxiliary device, not a file. A stem is a field name the method author chose, so
#: `$.aux.url` would otherwise reach one.
_WINDOWS_DEVICE_STEM = re.compile(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", re.IGNORECASE)

#: An object key rendered as `.key` in a path; any other key is rendered as `["…"]`.
_IDENTIFIER_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: The three steps a rendered path is read back from, each matched where the last one ended.
_KEY_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")
_INDEX_STEP = re.compile(r"\[([0-9]+)\]")
_QUOTED_STEP = re.compile(r'\[("(?:[^"\\]|\\.)*")\]', re.DOTALL)

#: A UTF-16 surrogate standing alone in a Python string, which `JSON.stringify` escapes as `\uXXXX`.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")

#: A `%` that does not open a two-digit escape, which makes `decodeURIComponent` refuse the segment.
_BARE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")

#: Ceiling on collision suffixes before the never-overwrite rule gives up.
_MAX_UNIQUE_ATTEMPTS = 10_000

#: How much of a body one read takes before it is written.
_STREAM_CHUNK_BYTES = 64 * 1024

#: The extension to add when the storage key has none and the resolved content type is one of the
#: artifact types a run produces. Deliberately short: an unknown type simply gets no extension,
#: never a guessed one.
_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "text/csv": ".csv",
    "application/json": ".json",
}

#: The codings the installed httpx decodes for us, read from its own decoder table (the one place
#: that knows): `br` and `zstd` join it only when their optional packages are installed, and
#: `x-gzip` never does. A coding it did not decode keeps its header, since the bytes handed on are
#: still encoded.
_DECODED_CODINGS = frozenset(SUPPORTED_DECODERS) - {"identity"}

_SKIPPED_CREDENTIAL = "The download stopped on a credential failure before this artifact was fetched."


# ── The client surfaces the operations need ──────────────────────────


class BulkResolveClient(Protocol):
    """The client surface the reading operations need — the raw bulk resolve call. Typed as
    `PipelexAPIClient.resolve_storage_urls_bulk`'s own signature, so the client satisfies it
    structurally and a test can inject a fake.
    """

    async def resolve_storage_urls_bulk(self, uris: list[str]) -> BulkResolvedStorageUrls: ...


class ArtifactCapableClient(BulkResolveClient, Protocol):
    """What `download_artifacts` needs on top: the single-shot result lookup, for the `run_id` arm."""

    async def get_run_result(self, run_id: str) -> RunResultState: ...


# ── locate_artifacts / collect_artifacts ─────────────────────────────

#: One step of a path into a walked value: an object key, or an array index.
_PathSegment: TypeAlias = str | int


@dataclass
class _LocatedReference:
    """A reference as the walk records it: the raw segments of every path it was found at.

    `download_artifacts` names its files from these segments directly, so it never parses a rendered
    path back; `found_at` is only their rendering.
    """

    uri: str
    paths: list[tuple[_PathSegment, ...]]


def is_storage_reference(value: str) -> bool:
    """A string that IS a storage reference: the scheme, then at least one character."""
    return value.startswith(PIPELEX_STORAGE_SCHEME) and len(value) > len(PIPELEX_STORAGE_SCHEME)


def locate_artifacts(value: Any) -> list[ArtifactLocation]:
    """Every `pipelex-storage://` reference inside a JSON-shaped value, each with every path at which
    it occurs.

    The references are deduplicated and kept in discovery order (the order of their first sighting),
    and each one's `found_at` lists its paths in walk order, so `found_at[0]` is where it was first
    seen. The string test is `collect_artifacts`'s: a string counts only when it IS a reference. A
    path is rooted at `$`, the walked value itself; an object key matching `^[A-Za-z_][A-Za-z0-9_]*$`
    is written `.key`, any other key `["…"]` in JSON string escaping, and an array index `[n]`. The
    runtime serializes a produced image or document as content carrying its reference in `url`, so a
    typical path ends there: `$.rooms[3].staged_photo.url`, `$.items[0].url`, or `$.url` for an
    output that is one image. Pure — no network, no key. Mappings, sequences and pydantic models are
    walked alike, a model by its `model_dump()` keys.
    """
    return [_render_location(located) for located in _walk_references(value, with_paths=True)]


def collect_artifacts(value: Any) -> list[str]:
    """Every `pipelex-storage://` reference inside a JSON-shaped value, deduplicated, in discovery
    order — the references of `locate_artifacts`, without their paths.

    A string counts only when it IS a reference — the whole string, scheme first, with something
    after the scheme; text that merely contains one does not. The scheme is unambiguous, so this walk
    is a contract rather than a heuristic: the runtime serializes a produced image or document as
    content carrying its reference in `url`, beside an expiring `public_url` this walk ignores. Pure
    — no network, no key — so a consumer can count or list a run's produced files without resolving
    any of them. Mappings, sequences and pydantic models are walked alike, so `results.main_stuff`
    (a parsed JSON value) and a whole `RunResults` both work.
    """
    return [located.uri for located in _walk_references(value, with_paths=False)]


def _walk_references(value: Any, *, with_paths: bool) -> list[_LocatedReference]:
    """The walk both public functions share: depth first, keys in mapping order.

    With `with_paths` off it records each reference once and no path at all, which is what
    `collect_artifacts` needs: copying the trail for every occurrence costs memory in proportion to
    occurrences times depth, where the deduplicated list needs only one entry per unique reference.
    """
    # A dict keeps insertion order, which is the order of first sighting.
    by_uri: dict[str, _LocatedReference] = {}
    trail: list[_PathSegment] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if not is_storage_reference(node):
                return
            known = by_uri.get(node)
            if known is None:
                known = _LocatedReference(uri=node, paths=[])
                by_uri[node] = known
            if with_paths:
                known.paths.append(tuple(trail))
            return
        if isinstance(node, BaseModel):
            visit(node.model_dump())
            return
        if isinstance(node, dict):
            for key, entry in cast("dict[Any, Any]", node).items():
                # A JSON object's keys are strings; any other key is read as the string it prints as.
                trail.append(key if isinstance(key, str) else str(key))
                visit(entry)
                trail.pop()
            return
        if isinstance(node, (list, tuple)):
            for index_item, item in enumerate(cast("list[Any]", node)):
                trail.append(index_item)
                visit(item)
                trail.pop()

    visit(value)
    return list(by_uri.values())


def _render_location(located: _LocatedReference) -> ArtifactLocation:
    return ArtifactLocation(uri=located.uri, found_at=[_render_path(segments) for segments in located.paths])


def _render_path(segments: Sequence[_PathSegment]) -> str:
    """Segments to the `$`-rooted notation `found_at` carries."""
    rendered = ["$"]
    for segment in segments:
        if isinstance(segment, int):
            rendered.append(f"[{segment}]")
        elif _IDENTIFIER_KEY.fullmatch(segment):
            rendered.append(f".{segment}")
        else:
            rendered.append(f"[{_json_string(segment)}]")
    return "".join(rendered)


def _json_string(key: str) -> str:
    r"""A key as `JSON.stringify` writes it, so a path reads the same from either SDK.

    `json.dumps` with `ensure_ascii` off escapes exactly what `JSON.stringify` does — the quote, the
    backslash and the control characters, the latter as lowercase `\u00XX` — and keeps every other
    character as typed, except a surrogate standing alone: `JSON.stringify` escapes it, and left raw
    it would make the path a string no UTF-8 encoder accepts.
    """
    dumped = json.dumps(key, ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", dumped)


def _parse_path(path: str) -> list[_PathSegment] | None:
    """The `$`-rooted notation back to segments, for a location that reaches `artifact_filename` from
    outside the walk. The rendering is lossless, so this is exact for every path the walk produced;
    anything else is `None`.
    """
    if not path.startswith("$"):
        return None
    segments: list[_PathSegment] = []
    position = 1
    while position < len(path):
        key_step = _KEY_STEP.match(path, position)
        if key_step is not None:
            segments.append(key_step.group(1))
            position = key_step.end()
            continue
        index_step = _INDEX_STEP.match(path, position)
        if index_step is not None:
            segments.append(int(index_step.group(1)))
            position = index_step.end()
            continue
        quoted_step = _QUOTED_STEP.match(path, position)
        if quoted_step is None:
            return None
        try:
            decoded: Any = json.loads(quoted_step.group(1))
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, str):
            return None
        segments.append(decoded)
        position = quoted_step.end()
    return segments


# ── artifact_filename ────────────────────────────────────────────────


def artifact_filename(location: ArtifactLocation, content_type: str | None, scope: ArtifactScope) -> str:
    """The bare filename a reference is saved under, named after the field it fills: the path in
    `location.found_at[0]`, where the reference was first seen.

    1. A final object key `url` is dropped, since the runtime's image and document contents carry
       their reference there; a reference under any other key keeps that key, and a `url` key that
       is not final is kept.
    2. Each key is reduced to `[A-Za-z0-9_]`, every other character (`-` and `.` included) becoming
       `_`; an index stays its decimal digits.
    3. The segments are joined with `-`. An empty result — the reference is the walked value itself,
       or its `url` — becomes the scope's name.
    4. Over the filename length cap (128 characters, extension included), the tail is kept: whole
       leading segments are dropped first, since the last ones are the specific ones, and a single
       segment still too long is cut to fit.
    5. A stem Windows reserves for a device (`con`, `prn`, `aux`, `nul`, `com0` to `com9`,
       `lpt0` to `lpt9`, in any case) gets a trailing `_`, so `$.aux.url` is saved as `aux_.png`.
    6. The extension is the storage key's own, reduced to `[A-Za-z0-9]`, when it has a short one;
       otherwise the content type's, for the types a run produces; otherwise there is none.

    So `$.rooms[3].staged_photo.url` is saved as `rooms-3-staged_photo.png`, and `$.url` in
    `main_stuff` as `main_stuff.png`. The stem is ASCII letters, digits, `_` and the `-` joins, never
    empty and never a device name, so the name can only ever be a regular file directly inside the
    target directory. A collision on disk is not this function's concern: `download_artifacts`
    suffixes the stem (`name-1.ext`) on exclusive creation, so a file is never overwritten. A
    `DownloadedArtifact` is an `ArtifactLocation`, so a verdict item can be passed as it is.

    Raises `ArtifactOperationError` for a location that is not an `ArtifactLocation`, one whose
    `found_at[0]` is not a path in the notation `locate_artifacts` writes, or an unknown scope.
    """
    checked_scope = _require_scope(scope)
    # A caller can hand anything here — the old signature's bare uri string among them — and every
    # shape must reach the documented refusal rather than an AttributeError.
    loose = cast("object", location)
    if not isinstance(loose, ArtifactLocation):
        msg = f"artifact_filename needs an ArtifactLocation, as locate_artifacts answers; got a {type(loose).__name__}."
        raise ArtifactOperationError(msg)
    first = loose.found_at[0] if loose.found_at else None
    segments = _parse_path(first) if first is not None else None
    if segments is None:
        msg = f'artifact_filename needs a location whose first "found_at" entry is a path such as "$.items[0].url"; got {first!r}.'
        raise ArtifactOperationError(msg)
    return _filename_for(segments, loose.uri, content_type, checked_scope)


def _filename_for(segments: Sequence[_PathSegment], uri: str, content_type: str | None, scope: ArtifactScope) -> str:
    """The naming rule of `artifact_filename`, over the walk's own segments."""
    named = segments[:-1] if segments and segments[-1] == "url" else segments
    words: list[str] = []
    for segment in named:
        word = str(segment) if isinstance(segment, int) else re.sub(r"[^A-Za-z0-9_]", "_", segment)
        # Only the empty key reduces to nothing, and it says nothing about the field.
        if word:
            words.append(word)
    extension = _extension_for(uri, content_type)
    stem = _fit_stem(words or [scope], _MAX_FILENAME_LENGTH - len(extension))
    # A device stem is at most four characters, so the `_` cannot overrun the cap.
    if _WINDOWS_DEVICE_STEM.fullmatch(stem):
        stem += "_"
    return stem + extension


def _fit_stem(words: Sequence[str], budget: int) -> str:
    """The words joined with `-` within `budget` characters, keeping the tail."""
    start = 0
    length = sum(len(word) for word in words) + len(words) - 1
    while length > budget and start < len(words) - 1:
        length -= len(words[start]) + 1
        start += 1
    return "-".join(words[start:])[:budget]


def _extension_for(uri: str, content_type: str | None) -> str:
    """`.ext` for the saved file — the storage key's own, else the content type's — or `""`."""
    from_key = _storage_key_extension(uri)
    if from_key:
        return f".{from_key}"
    if content_type is None:
        return ""
    return _EXTENSION_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip().lower(), "")


def _storage_key_extension(uri: str) -> str:
    r"""The extension the storage key's last segment carries, without its dot, reduced to
    `[A-Za-z0-9]` — or `""` when it has none, or none that short.

    The segment is what follows the last `/` or `\` once the scheme, query and fragment are gone,
    percent-decoded when it decodes; a leading dot is not an extension.
    """
    key = uri.removeprefix(PIPELEX_STORAGE_SCHEME)
    key = re.split(r"[?#]", key, maxsplit=1)[0]
    parts = [part for part in re.split(r"[\\/]", key) if part]
    decoded = _decode_uri_component(parts[-1]) if parts else ""
    dot = decoded.rfind(".")
    if dot <= 0:
        return ""
    extension = re.sub(r"[^A-Za-z0-9]", "", decoded[dot + 1 :])
    return extension if len(extension) <= _MAX_EXTENSION_LENGTH else ""


def _decode_uri_component(segment: str) -> str:
    """`decodeURIComponent`, or the segment as typed where the JS twin's call would throw.

    `unquote` alone is lenient twice over — it keeps a `%` that opens no escape and replaces bytes
    that are not UTF-8 — where `decodeURIComponent` refuses the whole segment, so the same key would
    otherwise yield a different extension in each SDK.
    """
    if _BARE_PERCENT.search(segment):
        return segment
    try:
        return unquote(segment, errors="strict")
    except UnicodeDecodeError:
        return segment


def _extension_of(name: str) -> str:
    """`os.path.splitext` for a bare filename: `.ext`, or `""` (a leading dot is not an extension)."""
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def _require_scope(scope: ArtifactScope) -> ArtifactScope:
    """The scope as the enum, or the refusal naming the two there are."""
    try:
        return ArtifactScope(scope)
    except ValueError as exc:
        msg = f'"scope" must be "main_stuff" or "working_memory", got {scope!r}.'
        raise ArtifactOperationError(msg) from exc


# ── resolve_artifacts ────────────────────────────────────────────────


async def resolve_artifacts(client: BulkResolveClient, uris: list[str]) -> list[ResolvedArtifact]:
    """Resolve a list of references through the bulk route, chunked at `BULK_RESOLVE_MAX_URIS` per
    request, and answer one `ResolvedArtifact` per reference in request order, duplicates included.

    Per-reference failure is a value on the item; only what is not about a reference raises — the
    route's whole-request refusals as `ApiResponseError` (a caller with no organization, a malformed
    request, a signing failure, or a `404` from a deployment that does not serve the route) and an
    unreachable host as `ApiUnreachableError`. An empty list resolves to an empty list with no
    request made.
    """
    items: list[ResolvedArtifact] = []
    for start in range(0, len(uris), BULK_RESOLVE_MAX_URIS):
        chunk = list(uris[start : start + BULK_RESOLVE_MAX_URIS])
        answer = await client.resolve_storage_urls_bulk(chunk)
        if len(answer.items) != len(chunk):
            msg = (
                f"The bulk resolve route answered {len(answer.items)} item(s) for {len(chunk)} reference(s) — "
                "a malformed answer, so no reference can be matched to its verdict."
            )
            raise ArtifactOperationError(msg)
        items.extend(answer.items)
    return items


# ── fetch_artifact ───────────────────────────────────────────────────


@dataclass(frozen=True)
class _FetchBounds:
    """The bounds every fetch runs under, defaults filled in and validated."""

    max_bytes: int
    timeout_seconds: float
    allow_http: bool


class ArtifactStream:
    """A bounded, header-neutral view of the object store's response for one reference.

    `status_code` and `headers` are the store's own — the one change being that a `Content-Encoding`
    httpx already decoded is dropped with the encoded `Content-Length`, since the bytes handed on are
    the decoded ones. A proxy relaying this response owns its hygiene (`X-Content-Type-Options`, a
    sandboxing CSP, a controlled `Content-Disposition`, private caching) and must set those itself,
    because this object does not.

    The body is read through `aiter_bytes()` or `read()`, either of which raises `ArtifactFetchError`
    (`too_large`) the moment the bytes cross the call's `max_bytes`.
    """

    def __init__(self, uri: str, response: httpx.Response, max_bytes: int) -> None:
        self.uri = uri
        self.status_code = response.status_code
        self.headers = _decoded_headers(response.headers)
        self.content_type: str | None = self.headers.get("content-type")
        self._response = response
        self._max_bytes = max_bytes

    async def aiter_bytes(self, chunk_size: int = _STREAM_CHUNK_BYTES) -> AsyncIterator[bytes]:
        """Yield the body in chunks, cutting it the moment it crosses the byte cap."""
        total = 0
        async for chunk in self._response.aiter_bytes(chunk_size):
            total += len(chunk)
            if total > self._max_bytes:
                msg = f"The artifact crossed the {_format_mib(self._max_bytes)} cap mid-stream."
                raise ArtifactFetchError(msg, uri=self.uri, code="too_large", status=self.status_code)
            yield chunk

    async def read(self) -> bytes:
        """The whole body in memory, under the same cap. For a large artifact, iterate instead."""
        parts: list[bytes] = []
        async for chunk in self.aiter_bytes():
            parts.append(chunk)
        return b"".join(parts)


def _new_storage_client(timeout_seconds: float) -> httpx.AsyncClient:
    """The httpx client the object store is fetched with: our own, never the API client's.

    It carries no credential of ours (the link's authorization is in its query string, and nothing
    must ride along to the store), refuses redirects rather than following them, and bounds every
    stalled connect, read and write at the call's budget — the per-stall bound a total timeout alone
    does not give.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)


@asynccontextmanager
async def _stream_resolved_url(
    storage_client: httpx.AsyncClient,
    uri: str,
    download_url: str,
    bounds: _FetchBounds,
) -> AsyncGenerator[ArtifactStream, None]:
    """The bounded fetch of an already-resolved link — the half of `fetch_artifact` that
    `download_artifacts` shares, its links coming from one bulk resolve ahead of the tasks.
    """
    checked_url = _checked_url(uri, download_url, allow_http=bounds.allow_http)
    try:
        async with asyncio.timeout(bounds.timeout_seconds):
            try:
                async with storage_client.stream("GET", checked_url) as response:
                    refusal = _status_refusal(uri, response)
                    if refusal is not None:
                        raise refusal
                    declared = _declared_length(response.headers)
                    if declared is not None and declared > bounds.max_bytes:
                        msg = f"The artifact is {_format_mib(declared)}, over the {_format_mib(bounds.max_bytes)} cap."
                        raise ArtifactFetchError(msg, uri=uri, code="too_large", status=response.status_code)
                    yield ArtifactStream(uri, response, bounds.max_bytes)
            except httpx.InvalidURL as exc:
                # Not an `HTTPError`: httpx refuses the link at request build, before any transport.
                msg = "The platform resolved the reference to a link that is not a valid absolute URL."
                raise ArtifactFetchError(msg, uri=uri, code="unsupported_url") from exc
            except httpx.TimeoutException as exc:
                msg = f"Fetching the artifact timed out after {bounds.timeout_seconds}s."
                raise ArtifactFetchError(msg, uri=uri, code="timeout") from exc
            except httpx.HTTPError as exc:
                msg = f"The artifact could not be fetched: {exc}."
                raise ArtifactFetchError(msg, uri=uri, code="network") from exc
    except TimeoutError as exc:
        msg = f"Fetching the artifact timed out after {bounds.timeout_seconds}s."
        raise ArtifactFetchError(msg, uri=uri, code="timeout") from exc


def fetch_artifact(
    client: BulkResolveClient,
    uri: str,
    options: FetchArtifactOptions | None = None,
) -> AbstractAsyncContextManager[ArtifactStream]:
    """A bounded stream for one reference, as an async context manager.

    The link is minted fresh through the bulk route, then fetched with redirects refused (a presigned
    link has no reason to redirect, and one that does is refused rather than followed), no headers of
    ours (the link carries its own authorization in the query string, and nothing must ride along to
    the object store), a budget covering the headers and the body, and a byte cap checked against
    `Content-Length` before a byte is read and again on every chunk.

    Only a `2xx` is yielded. Anything else raises `ArtifactFetchError` with a `code` (a redirect, a
    refused or vanished object, a store fault, a declared oversize, a timeout, a network fault, an
    unusable link, or the route's own per-reference refusal); a body that crosses the cap mid-stream
    raises the same error type (`too_large`) out of the iteration. Cancelling the awaiting task
    propagates `asyncio.CancelledError` as-is. Whole-request failures of the resolve step propagate
    unchanged (`ApiResponseError`, `ApiUnreachableError`).

    Usage:
        async with fetch_artifact(client, uri) as stream:
            async for chunk in stream.aiter_bytes():
                ...
    """
    return _fetch_artifact(client, uri, options)


@asynccontextmanager
async def _fetch_artifact(
    client: BulkResolveClient,
    uri: str,
    options: FetchArtifactOptions | None = None,
) -> AsyncGenerator[ArtifactStream, None]:
    """`fetch_artifact`'s body — resolve one reference fresh, then stream its link under the bounds."""
    bounds = _validated_bounds(options or FetchArtifactOptions())
    resolved = await resolve_artifacts(client, [uri])
    entry = resolved[0]
    if entry.error is not None:
        raise ArtifactFetchError(entry.error.detail, uri=uri, code=entry.error.code)
    if entry.url is None:
        msg = "The bulk resolve route answered an item with neither a link nor an error."
        raise ArtifactOperationError(msg)
    storage_client = _new_storage_client(bounds.timeout_seconds)
    try:
        async with _stream_resolved_url(storage_client, uri, entry.url, bounds) as stream:
            yield stream
    finally:
        await storage_client.aclose()


def _validated_bounds(options: FetchArtifactOptions) -> _FetchBounds:
    """The per-file bounds, refusing nonsense before anything is resolved."""
    _require_positive("max_bytes", options.max_bytes)
    _require_positive("timeout_seconds", options.timeout_seconds)
    return _FetchBounds(max_bytes=options.max_bytes, timeout_seconds=options.timeout_seconds, allow_http=options.allow_http)


def _require_positive(name: str, value: float) -> None:
    """Refuse a bound that is not a positive, finite number."""
    if not math.isfinite(value) or value <= 0:
        msg = f'"{name}" must be a positive number, got {value}.'
        raise ArtifactOperationError(msg)


def _checked_url(uri: str, download_url: str, *, allow_http: bool) -> str:
    """The boundary-approved link, or the typed refusal saying why it is not fetched."""
    try:
        parsed = urlsplit(download_url)
    except ValueError as exc:
        msg = "The platform resolved the reference to a link that is not a valid absolute URL."
        raise ArtifactFetchError(msg, uri=uri, code="unsupported_url") from exc
    if not parsed.scheme or not parsed.netloc:
        msg = "The platform resolved the reference to a link that is not a valid absolute URL."
        raise ArtifactFetchError(msg, uri=uri, code="unsupported_url")
    scheme = parsed.scheme.lower()
    if scheme == "http" and not allow_http:
        msg = (
            "The platform resolved the reference to a plain http link, which is refused by default; "
            "pass allow_http=True to accept it (the local stack's object store hands out such links)."
        )
        raise ArtifactFetchError(msg, uri=uri, code="plain_http_refused")
    if scheme not in {"http", "https"}:
        msg = f'The platform resolved the reference to a "{scheme}" link, which is not fetched; only http(s) links are.'
        raise ArtifactFetchError(msg, uri=uri, code="unsupported_url")
    if parsed.username or parsed.password:
        msg = "The platform resolved the reference to a link carrying credentials, which is not fetched."
        raise ArtifactFetchError(msg, uri=uri, code="unsupported_url")
    return download_url


def _status_refusal(uri: str, response: httpx.Response) -> ArtifactFetchError | None:
    """The typed refusal for a non-2xx store answer, or `None` when the status is a `2xx`."""
    status = response.status_code
    if 300 <= status < 400:
        msg = f"The resolved link redirected (HTTP {status}); redirects are not followed."
        return ArtifactFetchError(msg, uri=uri, code="redirect_refused", status=status)
    # The link is minted per call, so a 401/403 is the store refusing a fresh signature (clock skew,
    # a signing misconfiguration) rather than an expired link.
    if status in {401, 403}:
        msg = f"The object store refused the resolved link (HTTP {status})."
        return ArtifactFetchError(msg, uri=uri, code="store_refused", status=status)
    if status in {404, 410}:
        msg = f"The stored file is no longer available (HTTP {status})."
        return ArtifactFetchError(msg, uri=uri, code="not_found", status=status)
    if status < 200 or status >= 300:
        msg = f"The object store answered HTTP {status} for the resolved link."
        return ArtifactFetchError(msg, uri=uri, code="store_error", status=status)
    return None


def _decoded_headers(headers: httpx.Headers) -> httpx.Headers:
    """The store's headers as they describe the body we hand on.

    When httpx has decoded every coding the `Content-Encoding` lists, the stream is the decoded bytes:
    the encoding and the encoded length are dropped, or a proxy relaying the response would label
    plain bytes as compressed and give the wrong length. Any other encoding is passed through with the
    still-encoded body it describes.
    """
    raw = headers.get("content-encoding", "")
    codings = [coding.strip().lower() for coding in raw.split(",")]
    codings = [coding for coding in codings if coding and coding != "identity"]
    if not codings or not all(coding in _DECODED_CODINGS for coding in codings):
        return headers
    decoded = httpx.Headers(headers)
    del decoded["content-encoding"]
    if "content-length" in decoded:
        del decoded["content-length"]
    return decoded


def _declared_length(headers: httpx.Headers) -> int | None:
    """The body's declared length, or `None` when it is absent or unreadable."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _format_mib(byte_count: float) -> str:
    """A byte count as MiB, for the sentence a person reads."""
    mib = byte_count / (1024 * 1024)
    return f"{int(mib)} MiB" if mib.is_integer() else f"{mib:.1f} MiB"


# ── download_artifacts ───────────────────────────────────────────────


@dataclass
class _DownloadBudget:
    """The bytes the whole call may still write, and the flags that stop the tasks taking more.

    `committed` is the bytes of every file saved or being saved, a file in flight counting as the
    larger of its declared length and what it has written. Reserving the declared length up front is
    what stops parallel files from each passing the check and then all being cut together; a file
    that is unlinked gives its share back. Every item is checked against the room that leaves, on
    its own declared length: one refused item never skips a smaller one that still fits.
    """

    max_total_bytes: int
    committed: int = 0
    credential_failure: ApiResponseError | None = None


async def download_artifacts(
    client: ArtifactCapableClient,
    *,
    dir_path: str | Path,
    run_id: str | None = None,
    results: RunResults | None = None,
    options: DownloadArtifactsOptions | None = None,
) -> DownloadArtifactsResult:
    """Save a run's produced files under `dir_path`, and answer a produced verdict.

    Takes exactly one of `run_id` (the results are re-read, so a run is downloadable days later) or
    `results` (a `RunResults` in hand); walks the requested scope with `locate_artifacts`; resolves
    the whole set through the bulk route ahead of the tasks; then a bounded number of tasks
    (`concurrency`, held by an `asyncio.Semaphore`) each fetch, create their file exclusively and
    stream the body in, re-resolving any link that has expired by the time a task reaches it. The
    embedded `public_url` is never used. Each file is named after the field it fills, by
    `artifact_filename`'s rule, and never overwritten; a failed or cancelled download unlinks its
    partial file.

    Returns one entry per reference, errors as values. It raises only when no verdict can be produced:
    `RunStillRunningError` or `RunFailedError` for a run that has not completed, `FieldNotIncludedError`
    when the results read did not carry the scope's key, `ScopeUnavailableError` when the key was
    relayed as `None`, `ArtifactAuthenticationError` (carrying the verdict so far) when the resolve
    route refuses the credential, `ArtifactOperationError` for an unusable directory, an unknown
    scope or nonsense bounds, and the transport and lifecycle errors of the reads it makes
    (`ApiResponseError` for a deployment without the bulk route, `RunLifecycleUnavailableError` for a
    bare runner asked by id, `ApiUnreachableError`). Cancelling the awaiting task raises
    `asyncio.CancelledError` out of here, with every partial file unlinked first.
    """
    opts = options or DownloadArtifactsOptions()
    scope = _require_scope(opts.scope)
    bounds = _validated_bounds(opts)
    if opts.concurrency < 1:
        msg = f'"concurrency" must be a positive integer, got {opts.concurrency}.'
        raise ArtifactOperationError(msg)
    _require_positive("max_total_bytes", opts.max_total_bytes)

    # An empty `run_id` is no selector at all, and is refused here rather than sent to the results
    # read, which would answer a 404 about a run nobody named.
    if bool(run_id) == (results is not None):
        msg = "download_artifacts takes exactly one of `run_id` (the results are re-read) or `results` (a RunResults in hand)."
        raise ArtifactOperationError(msg)
    read_results = results if results is not None else await _read_completed_results(client, cast("str", run_id))
    walked = _scope_value(read_results, scope)

    # The walk's own record names the files; `locations` is what the verdict reports.
    located = _walk_references(walked, with_paths=True)
    if not located:
        return _assemble_verdict(scope, [])
    locations = [_render_location(reference) for reference in located]
    uris = [reference.uri for reference in located]

    target_dir = Path(dir_path).resolve()
    try:
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
    except OSError as exc:
        msg = f'The download directory "{target_dir}" cannot be created or used: {exc}.'
        raise ArtifactOperationError(msg) from exc

    budget = _DownloadBudget(max_total_bytes=opts.max_total_bytes)
    try:
        resolved = await resolve_artifacts(client, uris)
    except ApiResponseError as exc:
        if not _is_credential_refusal(exc):
            raise
        verdict = _assemble_verdict(scope, [_item_error(location, None, "aborted", _SKIPPED_CREDENTIAL) for location in locations])
        msg = f"The resolve route refused the credential ({exc.status}); no artifact was downloaded."
        raise ArtifactAuthenticationError(msg, status=exc.status, verdict=verdict) from exc

    semaphore = asyncio.Semaphore(opts.concurrency)
    storage_client = _new_storage_client(bounds.timeout_seconds)
    try:
        outcomes = await asyncio.gather(
            *[
                _process_one(
                    client=client,
                    storage_client=storage_client,
                    semaphore=semaphore,
                    budget=budget,
                    bounds=bounds,
                    target_dir=target_dir,
                    scope=scope,
                    location=location,
                    name_path=located[index].paths[0],
                    entry=resolved[index],
                )
                for index, location in enumerate(locations)
            ]
        )
    finally:
        await storage_client.aclose()

    verdict = _assemble_verdict(scope, list(outcomes))
    if budget.credential_failure is not None:
        status = budget.credential_failure.status
        msg = f"The resolve route refused the credential ({status}) part-way through the download; the verdict so far is on this error."
        raise ArtifactAuthenticationError(msg, status=status, verdict=verdict) from budget.credential_failure
    return verdict


async def _read_completed_results(client: ArtifactCapableClient, run_id: str) -> RunResults:
    """Read a run's results by id, turning a run that has not completed into its typed error."""
    state = await client.get_run_result(run_id)
    if isinstance(state, RunResultRunning):
        retry = state.retry_after_seconds
        hint = f" — retry in {retry}s." if retry is not None else "."
        msg = f"Run {run_id} is still running, so it has no artifacts to download yet{hint}"
        raise RunStillRunningError(msg, run_id=run_id, retry_after_seconds=retry)
    if isinstance(state, RunResultFailed):
        raise RunFailedError(state.message, run_id=run_id, status=state.status)
    completed: RunResultCompleted = state
    return completed.result


def _scope_value(results: RunResults, scope: ArtifactScope) -> Any:
    """The artifact the scope names, or the typed error saying why there is none to walk.

    The two readings of an absent value are distinct, and only one of them is the platform's answer:
    a key the results read never carried is not in `model_fields_set` and is `FieldNotIncludedError`,
    where a key relayed as `None` is a value and is `ScopeUnavailableError`.
    """
    field_name = scope.results_field
    if field_name not in results.model_fields_set:
        raise FieldNotIncludedError(field_name)
    value = getattr(results, field_name, None)
    if value is None:
        raise ScopeUnavailableError(scope, run_id=results.pipeline_run_id)
    return value


def _is_credential_refusal(exc: ApiResponseError) -> bool:
    """True for the resolve route refusing the caller's credential, which stops the whole download."""
    return exc.status in {401, 403}


def _item_error(location: ArtifactLocation, content_type: str | None, code: str, detail: str) -> DownloadedArtifact:
    """One reference's failure, as a value on the verdict — still saying where the reference sits."""
    return DownloadedArtifact(
        uri=location.uri,
        found_at=location.found_at,
        path=None,
        content_type=content_type,
        size=None,
        error=ArtifactItemError(code=code, detail=detail),
    )


def _is_expired(expires_at: str | None) -> bool:
    """True when a link is at or past its expiry, margin included; an unreadable stamp is not."""
    if expires_at is None:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed - datetime.now(UTC)).total_seconds() <= _EXPIRY_MARGIN_SECONDS


async def _process_one(
    *,
    client: ArtifactCapableClient,
    storage_client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    budget: _DownloadBudget,
    bounds: _FetchBounds,
    target_dir: Path,
    scope: ArtifactScope,
    location: ArtifactLocation,
    name_path: Sequence[_PathSegment],
    entry: ResolvedArtifact,
) -> DownloadedArtifact:
    """One reference's whole pipeline — the semaphore's slot, a re-resolve on expiry, then the save
    under the name its first path gives it.
    """
    uri = location.uri
    async with semaphore:
        if budget.credential_failure is not None:
            return _item_error(location, entry.content_type, "aborted", _SKIPPED_CREDENTIAL)
        if entry.error is None and _is_expired(entry.expires_at):
            try:
                entry = (await resolve_artifacts(client, [uri]))[0]
            except ApiResponseError as exc:
                if _is_credential_refusal(exc):
                    budget.credential_failure = exc
                    return _item_error(location, entry.content_type, "aborted", _SKIPPED_CREDENTIAL)
                msg = f"The expired link could not be re-resolved: {exc}."
                return _item_error(location, entry.content_type, "resolve_failed", msg)
            except (PipelineRequestError, ValidationError, ValueError) as exc:
                # Anything else the re-resolve can fail with — an unreachable host, a malformed
                # answer, a body that does not parse — is this one reference's error, never the
                # whole download's: the other references already have their links.
                msg = f"The expired link could not be re-resolved: {exc}."
                return _item_error(location, entry.content_type, "resolve_failed", msg)
        if entry.error is not None:
            return _item_error(location, None, entry.error.code, entry.error.detail)
        if entry.url is None:
            msg = "The bulk resolve route answered an item with neither a link nor an error."
            return _item_error(location, entry.content_type, "resolve_failed", msg)
        return await _save_one(
            storage_client=storage_client,
            budget=budget,
            bounds=bounds,
            target_dir=target_dir,
            location=location,
            filename=_filename_for(name_path, uri, entry.content_type, scope),
            download_url=entry.url,
            content_type=entry.content_type,
        )


class _TargetFile:
    """A file created exclusively under the download directory, written in the background thread.

    Exclusive creation is what makes "never overwrite" true rather than merely likely: an
    exists-check followed by a write would race a concurrent task.
    """

    def __init__(self, handle: BinaryIO, path: Path) -> None:
        self._handle = handle
        self.path = path

    async def write(self, chunk: bytes) -> None:
        """Write one whole chunk; Python's buffered writer never returns a short write."""
        await asyncio.to_thread(self._handle.write, chunk)

    def close(self) -> None:
        """Close the handle, surfacing a failed flush."""
        self._handle.close()

    def remove(self) -> None:
        """Close and unlink, so nothing truncated is left under a final name.

        Synchronous on purpose: this runs on the cancellation path too, where another `await` could
        be interrupted before the partial file is gone.
        """
        try:
            self._handle.close()
        except OSError:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _open_unique_file(target_dir: Path, base_name: str) -> _TargetFile:
    """Create `base_name` under `target_dir` exclusively, suffixing the stem until a free name is found."""
    extension = _extension_of(base_name)
    stem = base_name[: len(base_name) - len(extension)]
    for attempt in range(_MAX_UNIQUE_ATTEMPTS):
        candidate = target_dir / (base_name if attempt == 0 else f"{stem}-{attempt}{extension}")
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        return _TargetFile(os.fdopen(descriptor, "wb"), candidate)
    msg = f"Could not find a free filename for {base_name} in {target_dir}."
    raise OSError(msg)


async def _save_one(
    *,
    storage_client: httpx.AsyncClient,
    budget: _DownloadBudget,
    bounds: _FetchBounds,
    target_dir: Path,
    location: ArtifactLocation,
    filename: str,
    download_url: str,
    content_type: str | None,
) -> DownloadedArtifact:
    """Fetch one resolved link and write it under the download directory as `filename`, suffixed on a
    collision, within the total budget.
    """
    uri = location.uri
    target: _TargetFile | None = None
    written = 0
    reserved = 0
    share_taken = False

    def share() -> int:
        return max(written, reserved)

    try:
        async with _stream_resolved_url(storage_client, uri, download_url, bounds) as stream:
            declared = _declared_length(stream.headers)
            reserved = declared if declared is not None else 0
            if budget.committed + reserved > budget.max_total_bytes:
                msg = (
                    f"Saving this {_format_mib(reserved)} artifact would take the download past its "
                    f"{_format_mib(budget.max_total_bytes)} total limit."
                )
                return _item_error(location, content_type, "total_limit_exceeded", msg)
            budget.committed += reserved
            share_taken = True

            try:
                # Synchronous on purpose: a cancellation landing inside a worker thread would leave a file
                # the handler below cannot see, and an exclusive create is not worth a thread.
                target = _open_unique_file(target_dir, filename)
            except OSError as exc:
                budget.committed -= share()
                share_taken = False
                msg = f"The file could not be created: {exc}."
                return _item_error(location, content_type, "write_failed", msg)

            async for chunk in stream.aiter_bytes():
                # Only the bytes past this file's reservation are new to the total.
                growth = max(written + len(chunk), reserved) - share()
                if budget.committed + growth > budget.max_total_bytes:
                    target.remove()
                    budget.committed -= share()
                    share_taken = False
                    msg = f"This artifact took the download past its {_format_mib(budget.max_total_bytes)} total limit."
                    return _item_error(location, content_type, "total_limit_exceeded", msg)
                budget.committed += growth
                written += len(chunk)
                try:
                    await target.write(chunk)
                except OSError as exc:
                    target.remove()
                    budget.committed -= share()
                    share_taken = False
                    msg = f"The file could not be written: {exc}."
                    return _item_error(location, content_type, "write_failed", msg)
    except ArtifactFetchError as exc:
        if target is not None:
            target.remove()
        if share_taken:
            budget.committed -= share()
        return _item_error(location, content_type, exc.code, str(exc))
    except (httpx.HTTPError, OSError) as exc:
        if target is not None:
            target.remove()
        if share_taken:
            budget.committed -= share()
        msg = f"The artifact could not be read: {exc}."
        return _item_error(location, content_type, "network", msg)
    except asyncio.CancelledError:
        # A cancelled download leaves nothing truncated behind, then lets the cancellation through.
        if target is not None:
            target.remove()
        if share_taken:
            budget.committed -= share()
        raise

    try:
        target.close()
    except OSError as exc:
        target.remove()
        budget.committed -= share()
        msg = f"The file could not be closed: {exc}."
        return _item_error(location, content_type, "write_failed", msg)

    # A body shorter than it declared gives the unused reservation back.
    budget.committed -= share() - written
    return DownloadedArtifact(uri=uri, found_at=location.found_at, path=str(target.path), content_type=content_type, size=written, error=None)


def _assemble_verdict(scope: ArtifactScope, artifacts: list[DownloadedArtifact]) -> DownloadArtifactsResult:
    """The verdict over one walk: the saved paths in discovery order, and whether every one was saved."""
    saved_paths = [artifact.path for artifact in artifacts if artifact.error is None and artifact.path is not None]
    return DownloadArtifactsResult(
        scope=scope,
        artifacts=artifacts,
        saved_paths=saved_paths,
        all_saved=len(saved_paths) == len(artifacts),
    )
