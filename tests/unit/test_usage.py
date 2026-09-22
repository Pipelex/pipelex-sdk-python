"""Tests for pipelex_sdk.usage — the null-aware fold of a run's usage pair into one summary."""

from typing import Any

import pytest

from pipelex_sdk.errors import FieldNotIncludedError
from pipelex_sdk.runs import RunResults
from pipelex_sdk.usage import UsageSummaryState, summarize_usage

# A record in the shape the current runtime emits: the full contract key set, a value the runtime
# has none of sent as an explicit null. Overridden per case by `_record`.
_FULL_RECORD: dict[str, Any] = {
    "model_type": "llm",
    "inference_model_name": "test-model",
    "inference_model_id": "test-model-2026-01-01",
    "pipe_code": "test_domain.summarize",
    "job_category": "llm_job",
    "unit_job_id": "llm_gen_text",
    "nb_tokens_by_category": {"input": 100, "output": 20},
    "cost": 0.01,
    "started_at": "2026-09-22T10:00:01+00:00",
    "completed_at": "2026-09-22T10:00:03+00:00",
}

# A durable artifact written BEFORE the usage contract shipped, relayed verbatim ever since: no
# computed `cost`, no flattened `pipe_code`, and the legacy relics riding `model_extra`.
_PRE_CONTRACT_RECORD: dict[str, Any] = {
    "model_type": "llm",
    "inference_model_name": "legacy-model",
    "nb_tokens_by_category": {"input": 40, "output": 8},
    "unit_costs": {"input": 3.0, "output": 15.0},
    "job_metadata": {"pipe_code": "legacy_domain.summarize", "job_category": "llm_job"},
}


def _record(**overrides: Any) -> dict[str, Any]:
    """One wire record, the full key set with the case's own values written over it."""
    return {**_FULL_RECORD, **overrides}


def _results(**body: Any) -> RunResults:
    """A completed run's results body, validated from the wire so `model_fields_set` is truthful."""
    return RunResults.model_validate({"pipeline_run_id": "run_1", "main_stuff": {"text": "out"}, **body})


class TestSummarizeUsage:
    def test_a_body_that_did_not_carry_the_key_raises(self) -> None:
        results = _results()

        with pytest.raises(FieldNotIncludedError) as exc_info:
            summarize_usage(results)

        assert exc_info.value.field_name == "tokens_usages"
        assert "tokens_usages" in str(exc_info.value)

    def test_a_key_relayed_as_null_is_a_value_and_does_not_raise(self) -> None:
        summary = summarize_usage(_results(tokens_usages=None, usage_assembly_error=None))

        assert summary.state == UsageSummaryState.UNAVAILABLE
        assert summary.total_cost_usd is None

    @pytest.mark.parametrize(
        ("tokens_usages", "expected_state", "expected_cost", "expected_input", "expected_output", "expected_calls"),
        [
            pytest.param(None, UsageSummaryState.UNAVAILABLE, None, None, None, 0, id="unavailable"),
            pytest.param([], UsageSummaryState.NO_INFERENCE, 0.0, 0, 0, 0, id="no_inference"),
            pytest.param([_FULL_RECORD], UsageSummaryState.RECORDS, 0.01, 100, 20, 1, id="records"),
        ],
    )
    def test_each_state_reports_its_own_totals(
        self,
        tokens_usages: list[dict[str, Any]] | None,
        expected_state: UsageSummaryState,
        expected_cost: float | None,
        expected_input: int | None,
        expected_output: int | None,
        expected_calls: int,
    ) -> None:
        summary = summarize_usage(_results(tokens_usages=tokens_usages, usage_assembly_error=None))

        assert summary.state == expected_state
        assert summary.total_cost_usd == expected_cost
        assert summary.cost_partial is False
        assert summary.tokens.input == expected_input
        assert summary.tokens.output == expected_output
        assert summary.calls == expected_calls
        assert summary.assembly_error is None

    def test_an_empty_list_is_a_run_that_did_no_inference_not_an_unrated_one(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[], usage_assembly_error=None))

        assert summary.state == UsageSummaryState.NO_INFERENCE
        assert summary.total_cost_usd == 0.0
        assert summary.cost_partial is False
        assert summary.tokens.input == 0
        assert summary.tokens.output == 0
        assert summary.by_pipe == []

    def test_an_unavailable_run_knows_nothing_at_all(self) -> None:
        summary = summarize_usage(_results(tokens_usages=None, usage_assembly_error=None))

        assert summary.total_cost_usd is None
        assert summary.cost_partial is False
        assert summary.tokens.input is None
        assert summary.tokens.output is None
        assert summary.calls == 0
        assert summary.by_pipe == []

    def test_it_sums_the_priced_calls(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[_record(cost=0.25), _record(cost=0.5)], usage_assembly_error=None))

        assert summary.state == UsageSummaryState.RECORDS
        assert summary.total_cost_usd == 0.75
        assert summary.cost_partial is False
        assert summary.calls == 2

    def test_a_zero_cost_stays_priced_rather_than_unrated(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[_record(cost=0), _record(cost=0)], usage_assembly_error=None))

        assert summary.total_cost_usd == 0.0
        assert summary.cost_partial is False

    def test_a_run_whose_every_call_is_unrated_reports_no_total(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[_record(cost=None), _record(cost=None)], usage_assembly_error=None))

        assert summary.state == UsageSummaryState.RECORDS
        assert summary.total_cost_usd is None
        assert summary.cost_partial is False
        assert summary.calls == 2

    def test_mixing_priced_and_unrated_calls_flags_the_total_as_partial(self) -> None:
        summary = summarize_usage(
            _results(tokens_usages=[_record(cost=0.5), _record(cost=None), _record(cost=0)], usage_assembly_error=None),
        )

        assert summary.total_cost_usd == 0.5
        assert summary.cost_partial is True

    def test_only_the_joined_input_and_output_categories_are_summed(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[
                    _record(nb_tokens_by_category={"input": 1000, "input_cached": 800, "output": 50, "output_reasoning": 30}),
                    _record(nb_tokens_by_category={"input": 200, "output": 10, "some_future_category": 7}),
                ],
                usage_assembly_error=None,
            ),
        )

        assert summary.tokens.input == 1200
        assert summary.tokens.output == 60

    def test_a_category_no_record_reported_totals_to_none(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[_record(nb_tokens_by_category={"output": 12}), _record(nb_tokens_by_category=None)],
                usage_assembly_error=None,
            ),
        )

        assert summary.tokens.input is None
        assert summary.tokens.output == 12

    def test_a_reported_zero_stays_apart_from_an_unreported_count(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[_record(nb_tokens_by_category={"input": 0, "output": 0})], usage_assembly_error=None))

        assert summary.tokens.input == 0
        assert summary.tokens.output == 0

    @pytest.mark.parametrize("tokens_usages", [None, [], [_FULL_RECORD]], ids=["unavailable", "no_inference", "records"])
    def test_the_assembly_error_is_relayed_verbatim_in_every_state(self, tokens_usages: list[dict[str, Any]] | None) -> None:
        summary = summarize_usage(_results(tokens_usages=tokens_usages, usage_assembly_error="failed to read pipeline events"))

        assert summary.assembly_error == "failed to read pipeline events"

    def test_an_absent_assembly_error_reads_as_none(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[]))

        assert summary.assembly_error is None
        assert summary.state == UsageSummaryState.NO_INFERENCE

    def test_it_groups_calls_per_pipe_and_gathers_the_unattributed_ones_under_none(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[
                    _record(pipe_code="extract", cost=0.1, nb_tokens_by_category={"input": 10, "output": 1}),
                    _record(pipe_code=None, cost=0.2, nb_tokens_by_category={"input": 20, "output": 2}),
                    _record(pipe_code="extract", cost=0.3, nb_tokens_by_category={"input": 30, "output": 3}),
                    _record(pipe_code=None, cost=None, nb_tokens_by_category=None),
                ],
                usage_assembly_error=None,
            ),
        )

        extract_row, unattributed_row = summary.by_pipe
        assert extract_row.pipe_code == "extract"
        assert extract_row.total_cost_usd == 0.1 + 0.3
        assert extract_row.cost_partial is False
        assert extract_row.tokens.input == 40
        assert extract_row.tokens.output == 4
        assert extract_row.calls == 2
        assert unattributed_row.pipe_code is None
        assert unattributed_row.total_cost_usd == 0.2
        assert unattributed_row.cost_partial is True
        assert unattributed_row.tokens.input == 20
        assert unattributed_row.tokens.output == 2
        assert unattributed_row.calls == 2

    def test_it_sorts_by_cost_descending_with_unrated_pipes_after_every_priced_one(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[
                    _record(pipe_code="unrated", cost=None),
                    _record(pipe_code="cheap", cost=0.01),
                    _record(pipe_code="free", cost=0),
                    _record(pipe_code="expensive", cost=2),
                ],
                usage_assembly_error=None,
            ),
        )

        assert [row.pipe_code for row in summary.by_pipe] == ["expensive", "cheap", "free", "unrated"]
        assert [row.total_cost_usd for row in summary.by_pipe] == [2.0, 0.01, 0.0, None]

    def test_it_breaks_a_cost_tie_on_call_count_then_on_pipe_code_with_the_unattributed_group_last(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[
                    _record(pipe_code=None, cost=0.5),
                    _record(pipe_code="beta", cost=0.5),
                    _record(pipe_code="alpha", cost=0.5),
                    _record(pipe_code="busy", cost=0.25),
                    _record(pipe_code="busy", cost=0.25),
                ],
                usage_assembly_error=None,
            ),
        )

        assert [(row.pipe_code, row.calls) for row in summary.by_pipe] == [("busy", 2), ("alpha", 1), ("beta", 1), (None, 1)]

    def test_it_orders_unrated_pipes_among_themselves_by_call_count_then_pipe_code(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[
                    _record(pipe_code="bbb", cost=None),
                    _record(pipe_code="aaa", cost=None),
                    _record(pipe_code="ccc", cost=None),
                    _record(pipe_code="ccc", cost=None),
                ],
                usage_assembly_error=None,
            ),
        )

        assert [row.pipe_code for row in summary.by_pipe] == ["ccc", "aaa", "bbb"]
        assert summary.total_cost_usd is None

    def test_a_pre_contract_record_counts_as_unrated_and_unattributed(self) -> None:
        summary = summarize_usage(
            _results(
                tokens_usages=[_PRE_CONTRACT_RECORD, _record(pipe_code="summarize", cost=0.02)],
                usage_assembly_error=None,
            ),
        )

        assert summary.state == UsageSummaryState.RECORDS
        assert summary.total_cost_usd == 0.02
        assert summary.cost_partial is True
        assert summary.tokens.input == 140
        assert summary.tokens.output == 28
        priced_row, legacy_row = summary.by_pipe
        assert priced_row.pipe_code == "summarize"
        assert priced_row.total_cost_usd == 0.02
        assert priced_row.calls == 1
        assert legacy_row.pipe_code is None
        assert legacy_row.total_cost_usd is None
        assert legacy_row.cost_partial is False
        assert legacy_row.tokens.input == 40
        assert legacy_row.tokens.output == 8

    def test_a_record_carrying_no_field_at_all_is_tolerated(self) -> None:
        summary = summarize_usage(_results(tokens_usages=[{}], usage_assembly_error=None))

        assert summary.state == UsageSummaryState.RECORDS
        assert summary.total_cost_usd is None
        assert summary.cost_partial is False
        assert summary.tokens.input is None
        assert summary.tokens.output is None
        assert summary.calls == 1
        assert len(summary.by_pipe) == 1
        assert summary.by_pipe[0].pipe_code is None
        assert summary.by_pipe[0].calls == 1

    def test_it_takes_a_completed_runs_results_without_mutating_them(self) -> None:
        results = _results(
            tokens_usages=[_record(pipe_code="cheap", cost=0.01), _record(pipe_code="expensive", cost=1)],
            usage_assembly_error=None,
        )
        snapshot = results.model_dump()

        summary = summarize_usage(results)

        assert [row.pipe_code for row in summary.by_pipe] == ["expensive", "cheap"]
        assert results.model_dump() == snapshot
