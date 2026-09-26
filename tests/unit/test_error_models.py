"""Tests for pipelex_sdk.error_models — the runner's error report, typed whole and open to what it adds."""

from typing import Any

from pipelex_sdk.error_models import RunErrorReport

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

    def test_an_empty_report_parses(self) -> None:
        report = RunErrorReport.model_validate({})

        assert report.model_dump(exclude_none=True) == {}
