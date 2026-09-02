"""Contract round-trip tests for the 200-diagnostic `/validate` union.

Pins the Pipelex validation wire models (`pipelex_sdk.validation_models`) against the
canonical example bodies of the MTHDS Protocol's validation-report union. Mirrors
`mthds-js/tests/unit/protocol/validation-contract.test.ts`: parse a wire body at the boundary,
discriminate on `is_valid`, and assert the narrowed arm.
"""

from __future__ import annotations

from typing import Any

import pytest
from mthds.protocol.input_form import DocumentField, DocumentItem, ListField, TextField
from mthds.protocol.pipe_io_contracts import IOMultiplicity, PresenceMarker
from pydantic import ValidationError

from pipelex_sdk.validation_models import (
    DeleteKeyOp,
    DeleteTableOp,
    DryRunStatus,
    EnsureTableOp,
    FixOpKind,
    FixSafety,
    MoveKeyOp,
    PipelexInvalidReport,
    PipelexValidationReport,
    PipelexValidationResult,
    PipelexValidationResultAdapter,
    RemapValueOp,
    RenameTableKeyOp,
    SetKeyOp,
    ValidationErrorCategory,
)

# ── Canonical example bodies (verbatim shapes from the protocol spec) ─────────

VALID_BODY: dict[str, Any] = {
    "is_valid": True,
    "bundle_blueprint": {"source": "contracts.mthds", "domain": "legal_contracts"},
    "pipe_io_contracts": {
        "legal_contracts.summarize": {
            "inputs": {
                "contract": {
                    "concept_ref": "legal_contracts.Contract",
                    "presence": "plain",
                    "multiplicity": "single",
                    "item_count": None,
                    "json_schema": {"type": "object"},
                },
                "attachments": {
                    "concept_ref": "native.Document",
                    "presence": "plain",
                    "multiplicity": "variable",
                    "item_count": None,
                    "json_schema": {"type": "array", "items": {"type": "object"}},
                },
            },
            "output": {
                "concept_ref": "legal_contracts.Summary",
                "multiplicity": "single",
                "item_count": None,
                "optional": False,
                # Required on the contract since the output side gained a payload
                # schema. The empty schema rather than a plausible one: this fixture
                # is about the ENVELOPE parsing, and a shape here would invite an
                # assertion that belongs in a test about schemas.
                "json_schema": {},
            },
        }
    },
    "validated_pipes": [{"pipe_ref": "legal_contracts.summarize", "status": "SUCCESS"}],
    "pending_signatures": [],
    "is_runnable": True,
    "graph_spec": {"nodes": [], "edges": []},
    "mthds_contents": ["<verbatim submitted source>"],
    "message": "Validation succeeded.",
}

INVALID_BODY: dict[str, Any] = {
    "is_valid": False,
    "validation_errors": [
        {
            "category": "pipe_validation",
            "error_type": "PipeValidationError",
            "message": "Pipe references an unknown concept.",
            "pipe_code": "summarize",
            "concept_code": "Contractt",
            "field_name": "output",
            "source": "contracts.mthds",
        }
    ],
    "pending_signatures": [],
    "is_runnable": False,
    "message": "Validation found errors.",
}

DRY_RUN_BODY: dict[str, Any] = {
    "is_valid": False,
    "validation_errors": [{"category": "dry_run", "error_type": "DryRunError", "message": "Dry run failed: residual."}],
    "pending_signatures": [],
    "is_runnable": False,
    "message": "Validation found errors.",
}

BLUEPRINT_RESIDUAL_BODY: dict[str, Any] = {
    "is_valid": False,
    "validation_errors": [{"category": "blueprint_validation", "error_type": "TOMLDecodeError", "message": "Invalid TOML."}],
    "pending_signatures": [],
    "is_runnable": False,
    "message": "Validation found errors.",
}

PENDING_SIGNATURE_BODY: dict[str, Any] = {
    "is_valid": True,
    "bundle_blueprint": {"source": "draft.mthds"},
    "pipe_io_contracts": {},
    "validated_pipes": [],
    "pending_signatures": ["pending_sig.draft_step"],
    "is_runnable": False,
    "graph_spec": None,
    "message": "Validation succeeded.",
}

# The 0.17+ valid arm: advisory warnings, the liftable inventory, and the opt-in input form.
# The valid arm is dumped WITHOUT `exclude_none`, so an unset locator arrives as an explicit
# `null` where the invalid arm drops the key entirely — same item type, two serializations.
# Mirrors the JS fixture "carries advisory warnings on the VALID arm, with the valid arm's
# explicit nulls" (`pipelex-sdk-js/tests/client.test.ts`).
VALID_BODY_WITH_VIEWS: dict[str, Any] = {
    **VALID_BODY,
    "warnings": [
        {
            "category": "pipe_validation",
            "message": "the `!` on `profile` is redundant — the slot is always present",
            "error_type": "optional_force_redundant",
            "pipe_code": "legal_contracts.summarize",
            "concept_code": None,
            "domain_code": None,
            "source": None,
            "field_path": None,
            "field_name": None,
            "missing_concept_code": None,
            "missing_pipe_code": None,
            "variable_names": None,
            "declared_concepts": None,
            "suggested_fix": None,
        }
    ],
    "liftable_pipes": [
        {
            "pipe_ref": "legal_contracts.enrich",
            "within_pipe_ref": "legal_contracts.summarize",
            "skipped_when_absent": ["profile"],
            "absence_source": "optional input `profile` of legal_contracts.summarize",
        }
    ],
    "input_form": {
        "legal_contracts.summarize": {
            "fields": [
                {
                    "kind": "text",
                    "name": "contract",
                    "title": "Contract",
                    "concept_ref": "legal_contracts.Contract",
                    "required": True,
                    "presence": "plain",
                    "gating": True,
                    "max_length": 20000,
                },
                {
                    "kind": "list",
                    "name": "attachments",
                    "concept_ref": "native.Document",
                    "required": True,
                    "presence": "plain",
                    # A variable-length list is required yet never gates: the empty list is a
                    # legitimate value, which is why the wire states gating instead of deriving it.
                    "gating": False,
                    # The item states `required` like any node — only `presence` and `gating` are
                    # pipe-slot facts a nested node must not carry.
                    "item": {"kind": "document", "concept_ref": "native.Document", "required": True},
                },
            ]
        }
    },
}

# The 0.17+ invalid arm: the new `missing_pipe_code` locator and a structured repair proposal.
INVALID_BODY_WITH_FIX: dict[str, Any] = {
    "is_valid": False,
    "validation_errors": [
        {
            "category": "pipe_validation",
            "error_type": "PipeValidationError",
            "message": "Sequence step references an unknown pipe.",
            "pipe_code": "summarize",
            "missing_pipe_code": "enrichh",
            "source": "contracts.mthds",
            "suggested_fix": {
                "fix_code": "match-sequence-output",
                "description": "Rename the step and record its output.",
                "safety": "safe",
                "source": "contracts.mthds",
                "ops": [
                    {"kind": "rename_table_key", "table_path": ["pipe", "summarize", "steps"], "key": "enrichh", "new_key": "enrich"},
                    {"kind": "set_key", "table_path": ["pipe", "summarize"], "key": "output", "value": "legal_contracts.Summary"},
                ],
            },
        }
    ],
    "pending_signatures": [],
    "is_runnable": False,
    "message": "Validation found errors.",
}


# The contracts as a runner predating the presence/multiplicity reshape emitted them: a boolean
# `optional` on the input side, no `presence`, no `multiplicity`, no `item_count`. Typing the field
# by import is what makes this body a parse failure rather than an untyped passenger — the one
# behavioural break of the narrowing, and the shape that documents it.
PRE_RESHAPE_CONTRACTS_BODY: dict[str, Any] = {
    **VALID_BODY,
    "pipe_io_contracts": {
        "legal_contracts.summarize": {
            "inputs": {"contract": {"concept_ref": "legal_contracts.Contract", "optional": False, "json_schema": {}}},
            "output": {"concept_ref": "legal_contracts.Summary", "multiplicity": "single"},
        }
    },
}


def _body_with_contracts(input_contract: dict[str, Any]) -> dict[str, Any]:
    """A valid body whose one pipe declares exactly `input_contract` as its single input slot."""
    return {
        **VALID_BODY,
        "pipe_io_contracts": {
            "legal_contracts.summarize": {
                "inputs": {"contract": input_contract},
                "output": {"concept_ref": "legal_contracts.Summary", "multiplicity": "single", "item_count": None, "optional": False},
            }
        },
    }


def _body_with_descriptor_field(field: dict[str, Any]) -> dict[str, Any]:
    """A valid body whose one pipe's input form holds exactly `field`."""
    return {**VALID_BODY, "input_form": {"legal_contracts.summarize": {"fields": [field]}}}


def _parse(body: dict[str, Any]) -> PipelexValidationResult:
    """Parse a wire body through the real discriminated-union adapter — the exact parse path `PipelexAPIClient.validate()` uses."""
    return PipelexValidationResultAdapter.validate_python(body)


def _body_with_single_op(fix_op: dict[str, Any]) -> dict[str, Any]:
    """An invalid body whose one error carries a `suggested_fix` holding exactly `fix_op`."""
    return {
        **INVALID_BODY_WITH_FIX,
        "validation_errors": [
            {
                **INVALID_BODY_WITH_FIX["validation_errors"][0],
                "suggested_fix": {**INVALID_BODY_WITH_FIX["validation_errors"][0]["suggested_fix"], "ops": [fix_op]},
            }
        ],
    }


class TestValidationContract:
    def test_valid_arm_carries_typed_artifacts(self) -> None:
        """The valid arm parses to a typed report with structural artifacts."""
        report = _parse(VALID_BODY)
        assert isinstance(report, PipelexValidationReport)
        assert report.is_valid is True
        assert report.is_runnable is True
        assert report.bundle_blueprint["source"] == "contracts.mthds"
        assert "legal_contracts.summarize" in report.pipe_io_contracts
        assert report.validated_pipes[0].pipe_ref == "legal_contracts.summarize"
        assert report.validated_pipes[0].status is DryRunStatus.SUCCESS
        assert report.mthds_contents == ["<verbatim submitted source>"]

    def test_pipe_io_contracts_read_as_the_standards_models(self) -> None:
        """The contracts are typed by import: presence, multiplicity and the output asymmetry read as members."""
        report = _parse(VALID_BODY)
        assert isinstance(report, PipelexValidationReport)
        contract = report.pipe_io_contracts["legal_contracts.summarize"]

        single = contract.inputs["contract"]
        assert single.concept_ref == "legal_contracts.Contract"
        assert single.presence is PresenceMarker.PLAIN
        assert single.presence.is_optional is False
        assert single.multiplicity is IOMultiplicity.SINGLE
        assert single.multiplicity.is_plural is False
        assert single.item_count is None
        assert single.json_schema == {"type": "object"}

        plural = contract.inputs["attachments"]
        assert plural.multiplicity is IOMultiplicity.VARIABLE
        assert plural.multiplicity.is_plural is True

        # The output side is deliberately asymmetric: a two-valued `optional`, no schema.
        assert contract.output.concept_ref == "legal_contracts.Summary"
        assert contract.output.multiplicity is IOMultiplicity.SINGLE
        assert contract.output.item_count is None
        assert contract.output.optional is False

    def test_invalid_arm_carries_structured_errors_without_artifacts(self) -> None:
        """The invalid arm carries typed `validation_errors[]` and no structural artifacts."""
        report = _parse(INVALID_BODY)
        assert isinstance(report, PipelexInvalidReport)
        assert report.is_valid is False
        assert report.is_runnable is False
        item = report.validation_errors[0]
        assert item.category is ValidationErrorCategory.PIPE_VALIDATION
        assert item.pipe_code == "summarize"
        assert item.concept_code == "Contractt"
        assert item.field_name == "output"
        assert item.source == "contracts.mthds"
        # Structural artifacts do not exist on the invalid arm — not a field, not an extra.
        assert "bundle_blueprint" not in (report.model_extra or {})
        assert "graph_spec" not in (report.model_extra or {})

    def test_dry_run_residual_is_graph_level(self) -> None:
        """A dry-run residual is one `dry_run` item with no `source` (graph-level)."""
        report = _parse(DRY_RUN_BODY)
        assert isinstance(report, PipelexInvalidReport)
        item = report.validation_errors[0]
        assert item.category is ValidationErrorCategory.DRY_RUN
        assert item.error_type == "DryRunError"
        assert item.source is None

    def test_blueprint_residual_has_no_source(self) -> None:
        """A parse-level residual is one `blueprint_validation` item with no `source`."""
        report = _parse(BLUEPRINT_RESIDUAL_BODY)
        assert isinstance(report, PipelexInvalidReport)
        assert report.validation_errors[0].category is ValidationErrorCategory.BLUEPRINT_VALIDATION
        assert report.validation_errors[0].source is None

    def test_pending_signatures_is_valid_but_not_runnable(self) -> None:
        """Pending signatures ride a runnability fact on a VALID arm, never an error item."""
        report = _parse(PENDING_SIGNATURE_BODY)
        assert isinstance(report, PipelexValidationReport)
        assert report.is_valid is True
        assert report.is_runnable is False
        assert report.pending_signatures == ["pending_sig.draft_step"]

    def test_rendered_markdown_is_typed_on_both_arms_when_present(self) -> None:
        """The opt-in `rendered_markdown` extra parses to a typed field on both verdict arms."""
        valid = _parse({**VALID_BODY, "rendered_markdown": "# Validation passed"})
        assert isinstance(valid, PipelexValidationReport)
        assert valid.rendered_markdown == "# Validation passed"
        invalid = _parse({**INVALID_BODY, "rendered_markdown": "# Validation failed"})
        assert isinstance(invalid, PipelexInvalidReport)
        assert invalid.rendered_markdown == "# Validation failed"

    def test_rendered_markdown_is_none_when_absent(self) -> None:
        """Default responses omit `rendered_markdown` — the typed field defaults to None on both arms."""
        valid = _parse(VALID_BODY)
        assert isinstance(valid, PipelexValidationReport)
        assert valid.rendered_markdown is None
        invalid = _parse(INVALID_BODY)
        assert isinstance(invalid, PipelexInvalidReport)
        assert invalid.rendered_markdown is None

    # ── The 0.17+ valid arm: warnings, liftable pipes, the opt-in input form ──

    def test_valid_arm_carries_warnings_liftable_pipes_and_input_form(self) -> None:
        """The 0.17+ valid-arm additions parse into typed fields, `input_form` keyed like `pipe_io_contracts`."""
        report = _parse(VALID_BODY_WITH_VIEWS)
        assert isinstance(report, PipelexValidationReport)
        # Advisory items never flip the verdict.
        assert report.is_valid is True
        warning = report.warnings[0]
        assert warning.category is ValidationErrorCategory.PIPE_VALIDATION
        assert warning.error_type == "optional_force_redundant"
        assert warning.pipe_code == "legal_contracts.summarize"
        liftable = report.liftable_pipes[0]
        assert liftable.pipe_ref == "legal_contracts.enrich"
        assert liftable.within_pipe_ref == "legal_contracts.summarize"
        assert liftable.skipped_when_absent == ["profile"]
        assert liftable.absence_source == "optional input `profile` of legal_contracts.summarize"
        assert report.input_form is not None
        # Keyed exactly like `pipe_io_contracts` — the same `pipe_ref` set addresses both artifacts.
        assert set(report.input_form) == set(report.pipe_io_contracts)

    def test_input_form_reads_as_the_standards_models(self) -> None:
        """The descriptor is typed by import: nodes narrow on `kind`, and the recursion is typed through."""
        report = _parse(VALID_BODY_WITH_VIEWS)
        assert isinstance(report, PipelexValidationReport)
        assert report.input_form is not None
        descriptor = report.input_form["legal_contracts.summarize"]

        text_node, list_node = descriptor.fields
        # A node narrows to its per-kind model, which is what carries that kind's own slots.
        assert isinstance(text_node, TextField)
        assert text_node.name == "contract"
        assert text_node.title == "Contract"
        assert text_node.required is True
        assert text_node.presence is PresenceMarker.PLAIN
        assert text_node.gating is True
        assert text_node.max_length == 20000

        assert isinstance(list_node, ListField)
        assert list_node.name == "attachments"
        assert list_node.required is True
        # Required yet non-gating, stated rather than re-derived from `required`.
        assert list_node.gating is False
        # No `item_count`: the slot is variable-length, not a fixed `[N]`.
        assert list_node.item_count is None
        # The recursion is typed through: the item is itself a narrowed node — but on the
        # nameless layer. A list's item parses into the `*Item` union, never the `*Field` one.
        assert isinstance(list_node.item, DocumentItem)
        # `DocumentField` is `DocumentItem` plus `name`, so the negative is what pins the split:
        # narrowing to the item layer alone would still admit a named node.
        assert not isinstance(list_node.item, DocumentField)
        assert list_node.item.concept_ref == "native.Document"
        # Pipe-slot facts live on the top-level field only, never on a list's item.
        assert list_node.item.presence is None
        assert list_node.item.gating is None

    def test_typed_artifacts_do_not_close_the_report_envelope(self) -> None:
        """An unrelated future extension field on the report still parses and still rides `model_extra`.

        This is the guard that keeps the two strictness regimes composed the way the standard
        intends: the imported artifacts are closed shapes, while the report envelope around them
        stays extension-open. A future edit that reached for `extra="forbid"` on the report — or a
        narrowing that somehow propagated the artifacts' closure outward — fails here.
        """
        report = _parse({**VALID_BODY_WITH_VIEWS, "cost_estimate": {"usd": 0.01}, "some_future_view": ["anything"]})
        assert isinstance(report, PipelexValidationReport)
        extra = report.model_extra or {}
        assert extra["cost_estimate"] == {"usd": 0.01}
        assert extra["some_future_view"] == ["anything"]
        # And the typed artifacts parsed all the same.
        assert report.input_form is not None
        assert "legal_contracts.summarize" in report.pipe_io_contracts

    @pytest.mark.parametrize(
        "drifted_body",
        [
            pytest.param(
                _body_with_contracts(
                    {
                        "concept_ref": "legal_contracts.Contract",
                        "presence": "plain",
                        "multiplicity": "single",
                        "item_count": None,
                        "json_schema": {"type": "object"},
                        "tolerance": "lenient",
                    }
                ),
                id="undefined-member-inside-an-input-contract",
            ),
            pytest.param(
                _body_with_contracts(
                    {
                        "concept_ref": "legal_contracts.Contract",
                        "presence": "plain",
                        "multiplicity": "fixed",
                        "item_count": None,
                        "json_schema": {"type": "array", "items": {"type": "object"}},
                    }
                ),
                id="fixed-multiplicity-missing-its-item-count",
            ),
            pytest.param(
                _body_with_descriptor_field(
                    {"kind": "text", "name": "contract", "required": True, "presence": "plain", "gating": True, "widget": "textarea"}
                ),
                id="undefined-member-inside-a-field-descriptor",
            ),
            pytest.param(
                _body_with_descriptor_field({"kind": "text", "name": "contract", "required": True}),
                id="top-level-field-stating-no-pipe-slot-facts",
            ),
            pytest.param(PRE_RESHAPE_CONTRACTS_BODY, id="pre-reshape-contract-carrying-the-boolean-optional"),
        ],
    )
    def test_artifact_drift_fails_the_parse(self, drifted_body: dict[str, Any]) -> None:
        """Inside an artifact, an undefined member or a violated invariant is version drift and is refused.

        Deliberate, and the standard's own rule (both artifacts are closed shapes) rather than this
        SDK's invention: the artifact is a view of one version of the standard and does not grow,
        where the report is the envelope and does. The pre-reshape case is the one behavioural break
        of typing these fields — a runner older than the presence/multiplicity reshape emits an input
        contract this package refuses, where it used to ride through untyped.
        """
        with pytest.raises(ValidationError):
            _parse(drifted_body)

    def test_valid_arm_warning_reads_every_explicit_null_as_none(self) -> None:
        """Every explicitly-null locator on a warning reads as `None`.

        The valid arm is dumped without `exclude_none`, so an unset locator arrives as an
        explicit `null` where the invalid arm drops the key. This is the regression guard
        against a future "tighten to required" edit on `ValidationErrorItem`.
        """
        report = _parse(VALID_BODY_WITH_VIEWS)
        assert isinstance(report, PipelexValidationReport)
        warning = report.warnings[0]
        assert warning.concept_code is None
        assert warning.domain_code is None
        assert warning.source is None
        assert warning.field_path is None
        assert warning.field_name is None
        assert warning.missing_concept_code is None
        assert warning.missing_pipe_code is None
        assert warning.variable_names is None
        assert warning.declared_concepts is None
        assert warning.suggested_fix is None

    def test_valid_body_without_the_view_fields_parses_with_empty_defaults(self) -> None:
        """A verdict that carries none of the opt-in members parses: both lists empty, `input_form` None.

        That is what a caller who never asked for a view reads, and also what a runner predating
        those members emits — the defaults make the two indistinguishable, on purpose.
        """
        report = _parse(VALID_BODY)
        assert isinstance(report, PipelexValidationReport)
        assert report.warnings == []
        assert report.liftable_pipes == []
        assert report.input_form is None

    # ── The 0.17+ invalid arm: `missing_pipe_code` and the fix vocabulary ──

    def test_invalid_arm_carries_missing_pipe_code_and_narrowable_fix_ops(self) -> None:
        """A structured `suggested_fix` parses, and `match`-narrowing reaches each op's own members."""
        report = _parse(INVALID_BODY_WITH_FIX)
        assert isinstance(report, PipelexInvalidReport)
        item = report.validation_errors[0]
        assert item.missing_pipe_code == "enrichh"
        fix = item.suggested_fix
        assert fix is not None
        assert fix.fix_code == "match-sequence-output"
        assert fix.safety is FixSafety.SAFE
        assert fix.safety.is_safe is True
        assert fix.source == "contracts.mthds"

        rename_op, set_op = fix.ops
        # Narrowing is an exhaustive `match` over the op classes — the Python spelling of the
        # JS mirror's `kind` narrowing.
        match rename_op:
            case RenameTableKeyOp():
                assert rename_op.table_path == ["pipe", "summarize", "steps"]
                assert rename_op.key == "enrichh"
                assert rename_op.new_key == "enrich"
            case SetKeyOp() | EnsureTableOp() | DeleteKeyOp() | DeleteTableOp() | MoveKeyOp() | RemapValueOp():
                pytest.fail("expected a rename_table_key op")
        match set_op:
            case SetKeyOp():
                assert set_op.table_path == ["pipe", "summarize"]
                assert set_op.key == "output"
                assert set_op.value == "legal_contracts.Summary"
            case EnsureTableOp() | DeleteKeyOp() | DeleteTableOp() | RenameTableKeyOp() | MoveKeyOp() | RemapValueOp():
                pytest.fail("expected a set_key op")

    def test_ensure_table_op_rejects_an_empty_table_path(self) -> None:
        """`ensure_table` addresses the table itself, so its path is never empty (artifact `minItems: 1`)."""
        bad_body = _body_with_single_op({"kind": "ensure_table", "table_path": []})
        with pytest.raises(ValidationError, match="table_path"):
            PipelexValidationResultAdapter.validate_python(bad_body)

    def test_unknown_fix_op_kind_is_rejected(self) -> None:
        """An out-of-vocabulary op kind fails the whole verdict parse — the discriminator is closed."""
        bad_body = _body_with_single_op({"kind": "invent_key", "table_path": ["pipe"], "key": "x"})
        with pytest.raises(ValidationError, match="invent_key"):
            PipelexValidationResultAdapter.validate_python(bad_body)

    def test_fix_vocabularies_are_the_locked_sets(self) -> None:
        """`FixSafety` and `FixOpKind` mirror the runtime's closed vocabularies (drift guard)."""
        assert {safety.value for safety in FixSafety} == {"safe", "unsafe"}
        assert {kind.value for kind in FixOpKind} == {
            "set_key",
            "ensure_table",
            "delete_key",
            "delete_table",
            "rename_table_key",
            "move_key",
            "remap_value",
        }

    def test_category_vocabulary_is_the_locked_set(self) -> None:
        """The closed category set mirrors the locked conformance vocabulary (drift guard)."""
        assert {category.value for category in ValidationErrorCategory} == {
            "blueprint_validation",
            "pipe_factory",
            "pipe_validation",
            "dry_run",
        }

    def test_unknown_category_is_rejected(self) -> None:
        """An out-of-vocabulary category fails validation — the enum is a closed set."""
        bad_body = {**INVALID_BODY, "validation_errors": [{"category": "made_up", "message": "x"}]}
        with pytest.raises(ValidationError, match="made_up"):
            PipelexValidationResultAdapter.validate_python(bad_body)

    @pytest.mark.parametrize(
        "malformed_body",
        [
            {},  # no discriminant at all
            {"message": "x"},  # still no discriminant — must NOT be read as a valid verdict
            {"is_valid": None},  # null discriminant cannot be tagged
            {"is_valid": "false"},  # non-boolean discriminant cannot be tagged
            {"is_valid": False, "message": "x"},  # invalid arm tagged, but required validation_errors missing
        ],
    )
    def test_malformed_200_body_raises_no_silent_valid(self, malformed_body: dict[str, Any]) -> None:
        """A 200 body that can't be discriminated, or whose tagged arm misses a required field, raises.

        Regression guard for the silent-valid hole: the old hand-rolled `is_valid is False` check
        treated any non-`False` discriminant (missing, null, anything) as valid. Routing through the
        discriminated-union adapter makes a missing/bad discriminant a loud `ValidationError` instead.
        """
        with pytest.raises(ValidationError):
            PipelexValidationResultAdapter.validate_python(malformed_body)
