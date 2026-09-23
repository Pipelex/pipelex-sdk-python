"""`summarize_usage` — one run-level reading of a run's usage pair.

A completed run reports usage as `RunResults.tokens_usages` (one record per inference call)
beside `RunResults.usage_assembly_error`, and the wire carries no run-level aggregate. This
module folds the pair into a single summary under the rules `docs/run-usage.md` states, so
every consumer reads the same totals instead of re-deriving them:

- a `None` cost is unrated (the model has no rate table), a `0` cost is priced at zero;
- `input` and `output` are the only additive token categories (`input_cached` is a subset of
  `input`, and summing every category double-counts);
- a `None` list means usage is unavailable, and only `usage_assembly_error` says it broke;
- an empty list is a run that did no inference, which costs `0`, not `None`.

Pure: no I/O, no client, and the input is never mutated. It is the Python twin of
`@pipelex/sdk`'s `summarizeUsage` and reports the same summary shape, with two deliberate
divergences. Where the JS reads an absent `tokens_usages` as `unavailable`, this raises
`FieldNotIncludedError`, because Python tells an absent key from a relayed `null` through
`model_fields_set` and answering "nothing is known" for a key the read never carried would turn
a gap in the read into a fact about the run. And where the JS guards every read with `typeof`,
because its records are relayed JSON nothing validated, this reads the parsed fields directly:
`cost` is validated strict and finite at the client boundary, so a record whose `cost` is not a
number never reaches the fold — it fails the whole results body's parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from pipelex_sdk.errors import FieldNotIncludedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pipelex_sdk.runs import RunResults, TokensUsageRecord

#: The one `RunResults` field the fold cannot do without, named once so the membership test in
#: `model_fields_set` and the error that reports its absence can never drift apart.
_USAGE_RECORDS_FIELD = "tokens_usages"


class UsageSummaryState(StrEnum):
    """Which of the three readings of `tokens_usages` the summary describes."""

    #: A non-empty list: the totals are folded from it.
    RECORDS = "records"
    #: `[]`: usage assembly ran and no inference happened, so the cost is `0` and the token
    #: totals are zero.
    NO_INFERENCE = "no_inference"
    #: The list was relayed as `None`: usage assembly was off, it broke (then `assembly_error`
    #: is non-`None`), or on the hosted path the run was delivered before the usage artifact
    #: existed. Nothing is known, so the cost and the token totals are `None`.
    UNAVAILABLE = "unavailable"


class UsageTokenTotals(BaseModel):
    """The two additive token totals.

    Each is the sum of that category over the records that reported it, and `None` when no
    record reported it at all — which is different from a reported `0`.
    """

    model_config = ConfigDict(extra="forbid")

    input: int | None
    output: int | None


class PipeUsageSummary(BaseModel):
    """One pipe's share of a run's usage — the same fold as the run level, over its calls only."""

    model_config = ConfigDict(extra="forbid")

    #: The pipe that made the calls; `None` groups the calls the runtime did not attribute.
    pipe_code: str | None
    #: Sum of the priced calls' costs in USD; `None` when none of this pipe's calls was priced.
    total_cost_usd: float | None
    #: True when this pipe mixes priced and unrated calls, so `total_cost_usd` is a lower bound.
    cost_partial: bool
    tokens: UsageTokenTotals
    #: Number of inference calls this pipe made.
    calls: int


class UsageSummary(BaseModel):
    """A run's usage pair folded into one null-aware reading."""

    model_config = ConfigDict(extra="forbid")

    state: UsageSummaryState
    #: Sum of the priced calls' costs in USD. `0` for `no_inference`. `None` for `records` when
    #: no call was priced (unrated), and for `unavailable`, where nothing is known — read
    #: `state` first to tell the two apart.
    total_cost_usd: float | None
    #: True when priced and unrated calls are mixed, so `total_cost_usd` covers the priced calls
    #: only and is a lower bound. Never true outside `records`.
    cost_partial: bool
    tokens: UsageTokenTotals
    #: Number of usage records summarized — one per inference call; `0` outside `records`.
    calls: int
    #: `usage_assembly_error` as relayed: non-`None` when the runner's usage assembly failed,
    #: which is the only thing that separates a broken assembly from an `unavailable` one that
    #: was off.
    assembly_error: str | None
    #: Per-pipe rollup, most expensive first: priced pipes by cost descending, then unrated
    #: pipes, with ties broken by call count (descending) and then by pipe code. Empty outside
    #: `records`.
    by_pipe: list[PipeUsageSummary]


@dataclass(frozen=True)
class _UsageFold:
    """The null-aware part of the summary a set of records folds to, at any level."""

    total_cost_usd: float | None
    cost_partial: bool
    tokens: UsageTokenTotals


def summarize_usage(results: RunResults) -> UsageSummary:
    """Fold a run's usage pair into one summary: run totals, the call count, the assembly error
    and a per-pipe rollup. See `docs/run-usage.md` for the rules it applies.

    Raises `FieldNotIncludedError` when the results body never carried `tokens_usages` — the key
    is absent from `results.model_fields_set` — because a read that did not deliver the key says
    nothing about the run, and is not a run with no usage to report. A key relayed as `None` IS a
    value and reads as `unavailable`.

    Pre-contract records, relayed verbatim from artifacts written before the usage contract,
    carry no `cost` and no `pipe_code`: they count as unrated and unattributed. The legacy
    `job_metadata` / `unit_costs` fields are relics and are never read.
    """
    if _USAGE_RECORDS_FIELD not in results.model_fields_set:
        raise FieldNotIncludedError(_USAGE_RECORDS_FIELD)

    records = results.tokens_usages
    assembly_error = results.usage_assembly_error

    if records is None:
        return UsageSummary(
            state=UsageSummaryState.UNAVAILABLE,
            total_cost_usd=None,
            cost_partial=False,
            tokens=UsageTokenTotals(input=None, output=None),
            calls=0,
            assembly_error=assembly_error,
            by_pipe=[],
        )

    if not records:
        return UsageSummary(
            state=UsageSummaryState.NO_INFERENCE,
            total_cost_usd=0.0,
            cost_partial=False,
            tokens=UsageTokenTotals(input=0, output=0),
            calls=0,
            assembly_error=assembly_error,
            by_pipe=[],
        )

    run_fold = _fold_records(records)
    return UsageSummary(
        state=UsageSummaryState.RECORDS,
        total_cost_usd=run_fold.total_cost_usd,
        cost_partial=run_fold.cost_partial,
        tokens=run_fold.tokens,
        calls=len(records),
        assembly_error=assembly_error,
        by_pipe=_roll_up_by_pipe(records),
    )


def _fold_records(records: Sequence[TokensUsageRecord]) -> _UsageFold:
    """Null-aware totals over a non-empty set of records."""
    priced_sum = 0.0
    any_priced = False
    any_unrated = False
    input_sum: int | None = None
    output_sum: int | None = None

    for record in records:
        if record.cost is None:
            any_unrated = True
        else:
            priced_sum += record.cost
            any_priced = True

        by_category = record.nb_tokens_by_category
        if by_category is not None:
            # Only the two joined totals are additive; every other category is a subset of one.
            input_count = by_category.get("input")
            if input_count is not None:
                input_sum = (input_sum or 0) + input_count
            output_count = by_category.get("output")
            if output_count is not None:
                output_sum = (output_sum or 0) + output_count

    total_cost_usd: float | None = None
    if any_priced:
        total_cost_usd = priced_sum

    return _UsageFold(
        total_cost_usd=total_cost_usd,
        cost_partial=any_priced and any_unrated,
        tokens=UsageTokenTotals(input=input_sum, output=output_sum),
    )


def _roll_up_by_pipe(records: Sequence[TokensUsageRecord]) -> list[PipeUsageSummary]:
    """Group the records by `pipe_code` (a `None` key gathers the unattributed calls) and sort."""
    groups: dict[str | None, list[TokensUsageRecord]] = {}
    for record in records:
        groups.setdefault(record.pipe_code, []).append(record)

    rows: list[PipeUsageSummary] = []
    for pipe_code, pipe_records in groups.items():
        pipe_fold = _fold_records(pipe_records)
        rows.append(
            PipeUsageSummary(
                pipe_code=pipe_code,
                total_cost_usd=pipe_fold.total_cost_usd,
                cost_partial=pipe_fold.cost_partial,
                tokens=pipe_fold.tokens,
                calls=len(pipe_records),
            ),
        )

    rows.sort(key=_pipe_row_sort_key)
    return rows


def _pipe_row_sort_key(row: PipeUsageSummary) -> tuple[bool, float, int, bool, str]:
    """Cost descending with unrated pipes last, then calls descending, then pipe code (`None` last).

    The cost and pipe-code slots are constant within the group the flag beside them selects, so
    the placeholder each takes for a `None` never decides an order.
    """
    return (
        row.total_cost_usd is None,
        -(row.total_cost_usd or 0.0),
        -row.calls,
        row.pipe_code is None,
        row.pipe_code or "",
    )
