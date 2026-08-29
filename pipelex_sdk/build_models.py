"""Wire models for the `/v1/build/inputs` route — the signature source `prepare_inputs`
reads to resolve a pipe's declared inputs.

The Python SDK had no `/v1/build/*` coverage; `prepare_inputs` needs the explicit
inputs template, so this adds the `build_inputs` counterpart of `pipelex-sdk-js`'s
`buildInputs` (only this route — the other build projections are not needed here).
The crate routes (`/v1/resolve`, `/v1/codegen`) share this module's `CrateRequestBase`
envelope and `CrateInvalidReport` arm through `crate_models.py`.
A produced verdict is a `200` discriminated on `is_valid`; a no-verdict condition
(unknown `pipe_ref`, auth, server fault) throws `ApiResponseError`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from pipelex_sdk.validation_models import ValidationErrorItem

InputsTemplateFormat = Literal["json", "toml"]


class MthdsFileItem(BaseModel):
    """One MTHDS file in a build closure. `source` is an optional provenance label the
    server threads onto diagnostics raised from this file.
    """

    content: str
    source: str | None = None


class CrateRequestBase(BaseModel):
    """The closure selector every crate-family route shares — `/v1/resolve`,
    `/v1/codegen`, and `/v1/build/*` (mirror of the server's `MthdsFilesRequest` and
    of `pipelex-sdk-js`'s `CrateRequestBase`).

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

    The subclasses own the exclusivity validator, because the crate routes add a third
    selector (the hosted `method_id`) that the build projections deliberately refuse.
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


class BuildInputsRequest(CrateRequestBase):
    """Request for `POST /v1/build/inputs`. The closure is inline `files` XOR a
    `method_ref` address; there is NO by-id form — the `/v1/build/*` projections are
    deliberately excluded from the hosted tooling selector (`method_id` covers
    `validate` / `resolve` / `codegen` only), so a stored method is expanded first
    (fetch it with `get_method` and pass its source as `files`).
    """

    pipe_ref: str | None = None
    format: InputsTemplateFormat = "json"
    explicit: bool = False

    @model_validator(mode="before")
    @classmethod
    def _refuse_method_id(cls, data: Any) -> Any:
        # A teaching error beats pydantic's default extra="ignore" silently dropping the
        # key: a caller migrating from the by-id habit must learn the build routes have
        # no by-id form (mirrors the JS `method_id: never` pin + runtime guard).
        raw: Any = data
        if isinstance(data, dict) and "method_id" in data:
            msg = (
                "build_inputs takes no method_id — the /v1/build/* projections are excluded from the "
                "hosted tooling selector (it covers validate/resolve/codegen only). Expand the stored "
                "method first: fetch it with get_method and pass its MTHDS source as files."
            )
            raise ValueError(msg)
        return raw

    @model_validator(mode="after")
    def _exactly_one_closure_selector(self) -> Self:
        # Mirrors the server's own XOR so an illegal shape fails at construction, before
        # anything hits the wire.
        if (self.files is None) == (self.method_ref is None):
            msg = "provide exactly one of `files` or `method_ref`"
            raise ValueError(msg)
        return self


class BuildInputsValidReport(BaseModel):
    """The `is_valid: true` arm. The template rides `inputs` (json) or `inputs_toml` (toml)."""

    model_config = ConfigDict(extra="allow")

    is_valid: Literal[True]
    pipe_ref: str
    requested_pipe_ref: str | None = None
    message: str
    format: InputsTemplateFormat
    explicit: bool
    inputs: dict[str, Any] | None = None
    inputs_toml: str | None = None

    @model_validator(mode="after")
    def _template_matches_format(self) -> Self:
        # Honor the adapter's malformed-200 guarantee for the template shape too: a valid
        # verdict must carry the template field its `format` selects (and not the other).
        # Without this, an `is_valid: true` body missing both templates would parse as a valid
        # report and only fail one layer down in `prepare_inputs`.
        match self.format:
            case "json":
                if self.inputs is None:
                    msg = "inputs is required when format is 'json'"
                    raise ValueError(msg)
                if self.inputs_toml is not None:
                    msg = "inputs_toml must be absent when format is 'json'"
                    raise ValueError(msg)
            case "toml":
                if self.inputs_toml is None:
                    msg = "inputs_toml is required when format is 'toml'"
                    raise ValueError(msg)
                if self.inputs is not None:
                    msg = "inputs must be absent when format is 'toml'"
                    raise ValueError(msg)
        return self


class CrateInvalidReport(BaseModel):
    """The `is_valid: false` arm shared by the build routes — an unresolvable closure is a
    produced verdict on a `200`, never a thrown error. Branch on `is_valid`, not transport.
    """

    model_config = ConfigDict(extra="allow")

    is_valid: Literal[False]
    validation_errors: list[ValidationErrorItem]
    message: str


BuildInputsResponse: TypeAlias = Annotated[
    BuildInputsValidReport | CrateInvalidReport,
    Field(discriminator="is_valid"),
]

# The single parse path for a 200 `/build/inputs` body — discriminated on `is_valid`, built once at
# import (TypeAdapter construction is expensive), mirroring `PipelexValidationResultAdapter`. A
# malformed 200 (or an empty body) raises a clean `pydantic.ValidationError` rather than being
# mistaken for a valid verdict.
BuildInputsResponseAdapter: TypeAdapter[BuildInputsResponse] = TypeAdapter(BuildInputsResponse)  # pylint: disable=invalid-name
