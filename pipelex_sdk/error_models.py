"""Error-report wire models — a failed run's stored report, and the typed members of a problem document.

**The runner owns these shapes; this SDK follows them.** A run that fails reports why as the runner's
`ErrorReport` (`pipelex.base_exceptions`), which the hosted platform stores whole on the run row as
`error` and serves as it was stored: on the status read (`RunRead.error`), on the run records
(`PipelineRun.error`), and inside the problem document of the results read's `409`, where the
client lifts it onto `RunResultFailed.error` and `RunFailedError.error`. The same classification
fields (`error_domain`, `user_action`, …) ride a runner-rendered problem document as extension
members, which is why `ApiResponseError` types its `user_action` with the model declared here.

Every field is optional and every model is extension-open (`extra="allow"`), for the reason
`TokensUsageRecord` gives: the runner adds fields without asking this SDK, and a field this version
does not name must ride `model_extra` rather than fail the parse of a body whose whole point is to
say what went wrong. The enum-ish fields (`error_domain`, `error_category`, `user_action.kind`) are
open sets on the wire and stay plain `str`, never frozen enums, so a value the runner adds is not an
SDK break; their known values are listed where they are declared.

**A report never fails the read that carries it.** The platform stores the report as the runner
that wrote it sent it and never migrates it, so a row can outlive the runner version it was written
by. Every field of these models is therefore read leniently: a known field whose value does not fit
its type — a validation item with a category this SDK does not know, a `status_code` that is not a
number, a `user_action` that is not an object — reads as `None`, and the rest of the report stands.
`LenientRunErrorReport` extends that to the report as a whole: an `error` that is not an object at
all reads as `None`, so a status read, a run list page or a results read still answers.

**Nothing is stripped.** The platform serves the runner's VERBOSE report, so `message` and
`provider_metadata` can hold the provider's raw text. Deciding what of it a person should see is
each consumer's presentation, not this SDK's; the report arrives here whole.
"""

from __future__ import annotations

from typing import Annotated, Any, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, ValidatorFunctionWrapHandler, WrapValidator, field_validator

from pipelex_sdk.validation_models import ValidationErrorItem


def _none_when_it_does_not_fit(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Validate `value`, or read it as `None` when it does not fit its declared type."""
    try:
        return handler(value)
    except ValidationError:
        return None


class _LenientReportPart(BaseModel):
    """Base of every model here: extension-open, and a known field that does not fit its type reads as `None`."""

    model_config = ConfigDict(extra="allow")

    @field_validator("*", mode="wrap")
    @classmethod
    def _read_a_field_that_does_not_fit_as_none(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        return _none_when_it_does_not_fit(value, handler)


class UserAction(_LenientReportPart):
    """The next step a report advises — the runner's `UserAction`.

    `kind` names the category of advice, so a consumer can render consistent guidance; `detail` is
    the free-form, error-specific text (a billing URL, a retry hint, the model to change).
    """

    #: Known values: `wait_and_retry`, `check_billing`, `check_credentials`, `change_input`,
    #: `change_model`, `contact_support`, `unknown`.
    kind: str | None = None
    detail: str | None = None


class ProviderErrorMetadata(_LenientReportPart):
    """What the inference provider's SDK said about a failed call — the runner's `ProviderErrorMetadata`.

    Present on a report whose failure came back from a model provider. `message` is the provider
    SDK's own text, relayed raw. The provider's response body never crosses the wire: the runner
    excludes it from every serialization.
    """

    provider: str | None = None
    sdk_exception_type: str | None = None
    message: str | None = None
    #: The provider's HTTP status, when it answered one.
    status_code: int | None = None
    #: The provider's own request id — what its support desk asks for.
    request_id: str | None = None
    retry_after_seconds: float | None = None
    provider_error_code: str | None = None


class MigrationErrorBlock(_LenientReportPart):
    """A pending configuration migration that explains the failure — the runner's `MigrationErrorBlock`.

    Present only on a configuration failure whose raiser scanned the host's configuration
    directories; a consumer branches on its presence. `plans` is carried opaquely: it is the shape
    `pipelex-agent migrate --dry-run --format json` emits, which no published package declares.
    """

    #: The command that applies whatever can be applied without a decision.
    remedy: str | None = None
    #: Whether running `remedy` would rewrite any file.
    would_write: bool | None = None
    #: Whether something here is a person's to resolve rather than the tool's.
    needs_attention: bool | None = None
    plans: list[dict[str, Any]] | None = None


class RunErrorReport(_LenientReportPart):
    """Why a run failed — the runner's `ErrorReport`, typed with every field it carries.

    The one type for a failed run's report wherever the SDK hands it back: `RunPublic.error` (and so
    `RunRead.error` on the status read), `PipelineRun.error` on the run records, `RunResultFailed.error`
    on the results read's `409`, and `RunFailedError.error` when `wait_for_result`, `start_and_wait`
    or an artifact download raises for a run that ended without a result.

    **Branch on `error_domain`, `type_uri` and `retryable`**, never on the wording of `message`.
    `error_type` is the runner's open-ended exception class name: finer than `error_domain`, useful in
    a support line, but not a closed set to match against.

    A report is `None` where the run has none — a cancelled, terminated or timed-out run, or one the
    platform finalized itself — so the absence of a report says nothing about why.
    """

    #: The runner's exception class name (`LLMCompletionError`, `SandboxProvisioningError`, …) — an
    #: open set, for display and support, not for branching.
    error_type: str | None = None
    #: What went wrong, as the runner wrote it. On the VERBOSE report the platform serves, it can
    #: carry a provider's raw text.
    message: str | None = None
    #: A stable human label for the error class (`LLM completion`).
    title: str | None = None
    #: The stable URI naming the error class — a branch field, and where its documentation lives.
    type_uri: str | None = None
    #: Where the error comes from, the coarse branch field. Known values: `input` (the caller can fix
    #: it), `config` (a configuration change is needed), `runtime` (a failure during execution).
    error_domain: str | None = None
    #: A finer classification of an inference failure, when the runner has one. Known values:
    #: `transient`, `configuration`, `content`, `capacity`, `ambiguous`, `unknown`.
    error_category: str | None = None
    #: Whether retrying the same run can succeed. `None` means unknown, never "no".
    retryable: bool | None = None
    user_action: UserAction | None = None
    #: The model the failing call used, when the failure is an inference failure.
    model: str | None = None
    #: The provider the failing call reached, when the failure is an inference failure.
    provider: str | None = None
    provider_metadata: ProviderErrorMetadata | None = None
    #: True when `message` was written as caller-facing copy. The runner emits it only when true.
    caller_facing_message: bool | None = None
    #: The structured diagnostics of a bundle that failed validation — the same items the validate
    #: report and a `422`'s `ApiResponseError.validation_errors` carry.
    validation_errors: list[ValidationErrorItem] | None = None
    migration: MigrationErrorBlock | None = None


class FieldError(_LenientReportPart):
    """One field-level failure of a request, an item of the platform problem document's `errors[]`.

    `field` is the dotted path to the offending attribute, `code` a stable sub-code
    (`invalid_format`, `out_of_range`, …), `detail` optional human text.
    """

    field: str | None = None
    code: str | None = None
    detail: str | None = None


#: The type of every `error` field that holds a run's report: `RunPublic.error`, `PipelineRun.error`
#: and `RunResultFailed.error`. A report is read field by field as `RunErrorReport` says, and a value
#: that is not a report at all reads as `None`, so the read carrying it always answers.
LenientRunErrorReport: TypeAlias = Annotated[RunErrorReport | None, WrapValidator(_none_when_it_does_not_fit)]
