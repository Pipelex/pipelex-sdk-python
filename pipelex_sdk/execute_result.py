"""The blocking `execute()` result — a `DictRunResultExecute` that resolves its `.main_stuff`.

Kept in its own module (not `runs.py`) so it can import `MissingMainStuffError` from
`errors` without forming an import cycle (`errors` type-imports `runs`).
"""

from __future__ import annotations

from typing import Any

from mthds.runners.api.models import DictRunResultExecute

from pipelex_sdk.errors import MissingMainStuffError

# The execute response's extension field that names the working-memory root key of the main stuff.
# Distinct from mthds's `MAIN_STUFF_NAME` ("main_stuff"), which is the root key / alias name itself.
_MAIN_STUFF_NAME_FIELD = "main_stuff_name"


class PipelexExecuteResult(DictRunResultExecute):
    """The SDK's blocking `execute()` result — a `DictRunResultExecute` that also exposes the
    resolved main output as `.main_stuff`.

    The protocol's raw execute response carries the working memory (`pipe_output`) and names the
    main output via a `main_stuff_name` extension field, but not the output itself. This subclass
    digs it out on access so callers read `result.main_stuff` exactly the same way as on the
    durable path (`RunResults.main_stuff`) — one output accessor across both execution modes, no
    working-memory spelunking.
    """

    @property
    def main_stuff(self) -> Any:
        """The resolved main output content, dug out of the working memory via the response's
        `main_stuff_name` (pipelex >= 0.37). Raises `MissingMainStuffError` if the completed run
        named no locatable main stuff. A falsy-but-present value (empty list, `0`) is a valid
        output and is returned as-is.
        """
        extras = self.model_extra or {}
        raw_name = extras.get(_MAIN_STUFF_NAME_FIELD)
        main_stuff_name = raw_name if isinstance(raw_name, str) else None
        stuff = self.pipe_output.working_memory.root.get(main_stuff_name) if main_stuff_name is not None else None
        if stuff is None:
            msg = (
                f"Blocking run '{self.pipeline_run_id}' delivered no locatable main stuff "
                f"(main_stuff_name={main_stuff_name!r} is absent from the working-memory root) — "
                "a completed run always delivers a main stuff."
            )
            raise MissingMainStuffError(msg, run_id=self.pipeline_run_id)
        return stuff.content
