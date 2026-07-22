"""Wire models for the `/v1/build/inputs` route — the signature source `prepare_inputs`
reads to resolve a pipe's declared inputs.

The Python SDK had no `/v1/build/*` coverage; `prepare_inputs` needs the explicit
inputs template, so this adds the `build_inputs` counterpart of `pipelex-sdk-js`'s
`buildInputs` (only this route — the other build projections are not needed here).
A produced verdict is a `200` discriminated on `is_valid`; a no-verdict condition
(unknown `pipe_ref`, auth, server fault) throws `ApiResponseError`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pipelex_sdk.validation_models import ValidationErrorItem

InputsTemplateFormat = Literal["json", "toml"]


class MthdsFileItem(BaseModel):
    """One MTHDS file in a build closure. `source` is an optional provenance label the
    server threads onto diagnostics raised from this file.
    """

    content: str
    source: str | None = None


class BuildInputsRequest(BaseModel):
    """Request for `POST /v1/build/inputs`. The closure is supplied as inline `files`."""

    files: list[MthdsFileItem]
    pipe_ref: str | None = None
    format: InputsTemplateFormat = "json"
    explicit: bool = False


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
