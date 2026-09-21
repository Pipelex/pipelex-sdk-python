"""Tests for pipelex_sdk.runs — run-lifecycle models for the hosted polling surface."""

from typing import Any

import pytest
from mthds.protocol.input_form import PipeInputFormDescriptor, ProseField
from mthds.protocol.output_form import PipeOutputFormDescriptor
from mthds.protocol.pipe_io_contracts import IOMultiplicity, PipeIOContract
from pydantic import TypeAdapter, ValidationError

from pipelex_sdk.runs import RunResults, RunStatus, TokensUsageRecord

# A record in the shape the current runtime emits: every contract field present, absent values
# sent as explicit nulls. Mirrors the shared conformance corpus of usage records, which is what
# the platform arm asserts on the wire.
_RATED_RECORD: dict[str, Any] = {
    "model_type": "llm",
    "inference_model_name": "test-model",
    "inference_model_id": "test-model-2026-01-01",
    "pipe_code": "test_domain.summarize",
    "job_category": "llm_job",
    "unit_job_id": "llm_gen_text",
    "nb_tokens_by_category": {"input": 15, "input_cached": 5, "output": 4},
    "cost": 0.000105,
    "started_at": "2026-06-20T10:00:01+00:00",
    "completed_at": "2026-06-20T10:00:03+00:00",
}

# A durable artifact written BEFORE the wire contract shipped, relayed verbatim ever since: a dump
# of the runtime's internal reporting model, carrying the nested `job_metadata` and the `unit_costs`
# rate table, and lacking the computed `cost`. Old artifacts are never migrated, so the mirror must
# parse this without complaint.
_PRE_CONTRACT_RECORD: dict[str, Any] = {
    "model_type": "llm",
    "inference_model_name": "legacy-model",
    "inference_model_id": "legacy-model-v0",
    "nb_tokens_by_category": {"input": 20, "output": 6},
    "unit_costs": {"input": 3.0, "output": 15.0},
    "job_metadata": {
        "pipe_code": "legacy_domain.summarize",
        "job_category": "llm_job",
        "session_id": "legacy-session",
        "user_id": "legacy-user",
    },
}

# The executed graph as the runner writes it to `graphspec.json`: opaque to this SDK, relayed as is.
_GRAPH_SPEC: dict[str, Any] = {
    "meta": {"format": "mthds", "mode": "live"},
    "nodes": [{"id": "pipe_1", "status": "COMPLETED"}],
    "edges": [],
}

# The three I/O artifacts for a one-pipe library, in the standard's own shapes and keyed over the
# one shared `pipe_ref` set — the same fixture the JS SDK's tests carry, so the two mirrors are
# exercised on one document.
_PIPE_IO_CONTRACTS: dict[str, Any] = {
    "x.greet": {
        "inputs": {},
        "output": {
            "concept_ref": "native.Text",
            "multiplicity": "single",
            "item_count": None,
            "optional": False,
            "json_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    },
}
_INPUT_FORM: dict[str, Any] = {"x.greet": {"fields": []}}
_OUTPUT_FORM: dict[str, Any] = {"x.greet": {"field": {"name": "text", "kind": "prose", "required": True}}}

_GRAPH_ASSEMBLY_ERROR = "failed to assemble the graph for the run"
_PIPE_IO_ARTIFACTS_ERROR = "failed to build the I/O artifacts for the run"

# Every field the hosted results body may carry beside the ones always present. Their absence,
# their explicit null and their value are three different readings, and the tests below pin each.
_OPTIONAL_RESULT_KEYS = (
    "graph_spec",
    "graph_assembly_error",
    "pipe_io_contracts",
    "input_form",
    "output_form",
    "pipe_io_artifacts_error",
    "tokens_usages",
    "usage_assembly_error",
)


class TestRuns:
    @pytest.mark.parametrize(
        ("status", "is_terminal", "is_success"),
        [
            (RunStatus.PENDING, False, False),
            (RunStatus.STARTED, False, False),
            (RunStatus.RUNNING, False, False),
            (RunStatus.COMPLETED, True, True),
            (RunStatus.FAILED, True, False),
            (RunStatus.CANCELLED, True, False),
            (RunStatus.TERMINATED, True, False),
            (RunStatus.TIMED_OUT, True, False),
        ],
    )
    def test_run_status_predicates(self, status: RunStatus, is_terminal: bool, is_success: bool) -> None:
        """is_terminal / is_success classify every status correctly."""
        assert status.is_terminal is is_terminal
        assert status.is_success is is_success

    def test_run_status_parses_from_string(self) -> None:
        """A wire string parses into the enum."""
        adapter = TypeAdapter(RunStatus)
        assert adapter.validate_python("TIMED_OUT") == RunStatus.TIMED_OUT

    def test_tokens_usage_record_parses_every_contract_field(self) -> None:
        """A current-shape record round-trips each contract field with its wire value and type."""
        record = TokensUsageRecord.model_validate(_RATED_RECORD)

        assert record.model_type == "llm"
        assert record.inference_model_name == "test-model"
        assert record.inference_model_id == "test-model-2026-01-01"
        assert record.pipe_code == "test_domain.summarize"
        assert record.job_category == "llm_job"
        assert record.unit_job_id == "llm_gen_text"
        assert record.nb_tokens_by_category == {"input": 15, "input_cached": 5, "output": 4}
        assert record.cost == 0.000105
        assert record.started_at == "2026-06-20T10:00:01+00:00"
        assert record.completed_at == "2026-06-20T10:00:03+00:00"
        # Nothing rode `model_extra`: the contract field set covers the whole record.
        assert record.model_extra == {}

    def test_tokens_usage_record_parses_pre_contract_record(self) -> None:
        """A pre-contract artifact record parses instead of raising: the contract fields it predates
        come back None, and its legacy fields survive as extras rather than being dropped.
        """
        record = TokensUsageRecord.model_validate(_PRE_CONTRACT_RECORD)

        assert record.inference_model_name == "legacy-model"
        assert record.nb_tokens_by_category == {"input": 20, "output": 6}
        # `cost` is server-computed and did not exist when this artifact was written; `pipe_code` was
        # still nested inside `job_metadata` rather than flattened onto the record.
        assert record.cost is None
        assert record.pipe_code is None
        # The legacy fields ride `model_extra` — relayed, never reshaped. A client must not read them
        # as contract fields, but the mirror must not choke on them either.
        assert record.model_extra == {
            "unit_costs": {"input": 3.0, "output": 15.0},
            "job_metadata": {
                "pipe_code": "legacy_domain.summarize",
                "job_category": "llm_job",
                "session_id": "legacy-session",
                "user_id": "legacy-user",
            },
        }

    def test_tokens_usage_record_keeps_unrated_cost_null(self) -> None:
        """An unrated call sends `cost: null` — distinct from a rate table that priced it at zero."""
        unrated = TokensUsageRecord.model_validate({**_RATED_RECORD, "cost": None})
        priced_at_zero = TokensUsageRecord.model_validate({**_RATED_RECORD, "cost": 0})

        assert unrated.cost is None
        assert priced_at_zero.cost == 0.0
        assert priced_at_zero.cost is not None

    def test_run_results_validates_usage_records(self) -> None:
        """A results body's raw records become typed records; the null branch stays None."""
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {"answer": "42"},
                "tokens_usages": [_RATED_RECORD, _PRE_CONTRACT_RECORD],
                "usage_assembly_error": None,
            }
        )

        assert results.tokens_usages is not None
        assert [record.inference_model_name for record in results.tokens_usages] == ["test-model", "legacy-model"]
        assert [record.cost for record in results.tokens_usages] == [0.000105, None]
        assert results.usage_assembly_error is None

    @pytest.mark.parametrize(
        ("tokens_usages", "usage_assembly_error"),
        [
            pytest.param(None, None, id="assembly-off-or-pre-artifact"),
            pytest.param(None, "failed to read usage events for the run", id="assembly-broke"),
            pytest.param([], None, id="assembly-ran-no-inference"),
        ],
    )
    def test_run_results_preserves_usage_null_semantics(self, tokens_usages: list[dict[str, Any]] | None, usage_assembly_error: str | None) -> None:
        """`None` (off / broke / pre-artifact) and `[]` (ran, no inference) stay distinct, and
        `usage_assembly_error` is the only field separating a broken assembly from the other nulls.
        """
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {"answer": "42"},
                "tokens_usages": tokens_usages,
                "usage_assembly_error": usage_assembly_error,
            }
        )

        assert results.tokens_usages == tokens_usages
        assert results.usage_assembly_error == usage_assembly_error

    def test_run_results_defaults_usage_pair_to_none(self) -> None:
        """A body with no usage keys at all (older platform) leaves both fields None, never raises."""
        results = RunResults.model_validate({"pipeline_run_id": "run_1", "main_stuff": {"answer": "42"}})

        assert results.tokens_usages is None
        assert results.usage_assembly_error is None
        assert results.pipe_output is None

    # ── The graph pair and the I/O artifacts ─────────────────────

    def test_run_results_declares_every_optional_field_absent_as_none_and_unset(self) -> None:
        """A hosted body carrying none of the optional keys (an older platform, or a lean relay)
        leaves each declared field `None` and out of `model_fields_set` — which is how a Python
        reader tells "the platform relayed no such key" from "the platform relayed null", where the
        JS twin reads `undefined`.
        """
        results = RunResults.model_validate({"pipeline_run_id": "run_1", "main_stuff": {"answer": "42"}})

        assert results.graph_spec is None
        assert results.graph_assembly_error is None
        assert results.pipe_io_contracts is None
        assert results.input_form is None
        assert results.output_form is None
        assert results.pipe_io_artifacts_error is None
        assert results.model_fields_set == {"pipeline_run_id", "main_stuff"}
        # Nothing rode `model_extra` either: every key of the body is declared.
        assert results.model_extra == {}

    def test_run_results_reads_a_relayed_null_as_set(self) -> None:
        """A key the platform relays as `null` (the artifact was not written, or the run described no
        data) reads `None` too, but is IN `model_fields_set`: relayed-as-null and not-relayed are
        distinguishable, which is what the JS `null` / `undefined` split carries.
        """
        body: dict[str, Any] = {"pipeline_run_id": "run_1", "main_stuff": {"answer": "42"}}
        for key in _OPTIONAL_RESULT_KEYS:
            body[key] = None
        results = RunResults.model_validate(body)

        for key in _OPTIONAL_RESULT_KEYS:
            assert getattr(results, key) is None, key
        assert set(_OPTIONAL_RESULT_KEYS) <= results.model_fields_set

    def test_run_results_types_the_io_artifacts_from_the_standard(self) -> None:
        """The three artifacts parse into the standard's own models, imported from `mthds.protocol`
        rather than restated: a contract entry is a `PipeIOContract`, a form entry a descriptor whose
        field is the kind-discriminated node union.
        """
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {"text": "hello"},
                "graph_spec": _GRAPH_SPEC,
                "graph_assembly_error": None,
                "pipe_io_contracts": _PIPE_IO_CONTRACTS,
                "input_form": _INPUT_FORM,
                "output_form": _OUTPUT_FORM,
                "pipe_io_artifacts_error": None,
            }
        )

        # The graph stays opaque and rides through unchanged.
        assert results.graph_spec == _GRAPH_SPEC
        assert results.graph_assembly_error is None
        assert results.pipe_io_contracts is not None
        contract = results.pipe_io_contracts["x.greet"]
        assert isinstance(contract, PipeIOContract)
        assert contract.inputs == {}
        assert contract.output.concept_ref == "native.Text"
        assert contract.output.multiplicity == IOMultiplicity.SINGLE
        assert contract.output.item_count is None
        assert contract.output.optional is False
        assert contract.output.json_schema == {"type": "object", "properties": {"text": {"type": "string"}}}
        assert results.input_form is not None
        input_descriptor = results.input_form["x.greet"]
        assert isinstance(input_descriptor, PipeInputFormDescriptor)
        assert input_descriptor.fields == []
        assert results.output_form is not None
        output_descriptor = results.output_form["x.greet"]
        assert isinstance(output_descriptor, PipeOutputFormDescriptor)
        assert isinstance(output_descriptor.field, ProseField)
        assert output_descriptor.field.name == "text"
        assert output_descriptor.field.required is True
        assert results.pipe_io_artifacts_error is None
        # The three share one key set — the reader's rule that they are taken together.
        assert set(results.pipe_io_contracts) == set(results.input_form) == set(results.output_form) == {"x.greet"}

    def test_run_results_round_trips_the_io_artifacts_verbatim(self) -> None:
        """Dumping the parsed artifacts in JSON mode gives back the relayed documents: typing them
        adds nothing and drops nothing.
        """
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {"text": "hello"},
                "pipe_io_contracts": _PIPE_IO_CONTRACTS,
                "input_form": _INPUT_FORM,
                "output_form": _OUTPUT_FORM,
            }
        )
        dumped = results.model_dump(mode="json", exclude_unset=True)

        assert dumped["pipe_io_contracts"] == _PIPE_IO_CONTRACTS
        assert dumped["input_form"] == _INPUT_FORM
        assert dumped["output_form"] == _OUTPUT_FORM

    @pytest.mark.parametrize(
        ("key", "drifted_value"),
        [
            pytest.param(
                "pipe_io_contracts",
                {"x.greet": {**_PIPE_IO_CONTRACTS["x.greet"], "not_in_this_standard": 1}},
                id="contract-member",
            ),
            pytest.param("input_form", {"x.greet": {"fields": [], "not_in_this_standard": 1}}, id="input-form-member"),
            pytest.param(
                "output_form",
                {"x.greet": {"field": {"name": "text", "kind": "prose", "required": True}, "not_in_this_standard": 1}},
                id="output-form-member",
            ),
        ],
    )
    def test_run_results_refuses_an_artifact_member_the_pinned_standard_does_not_define(self, key: str, drifted_value: dict[str, Any]) -> None:
        """The artifacts are closed shapes: a member the pinned `mthds` does not define is version
        drift, refused at the parse of the whole results body rather than read half-way — the same
        ruling the validate report follows. The envelope around them stays open (see the extras test
        below), so strictness composes rather than spreads.
        """
        with pytest.raises(ValidationError):
            RunResults.model_validate({"pipeline_run_id": "run_1", "main_stuff": {}, key: drifted_value})

    def test_run_results_stays_extension_open_around_the_typed_artifacts(self) -> None:
        """Declaring typed fields does not close the envelope: a key the SDK does not name still
        parses and rides `model_extra`, so the platform can add an artifact without a client release.
        """
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {},
                "pipe_io_contracts": _PIPE_IO_CONTRACTS,
                "some_future_artifact": {"k": "v"},
            }
        )

        assert results.model_extra == {"some_future_artifact": {"k": "v"}}

    @pytest.mark.parametrize(
        ("graph_spec", "graph_assembly_error"),
        [
            pytest.param(None, None, id="no-graph-or-not-written"),
            pytest.param(None, _GRAPH_ASSEMBLY_ERROR, id="assembly-broke"),
            pytest.param(_GRAPH_SPEC, None, id="assembled"),
        ],
    )
    def test_run_results_keeps_the_graph_null_semantics_distinct(self, graph_spec: dict[str, Any] | None, graph_assembly_error: str | None) -> None:
        """A run with no graph and a run whose assembly broke both carry a null `graph_spec`;
        `graph_assembly_error` is the only field that tells them apart, as `usage_assembly_error`
        does for the usage pair.
        """
        results = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {},
                "graph_spec": graph_spec,
                "graph_assembly_error": graph_assembly_error,
            }
        )

        assert results.graph_spec == graph_spec
        assert results.graph_assembly_error == graph_assembly_error

    def test_run_results_keeps_the_artifact_null_semantics_distinct(self) -> None:
        """Three null artifacts alone cannot say whether the run described no data or the build
        broke; `pipe_io_artifacts_error` is the only field that tells the two apart.
        """
        described_nothing = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {},
                "pipe_io_contracts": None,
                "input_form": None,
                "output_form": None,
                "pipe_io_artifacts_error": None,
            }
        )
        build_broke = RunResults.model_validate(
            {
                "pipeline_run_id": "run_1",
                "main_stuff": {},
                "pipe_io_contracts": None,
                "input_form": None,
                "output_form": None,
                "pipe_io_artifacts_error": _PIPE_IO_ARTIFACTS_ERROR,
            }
        )

        assert described_nothing.pipe_io_contracts is None
        assert described_nothing.pipe_io_artifacts_error is None
        assert build_broke.pipe_io_contracts is None
        assert build_broke.input_form is None
        assert build_broke.output_form is None
        assert build_broke.pipe_io_artifacts_error == _PIPE_IO_ARTIFACTS_ERROR
