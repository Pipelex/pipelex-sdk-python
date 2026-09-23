"""Tests for `results_from_execute` — the public lift from a blocking execute result onto `RunResults`.

The blocking fallback's own path is covered in `test_client_run_fallback.py`, through the client. What
this module pins is the function a direct `execute()` caller reaches for: called on its own, with no
client and no network, it must produce the same `RunResults` the durable path hands back, so
`summarize_usage` and the parity fields read the same whichever route ran.
"""

from typing import Any

import pytest

from pipelex_sdk.errors import MissingMainStuffError
from pipelex_sdk.execute_result import PipelexExecuteResult, results_from_execute
from pipelex_sdk.usage import UsageSummaryState, summarize_usage

_TOKENS_USAGES: list[dict[str, Any]] = [
    {
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
]

_PIPE_IO_ARTIFACTS: dict[str, Any] = {
    "pipe_io_contracts": {
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
    },
    "input_form": {"x.greet": {"fields": []}},
    "output_form": {"x.greet": {"field": {"name": "text", "kind": "prose", "required": True}}},
}


def _execute_result(**pipe_output_extension_fields: Any) -> PipelexExecuteResult:
    """A completed blocking response, validated from the wire, with extension fields on `pipe_output`."""
    return PipelexExecuteResult.model_validate(
        {
            "pipeline_run_id": "run-x",
            "main_stuff_name": "result",
            "pipe_output": {
                "pipeline_run_id": "run-x",
                "working_memory": {
                    "root": {"result": {"concept": "native.Text", "content": {"text": "hello"}}},
                    "aliases": {"main_stuff": "result"},
                },
                **pipe_output_extension_fields,
            },
        }
    )


class TestResultsFromExecute:
    def test_lifts_every_extension_field_onto_its_own_field(self) -> None:
        """The usage pair, the graph pair and the `pipe_io_artifacts` envelope all come off `model_extra`."""
        results = results_from_execute(
            _execute_result(
                tokens_usages=_TOKENS_USAGES,
                graph_spec={"meta": {"format": "mthds"}, "nodes": [], "edges": []},
                pipe_io_artifacts=_PIPE_IO_ARTIFACTS,
            )
        )

        assert results.pipeline_run_id == "run-x"
        assert results.main_stuff == {"text": "hello"}
        assert results.graph_spec == {"meta": {"format": "mthds"}, "nodes": [], "edges": []}
        assert results.tokens_usages is not None
        assert results.tokens_usages[0].cost == 0.01
        assert results.pipe_io_contracts is not None
        assert "x.greet" in results.pipe_io_contracts
        assert results.input_form is not None
        assert results.output_form is not None

    def test_carries_the_working_memory_and_the_parsed_pipe_output(self) -> None:
        """`working_memory` is lifted off the runner's output and `pipe_output` is carried over as-is."""
        result = _execute_result()
        results = results_from_execute(result)

        assert results.working_memory is not None
        assert results.working_memory.root["result"].content == {"text": "hello"}
        assert results.pipe_output is result.pipe_output

    def test_every_lifted_field_is_set_even_when_the_runner_carried_none(self) -> None:
        """The blocking path always answers: a field the runner did not carry is `None` AND in the set."""
        results = results_from_execute(_execute_result())

        for field_name in (
            "graph_spec",
            "graph_assembly_error",
            "pipe_io_contracts",
            "input_form",
            "output_form",
            "pipe_io_artifacts_error",
            "tokens_usages",
            "usage_assembly_error",
        ):
            assert getattr(results, field_name) is None
            assert field_name in results.model_fields_set

    def test_a_null_artifacts_envelope_leaves_the_three_none_beside_its_error(self) -> None:
        """A runner that failed to build the artifacts sends a null envelope and says why."""
        results = results_from_execute(_execute_result(pipe_io_artifacts=None, pipe_io_artifacts_error="build broke"))

        assert results.pipe_io_contracts is None
        assert results.input_form is None
        assert results.output_form is None
        assert results.pipe_io_artifacts_error == "build broke"

    def test_the_lifted_results_feed_summarize_usage(self) -> None:
        """The reason the lift is public: a direct `execute()` caller reaches the run-level usage reading."""
        summary = summarize_usage(results_from_execute(_execute_result(tokens_usages=_TOKENS_USAGES)))

        assert summary.state == UsageSummaryState.RECORDS
        assert summary.total_cost_usd == 0.01
        assert summary.calls == 1
        assert summary.tokens.input == 100

    def test_a_run_with_no_locatable_main_stuff_raises(self) -> None:
        """`main_stuff` is resolved through `main_stuff_name`, so an unlocatable one fails here too."""
        result = PipelexExecuteResult.model_validate(
            {
                "pipeline_run_id": "run-x",
                "main_stuff_name": "absent",
                "pipe_output": {"pipeline_run_id": "run-x", "working_memory": {"root": {}, "aliases": {}}},
            }
        )

        with pytest.raises(MissingMainStuffError):
            results_from_execute(result)
