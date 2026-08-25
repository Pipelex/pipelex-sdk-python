"""Pipelex-product wire models — the snake_case JSON shapes the hosted-product routes speak.

These mirror `pipelex-sdk-js/src/product-models.ts`. They are the management surface
the hosted product (`/v1/me`, `/v1/methods`, `/v1/organizations`, `/v1/billing/*`,
`/v1/pipelex-api-keys`, `/v1/gateway-api-key`, `/v1/onboarding/submit`,
`/v1/resolve-storage-url`, `/v1/upload`, `/v1/runs`) drives.

The wire is snake_case. Each model holds only the fields the product actually
consumes — not a speculative mirror of every server field. Response models are
extension-open (`extra="allow"`): an unknown server field is preserved, not
rejected — the SDK never has to ship just to read a newly-added field. Input
models name exactly what the routes accept.

These are Pipelex-branded (the hosted product surface), so they live in this SDK,
not in `mthds`. `PipelineRun.status` reuses the run-lifecycle `RunStatus`.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_serializer, field_validator

from pipelex_sdk._pydantic_utils import empty_list_factory_of
from pipelex_sdk.runs import RunStatus

# ── User profile (`/v1/me`) ─────────────────────────────────────────────


class UserProfile(BaseModel):
    """The authenticated user's profile — `GET /v1/me`."""

    model_config = ConfigDict(extra="allow")

    email: str
    user_id: str
    full_name: str
    #: ISO timestamp the user completed onboarding; absent/None until they do.
    onboarding_completed_at: str | None = None


# ── Methods catalog (`/v1/methods`) ──────────────────────────────────────


class MethodDeletionState(StrEnum):
    """Where a method is in the erasure cascade; absent on a normal method."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"


class MethodFile(BaseModel):
    """One named source file of a stored method — the at-rest catalog form.

    This is the shape the hosted platform persists for a method's custom PipeFunc Python:
    a JSON `[{name, content}]` array in one wire string. It is deliberately distinct from
    two neighbours that look similar and are not: `MthdsFile` (`client.py`) is the *validate*
    input, content plus an optional provenance URI; `MthdsFileItem` (`build_models.py`) is
    the *build* closure entry. Three shapes for three surfaces — do not merge them.
    """

    model_config = ConfigDict(extra="allow")

    #: Bundle-relative path, e.g. `"funcs/price.py"`.
    name: str
    #: The file's UTF-8 text content.
    content: str


_METHOD_FILES_ADAPTER: TypeAdapter[list[MethodFile]] = TypeAdapter(list[MethodFile])
"""Built once at import — TypeAdapter construction is expensive."""

_METHOD_FILES_SHAPE = "a JSON array of {name, content} entries"


def _is_blank(content: str) -> bool:
    """A file carries no source when its content is empty or whitespace-only."""
    return not content.strip()


def parse_method_files(source: str | None) -> list[MethodFile]:
    """Parse the catalog wire string into method files.

    A blank source (`None`, `""`, whitespace) and an empty JSON array both yield `[]`.
    A JSON `[{name, content}]` array yields those files, with blank-content entries
    dropped so the round-trip with `serialize_method_files` is stable.

    Raises:
        ValueError: For anything else — a non-array JSON value, an entry that is not a
            `{name: str, content: str}` object, or unparseable text. Reached through
            `MethodData`'s validator, this surfaces as a `pydantic.ValidationError`, the
            same way any other malformed response body fails here.
    """
    if source is None or _is_blank(source):
        return []

    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        msg = f"Method file source is not valid JSON; expected {_METHOD_FILES_SHAPE}."
        raise ValueError(msg) from exc

    try:
        files = _METHOD_FILES_ADAPTER.validate_python(parsed)
    except ValidationError as exc:
        msg = f"Method file source must be {_METHOD_FILES_SHAPE}."
        raise ValueError(msg) from exc

    return [file for file in files if not _is_blank(file.content)]


def serialize_method_files(files: list[MethodFile]) -> str:
    """Serialize method files to the catalog wire string.

    Blank-content entries are dropped (a zero-source file is not persisted), and an empty
    result serializes to `""` — the platform's "no source" / "clear the field" sentinel —
    never to the literal `"[]"`. Only `name` and `content` cross the wire; anything an
    extension-open `MethodFile` picked up on the way in is not written back.
    """
    kept = [file for file in files if not _is_blank(file.content)]
    if not kept:
        return ""
    return json.dumps([{"name": file.name, "content": file.content} for file in kept])


class MethodData(BaseModel):
    """One saved method record."""

    model_config = ConfigDict(extra="allow")

    method_id: str
    name: str
    #: The `.mthds` bundle source.
    mthds: str
    org_id: str
    created_by_user_id: str
    description: str | None = None
    deletion_state: MethodDeletionState | None = None
    input_data: dict[str, Any] | None = None
    #: Legacy persisted output spec; optional.
    pipe_output: dict[str, Any] | None = None
    python: list[MethodFile] = Field(default_factory=empty_list_factory_of(MethodFile))
    """The method's custom PipeFunc source files.

    On the wire this is one string — the JSON text of a `[{name, content}]` array, or `""`
    for a method with no custom Python. The validator below converts at the boundary so
    callers never see that string."""

    created_at: str
    updated_at: str

    @field_validator("python", mode="before")
    @classmethod
    def _parse_python_files(cls, value: object) -> object:
        """Convert the catalog wire string into `MethodFile` entries.

        A `str` or `None` is the wire form and goes through `parse_method_files`; anything
        else (a list, from programmatic construction) passes through to normal validation.
        """
        if value is None or isinstance(value, str):
            return parse_method_files(value)
        return value


class MethodWriteInput(BaseModel):
    """The create/update payload — a rename is a `PUT` with a changed `name`."""

    name: str
    mthds: str
    input_data: dict[str, Any] | None = None
    python: list[MethodFile] | None = None
    """The custom PipeFunc source files to write, with a deliberate three-way contract.

    The write body is dumped with `exclude_none=True`, so `None` (the default) leaves the key
    out entirely and a `PUT` **preserves** the stored Python. An empty list serializes to `""`,
    the platform's clear sentinel, which **erases** it. A non-empty list **replaces** it."""

    @field_serializer("python")
    def _serialize_python_files(self, value: list[MethodFile] | None) -> str | None:
        """Render the file list as the catalog wire string, leaving `None` for `exclude_none`."""
        if value is None:
            return None
        return serialize_method_files(value)


class MethodSummary(BaseModel):
    """One row of the paged method index — `GET /v1/methods`.

    Deliberately **not** a `MethodData`: no `mthds`, no `python`, no `updated_at`, because
    none of them is in the index projection. Putting `mthds` back is exactly what restored
    the truncation bug paging was introduced to fix. A method mid-deletion still appears
    here — so a UI can render "Deleting…" — while `get_method` refuses it with a `409`.
    """

    model_config = ConfigDict(extra="allow")

    method_id: str
    name: str
    description: str | None = None
    created_at: str
    deletion_state: MethodDeletionState | None = None


class MethodPage(BaseModel):
    """One page of the method index — `{items, next_cursor}`.

    The cursor is opaque: pass it straight back as `cursor` to get the next page, and treat
    a `None` as the last page. There is no total by design — counting a catalog costs a full
    scan, and no caller needs one.
    """

    model_config = ConfigDict(extra="allow")

    items: list[MethodSummary]
    next_cursor: str | None = None


class MethodDeletionAccepted(BaseModel):
    """The `202` acceptance of `DELETE /v1/methods/{id}`.

    Returned the moment the erasure is CLAIMED and handed off, not when it completes.
    Nothing in this body means "done": completion is the method's row disappearing from
    `list_methods`. What the body buys a caller is a claim it can log and correlate
    (`deletion_job_id`) plus the state the cascade started in.
    """

    model_config = ConfigDict(extra="allow")

    method_id: str
    deletion_state: MethodDeletionState
    deletion_job_id: str


# ── Organizations (`/v1/organizations`) ──────────────────────────────────


class OrgRole(StrEnum):
    """A member's role within an organization."""

    ADMIN = "admin"
    MEMBER = "member"


class Membership(BaseModel):
    """One organization membership."""

    model_config = ConfigDict(extra="allow")

    org_id: str
    #: None for the implicit personal org (no backing WorkOS organization).
    workos_organization_id: str | None
    name: str
    is_personal: bool
    role_in_org: OrgRole


class MembershipsResponse(BaseModel):
    """The caller's memberships + the active org's feature flags — `GET /v1/organizations/memberships`."""

    model_config = ConfigDict(extra="allow")

    memberships: list[Membership]
    active_org_feature_flags: list[str]


# ── Billing (`/v1/billing/*`) ────────────────────────────────────────────


class SubscriptionResponse(BaseModel):
    """The active org's subscription state — `GET /v1/billing/subscription`."""

    model_config = ConfigDict(extra="allow")

    plan: str | None
    status: str | None
    can_use_service: bool
    renews_at: str | None = None
    ends_at: str | None = None


class PlanView(BaseModel):
    """One available plan (with `is_current`) — `GET /v1/billing/plans`."""

    model_config = ConfigDict(extra="allow")

    slug: str
    name: str
    price_display: str
    monthly_price_cents: int
    period: str
    features: list[str]
    highlight: bool
    is_current: bool


class InvoiceView(BaseModel):
    """One past invoice — `GET /v1/billing/invoices`."""

    model_config = ConfigDict(extra="allow")

    id: str
    created_at: str
    status: str
    amount_cents: int
    currency: str
    card_brand: str | None
    card_last_four: str | None
    refunded: bool
    download_url: str | None


class CheckoutResponse(BaseModel):
    """A Stripe checkout session URL — `POST /v1/billing/checkout`."""

    model_config = ConfigDict(extra="allow")

    checkout_url: str | None = None


class ChangePlanResponse(BaseModel):
    """The outcome of switching plan — `POST /v1/billing/change-plan`."""

    model_config = ConfigDict(extra="allow")

    plan: str | None = None
    status: str | None = None
    charged_immediately: bool | None = None
    resumed: bool | None = None


class BillingPortalResponse(BaseModel):
    """A Stripe billing-portal session URL — `GET /v1/billing/portal`."""

    model_config = ConfigDict(extra="allow")

    portal_url: str | None = None


# ── Pipelex API keys (`/v1/pipelex-api-keys`, `plx_sk_…`) ────────────────


class PipelexApiKey(BaseModel):
    """One Pipelex API key (metadata only — never the plaintext)."""

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    prefix: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None


class PipelexApiKeyCreated(BaseModel):
    """The create/rotate response — the plaintext `api_key` is returned ONCE."""

    model_config = ConfigDict(extra="allow")

    api_key: str
    id: str
    label: str
    prefix: str
    created_at: str


class PipelexApiKeyList(BaseModel):
    """The caller's Pipelex API keys — `GET /v1/pipelex-api-keys`."""

    model_config = ConfigDict(extra="allow")

    keys: list[PipelexApiKey]


# ── Gateway API key (`/v1/gateway-api-key`, Portkey/LLM inference key) ────


class GatewayApiKey(BaseModel):
    """The provisioned gateway (LLM inference) API key — `POST /v1/gateway-api-key`."""

    model_config = ConfigDict(extra="allow")

    gateway_api_key: str
    budget_usd: float | None = None


class GatewayApiKeyStatus(BaseModel):
    """The gateway key status — `GET /v1/gateway-api-key`."""

    model_config = ConfigDict(extra="allow")

    #: None until a gateway key has been provisioned.
    gateway_api_key: str | None


# ── Onboarding (`/v1/onboarding/submit`) ─────────────────────────────────


class OnboardingRole(StrEnum):
    """The respondent's role."""

    DEVELOPER = "developer"
    FOUNDER = "founder"
    DATA_SCIENTIST = "data_scientist"
    RESEARCHER = "researcher"
    OTHER = "other"


class OnboardingCurrentTool(StrEnum):
    """The respondent's current tool."""

    LANGCHAIN = "langchain"
    CREWAI = "crewai"
    LLAMAINDEX = "llamaindex"
    CUSTOM = "custom"
    NONE = "none"
    OTHER = "other"


class OnboardingInputType(StrEnum):
    """A kind of material the respondent works with."""

    DOCUMENTS = "documents"
    IMAGES = "images"
    VIDEOS = "videos"
    AUDIO = "audio"
    STRUCTURED_DATA = "structured_data"
    TEXT = "text"


class OnboardingHeardFrom(StrEnum):
    """Where the respondent heard about Pipelex."""

    TWITTER = "twitter"
    YOUTUBE = "youtube"
    HACKERNEWS = "hackernews"
    DISCORD = "discord"
    FRIEND = "friend"
    GOOGLE = "google"
    CONFERENCE = "conference"
    OTHER = "other"


class OnboardingSubmission(BaseModel):
    """The onboarding questionnaire payload — `POST /v1/onboarding/submit`."""

    role: OnboardingRole
    company: str | None = None
    use_case: str
    process_to_transform: str
    input_types: list[OnboardingInputType]
    material_domain: str
    current_tool: OnboardingCurrentTool
    current_tool_other: str | None = None
    heard_from: OnboardingHeardFrom


# ── Storage (`/v1/resolve-storage-url`, `/v1/upload`) ────────────────────


class ResolvedStorageUrl(BaseModel):
    """A storage URI resolved to a presigned URL — `POST /v1/resolve-storage-url`."""

    model_config = ConfigDict(extra="allow")

    url: str
    expires_at: str
    content_type: str | None


class UploadInput(BaseModel):
    """Upload payload — base64 `data` (the multipart hop is browser→BFF only)."""

    filename: str
    data: str
    content_type: str


class UploadedFile(BaseModel):
    """An uploaded file's storage handle — `POST /v1/upload`."""

    model_config = ConfigDict(extra="allow")

    uri: str
    filename: str


# ── Run records (`/v1/runs`) ─────────────────────────────────────────────
#
# The run-lifecycle status/results/start routes already live on the client
# (`runs.py`); these are the remaining catalog-style paged list, the single-run
# detail read, and the admin-update route.


class PipeStatus(StrEnum):
    """Per-pipe progress marker surfaced in a run's `pipe_statuses` map."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunErrorReport(BaseModel):
    """A failed run's error, narrowed to the two fields a consumer may rely on.

    The runner's own report is considerably more verbose; only these two are contractual.
    """

    model_config = ConfigDict(extra="allow")

    message: str | None = None
    error_type: str | None = None


class PipelineRun(BaseModel):
    """One run record in a method's run list — `GET /v1/runs?method_id=…`."""

    model_config = ConfigDict(extra="allow")

    pipeline_run_id: str
    method_id: str | None = None
    """The stored method this run is linked to, when there is one. An ad-hoc run from an
    inline bundle belongs to no stored method, so the platform serves this as null."""

    pipe_code: str | None = None
    """The pipe that ran, when it was named. A run that let the bundle's `main_pipe` decide
    has none to report, so the platform serves this as null."""

    org_id: str | None = None
    created_by_user_id: str | None = None
    workflow_id: str | None = None
    status: RunStatus
    result_url: str | None = None
    error: RunErrorReport | None = None
    pipe_statuses: dict[str, PipeStatus] | None = None
    created_at: str
    finished_at: str | None = None


class RunDetail(PipelineRun):
    """One run read on its own — `GET /v1/runs/{id}`.

    Adds the two heavy fields the list and the polled status deliberately leave out (their
    cost scales with page size and poll rate respectively). `mthds_contents` is what the run
    actually executed, and the only record of it: a method edited since the run no longer
    describes what happened.
    """

    mthds_contents: list[str] | None = None
    inputs: dict[str, Any] | None = None


class RunPage(BaseModel):
    """One page of a method's run list — `{items, next_cursor}`.

    Same opaque-cursor contract as `MethodPage`: pass `next_cursor` straight back, and a
    `None` means the last page.
    """

    model_config = ConfigDict(extra="allow")

    items: list[PipelineRun]
    next_cursor: str | None = None


class UpdateRunInput(BaseModel):
    """The admin/manual run-status patch — `status` is a free string here."""

    status: str
    result_url: str | None = None
    finished_at: str | None = None
