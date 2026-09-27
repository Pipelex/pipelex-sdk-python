"""Tests for pipelex_sdk.error_models — the runner's error report, typed whole and open to what it adds."""

from typing import Any

from pipelex_sdk.error_models import RunErrorReport
from pipelex_sdk.product_models import RunPage

# A configuration failure carrying the migration block, the one report field the recorded run
# responses do not exercise.
_STALE_CONFIG_REPORT: dict[str, Any] = {
    "error_type": "PipelexConfigError",
    "message": "The configuration under .pipelex/ uses a retired key.",
    "title": "Pipelex config",
    "type_uri": "https://docs.pipelex.com/latest/errors/pipelex-config-error/",
    "error_domain": "config",
    "migration": {
        "remedy": "pipelex-agent migrate",
        "would_write": True,
        "needs_attention": False,
        "plans": [{"path": ".pipelex/pipelex.toml", "operations": [{"kind": "rename_key", "from": "a", "to": "b"}]}],
    },
}


class TestErrorModels:
    def test_the_migration_block_is_typed(self) -> None:
        report = RunErrorReport.model_validate(_STALE_CONFIG_REPORT)

        assert report.migration is not None
        assert report.migration.remedy == "pipelex-agent migrate"
        assert report.migration.would_write is True
        assert report.migration.needs_attention is False
        assert report.migration.plans == _STALE_CONFIG_REPORT["migration"]["plans"]
        assert report.model_dump(exclude_none=True) == _STALE_CONFIG_REPORT

    def test_fields_the_runner_adds_ride_model_extra_at_every_level(self) -> None:
        raw: dict[str, Any] = {
            "error_type": "PipelineExecutionError",
            "message": "Pipe 'summarize' failed",
            "location": "two_steps > summarize",
            "user_action": {"kind": "change_model", "detail": "Pick a served model.", "link": "https://docs.pipelex.com"},
            "provider_metadata": {"provider": "openai", "status_code": 404, "trace": "abc"},
        }
        report = RunErrorReport.model_validate(raw)

        assert report.model_extra == {"location": "two_steps > summarize"}
        assert report.user_action is not None
        assert report.user_action.model_extra == {"link": "https://docs.pipelex.com"}
        assert report.provider_metadata is not None
        assert report.provider_metadata.model_extra == {"trace": "abc"}
        assert report.model_dump(exclude_none=True) == raw

    def test_enum_like_fields_accept_values_this_version_does_not_know(self) -> None:
        raw: dict[str, Any] = {"error_domain": "network", "error_category": "brand_new", "user_action": {"kind": "wait_for_quota"}}
        report = RunErrorReport.model_validate(raw)

        assert report.error_domain == "network"
        assert report.error_category == "brand_new"
        assert report.user_action is not None
        assert report.user_action.kind == "wait_for_quota"

    def test_a_run_list_page_answers_whatever_its_reports_hold(self) -> None:
        """One run whose stored report drifted, or is not a report at all, never fails the page it sits on."""
        page = RunPage.model_validate(
            {
                "items": [
                    {
                        "pipeline_run_id": "run_1",
                        "status": "FAILED",
                        "created_at": "2026-06-10T00:00:00Z",
                        "error": {"message": "boom", "validation_errors": [{"category": "brand_new", "message": "x"}]},
                    },
                    {"pipeline_run_id": "run_2", "status": "FAILED", "created_at": "2026-06-10T00:00:00Z", "error": "not a report"},
                    {"pipeline_run_id": "run_3", "status": "COMPLETED", "created_at": "2026-06-10T00:00:00Z"},
                ],
                "next_cursor": None,
            }
        )

        first, second, third = page.items
        assert first.error is not None
        assert first.error.message == "boom"
        assert first.error.validation_errors is None
        assert second.error is None
        assert third.error is None

    def test_an_empty_report_parses(self) -> None:
        report = RunErrorReport.model_validate({})

        assert report.model_dump(exclude_none=True) == {}
