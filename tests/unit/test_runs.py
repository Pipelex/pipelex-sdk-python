"""Tests for pipelex_sdk.runs — run-lifecycle models for the hosted polling surface."""

from typing import Any

import pytest
from pydantic import TypeAdapter

from pipelex_sdk.runs import RunResults, RunStatus, TokensUsageRecord

# A record in the shape the current runtime emits: every contract field present, absent values
# sent as explicit nulls. Mirrors the conformance seed corpus
# (conformance/conformance/usage_records.py), which is what the platform arm asserts on the wire.
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
