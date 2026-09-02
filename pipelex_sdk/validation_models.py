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
`ValidationErrorCategory`, `ValidationErrorItem`, `LiftablePipeEntry`, `SuggestedFix`, the
`FixOp` variants, `FixOpKind`, `FixSafety`) stay neutrally named — branding the envelope,
not the field names inside it. Fixes and lints are language-level concepts, and the runtime
names them brand-neutrally too.

Two members of the valid arm are deliberately **not** Pipelex's to declare. `pipe_io_contracts`
and `input_form` are the standard's own recommended extension fields of the validate report, each
with a normative page and a client model since `mthds` v0.9.0, so they are narrowed here **by
import** — `PipeIOContracts` from `mthds.protocol.pipe_io_contracts`, `InputForm` from
`mthds.protocol.input_form`. That keeps the "this SDK is transport, it does not own these types"
principle intact while the payloads stop being opaque: there is one declaration per language, and
an import cannot drift from it the way a restatement could. The types are used, never re-exported —
a consumer that wants to name a node's type (`ListField`, `PresenceMarker`, …) imports it from
`mthds.protocol` directly, where it belongs. `bundle_blueprint` and `graph_spec` stay opaque for
the reason that used to cover all four: no published package declares them, so a type here could
only be a copy.

Strictness composes rather than spreads. The imported artifacts are **closed** shapes
(`extra="forbid"`) — a member this `mthds` version does not define is version drift, refused at the
parse — while the report envelope around them stays extension-open (`extra="allow"`, inherited from
`ValidationReport`), so an unrelated field a future server adds to the report still parses and still
rides `model_extra`.

`PipelexAPIClient.validate()` returns this `PipelexValidationResult` (parsed via
`PipelexValidationResultAdapter`); the protocol base `MthdsAPIClient.validate()` returns the
neutral `mthds` `ValidationResult`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, TypeAlias

from mthds.protocol.input_form import InputForm
from mthds.protocol.models import InvalidValidationReport, ValidationDiagnostic, ValidationReport
from mthds.protocol.output_form import OutputForm
from mthds.protocol.pipe_io_contracts import PipeIOContracts
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pipelex_sdk._pydantic_utils import empty_list_factory_of

VALIDATION_VIEW_INPUT_FORM: Final[str] = "input_form"
"""A `views` token — asks for `PipelexValidationReport.input_form`.

Deliberately a constant rather than a closed enum: the request boundary is open, the server
resolves the tokens as a set and lenient-ignores the ones it does not know (never a `422`), so
a stale token must never fail a call.
"""

VALIDATION_VIEW_OUTPUT_FORM: Final[str] = "output_form"
"""A `views` token — asks for `PipelexValidationReport.output_form`.

The twin of the above on the other side of the pipe. Named as a constant for the same reason:
a caller passing the literal string gets no help from a type checker when the token changes."""


class DryRunStatus(StrEnum):
    """Per-pipe dry-run sweep outcome on `ValidatedPipeEntry.status`."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"


class ValidationErrorCategory(StrEnum):
    """The closed `validation_errors[].category` vocabulary (locked).

    This is the locked category vocabulary shared with the conformance corpus; keep the two
    in sync — a category the corpus knows and this set does not fails the whole verdict parse.
    """

    BLUEPRINT_VALIDATION = "blueprint_validation"
    PIPE_FACTORY = "pipe_factory"
    PIPE_VALIDATION = "pipe_validation"
    DRY_RUN = "dry_run"


class FixSafety(StrEnum):
    """Whether a suggested fix is safe to auto-apply (SAFE) or needs an explicit opt-in (UNSAFE)."""

    SAFE = "safe"
    UNSAFE = "unsafe"

    @property
    def is_safe(self) -> bool:
        match self:
            case FixSafety.SAFE:
                return True
            case FixSafety.UNSAFE:
                return False


class FixOpKind(StrEnum):
    """The closed vocabulary of semantic patch operations a `SuggestedFix` is composed of.

    A kind this SDK does not know fails the parse of the whole verdict, which is deliberate and
    consistent with `ValidationErrorCategory`: the vocabulary is closed upstream, a new kind is a
    runtime release this SDK mirrors, and a loud failure beats a silently unnarrowable op.
    """

    SET_KEY = "set_key"
    ENSURE_TABLE = "ensure_table"
    DELETE_KEY = "delete_key"
    DELETE_TABLE = "delete_table"
    RENAME_TABLE_KEY = "rename_table_key"
    MOVE_KEY = "move_key"
    REMAP_VALUE = "remap_value"


TomlScalar: TypeAlias = str | int | float | bool
"""What a `set_key` op can write as a bare TOML value."""

TomlValue: TypeAlias = TomlScalar | dict[str, TomlScalar]
"""A `set_key` value: a scalar, or a flat scalar mapping written as an inline table.

Deeper nesting is not modelled because the server does not emit it — the fixes that create a
whole table at once create a flat one.
"""


class _FixOpBase(BaseModel):
    """Fields every fix op shares: the table it acts in.

    These are **reader** models. The runtime declares its own copies `frozen`, `extra="forbid"`,
    with validators that refuse the wildcard segment, because it *plans* fixes; this SDK only
    reads them, so the ops follow the response-model convention (`extra="allow"`) and carry none
    of those validators — a new server-side member on an op must not break parsing here.

    The two runtime invariants a type cannot carry, recorded so a consumer knows them: `*` is the
    wildcard path segment and is refused as a `key` on every kind but `remap_value`; `ensure_table`
    and `delete_table` address the table itself rather than its parent, so their `table_path` is
    never empty (that one *is* expressed below, mirroring the OpenAPI artifact's `minItems: 1`).
    """

    model_config = ConfigDict(extra="allow")

    table_path: list[str]
    """The table the op acts in, e.g. `["pipe", "my_seq"]`. Empty means the document root."""


class SetKeyOp(_FixOpBase):
    """Write `key = value` in the addressed table, whatever it currently holds."""

    kind: Literal[FixOpKind.SET_KEY]
    key: str
    value: TomlValue


class EnsureTableOp(_FixOpBase):
    """Create the addressed table when it is absent, leaving an existing one untouched."""

    kind: Literal[FixOpKind.ENSURE_TABLE]
    table_path: list[str] = Field(min_length=1)


class DeleteKeyOp(_FixOpBase):
    """Drop `key` from the addressed table."""

    kind: Literal[FixOpKind.DELETE_KEY]
    key: str


class DeleteTableOp(_FixOpBase):
    """Drop the addressed table, including every chunk of one written out of order."""

    kind: Literal[FixOpKind.DELETE_TABLE]
    table_path: list[str] = Field(min_length=1)


class RenameTableKeyOp(_FixOpBase):
    """Rename `key` to `new_key` in place within the addressed table, keeping its position."""

    kind: Literal[FixOpKind.RENAME_TABLE_KEY]
    key: str
    new_key: str


class MoveKeyOp(_FixOpBase):
    """Relocate `key` from the addressed table into `new_table_path`, under `new_key`."""

    kind: Literal[FixOpKind.MOVE_KEY]
    key: str
    new_table_path: list[str]
    new_key: str


class RemapValueOp(_FixOpBase):
    """Rewrite `key`'s value through `mapping`, doing nothing when it is not a mapped value.

    This is the one kind for which `key` may be the wildcard segment `*`, meaning "each key of
    the addressed table" — the only shape in which a renamed enumerated value beneath an open
    mapping can be repaired at all.
    """

    kind: Literal[FixOpKind.REMAP_VALUE]
    key: str
    mapping: dict[str, str]


FixOp: TypeAlias = Annotated[
    SetKeyOp | EnsureTableOp | DeleteKeyOp | DeleteTableOp | RenameTableKeyOp | MoveKeyOp | RemapValueOp,
    Field(discriminator="kind"),
]
"""One semantic patch operation, discriminated on `kind`.

Narrow it with an exhaustive `match op: case SetKeyOp(): ...` — the Python spelling of the
JS mirror's `kind` narrowing.
"""


class SuggestedFix(BaseModel):
    """A deterministic fix for one validation error, ready for a style-preserving applier.

    `fix_code` is the kebab-case rule id (e.g. `"match-sequence-output"`). `source` is the file
    the ops target, when known (multi-file libraries) — an applier must only apply ops to the
    file they target. The ops are the machine contract; any rendered diff is presentation.
    """

    model_config = ConfigDict(extra="allow")

    fix_code: str
    description: str
    safety: FixSafety
    source: str | None = None
    ops: list[FixOp]


class LiftablePipeEntry(BaseModel):
    """One pipe the runtime may skip (lift) when an optional slot resolves absent."""

    model_config = ConfigDict(extra="allow")

    pipe_ref: str
    """Namespaced ref of the liftable pipe."""

    within_pipe_ref: str
    """Namespaced ref of the controller in whose flow the lift happens."""

    skipped_when_absent: list[str] = Field(default_factory=list)
    """The slot names whose absence lifts the pipe."""

    absence_source: str
    """Where the possible absence originates (human-readable)."""


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

    The same item type serves `PipelexValidationReport.warnings[]`, where the locators are
    serialized as explicit `null` rather than dropped. Every optional member is therefore
    `T | None = None` on purpose — pydantic reads a dropped key and an explicit `null` into the
    same `None`, and tightening any of them to required would break the valid arm.
    """

    category: ValidationErrorCategory  # pyright: ignore[reportIncompatibleVariableOverride]
    error_type: str | None = None
    """Deliberately an open string, not an enum: the runtime union keeps gaining advisory
    members (the `hint_*` lint types), and closing it here would turn every runtime addition
    into an SDK break for no consumer benefit."""

    pipe_code: str | None = None
    concept_code: str | None = None
    domain_code: str | None = None
    source: str | None = None
    field_path: str | None = None
    field_name: str | None = None
    missing_concept_code: str | None = None
    missing_pipe_code: str | None = None
    variable_names: list[str] | None = None
    declared_concepts: list[str] | None = None
    suggested_fix: SuggestedFix | None = None
    """The structured repair proposal for this error, when the runtime's fix planner produced
    one; absent otherwise."""


class PipelexValidationReport(ValidationReport):
    """The valid arm narrowed with pipelex's structural artifacts (`is_valid: true`)."""

    bundle_blueprint: dict[str, Any] = Field(default_factory=dict)
    """The parsed bundle, carried opaquely: no published package declares its shape, so a type
    here could only be a copy free to drift from the runtime that emits it."""

    pipe_io_contracts: PipeIOContracts = Field(default_factory=dict)
    """The per-pipe I/O contracts, typed by importing the standard's own client models.

    `PipeIOContracts` is `dict[pipe_ref, PipeIOContract]` (`mthds.protocol.pipe_io_contracts`), so a
    declared input slot reads as typed members — `concept_ref`, a three-valued `presence`
    (`PresenceMarker`), a `multiplicity` (`IOMultiplicity`), the `item_count` that is non-null exactly
    on the fixed arm, and its `json_schema` — and the output side reads its own asymmetric shape
    (a two-valued `optional`, because `!` is rejected on an output). The artifact belongs to the
    standard, so it is imported rather than restated: one declaration per language is what makes
    drift impossible, which is precisely what keeping it opaque used to buy.

    Contracts are **closed** shapes: a member this `mthds` version does not define is version drift
    and fails the parse. That closure is scoped to the artifact — the report around it stays
    extension-open — and it is the reason a contract from a runner predating the presence/multiplicity
    reshape no longer parses.

    Defaults to an empty map rather than `None`: the Pipelex valid arm always states the artifact, so
    no caller has to test for its absence."""

    graph_spec: Any = None
    """The execution graph, carried opaquely for the same reason as `bundle_blueprint`."""

    validated_pipes: list[ValidatedPipeEntry] = Field(default_factory=empty_list_factory_of(ValidatedPipeEntry))
    pending_signatures: list[str] = Field(default_factory=list)
    is_runnable: bool = True
    message: str = ""
    mthds_contents: list[str] | None = None
    warnings: list[ValidationErrorItem] = Field(default_factory=empty_list_factory_of(ValidationErrorItem))
    """Advisory lints on a bundle that is nonetheless valid — they never flip `is_valid`.

    Same item type as `validation_errors[]`, so one parser serves both channels. Defaults empty
    rather than being required, so a body from a runner predating the field still parses; that
    default is also what a clean bundle yields, so no caller can tell the two apart."""

    liftable_pipes: list[LiftablePipeEntry] = Field(default_factory=empty_list_factory_of(LiftablePipeEntry))
    """The pipes the runtime may skip when an optional slot resolves absent.

    Defaults empty for the same reason as `warnings`: an older runner's body must keep parsing."""

    input_form: InputForm | None = None
    """Per-pipe input-form descriptors, keyed exactly like `pipe_io_contracts`, typed by importing
    the standard's own client models.

    `InputForm` is `dict[pipe_ref, PipeInputFormDescriptor]` (`mthds.protocol.input_form`), whose
    `fields` are the recursive `InputFormField` union discriminated on `kind`: narrow a node with
    `match node: case ListField(): ...` or an `isinstance` check, importing the per-kind models from
    `mthds.protocol.input_form`. Imported rather than restated, for the same reason as the contracts,
    and closed the same way.

    The recursion changes layer, and a consumer narrowing it has to follow. Since `mthds` v0.10.0 the
    union is split in two by whether the node names itself: a top-level field is the **named** union
    (`TextField`, `DocumentField`, …, each requiring `name: str`), while a `ListField.item` is the
    **nameless** counterpart (`TextItem`, `DocumentItem`, …), which refuses a `name` at the parse. So
    a list's item narrows to `DocumentItem`, never `DocumentField` — and since each `*Field` derives
    from its `*Item`, only the item layer is a safe narrowing target at that position.

    Optional on purpose: it is present only when the request named the `input_form` view
    (`VALIDATION_VIEW_INPUT_FORM`), and an older runner emitted it unconditionally — `None` by
    default is the one typing that reads a body from either runner correctly."""

    output_form: OutputForm | None = None
    """Opt-in structured view: the per-pipe output-form descriptors, the twin of `input_form` on
    the other side of the pipe, requested through `views: ["output_form"]`.

    One `field` rather than a list of them, and no `presence` or `gating` — those are facts of a
    slot a caller fills, and a result is not one. Read together with that pipe's
    `output.json_schema` off `pipe_io_contracts`: the descriptor says what the result IS, the
    schema names the property its payload arrives under, and a consumer holding one but not the
    other is back to inferring the other from the value."""

    rendered_markdown: str | None = None
    """Opt-in Pipelex-API presentation extra: the server-rendered Markdown view of the verdict,
    present only when the request asked for it (`render: ["markdown"]`); absent (None) otherwise."""


class PipelexInvalidReport(InvalidValidationReport[ValidationErrorItem]):
    """The invalid arm carrying pipelex's structured `validation_errors[]` (`is_valid: false`).

    It gains none of the valid arm's additions: `warnings` and `input_form` derive from a crate
    that was never assembled, so the invalid arm never carries them.
    """

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
