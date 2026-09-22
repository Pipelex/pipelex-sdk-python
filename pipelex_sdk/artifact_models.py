"""The artifact stack's wire models, options and verdict — the shapes `pipelex_sdk.artifacts`
produces and consumes, and the defaults its operations apply.

They live in a module of their own, beside `product_models` and `crate_models`, because
`pipelex_sdk.errors` types two of its artifact errors with them (`ScopeUnavailableError.scope`,
`ArtifactAuthenticationError.verdict`) and the operations module imports those errors — one home for
the shapes keeps that from being an import cycle. Mirrors the types of `pipelex-sdk-js`'s
`src/artifacts.ts`, which needs no such split because TypeScript tolerates the cycle.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from pipelex_sdk._pydantic_utils import empty_list_factory_of

# ── Constants ────────────────────────────────────────────────────────

#: The scheme of a durable storage reference.
PIPELEX_STORAGE_SCHEME = "pipelex-storage://"

#: How many references one bulk resolve request takes — the route's bound, fixed by the platform
#: contract rather than configured per deployment. A longer list is a `422`, so `resolve_artifacts`
#: chunks at this size.
BULK_RESOLVE_MAX_URIS = 100

#: The per-file byte cap. An accident guard against filling a disk from a runaway output, not a
#: judgment about artifact size: a produced file is server-side, so a caller cannot shrink it the
#: way they can shrink an upload.
DEFAULT_ARTIFACT_MAX_BYTES = 1024 * 1024 * 1024

#: The per-file budget for connecting, receiving the headers and reading the body.
DEFAULT_ARTIFACT_TIMEOUT_SECONDS = 120.0

#: The cap on the bytes one `download_artifacts` call writes in total.
DEFAULT_DOWNLOAD_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024

#: How many artifacts one `download_artifacts` call has in flight at once.
DEFAULT_DOWNLOAD_CONCURRENCY = 4


# ── Types ────────────────────────────────────────────────────────────


class ArtifactScope(StrEnum):
    """Which of a run's artifacts `download_artifacts` walks for references."""

    MAIN_STUFF = "main_stuff"
    WORKING_MEMORY = "working_memory"

    @property
    def results_field(self) -> str:
        """The `RunResults` field this scope walks."""
        match self:
            case ArtifactScope.MAIN_STUFF:
                return "main_stuff"
            case ArtifactScope.WORKING_MEMORY:
                return "working_memory"


class ArtifactItemError(BaseModel):
    """Why one reference failed — a value, never a raised error.

    `code` is the resolve route's own per-reference code (`invalid_storage_uri`, `forbidden`) or one
    of the fetch boundary's (see `ArtifactFetchError`), plus the download's own `resolve_failed`,
    `total_limit_exceeded`, `write_failed` and `aborted`. `detail` is the sentence a person reads.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    detail: str


class ResolvedArtifact(BaseModel):
    """One reference's resolution — the bulk resolve route's item, verbatim.

    Either the link fields are set and `error` is `None`, or `error` is set and the three link fields
    are `None`. A consumer branches on `error`, never on an HTTP status: the request was a `200`
    whenever every reference got a verdict.
    """

    model_config = ConfigDict(extra="allow")

    #: The reference exactly as sent.
    uri: str
    #: A presigned link, fetchable now and for about fifteen minutes.
    url: str | None = None
    #: UTC expiry of the link, ISO 8601.
    expires_at: str | None = None
    #: The platform's content-type guess from the reference's extension; `None` when it has none.
    content_type: str | None = None
    error: ArtifactItemError | None = None


class BulkResolvedStorageUrls(BaseModel):
    """The wire response of `POST /v1/resolve-storage-url/bulk` — one item per requested reference,
    in request order.
    """

    model_config = ConfigDict(extra="allow")

    items: list[ResolvedArtifact] = Field(default_factory=empty_list_factory_of(ResolvedArtifact))


class FetchArtifactOptions(BaseModel):
    """The bounds `fetch_artifact` applies. Every one has a safe default."""

    model_config = ConfigDict(extra="forbid")

    #: Refuse (before a byte is read) and cut (mid-stream) a body over this many bytes. Default 1 GiB.
    max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES
    #: Budget for the whole exchange — connect, headers and body. Default 120 s.
    timeout_seconds: float = DEFAULT_ARTIFACT_TIMEOUT_SECONDS
    #: Accept a plain `http:` link. Off by default: a general-purpose library does not fetch over
    #: plain http silently. The local compose stack's object store hands out such links, which is
    #: the case this opts into.
    allow_http: bool = False


class DownloadArtifactsOptions(FetchArtifactOptions):
    """The bounds and choices of one `download_artifacts` call, the per-file ones included."""

    #: `main_stuff` (the default) walks the run's main output. `working_memory` is the opt-in that
    #: also brings down the echoed inputs and every intermediate.
    scope: ArtifactScope = ArtifactScope.MAIN_STUFF
    #: How many artifacts are in flight at once. Default 4.
    concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY
    #: Cap on the bytes written by the whole call. Default 4 GiB. The item that would cross it is an
    #: item error, and the items not yet started are skipped with the same reason.
    max_total_bytes: int = DEFAULT_DOWNLOAD_MAX_TOTAL_BYTES


class DownloadedArtifact(BaseModel):
    """One reference's outcome in a download verdict — one shape with nullable fields, like
    `ResolvedArtifact`: either `path` and `size` are set and `error` is `None`, or `error` is set and
    both are `None`. `content_type` is the platform's guess from the reference's extension, known
    before the fetch, on both arms.
    """

    uri: str
    #: Absolute path of the written file.
    path: str | None = None
    content_type: str | None = None
    #: Bytes written.
    size: int | None = None
    error: ArtifactItemError | None = None


class DownloadArtifactsResult(BaseModel):
    """The produced verdict of `download_artifacts`.

    `len(artifacts)` is the count of references walked, errors included; an empty list over a present
    scope is a verdict ("this output references no stored file"), not an error.
    """

    scope: ArtifactScope
    #: One entry per reference, in discovery order.
    artifacts: list[DownloadedArtifact] = Field(default_factory=empty_list_factory_of(DownloadedArtifact))
    #: The absolute paths of the files saved, in the same order.
    saved_paths: list[str] = Field(default_factory=list)
    #: True when every walked reference was saved — vacuously true for an empty walk.
    all_saved: bool
