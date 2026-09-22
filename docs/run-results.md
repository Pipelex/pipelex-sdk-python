# Reading a run's results

A completed run hands back one object, `RunResults` (`pipelex_sdk/runs.py`), and every field of it is described here. The same object comes back from `start_and_wait`, from `wait_for_result(run_id)`, and from the `completed` arm of `get_run_result(run_id)` — one accessor set whichever path ran, which is the point of the type. It is the Python twin of the page of the same name in `@pipelex/sdk`, and the fields are the same; what differs is idiomatic and is called out where it matters, above all how an absent key is read.

Two paths produce it. Against the hosted API the SDK starts a durable run and polls `GET /v1/runs/{id}/results`, where the platform relays the run's S3 artifacts verbatim. Against a bare `pipelex-api` runner, which has no run store, the SDK falls back to the blocking `POST /v1/execute` and maps the runner's native `pipe_output` onto the same shape, lifting the artifacts that ride it onto their own fields. `start_and_wait` picks between the two from the `GET /v1/version` handshake, so a consumer does not choose.

**Calling the blocking route yourself.** `execute()` returns a `PipelexExecuteResult` rather than a `RunResults`, because that model is the runner's whole typed envelope and nothing of it is thrown away. To read such a result through this page's fields, lift it: `results_from_execute(result)` (`pipelex_sdk/execute_result.py`) is the same mapping `start_and_wait` applies on its fallback, exposed for the caller who drives `execute()` directly. It is pure — no client, no network — and what it buys is everything written against `RunResults`: `summarize_usage`, `download_artifacts`, the graph pair and the three I/O artifacts, instead of re-reading `pipe_output.model_extra` by hand.

```python
from pipelex_sdk.execute_result import results_from_execute
from pipelex_sdk.usage import summarize_usage

execute_result = await client.execute(pipe_code="my_domain.my_pipe", mthds_contents=[source])
results = results_from_execute(execute_result)
print(results.main_stuff, summarize_usage(results).total_cost_usd)
```

| field | type | hosted (durable) path | bare-runner (blocking) path |
|---|---|---|---|
| `pipeline_run_id` | `str` | the run store's id | the runner's own id for the call |
| `main_stuff` | `Any` | the `main_stuff.json` artifact | resolved out of the returned working memory |
| `graph_spec` | `Any` | the `graphspec.json` artifact | lifted off `pipe_output` |
| `graph_assembly_error` | `str \| None` | absent until the platform relays it | lifted off `pipe_output` |
| `pipe_io_contracts` | `PipeIOContracts \| None` | the `pipe_io_contracts.json` artifact | lifted off `pipe_output` |
| `input_form` | `InputForm \| None` | the `input_form.json` artifact | lifted off `pipe_output` |
| `output_form` | `OutputForm \| None` | the `output_form.json` artifact | lifted off `pipe_output` |
| `pipe_io_artifacts_error` | `str \| None` | absent until the platform relays it | lifted off `pipe_output` |
| `tokens_usages` | `list[TokensUsageRecord] \| None` | the `tokens_usages.json` artifact | lifted off `pipe_output` |
| `usage_assembly_error` | `str \| None` | relayed | lifted off `pipe_output` |
| `working_memory` | `DictWorkingMemoryAbstract \| None` | the `working_memory.json` artifact | lifted off `pipe_output.working_memory` |
| `pipe_output` | `DictPipeOutputAbstract \| None` | absent | the runner's whole native output |

## `None` versus absent — the reading this page relies on

Every field but the first two is optional, and two readings of an optional field are distinct on purpose. The JS SDK reads a key the hosted body did not carry as `undefined` and a key relayed as `null` as `null`. Python has one `None`, so the SDK keeps the distinction where pydantic keeps it: in `model_fields_set`. A key the hosted body did not carry is not in the set and reads `None`; a key relayed as `null` is in the set and reads `None` too.

```python
results = await client.wait_for_result(run_id)

if "graph_assembly_error" not in results.model_fields_set:
    ...  # the platform relayed no such key: no information, not "assembly succeeded"
elif results.graph_assembly_error is None:
    ...  # relayed as null: assembly did not fail
```

That distinction matters on the hosted path only. On the blocking path the SDK lifts every field off the runner's output and passes each one explicitly, so every field is set there whether or not the runner carried the key — the blocking path always answers, exactly as the JS twin writes `null` for each. Most consumers never need the set: a check of `is None` is the right branch for "is there a value", and `model_fields_set` is for the one question it answers, whether the wire said anything at all.

## `pipeline_run_id` — the durable handle

The run id is what makes a run readable after the process that started it has gone. `start` returns it in its acknowledgement before the run finishes, and every lifecycle read takes it: `get_run_status(run_id)` for the status row, `get_run_result(run_id)` for a single result lookup, `wait_for_result(run_id)` to resume polling a run an earlier session started. It is also what a `RunTimeoutError` leaves you with — the run keeps executing server-side, so the timeout is a reason to re-poll by id, not a reason to run the method again.

```python
ack = await client.start(pipe_code="my_domain.summarize", inputs={"text": "..."})
print(ack.pipeline_run_id)  # persist this — it outlives the process

# …later, in another process
results = await client.wait_for_result(ack.pipeline_run_id)
```

Against a bare runner the id identifies the call the runner just answered, but there is no run store behind it: the lifecycle routes are absent, so re-reading it raises `RunLifecycleUnavailableError`. Durable resumption is a hosted capability.

## `main_stuff` — the output

`main_stuff` is the resolved content of the run's main output and is always present for a completed run. On the hosted path it is the `main_stuff.json` artifact; on the blocking path the SDK resolves it out of the returned working memory through the response's `main_stuff_name`. Both deliver the same content shape, so there is no shape-guessing and no path-dependent branch to write. A completed run that cannot deliver one raises `MissingMainStuffError` rather than handing back a half-filled result.

It is typed `Any` because the content is polymorphic: a structured output arrives as a dict of the concept's fields, and a multiple output as the envelope `{"items": [...]}` that the runtime's `ListContent` serialises to. Every content type serialises to an object, natives included — a text output is `{"text": "…"}` and a number `{"number": 0}` — so a guard written for a bare `""` or `0` never fires, and an empty multiple output is `{"items": []}` rather than `[]`. Narrow it where you read it, ideally through the types generated for the method rather than a hand-written cast.

For a multiple output that means reading `items` off the dict and validating each member with the generated per-concept model, because codegen emits a model per concept and no wrapper type for the envelope. **Do not rely on the model to catch the mistake for you.** The generated models are not strict, so a concept with a required field rejects the envelope loudly, while one whose fields are all optional validates it to an empty instance and discards the output in silence.

```python
from pipelex_sdk.runs import RunResultCompleted, RunResultFailed, RunResultRunning

state = await client.get_run_result(run_id)

match state:
    case RunResultCompleted():
        # `state.result` is the RunResults; `main_stuff` is the output content.
        summary = state.result.main_stuff
        print(summary["title"], len(summary["bullets"]))
    case RunResultRunning():
        print(f"not finished — poll again in {state.retry_after_seconds or 2}s")
    case RunResultFailed():
        print(f"run ended as {state.status}: {state.message}")
```

`get_run_result` is the single-shot lookup and returns that discriminated state. `wait_for_result(run_id)` drives the same lookup in a loop, honouring the server's `Retry-After`, and returns the `RunResults` directly — raising `RunFailedError` on a terminal non-completed status and `RunTimeoutError` when the budget runs out.

## `working_memory` — every named stuff of the run

`working_memory` is everything the run held when it finished — the inputs it was given, the intermediates it produced and the main output, each under the name the method gave it. It is a declared field on both paths and reads the same on each: on the hosted path the platform relays the `working_memory.json` artifact as its own key, and on the blocking path the SDK lifts it off `pipe_output.working_memory`. The standard declares that member required on the runner's output, so on the blocking path the field always carries a value.

It is typed as the standard's `DictWorkingMemoryAbstract`, imported from `mthds.runners.api.models` rather than restated here — the same ruling that types `pipe_output`. Two members: `root`, a dict of stuff name to stuff, each stuff carrying a `concept` (the namespaced ref, or the whole concept object when the runner dumps one) and its `content`; and `aliases`, a dict mapping a role onto a root key, which is where `main_stuff` names the entry `results.main_stuff` already resolved for you. Every level is extension-open, so a runner's per-stuff extras — `stuff_code`, `stuff_name` — ride `model_extra` instead of being dropped.

Reading a stuff by name means going through `root`, and the alias table is what turns a role into that name:

```python
results = await client.wait_for_result(run_id)

memory = results.working_memory
if memory is not None:
    draft = memory.root["draft"]
    print(draft.concept_ref, draft.content)  # e.g. "my_domain.Draft" {...}
    main_name = memory.aliases.get("main_stuff", "main_stuff")
    print(memory.root[main_name].content)  # the same content as `results.main_stuff`
```

`content` is typed `Any` for the same reason `main_stuff` is: it is the serialized content of whatever concept the stuff holds, so narrow it where you read it, ideally through the types generated for the method.

**When it is `None`, and when it is absent.** The two readings the page states above apply here: on the hosted path a relayed `null` means the platform has no such artifact for this run (it was not written), and is in `model_fields_set`; a body that carried no `working_memory` key at all leaves the field `None` and out of the set, which is no information rather than "the run held nothing". On the blocking path the field is always set and never `None`. `download_artifacts` makes that distinction an error rather than a branch when its scope is `working_memory` — a never-relayed key raises `FieldNotIncludedError` and a relayed `null` raises `ScopeUnavailableError` ([`artifact-download.md`](./artifact-download.md)).

## `graph_spec` — the executed graph

`graph_spec` is the graph the run actually executed: `meta.mode` is `"live"`, and there is one node per pipe with its execution status, its start and end timestamps, its inputs and outputs, and the inference models and cost attributed to it. It is the same document a local `pipelex` run writes as `graphspec.json`, so anything that reads one of those files reads this value unchanged.

The field is typed `Any` by a standing ruling ([`architecture.md`](./architecture.md#typed-by-import-the-descriptors-and-the-pipe-io-contracts)): no published Python package declares the graph spec, so a type here could only be a copy that drifts from the runtime that emits it. The value is relayed verbatim either way — the typing says where the schema lives, not that the content is uncertain.

**Rendering it.** The viewer that consumes it is `@pipelex/mthds-ui`'s `GraphViewer`, a React component; a Python service hands the JSON to the front end that renders it. The viewer takes `graph_spec` with the pair `pipe_io_contracts` and `output_form` below to show each node's value rather than its concept's structure, so a service that serves the graph should serve the three artifacts beside it.

**Keeping it.** The value is plain JSON, so persisting it is a write; there is no SDK helper and none is needed. Keeping it is worth doing for anything you may have to explain later, because it is the only record of what the run did pipe by pipe:

```python
import json
from pathlib import Path

Path("graphspec.json").write_text(json.dumps(results.graph_spec, indent=2))
```

**When it is `None`.** On the hosted path, the artifact may not have been written when the results were delivered. On either path, the runner may have assembled no graph at all — which is what the next field is for.

## `graph_assembly_error` — why there is no graph

`graph_assembly_error` is the graph's twin of `usage_assembly_error`, and it exists for the same reason: a `None` `graph_spec` alone cannot say whether graph assembly was off, broke, or simply had not finished writing. When the runner's assembly failed, this field carries the runner's message.

On the blocking path the SDK lifts it off `pipe_output`, beside the graph itself. **On the hosted path the key is absent, so the field reads `None` and is not in `model_fields_set`**: the platform's results body relays no such key and the SDK parses that body as it arrives, so the failure the bare runner reports is not yet observable through the hosted API. The field is declared ahead of that relay so consumers have one accessor to write against and nothing breaks the day the wire gains the key — the value appears on its own, with no SDK change. Until then, treat an unset field on the hosted path as "no information", not as "assembly succeeded".

```python
if results.graph_assembly_error is not None:
    log.warning("graph assembly failed for this run: %s", results.graph_assembly_error)
elif results.graph_spec is None:
    ...  # no graph: assembly was off, the artifact was not written, or (hosted) the error is not relayed
```

## `pipe_io_contracts`, `input_form` and `output_form` — what the graph's data is

`graph_spec` carries the values a run produced; these three say what those values ARE. They are the validate report's own artifacts — the standard's `PipeIOContracts`, `InputForm` and `OutputForm`, imported from `mthds.protocol` rather than restated here, under the same ruling that governs them on the validate report ([`architecture.md`](./architecture.md#typed-by-import-the-descriptors-and-the-pipe-io-contracts)) — built over the library the run actually executed against and keyed by namespaced `pipe_ref` (`domain.code`) over one shared key set. They are the same documents a local `pipelex` run writes beside its `graphspec.json` as `pipe_io_contracts.json`, `input_form.json` and `output_form.json`, so a consumer reads one thing whether the artifacts came from `/v1/validate`, from a results directory, or from a hosted run.

The contract names each pipe's inputs and its output — the concept, the multiplicity, the JSON Schema of the payload — and the two form descriptors say what each of those slots IS as a typed field, which is what a renderer needs to lay a value out without inspecting it. The types are the standard's: a contract entry is a `PipeIOContract`, a form entry a `PipeInputFormDescriptor` or `PipeOutputFormDescriptor` whose nodes are the kind-discriminated field union — narrow a node with `match node: case ListField(): …`, importing the per-kind models from `mthds.protocol.input_form`.

**Read the contracts and the output form together.** `@pipelex/mthds-ui`'s `GraphViewer` gates a data node's value on holding both: given the pair it renders the payload, and given one or neither it falls back to the concept's structure table with no data tab. That is why they arrive as a set rather than one at a time. `input_form` is optional even then — it is what lets the method's own inputs show their values, since no pipe produced them and no output descriptor describes them.

```python
contracts = results.pipe_io_contracts
output_form = results.output_form
if contracts is not None and output_form is not None:
    summarize = contracts["my_domain.summarize"]
    print(summarize.output.concept_ref)  # e.g. "my_domain.Summary"
    print(output_form["my_domain.summarize"].field.kind)  # e.g. "object"
```

**They are closed shapes, and the parse is strict.** The three artifacts are `extra="forbid"` in `mthds.protocol`: a member the pinned `mthds` does not define is version drift, and it fails the parse of the whole results body — main output included — rather than being read half-way. What is raised is pydantic's own `ValidationError`, out of `get_run_result`, `wait_for_result` and `start_and_wait` alike, and it is neither an `ApiResponseError` nor a `PipelineRequestError`, so a handler written for this SDK's errors does not catch it. On the hosted path nothing is lost by it: the artifacts stay in the run store, and the same results fetch parses from a `pipelex-sdk` release whose `mthds` pin knows the member. On the blocking path there is no store, the runner's response is discarded with the exception, and the completed `main_stuff` is not recoverable from that call; the one way to read such a run is `execute()`, whose extension-open `pipe_output` carries the artifacts raw — a different call, not a recovery of the one that failed. That is the same ruling the validate report follows, for the same reason: one declaration per language, drift refused at the parse. It bites only against a runtime newer than the artifacts this package's `mthds` pin describes, because the artifacts are written by the same `pipelex` the pin tracks, and every run older than the artifacts carries them as `None`, which parses. The envelope around them stays open: an unrelated key the platform adds still parses and rides `model_extra`.

**When they are `None`.** On the blocking path the SDK unwraps the runner's `pipe_io_artifacts` envelope — the runner carries the three together, since they share a key set and are built in one pass — onto these three fields, so each has one accessor whichever path ran. On the hosted path the platform relays each as its own key, and the key is always in the body, so all three are set there: `None` when the artifact was not written. On either path `None` means the run described no data at all — graph tracing off, a runtime older than the artifacts, or a build that broke, which only `pipe_io_artifacts_error` tells apart, where it is relayed.

## `pipe_io_artifacts_error` — why there is no description

`pipe_io_artifacts_error` is the three artifacts' twin of `graph_assembly_error`, and it exists for the same reason: three `None` artifacts alone cannot say whether the run described no data or whether building the description broke. When the runner's build failed, this field carries its message. It is lifted off `pipe_output` on the blocking path and, like `graph_assembly_error`, the hosted results body relays no such key — so treat an unset field there as "no information", not as "the build succeeded".

## `tokens_usages` and `usage_assembly_error` — what the run consumed

The usage pair reports what each inference call consumed and cost — one `TokensUsageRecord` per call, in completion order — and reads identically on both paths. The `None`-versus-empty semantics, the cost rules (`None` is unrated, `0` is priced at zero), the non-additive token categories and the pre-contract artifacts that still parse all have their own page: [`run-usage.md`](./run-usage.md). For the run's totals, do not add the records up by hand — `pipelex_sdk.usage.summarize_usage(results)` folds the pair into one null-aware reading with a per-pipe rollup, under every rule that page states. It is the one place the `None`-versus-absent distinction above becomes an error rather than a branch: a results body that never carried `tokens_usages` raises `FieldNotIncludedError`, because a key the read did not carry is not a run that reported no usage.

## `pipe_output` — the runner's native output

`pipe_output` is the bare runner's whole native output, and it is present on the blocking path only — the hosted results body carries no such key, so on that path it reads `None`. It is supplementary: `main_stuff`, the graph pair, the three I/O artifacts, the working memory and the usage pair are all lifted out of it onto fields that read the same on both paths, so a consumer that reads those fields keeps working against the hosted API. What `pipe_output` adds is the runner's output exactly as it arrived, typed as the standard's `DictPipeOutputAbstract`, which is extension-open — the runner's Pipelex extension fields, the `pipe_io_artifacts` envelope among them, stay reachable in their raw form through `model_extra`. A caller holding an `execute()` result rather than a `RunResults` does that lift with `results_from_execute`, described at the top of this page, instead of reading the bag itself.

## Produced files

A run that produces an image, a PDF or a document does not embed the bytes. The content inside `main_stuff` carries the file's durable reference — a `pipelex-storage://` URI, in the content's `url` — beside a `public_url` the storage provider signed when the run wrote the file. **That signed link is short-lived and must not be stored**: it expires on the provider's own schedule, so a link persisted in a database or rendered into a cached page stops working without warning, while the `pipelex-storage://` reference beside it is permanent and is what belongs in your records.

**The whole download direction is [`artifact-download.md`](./artifact-download.md)**, and it is where a consumer should start: `collect_artifacts(results.main_stuff)` lists the references without touching the network, `resolve_artifacts` mints a fresh link for each through the platform's bulk route, `fetch_artifact` streams one within bounds, and `download_artifacts` saves a whole run's files under a directory and answers a produced verdict. None of them reads the embedded `public_url`.

For the single reference you already hold, the raw primitive is still there:

```python
resolved = await client.resolve_storage_url("pipelex-storage://...")
# resolved.url, resolved.expires_at, resolved.content_type — fetch `url` now; re-resolve for the next reader.
```

The same rule holds in a browser: resolve on the server, hand the client a link it uses immediately, and never let a presigned URL outlive the request it was minted for. The upload direction — turning local files into `pipelex-storage://` references before a run — is the mirror of this and has its own page, [`input-preparation.md`](./input-preparation.md).
