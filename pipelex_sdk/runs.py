"""Run-lifecycle models for the hosted polling surface (`/v1/runs/*`).

Long method runs outlive the hosted gateway's ~30s synchronous cap, so the SDK
submits a run (`POST /v1/start`), then polls a self-healing endpoint by bare
`pipeline_run_id` until the run reaches a terminal state. All state lives behind the id
(DynamoDB + Temporal on the platform), so a caller can drop the poll loop and
resume later with just the id.

Polling is NOT part of the MTHDS Protocol — it is a hosted-API extension. A
bare runner 404s these routes, which the client translates into
`RunLifecycleUnavailableError`.

The lifecycle types **defined here are owned by this SDK** (not imported from
`mthds`): the run lifecycle is a Pipelex-branded hosted surface, mirroring
`pipelex-sdk-js/src/runs.ts`. During the transition (HANDOFF Phase 2) the same
shapes still exist in `mthds-python`; that duplication is deliberate and is
removed from `mthds-python` in Phase 6, leaving these as the single home.

Two things in this module are deliberately NOT owned here, and both reuse rather
than redefine. `RunResults.pipe_output` is typed with the protocol's own
`DictPipeOutputAbstract` wire model from `mthds` — a shared wire contract the
`pipelex` runtime also builds on, not a lifecycle concept. `TokensUsageRecord`
mirrors the runtime's own record: inference accounting is a Pipelex runtime
extension the MTHDS Protocol does not model, so the hosted API is what pins that
wire contract; this SDK follows the shape, it does not define it.

Wire contract mirrors `pipelex-platform`:
    POST /v1/start                           -> RunResultStart   (start, 202)
    GET  /v1/runs/{pipeline_run_id}/status   -> RunRead          (status, self-healing)
    GET  /v1/runs/{pipeline_run_id}/results  -> 202 / 200 / 409  (results)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias

from mthds.protocol.models import RunResultStart
from mthds.runners.api.models import DictPipeOutputAbstract
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Status ──────────────────────────────────────────────────────────


class RunStatus(StrEnum):
    """Hosted run lifecycle status. Mirrors `pipelex_shared.schemas.run.RunStatus`.

    Run states are a hosted-implementation concept — the protocol defines none.
    `STARTED` is deprecated server-side but kept here for historical rows.
    """

    PENDING = "PENDING"
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        """True if the run has reached a terminal state (no further transitions)."""
        match self:
            case RunStatus.COMPLETED | RunStatus.FAILED | RunStatus.CANCELLED | RunStatus.TERMINATED | RunStatus.TIMED_OUT:
                return True
            case RunStatus.PENDING | RunStatus.STARTED | RunStatus.RUNNING:
                return False

    @property
    def is_success(self) -> bool:
        """True only for `COMPLETED`; every other terminal status is a failure."""
        match self:
            case RunStatus.COMPLETED:
                return True
            case (
                RunStatus.PENDING
                | RunStatus.STARTED
                | RunStatus.RUNNING
                | RunStatus.FAILED
                | RunStatus.CANCELLED
                | RunStatus.TERMINATED
                | RunStatus.TIMED_OUT
            ):
                return False


# ── Responses ───────────────────────────────────────────────────────


class MethodProvenance(BaseModel):
    """Provenance of a `method_ref` run — a Pipelex-API extension on the run acks.

    The package's resolved full address, the requested tag (`None` for a bare address,
    which resolves the default branch at HEAD), and the commit SHA that was actually
    fetched — the SHA is what keeps the run explainable when a tag moves. Attached to
    the `POST /v1/start` 202 ack (`PipelexRunResultStart.method_provenance`) and the
    blocking execute response (`PipelexExecuteResult.method_provenance`) for
    `method_ref` runs, absent (or `None`) otherwise. Extension-open (`extra="allow"`)
    like every wire model here, so a future server field is preserved.
    """

    model_config = ConfigDict(extra="allow")

    address: str
    tag: str | None = None
    commit_sha: str


class PipelexRunResultStart(RunResultStart):
    """The `POST /v1/start` 202 ack as the Pipelex API returns it — the protocol's
    `RunResultStart` plus the server's `method_provenance` extension, populated for
    `method_ref` runs and absent (`None`) otherwise. The base is extension-open, so
    any other implementation field still rides `model_extra`.
    """

    method_provenance: MethodProvenance | None = None


class RunPublic(BaseModel):
    """A run record — the BASE shape of the run-lifecycle read surface.

    Only the base fields are declared here. An implementation may return more
    (identity, workflow ids, storage URLs, anything else) — those are
    server-specific response fields, never named in this SDK. The model is
    extension-open (`extra="allow"`): unknown fields are preserved and remain
    accessible as attributes, mirroring the request-side `extra` passthrough.
    """

    model_config = ConfigDict(extra="allow")

    pipeline_run_id: str
    pipe_code: str | None = None
    status: RunStatus
    created_at: str
    finished_at: str | None = None


class RunRead(RunPublic):
    """A run read through the self-healing path (`RunPublic` + `degraded`).

    When `degraded` is true, Temporal was unreachable and `status` is the
    last-known DB value, not a freshly-derived one — pair with
    `retry_after_seconds` (parsed from the `Retry-After` header by the client).
    """

    degraded: bool = False
    retry_after_seconds: int | None = None


class TokensUsageRecord(BaseModel):
    """One inference call's token usage — the client-facing wire record.

    Mirrors the runtime's `TokensUsageRecord`. Inference accounting is a Pipelex runtime
    extension — the MTHDS Protocol does not model it — so the hosted API is what pins this
    wire contract. The same shape rides both surfaces: the durable `tokens_usages.json`
    artifact that the hosted results route relays, and the blocking execute response's
    `pipe_output.tokens_usages`.

    Every field is optional and the model is extension-open **on purpose**. A record the
    current runtime emits always carries the full key set (a field with no value is an
    explicit `null`, never an omitted key), so callers may read any field without an
    existence check. But durable artifacts written before the contract shipped are relayed
    verbatim and never migrated: such a record parses here with `cost` and `pipe_code` unset
    and keeps its legacy `job_metadata` / `unit_costs` in `model_extra`.

    The enum-ish fields are open sets on the wire and stay plain `str` here — never frozen
    enums — so runtime enum churn is non-breaking for consumers.
    """

    model_config = ConfigDict(extra="allow")

    #: Kind of inference. Known values: `llm`, `img_gen`, `extract`, `search`.
    model_type: str | None = None
    #: Human model name (e.g. `gpt-4o`).
    inference_model_name: str | None = None
    #: Provider/platform model id (e.g. `gpt-4o-2024-11-20`).
    inference_model_id: str | None = None
    #: The pipe that made the call — what makes per-pipe cost attribution possible.
    pipe_code: str | None = None
    #: Known values: `llm_job`, `img_gen_job`, `extract_job`, `search_job`, `jinja2_job`, `mock_job`.
    job_category: str | None = None
    #: Known values: `llm_gen_text`, `llm_gen_object`, `img_gen_text_to_image`, `extract_pages`,
    #: `search_sourced_answer`, `search_structured`.
    unit_job_id: str | None = None
    #: Raw provider-reported token counts, keyed by token category (`input`, `input_cached`,
    #: `output`, `output_reasoning`, …). `input` is the joined total and `input_cached` a subset
    #: of it — the categories are NOT additive, so summing them double-counts.
    nb_tokens_by_category: dict[str, int] | None = None
    #: Computed USD cost of this call. `None` when the model has no rate table at all (own-GPU,
    #: mock, dry run); `0` means a rate table existed and priced the call at zero. The underlying
    #: rate table never crosses the wire and there is no run-level aggregate — sum the records.
    cost: float | None = None
    #: ISO 8601 start of the call.
    started_at: str | None = None
    #: ISO 8601 end of the call. Duration is derivable from the pair and deliberately not shipped.
    completed_at: str | None = None


class RunResults(BaseModel):
    """Result artifacts for a completed run — `GET /v1/runs/{pipeline_run_id}/results`.

    `main_stuff` is the resolved main output content and is ALWAYS present for a
    completed run (the pipelex >= 0.37 main-stuff invariant): on the hosted path
    it is the `main_stuff.json` S3 artifact relayed verbatim; on the bare-runner
    blocking path the SDK resolves it from the returned working memory via the
    run's `main_stuff_name`, so both paths deliver the same content shape.
    Consumers read `main_stuff` directly — no shape-guessing. A completed run that
    cannot deliver a main stuff raises `MissingMainStuffError`. Extension-open
    (`extra="allow"`): any other server artifact (e.g. the hosted `working_memory`)
    is preserved without being named by the SDK.
    """

    model_config = ConfigDict(extra="allow")

    pipeline_run_id: str
    #: The resolved main output content — always present for a completed run. Typed `Any` because the
    #: content is polymorphic (a list output renders to a top-level array, a structured output to an
    #: object) and may be a valid falsy value (empty list, `0`); it is never absent for a completed run.
    main_stuff: Any
    #: Method graph spec (`graphspec.json`); `None` if missing mid-write or on the bare-runner path.
    graph_spec: Any = None
    #: Bare runner's native pipe output — the full working memory, blocking-execute path only;
    #: `None` on the hosted path. Supplementary to `main_stuff`, which is already resolved out of
    #: it; kept for consumers that need the whole working memory. Extension-open, so the Pipelex
    #: extension fields the runner rides on it stay reachable via `model_extra` — including the
    #: usage pair, in its **raw** form. Read `tokens_usages` below instead: same data, validated
    #: into records, and present on the hosted path too (where `pipe_output` is `None`).
    pipe_output: DictPipeOutputAbstract | None = None
    #: Per-call usage records — token counts by category, computed `cost` in USD, model id — for
    #: LLM and img-gen/extract/search calls alike. On the hosted path this is the
    #: `tokens_usages.json` artifact's record list relayed verbatim; on the blocking path it is the
    #: execute response's `pipe_output.tokens_usages`. `None` whenever assembly produced no list —
    #: it was off, it broke (see `usage_assembly_error`), or (hosted) the run was delivered before
    #: the artifact existed; `[]` when assembly ran and no inference happened.
    tokens_usages: list[TokensUsageRecord] | None = None
    #: Non-`None` when the runner's usage assembly failed for the run. The ONLY field that
    #: separates "usage broke" from "usage was off" / "pre-artifact run" — all three leave
    #: `tokens_usages` as `None`, so a caller that cares must branch on this, not on the list.
    usage_assembly_error: str | None = None


# ── Single-shot result lookup outcome (discriminated on `state`) ─────


class RunResultRunning(BaseModel):
    """HTTP 202 — the run is in-flight; poll again after `retry_after_seconds`."""

    state: Literal["running"] = "running"
    pipeline_run_id: str
    retry_after_seconds: int | None = None


class RunResultCompleted(BaseModel):
    """HTTP 200 — the run is `COMPLETED`; `result` carries the artifacts."""

    state: Literal["completed"] = "completed"
    pipeline_run_id: str
    result: RunResults


class RunResultFailed(BaseModel):
    """HTTP 409 — the run reached a terminal non-`COMPLETED` status."""

    state: Literal["failed"] = "failed"
    pipeline_run_id: str
    status: RunStatus
    message: str


RunResultState: TypeAlias = Annotated[
    RunResultRunning | RunResultCompleted | RunResultFailed,
    Field(discriminator="state"),
]


# ── Polling options ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PollInfo:
    """Progress info handed to a `WaitForResultOptions.on_poll` callback before each sleep."""

    attempt: int
    elapsed_seconds: float


@dataclass
class WaitForResultOptions:
    """Tuning for `wait_for_result`'s poll loop.

    The client is async-native: cancellation is via `asyncio.CancelledError`
    (the Python analog of mthds-js's `AbortSignal`), so there is no `signal`
    field — cancel the awaiting task instead.
    """

    interval_seconds: float = 2.0
    timeout_seconds: float = 1200.0
    on_poll: Callable[[PollInfo], None] | None = None
