"""Pipelex's narrowing of the MTHDS `POST /v1/validate` 200-diagnostic union.

The MTHDS Protocol layer (`mthds.protocol.models`) declares the brand-neutral verdict
shapes: `ValidationReport` (`is_valid: true`), `InvalidValidationReport` (`is_valid: false`),
the `ValidationResult` discriminated union, and the neutral `ValidationDiagnostic` item.
The Pipelex runtime *narrows* those with its structural artifacts and its closed
`ValidationErrorCategory` vocabulary.

These narrowings are **Pipelex-branded implementation envelopes**, so they live here in
`pipelex-sdk`, not in the brand-neutral `mthds` package — mirroring `pipelex-sdk-js/src/models.ts`
and the documented brand boundary (`docs/architecture.md` → "Brand boundary"). The report/union
types carry the `Pipelex` prefix; the supporting types (`DryRunStatus`, `ValidatedPipeEntry`,
`ValidationErrorCategory`, `ValidationErrorItem`) stay neutrally named — branding the envelope,
not the field names inside it.

`PipelexAPIClient.validate()` returns this `PipelexValidationResult` (parsed via
`PipelexValidationResultAdapter`); the protocol base `MthdsAPIClient.validate()` returns the
neutral `mthds` `ValidationResult`.
"""

from __future__ import annotations

from typing import Annotated, Any, TypeAlias

from mthds.protocol.models import InvalidValidationReport, ValidationDiagnostic, ValidationReport
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pipelex_sdk._compat import StrEnum
from pipelex_sdk._pydantic_utils import empty_list_factory_of


class DryRunStatus(StrEnum):
    """Per-pipe dry-run sweep outcome on `ValidatedPipeEntry.status`."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"


class ValidationErrorCategory(StrEnum):
    """The closed `validation_errors[].category` vocabulary (locked).

    Mirrors the single source of truth in the conformance suite
    (`conformance/conformance/validation_contract.py`); keep in sync with it.
    """

    BLUEPRINT_VALIDATION = "blueprint_validation"
    PIPE_FACTORY = "pipe_factory"
    PIPE_VALIDATION = "pipe_validation"
    DRY_RUN = "dry_run"


class ValidatedPipeEntry(BaseModel):
    """One entry of `PipelexValidationReport.validated_pipes[]`."""

    model_config = ConfigDict(extra="allow")

    pipe_ref: str
    status: DryRunStatus


class ValidationErrorItem(ValidationDiagnostic):
    """Pipelex's structured `validation_errors[]` item — narrows the protocol base.

    `category` narrows to the closed `ValidationErrorCategory` set; the locators are
    populated per category and dropped from the wire when unset. Built by pipelex's
    one shared builder, so the hosted `InvalidReport` and the agent-CLI envelope
    cannot drift.
    """

    category: ValidationErrorCategory  # pyright: ignore[reportIncompatibleVariableOverride]
    error_type: str | None = None
    pipe_code: str | None = None
    concept_code: str | None = None
    domain_code: str | None = None
    source: str | None = None
    field_path: str | None = None
    field_name: str | None = None
    missing_concept_code: str | None = None
    variable_names: list[str] | None = None
    declared_concepts: list[str] | None = None


class PipelexValidationReport(ValidationReport):
    """The valid arm narrowed with pipelex's structural artifacts (`is_valid: true`)."""

    bundle_blueprint: dict[str, Any] = Field(default_factory=dict)
    pipe_io_contracts: dict[str, Any] = Field(default_factory=dict)
    graph_spec: Any = None
    validated_pipes: list[ValidatedPipeEntry] = Field(default_factory=empty_list_factory_of(ValidatedPipeEntry))
    pending_signatures: list[str] = Field(default_factory=list)
    is_runnable: bool = True
    message: str = ""
    mthds_contents: list[str] | None = None
    rendered_markdown: str | None = None
    """Opt-in Pipelex-API presentation extra: the server-rendered Markdown view of the verdict,
    present only when the request asked for it (`render: ["markdown"]`); absent (None) otherwise."""


class PipelexInvalidReport(InvalidValidationReport[ValidationErrorItem]):
    """The invalid arm carrying pipelex's structured `validation_errors[]` (`is_valid: false`)."""

    rendered_markdown: str | None = None
    """Opt-in Pipelex-API presentation extra: the server-rendered Markdown view of the invalid
    verdict, present only when the request asked for it (`render: ["markdown"]`); absent otherwise."""


PipelexValidationResult: TypeAlias = Annotated[
    PipelexValidationReport | PipelexInvalidReport,
    Field(discriminator="is_valid"),
]
"""Pipelex's `POST /v1/validate` 200 response — discriminated on `is_valid`."""


PipelexValidationResultAdapter: TypeAdapter[PipelexValidationResult] = TypeAdapter(PipelexValidationResult)  # pylint: disable=invalid-name
"""The single parse path for a 200 `/validate` body — built once at import (TypeAdapter construction is expensive).

Routes on the `is_valid` discriminant: a present `True` → `PipelexValidationReport`, a present
`False` → `PipelexInvalidReport`. A body missing/with-a-bad `is_valid` cannot be tagged and raises
`pydantic.ValidationError`, so a malformed 200 can never be mistaken for a valid verdict.
"""
