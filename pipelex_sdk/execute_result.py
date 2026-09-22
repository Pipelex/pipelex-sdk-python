"""The blocking `execute()` result — a `DictRunResultExecute` that resolves its `.main_stuff`, and
the public lift from that result onto `RunResults`.

Kept in its own module (not `runs.py`) so it can import `MissingMainStuffError` from
`errors` without forming an import cycle (`errors` type-imports `runs`). The lift lives here for
the same reason and in the same direction: it takes a `PipelexExecuteResult` and builds a
`RunResults`, so it belongs on the side that already depends on `runs`.
"""

from __future__ import annotations

from typing import Any, cast

from mthds.runners.api.models import DictRunResultExecute

from pipelex_sdk.errors import MissingMainStuffError
from pipelex_sdk.runs import MethodProvenance, RunResults


class PipelexExecuteResult(DictRunResultExecute):
    """The SDK's blocking `execute()` result — a `DictRunResultExecute` that also exposes the
    resolved main output as `.main_stuff`.

    The protocol's raw execute response carries the working memory (`pipe_output`) and names the
    main output via `main_stuff_name`, but not the output itself. The neutral `mthds` model leaves
    `main_stuff_name` in its extension bag; this Pipelex-branded subclass declares it as a typed
    field (Pipelex owns that concept) and digs the output out on access, so callers read
    `result.main_stuff` exactly the same way as on the durable path (`RunResults.main_stuff`) — one
    output accessor across both execution modes, no working-memory spelunking.
    """

    #: The working-memory `root` key the completed execute response names as its main stuff
    #: (pipelex >= 0.37 always sends it). `None` only if a runner omits it, in which case
    #: `.main_stuff` raises `MissingMainStuffError`.
    main_stuff_name: str | None = None

    #: Provenance of a `method_ref` run — the resolved address, the requested tag, and the
    #: commit SHA that was actually fetched (a Pipelex-API extension, pipelex-api >= 0.21.0).
    #: `None` for inline-source and `method_id` runs, mirroring `PipelexRunResultStart` on
    #: the durable path.
    method_provenance: MethodProvenance | None = None

    @property
    def main_stuff(self) -> Any:
        """The resolved main output content, dug out of the working memory via `main_stuff_name`.
        Raises `MissingMainStuffError` if the completed run named no locatable main stuff. A
        falsy-but-present value (empty list, `0`) is a valid output and is returned as-is.
        """
        main_stuff_name = self.main_stuff_name
        stuff = self.pipe_output.working_memory.root.get(main_stuff_name) if main_stuff_name is not None else None
        if stuff is None:
            msg = (
                f"Blocking run '{self.pipeline_run_id}' delivered no locatable main stuff "
                f"(main_stuff_name={main_stuff_name!r} is absent from the working-memory root) — "
                "a completed run always delivers a main stuff."
            )
            raise MissingMainStuffError(msg, run_id=self.pipeline_run_id)
        return stuff.content


def results_from_execute(result: PipelexExecuteResult) -> RunResults:
    """Lift a blocking `execute()` result onto the lifecycle's `RunResults` — the same shape a durable run hands back.

    `execute()` returns the runner's whole typed envelope, where the usage pair, the graph pair, the
    working memory and the three I/O artifacts ride the extension-open `pipe_output` as Pipelex
    extension fields on `model_extra`. This function is the one place that lifts each onto the
    declared field of the same name, so a caller driving the blocking route itself reaches
    `pipelex_sdk.usage.summarize_usage`, `pipelex_sdk.artifacts.collect_artifacts` and every other
    run-results field exactly as it would on the hosted path, instead of re-reading `model_extra` by
    hand. It is pure: no client, no network, no I/O. `start_and_wait` calls it on its bare-runner
    fallback, which is the only caller inside the SDK.

    `result.main_stuff` resolves the main output out of the returned working memory (and raises
    `MissingMainStuffError` if the run named no locatable main stuff), so the durable and blocking
    paths hand back the same `main_stuff` content shape. The already-parsed `pipe_output` model is
    carried over as-is — no `.model_dump()` round-trip — so the runner's whole envelope stays typed
    (blocking only; the hosted path has none), and its `working_memory` is lifted onto the field of
    that name, where the hosted path relays the artifact as its own key. The standard declares
    `DictPipeOutputAbstract.working_memory` required, so that lift always carries a value here.

    Lifting the graph pair (`graph_spec` / `graph_assembly_error`), the usage pair (`tokens_usages` /
    `usage_assembly_error`) and the `pipe_io_artifacts` envelope with its `pipe_io_artifacts_error`
    onto their top-level fields is what makes `.graph_spec`, `.tokens_usages` and `.pipe_io_contracts`
    read the same on the blocking and durable paths; `RunResults` validates the raw records into
    `TokensUsageRecord`s and the raw artifacts into the standard's models on the way in. The runner
    carries the three I/O artifacts in one envelope — they share a key set and are always built
    together — where the hosted results body relays them as three sibling keys; `RunResults` follows
    the hosted shape and this unwraps the envelope onto it. A null envelope leaves all three `None`,
    beside whatever `pipe_io_artifacts_error` says about why.

    Every lifted field is passed explicitly, so on this path each is in `model_fields_set` whether
    or not the runner carried the key — the blocking path always answers, as the JS twin writes
    `null` there; only the hosted path leaves a field unset when the body did not carry its key.
    """
    pipe_output_extras: dict[str, Any] = result.pipe_output.model_extra or {}
    pipe_io_artifacts: dict[str, Any] = {}
    raw_pipe_io_artifacts = pipe_output_extras.get("pipe_io_artifacts")
    if isinstance(raw_pipe_io_artifacts, dict):
        pipe_io_artifacts = cast("dict[str, Any]", raw_pipe_io_artifacts)
    return RunResults(
        pipeline_run_id=result.pipeline_run_id,
        main_stuff=result.main_stuff,
        graph_spec=pipe_output_extras.get("graph_spec"),
        graph_assembly_error=pipe_output_extras.get("graph_assembly_error"),
        pipe_io_contracts=pipe_io_artifacts.get("pipe_io_contracts"),
        input_form=pipe_io_artifacts.get("input_form"),
        output_form=pipe_io_artifacts.get("output_form"),
        pipe_io_artifacts_error=pipe_output_extras.get("pipe_io_artifacts_error"),
        working_memory=result.pipe_output.working_memory,
        pipe_output=result.pipe_output,
        tokens_usages=pipe_output_extras.get("tokens_usages"),
        usage_assembly_error=pipe_output_extras.get("usage_assembly_error"),
    )
