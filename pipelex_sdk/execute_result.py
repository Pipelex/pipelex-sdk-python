"""The blocking `execute()` result — a `DictRunResultExecute` that resolves its `.main_stuff`.

Kept in its own module (not `runs.py`) so it can import `MissingMainStuffError` from
`errors` without forming an import cycle (`errors` type-imports `runs`).
"""

from __future__ import annotations

from typing import Any

from mthds.runners.api.models import DictRunResultExecute

from pipelex_sdk.errors import MissingMainStuffError


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
