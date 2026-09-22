# Run usage — reading what a run consumed

A completed run reports what its inference calls consumed as a list of `TokensUsageRecord` objects on `RunResults`, one per inference call, in the order the calls completed. This page covers how to read them, what each field means, the edge cases the model is deliberately shaped around, and `summarize_usage`, which folds them into one run-level summary under those rules.

The wire shape is not this SDK's invention. Inference accounting is a Pipelex runtime extension — the MTHDS Protocol does not model it, and says nothing about usage reporting — so the hosted API is what pins the contract, and `pipelex_sdk.runs.TokensUsageRecord` is a client-side mirror of the runtime's own record. `@pipelex/sdk` carries the same mirror in TypeScript.

## Reading the records

```python
result = await client.start_and_wait(pipe_code="my_domain.summarize", inputs={"text": "..."})

if result.tokens_usages is not None:
    for record in result.tokens_usages:
        print(record.pipe_code, record.inference_model_name, record.nb_tokens_by_category, record.cost)
```

For the run's totals, do not add the records up by hand: [`summarize_usage`](#summarizing-a-run--summarize_usage) does it under the rules below.

The accessor is the same whichever path ran. `start_and_wait` picks a path from the `GET /v1/version` handshake:

- **Hosted (durable) path** — the records come from the runner's `tokens_usages.json` artifact, which `GET /v1/runs/{id}/results` unpacks onto the results body as top-level keys and relays verbatim.
- **Bare runner (blocking) path** — the records ride the execute response's extension-open `pipe_output` as Pipelex extension fields; the SDK lifts them onto the same two top-level fields.

Because the runtime emits both surfaces through one helper, the two cannot structurally diverge.

## Field reference

| field | type | meaning |
|---|---|---|
| `model_type` | `str \| None` | Kind of inference. Known values: `llm`, `img_gen`, `extract`, `search`. |
| `inference_model_name` | `str \| None` | Human model name (e.g. `gpt-4o`). |
| `inference_model_id` | `str \| None` | Provider/platform model id (e.g. `gpt-4o-2024-11-20`). |
| `pipe_code` | `str \| None` | The pipe that made the call — what makes per-pipe cost attribution possible. |
| `job_category` | `str \| None` | Known values: `llm_job`, `img_gen_job`, `extract_job`, `search_job`, `jinja2_job`, `mock_job`. |
| `unit_job_id` | `str \| None` | Known values: `llm_gen_text`, `llm_gen_object`, `img_gen_text_to_image`, `extract_pages`, `search_sourced_answer`, `search_structured`. |
| `nb_tokens_by_category` | `dict[str, int] \| None` | Raw provider-reported token counts, keyed by token category (`input`, `input_cached`, `output`, `output_reasoning`, …). |
| `cost` | `float \| None` | Computed USD cost of this call. |
| `started_at` | `str \| None` | ISO 8601. |
| `completed_at` | `str \| None` | ISO 8601. |

Two traps worth naming explicitly:

- **Token categories are not additive.** `input` is the joined total and `input_cached` is a *subset* of it. Summing every category double-counts the cached tokens.
- **Duration is not shipped.** Derive it from the `started_at` / `completed_at` pair.

### Enum-ish fields are open sets

`model_type`, `job_category`, `unit_job_id`, and the `nb_tokens_by_category` keys are plain strings, never frozen enums, and the values listed above are *known* values rather than an exhaustive set. This is deliberate: the runtime can add an inference kind without breaking any SDK consumer. Match on them defensively — do not assume the list is closed.

## Cost semantics

`cost` is a server-computed USD total for that one call. The rate table behind it is not a contract field and does not cross the wire, so there is nothing to recompute client-side and no risk of a client's arithmetic disagreeing with the runtime's own reporting — the figure comes from the same cost engine that produces the local CLI cost table. (Pre-contract artifacts are the one exception: they carry a raw `unit_costs` table, which is a relic rather than an API — see [Old artifacts parse too](#old-artifacts-parse-too).)

- `cost is None` means the model has **no rate table at all** — an own-GPU model, a mock run, a dry run.
- `cost == 0` means a rate table existed and priced the call at zero.
- `cost` is strict and finite on the wire: a value relayed as a string, a bool or a NaN fails the whole results body's parse, so a number that reaches a sum is a number the artifact carried.

Those are different facts; `record.cost or 0.0` conflates them, which is fine for a sum but wrong for "was this call priced?".

There is no per-category cost breakdown and no run-level aggregate on the wire. The run total is the sum of the records, and [`summarize_usage`](#summarizing-a-run--summarize_usage) computes it with the `None` / `0` distinction kept.

## Null and empty semantics

`tokens_usages` is `None` whenever usage assembly produced no list at all, which happens for three different reasons:

- usage assembly was **off** for the run;
- usage assembly **broke** (an event-read failure);
- on the hosted path, the run was **delivered before the artifact existed**.

It is `[]` when assembly ran, succeeded, and no inference happened, and non-empty otherwise. An empty list is a run that did no inference, so the run's total cost is `0` and its token totals are zero, never `None`: `None` stays reserved for calls that were not rated.

`usage_assembly_error` is the **only** field that distinguishes the broken case from the other two — they are otherwise indistinguishable on the wire. A caller that needs to tell "we have no usage data because something failed" from "there was nothing to report" must branch on `usage_assembly_error`, not on `tokens_usages` alone:

```python
if result.usage_assembly_error is not None:
    log.warning("usage assembly failed for this run: %s", result.usage_assembly_error)
elif result.tokens_usages is None:
    ...  # usage was off, or this run predates the artifact
elif not result.tokens_usages:
    ...  # ran, but no inference happened
```

## Summarizing a run — `summarize_usage`

`summarize_usage(results)` folds a run's usage pair into one run-level reading and a per-pipe rollup, applying every rule on this page so that no consumer has to re-derive them. It is pure: it does no I/O, needs no client, and leaves its input untouched. It takes the whole `RunResults` a completed run came back with.

```python
from pipelex_sdk.usage import UsageSummaryState, summarize_usage

usage = summarize_usage(result)

match usage.state:
    case UsageSummaryState.RECORDS:
        cost = "not rated" if usage.total_cost_usd is None else f"${usage.total_cost_usd:.4f}"
        partial = " (partial)" if usage.cost_partial else ""
        print(f"{usage.calls} calls, {cost}{partial}")
        for row in usage.by_pipe:
            print(row.pipe_code or "(unattributed)", row.total_cost_usd, row.calls)
    case UsageSummaryState.NO_INFERENCE:
        print("no inference happened: $0")
    case UsageSummaryState.UNAVAILABLE:
        print(usage.assembly_error or "usage was not reported for this run")
```

| field | type | meaning |
|---|---|---|
| `state` | `UsageSummaryState` | Which reading of `tokens_usages` the summary describes. Read it first. |
| `total_cost_usd` | `float \| None` | Sum of the priced calls' costs, in USD. |
| `cost_partial` | `bool` | True when priced and unrated calls are mixed, so the total covers the priced calls only and is a lower bound. |
| `tokens` | `UsageTokenTotals` | The two additive token totals, `input` and `output`, each summed over the records that reported it. A category no record reported is `None`, which is different from a reported `0`. |
| `calls` | `int` | Number of records summarized, one per inference call. |
| `assembly_error` | `str \| None` | `usage_assembly_error` as relayed. |
| `by_pipe` | `list[PipeUsageSummary]` | One row per `pipe_code`, each carrying `pipe_code`, `total_cost_usd`, `cost_partial`, `tokens` and `calls`, folded over that pipe's calls exactly as the run level is. |

The three states follow the [null and empty semantics](#null-and-empty-semantics) above, and `UsageSummaryState` is a `StrEnum`, so a state also compares and prints as its wire string:

| `state` | `tokens_usages` | `total_cost_usd` | `tokens` | `calls` | `by_pipe` |
|---|---|---|---|---|---|
| `records` | a non-empty list | the priced sum, or `None` when no call was priced | the summed totals | the record count | one row per pipe |
| `no_inference` | `[]` | `0` | `input` and `output` both `0` | `0` | `[]` |
| `unavailable` | `None` | `None` | `input` and `output` both `None` | `0` | `[]` |

A `None` `total_cost_usd` therefore means one of two things, and `state` tells them apart: under `records` no call was rated, and under `unavailable` nothing is known. Within `unavailable`, `assembly_error` is still the only sign that usage assembly broke rather than being off or not yet written.

`by_pipe` puts the most expensive pipe first. Priced pipes come by cost, descending, and unrated pipes after every priced one; a tie breaks on the call count, descending, and then on the pipe code. The calls the runtime did not attribute to a pipe (`pipe_code` `None`) form one group of their own, which sorts after the named pipes when everything else ties.

A [pre-contract record](#old-artifacts-parse-too) carries no `cost` and no `pipe_code`, so it counts as unrated and unattributed. Its legacy `job_metadata` and `unit_costs` are never read, so an old artifact shows up as a partial or `None` total rather than as a figure the SDK guessed.

### A key that was never carried is not a `None` list

A results body that never carried `tokens_usages` at all raises `FieldNotIncludedError` rather than answering `unavailable`. That is the Python reading of the difference [`run-results.md`](./run-results.md) sets out: a key the platform relayed as `null` is in `results.model_fields_set` and is a value, while a key the body did not carry is absent from the set and says nothing about the run. Answering "nothing is known about this run's usage" for a key the read never delivered would turn a gap in the read into a fact about the run. The error carries the field's name in `field_name`. Today it is dormant: the blocking path lifts the pair off the runner's output and sets it explicitly, and the hosted body relays the key on every read, so the only body without it comes from a runner that relays no usage at all. It becomes reachable the day a results read can leave a key out, which is the include selector's to add, and it then names what the read left out. The TypeScript twin, which cannot tell the two apart, reads both as `unavailable`.

`usage_assembly_error` is not guarded the same way: both paths relay it beside the list, so its absence carries no such ambiguity and reads as `None`.

## Old artifacts parse too

Durable artifacts written before this contract shipped are relayed verbatim and never migrated. `TokensUsageRecord` therefore keeps **every field optional** and is extension-open (`extra="allow"`) — a pre-contract record parses without raising:

- `cost` comes back `None` (it did not exist yet — the record carried a raw `unit_costs` rate table instead);
- `pipe_code` comes back `None` (it was still nested inside a `job_metadata` object rather than flattened onto the record);
- the legacy `job_metadata` and `unit_costs` survive in `model_extra`.

Those legacy fields are **not** contract fields. They exist on old records only, and reading them is reading a relic — a record the current runtime emits never carries them. Treat their presence as a signal that you are looking at an old artifact, not as an API.

Conversely, a record the current runtime emits always carries the **full key set**: a field with no value is an explicit `null`, never an omitted key. You can read any field without an existence check.

## What is deliberately absent

The runtime's internal reporting models carry execution plumbing — `job_metadata`, `otel_context`, `trace_context`, `session_id`, `request_id`, `user_id`, `pipe_run_id`, `content_generation_job_id` — that is dropped at the boundary: on a record emitted under this contract, finding one of these is reading a leak, not a contract field. This is enforced upstream by leak-regression tests in `pipelex` and a conformance leak guard that walks relayed records at any nesting depth. Pre-contract artifacts are the documented exemption — relayed verbatim, they legitimately still carry `job_metadata` and `unit_costs`, and the leak guard does not run on them.

One consequence worth knowing: the record shape is **invariant** with respect to server-side telemetry and tracing settings, because the only fields that varied with them are precisely the ones the boundary drops. You never get a structurally different record because an operator changed an observability setting.
