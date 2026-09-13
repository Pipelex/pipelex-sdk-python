"""Wire models for the crate routes — `POST /v1/resolve` and `POST /v1/codegen` — and the
shared crate envelope they are built on.

The envelope lives here because these are the routes that still use it. `MthdsFileItem`,
`CrateRequestBase` and `CrateInvalidReport` used to sit in a `build_models` module beside the
`/v1/build/inputs` wire models; those went when `prepare_inputs` moved its signature source to
the input-form descriptor and this SDK stopped calling `/v1/build/*` (workspace campaign
`wip/build-retirement/`). Nothing about the envelope changed in the move.

`/v1/resolve` emits the normalized library crate, `/v1/codegen` projects that crate into stamped
typed artifacts plus their lock. Both are Pipelex API extensions (NOT MTHDS Protocol routes) over
the standard-owned artifact, so their wire fields stay brand-neutral. A produced verdict is a
`200` discriminated on `is_valid`, with `CrateInvalidReport` as the shared invalid arm; a
no-verdict condition (a malformed selector, a selector-resolution failure, auth, a server fault)
raises `ApiResponseError`.

The closure arrives in exactly one of three forms — the tooling routes' strict three-way
XOR: inline `files`, an address-form `method_ref` (server-resolved, pipelex-api >= 0.21.0;
the registry form stays a `501`), or a hosted `method_id` (platform-resolved — meaningless
against a bare runner, which has no catalog). The routes are stateless, so there is no
linkage exception: a second selector is a request-shape `422`, mirrored client-side by the
construction-time validator here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from pipelex_sdk.validation_models import ValidationErrorItem


class MthdsFileItem(BaseModel):
    """One MTHDS file in a crate closure. `source` is an optional provenance label the
    server threads onto diagnostics raised from this file.
    """

    content: str
    source: str | None = None


class CrateRequestBase(BaseModel):
    """The closure selector every crate-family route shares — `/v1/resolve` and
    `/v1/codegen` (mirror of the server's `MthdsFilesRequest` and of
    `pipelex-sdk-js`'s `CrateRequestBase`).

    Supply the closure EITHER as inline `files` OR as a `method_ref` — never both, and
    never neither. An **address-form** `method_ref`
    (`github.com/<owner>/<repo>[/<selector>][@<tag>]`) is resolved by the server
    (pipelex-api >= 0.21.0): the repository is fetched at the tag, the package is
    located by manifest identity, and its `.mthds` files feed the closure with their
    real relative paths as per-file sources. The **registry form** (any non-address
    reference) stays reserved and answers `501` until a method registry exists.

    An EMPTY selector is normalized to absent before the exclusivity check — `files=[]`
    selects no closure and `method_ref=""` (or whitespace-only) no address, the same
    empty-as-absent rule the run routes apply — so an unusable value never counts as the
    sole selector and never reaches the wire.

    The subclass owns the exclusivity validator, because the crate routes add a third
    selector (the hosted `method_id`) this base does not know about.
    """

    files: list[MthdsFileItem] | None = None
    method_ref: str | None = None

    @field_validator("files")
    @classmethod
    def _empty_files_are_absent(cls, value: list[MthdsFileItem] | None) -> list[MthdsFileItem] | None:
        # `files=[]` is not a closure — normalize to absent so the XOR counts real selectors only.
        return value or None

    @field_validator("method_ref")
    @classmethod
    def _blank_method_ref_is_absent(cls, value: str | None) -> str | None:
        # A blank address selects nothing — same empty-as-absent rule as the run routes'
        # `_normalized_selector` boundary. A real value is passed through untouched.
        if value is None or not value.strip():
            return None
        return value


class CrateInvalidReport(BaseModel):
    """The `is_valid: false` arm shared by the crate routes — an unresolvable closure is a
    produced verdict on a `200`, never a thrown error. Branch on `is_valid`, not transport.
    """

    model_config = ConfigDict(extra="allow")

    is_valid: Literal[False]
    validation_errors: list[ValidationErrorItem]
    message: str


class CrateToolingRequest(CrateRequestBase):
    """The crate envelope plus the hosted tooling selector — the request base
    `/v1/resolve` and `/v1/codegen` share.

    `method_id` is a stored method's catalog id (`mt_…`), a **pass-through to the hosted
    API**: the platform resolves it against the org's catalog and injects the stored
    source before the runner sees the request — nothing is expanded client-side, and it
    is meaningless off-platform. An unknown or foreign-org id is a `404`
    (indistinguishable by design); a stored method with no MTHDS source is a `422`.

    An EMPTY selector is normalized to absent before the XOR counts (the base normalizes
    `files` / `method_ref`; `method_id` follows the same rule here), so an unusable value
    never counts as the sole selector and never reaches the wire.
    """

    method_id: str | None = None

    @field_validator("method_id")
    @classmethod
    def _blank_method_id_is_absent(cls, value: str | None) -> str | None:
        # A blank id selects nothing — same empty-as-absent rule as the run routes'
        # `_normalized_selector` boundary. A real value is passed through untouched.
        if value is None or not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        # The strict tooling XOR, enforced at construction so an illegal shape fails
        # before anything hits the wire (the server 422s the same shapes). Runs after
        # the field-level empty-as-absent normalization, so it counts real selectors.
        selector_count = sum(1 for selector in (self.files, self.method_ref, self.method_id) if selector is not None)
        if selector_count != 1:
            msg = "provide exactly one of `files`, `method_ref`, or `method_id`"
            raise ValueError(msg)
        return self


class ResolveRequest(CrateToolingRequest):
    """Request for `POST /v1/resolve` — the crate envelope (no projection axes) plus the
    hosted `method_id` selector. Exactly one of `files` / `method_ref` / `method_id`.
    """


class ResolveValidReport(BaseModel):
    """The `/v1/resolve` valid arm — the normalized library crate.

    `crate` is the MTHDS **Library Crate Format**: fully qualified refs, refinement
    flattened, natives materialized, top-level maps key-sorted. Its `fingerprint` and
    `mthds_version` ride INSIDE the payload, not beside it. Typed as opaque transport
    (`dict[str, Any]`): the crate schema is owned by the MTHDS standard, not by this SDK,
    and restating it here would be a second source of truth free to drift. Do not
    recompute the fingerprint by hashing this object — it is a property of the logical
    crate, not of any particular serialization; compare `fingerprint` values only.
    """

    model_config = ConfigDict(extra="allow")

    is_valid: Literal[True]
    crate: dict[str, Any]
    message: str


ResolveResponse: TypeAlias = Annotated[
    ResolveValidReport | CrateInvalidReport,
    Field(discriminator="is_valid"),
]

# The single parse path for a 200 `/resolve` body — discriminated on `is_valid`, built once
# at import (TypeAdapter construction is expensive), mirroring `PipelexValidationResultAdapter`.
ResolveResponseAdapter: TypeAdapter[ResolveResponse] = TypeAdapter(ResolveResponse)  # pylint: disable=invalid-name


CodegenKind = Literal["types"]
"""What `/v1/codegen` projects — the `kind` axis. `types` (the crate's whole concept set
projected into typed models) is the only kind served today."""

CodegenTarget = Literal["ts-zod", "python-pydantic", "python-structures"]
"""For whom `/v1/codegen` projects — the `target` axis, mirroring pipelex's `CodegenTarget`.
`python-pydantic` emits self-contained BaseModels (the natural target for Python consumers);
`python-structures` emits runtime StructuredContent classes for a Pipelex host; `ts-zod`
emits zod schemas plus inferred types."""


class CodegenRequest(CrateToolingRequest):
    """Request for `POST /v1/codegen` — the crate envelope plus the two explicit
    projection axes and the hosted `method_id` selector (exactly one of `files` /
    `method_ref` / `method_id`).

    `pipe_ref` exists for the future per-pipe projection kinds; the concept-set-wide
    `types` kind REJECTS it with a request-shape `422` rather than silently ignoring it.
    """

    kind: CodegenKind = "types"
    target: CodegenTarget
    pipe_ref: str | None = None


class GeneratedArtifact(BaseModel):
    """One stamped generated file. `path` is relative to the output root the caller
    chooses; `content` is complete, stamp header included, and is written verbatim.
    """

    path: str
    content: str


class CodegenValidReport(BaseModel):
    """The `/v1/codegen` valid arm — the stamped artifact set plus its lock.

    The trust chain: write every `artifacts` entry at its `path` and the `lock` content
    as `lock_filename`, both verbatim, and the tree is byte-identical to what a local
    `pipelex codegen types` run produces — same stamps, same lock — so the offline
    `pipelex codegen check` passes on it. Editing an artifact (or re-serializing the
    lock) breaks that chain.
    """

    model_config = ConfigDict(extra="allow")

    is_valid: Literal[True]
    #: Echo of the request's projection axes.
    kind: CodegenKind
    target: CodegenTarget
    #: Fingerprint of the normalized crate the artifacts were generated from.
    crate_fingerprint: str
    #: The pipelex engine version that generated them.
    engine_version: str
    artifacts: list[GeneratedArtifact]
    #: The lock file's TOML content — write verbatim beside the artifacts.
    lock: str
    #: The filename `lock` must be written as (`codegen.lock`).
    lock_filename: str
    message: str


CodegenResponse: TypeAlias = Annotated[
    CodegenValidReport | CrateInvalidReport,
    Field(discriminator="is_valid"),
]

# The single parse path for a 200 `/codegen` body — same regime as `ResolveResponseAdapter`.
CodegenResponseAdapter: TypeAdapter[CodegenResponse] = TypeAdapter(CodegenResponse)  # pylint: disable=invalid-name
