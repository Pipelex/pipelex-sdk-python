"""`SyncPipelexAPIClient` — a synchronous facade over `PipelexAPIClient`.

For the callers that have no event loop of their own — a script, a batch job, a Django view,
a notebook cell run without top-level `await` — so none of them writes an `asyncio.run`
wrapper around each call.

The facade wraps and delegates; it is not a second transport. Every public method calls the
same-named coroutine on one private `PipelexAPIClient`, so there is exactly one HTTP
implementation, and the retry, error-mapping, paging and polling logic is never restated.
Each method has the same signature as its async twin, returns what the coroutine returns, and
raises what it raises; the two paged iterators come back as plain `Iterator`s. A unit test
pins the one-to-one signature match, so a coroutine added to the async client without a twin
here fails the suite.

How the calls run. The facade owns one event loop, started lazily on a daemon thread at the
first call and kept until `close()`. `asyncio.run` per call is the obvious version and the
wrong one: the wrapped client's httpx connection pool belongs to the loop that opened it, so a
fresh loop per call either breaks on the second call or reopens the pool every time. A single
long-lived loop keeps the pool warm. Running it on its own thread, rather than driving it from
the caller's thread with `asyncio.Runner`, is what makes one facade shareable across threads:
calls from several threads run concurrently on the loop instead of queueing behind each
other, which matters when one of them is a twenty-minute `start_and_wait`.

A caller that already runs an event loop gets `SyncClientInEventLoopError`, before anything is
sent. The call would work mechanically, but it would block that loop for the whole request and
freeze everything else scheduled on it; such a caller wants `PipelexAPIClient` and `await`.

Interrupting a blocked call (Ctrl+C) cancels the coroutine on the facade's loop and re-raises
in the caller. As on the async client, cancelling a `wait_for_result` or `start_and_wait` stops
the waiting, never the run itself, which keeps executing server-side and stays resumable by id.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import SyncClientInEventLoopError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator

    from mthds.protocol.models import ModelCategory, ModelDeck, VersionInfo
    from mthds.protocol.pipe_output import VariableMultiplicity
    from mthds.protocol.pipeline_inputs import PipelineInputs
    from mthds.protocol.stuff import StuffType
    from mthds.protocol.working_memory import WorkingMemoryAbstract

    from pipelex_sdk.client import MthdsFile
    from pipelex_sdk.crate_models import CodegenRequest, CodegenResponse, MthdsFileItem, ResolveRequest, ResolveResponse
    from pipelex_sdk.execute_result import PipelexExecuteResult
    from pipelex_sdk.prepare_inputs import PreparedInputs
    from pipelex_sdk.product_models import (
        BillingPortalResponse,
        ChangePlanResponse,
        CheckoutResponse,
        GatewayApiKey,
        GatewayApiKeyStatus,
        InvoiceView,
        Membership,
        MembershipsResponse,
        MethodData,
        MethodDeletionAccepted,
        MethodPage,
        MethodSummary,
        MethodWriteInput,
        OnboardingSubmission,
        PipelexApiKeyCreated,
        PipelexApiKeyList,
        PipelineRun,
        PlanView,
        ResolvedStorageUrl,
        RunDetail,
        RunPage,
        SubscriptionResponse,
        UpdateRunInput,
        UploadedFile,
        UploadInput,
        UserProfile,
    )
    from pipelex_sdk.runs import PipelexRunResultStart, RunRead, RunResults, RunResultState, WaitForResultOptions
    from pipelex_sdk.upload import UploadRecord, UploadSource
    from pipelex_sdk.validation_models import PipelexValidationResult

_ResultT = TypeVar("_ResultT")
_ItemT = TypeVar("_ItemT")

_LOOP_THREAD_NAME = "pipelex-sdk-sync-client"


class SyncPipelexAPIClient:
    """Synchronous facade over `PipelexAPIClient` — the same surface, without `await`.

    Construction takes the same arguments and resolves credentials exactly as
    `PipelexAPIClient` does (it builds one). Use it as a context manager, or call `close()`
    when done; a facade is reusable after `close()`, restarting its loop at the next call.
    One instance may be shared across threads. It must not be called from a thread that runs
    an event loop (`SyncClientInEventLoopError`), which includes a `WaitForResultOptions.on_poll`
    callback: that callback runs on the facade's own loop.

    The semantics of every method, its errors included, are those of the same-named method on
    `PipelexAPIClient`, which documents them.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, request_timeout_seconds: float | None = None) -> None:
        self._async_client = PipelexAPIClient(api_key=api_key, base_url=base_url, request_timeout_seconds=request_timeout_seconds)
        # Guards the loop's lifecycle: starting it, detaching it at close, and submitting to it.
        # Submission happens under the lock so a call can never land on a loop `close()` has
        # already detached; only the blocking wait for the result happens outside it.
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return self._async_client.base_url

    @property
    def origin_url(self) -> str:
        return self._async_client.origin_url

    @property
    def request_timeout_seconds(self) -> float:
        return self._async_client.request_timeout_seconds

    # ── Lifecycle ──────────────────────────────────────────────────────

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client and stop the facade's event loop.

        A call still in flight on another thread is cancelled and raises
        `concurrent.futures.CancelledError` there. Closing a facade that never made a call, or
        closing it twice, does nothing.

        Raises:
            SyncClientInEventLoopError: If called from a thread that runs an event loop.
        """
        if _inside_running_event_loop():
            raise SyncClientInEventLoopError(_in_event_loop_message("SyncPipelexAPIClient.close"))
        with self._lock:
            loop = self._loop
            loop_thread = self._loop_thread
            self._loop = None
            self._loop_thread = None
        if loop is None or loop_thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join()
            loop.close()

    async def _shutdown(self) -> None:
        """Runs on the facade's loop: cancel what is in flight, then release every resource."""
        current_task = asyncio.current_task()
        in_flight = [task for task in asyncio.all_tasks() if task is not current_task]
        for task in in_flight:
            task.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)
        await self._async_client.close()
        await asyncio.get_running_loop().shutdown_asyncgens()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Return the facade's loop, starting it on its daemon thread if needed. Caller holds the lock."""
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=_run_loop_forever, args=(loop,), name=_LOOP_THREAD_NAME, daemon=True)
        loop_thread.start()
        self._loop = loop
        self._loop_thread = loop_thread
        return loop

    def _run(self, coroutine: Coroutine[Any, Any, _ResultT]) -> _ResultT:
        """Run one coroutine of the wrapped client on the facade's loop and block for its result."""
        if _inside_running_event_loop():
            call_name = str(getattr(coroutine, "__qualname__", "PipelexAPIClient call"))
            # Close it rather than drop it: an unawaited coroutine would otherwise surface as a
            # "never awaited" RuntimeWarning pointing away from the real mistake.
            coroutine.close()
            raise SyncClientInEventLoopError(_in_event_loop_message(call_name))
        with self._lock:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._ensure_loop())
        try:
            return future.result()
        except BaseException:
            # Reached on an interrupt (KeyboardInterrupt) while blocked, as well as on a normal
            # error; cancelling an already finished future is a no-op, so this only stops work
            # that is still running.
            future.cancel()
            raise

    def _iterate(self, iterator: AsyncIterator[_ItemT]) -> Iterator[_ItemT]:
        """Drive an async iterator of the wrapped client one item per round trip to the loop."""
        try:
            while True:
                next_item = self._run(_next_item(iterator))
                if not next_item:
                    return
                yield next_item[0]
        finally:
            self._close_async_generator(iterator)

    def _close_async_generator(self, iterator: AsyncIterator[_ItemT]) -> None:
        """Finalize an async generator left part-way, e.g. by a `break` out of the sync loop.

        Skipped when there is no loop any more (`close()` already finalized every async
        generator on it) or when this runs inside an event loop (a blocking call is not allowed
        there; the loop finalizes the generator at shutdown instead).
        """
        if not isinstance(iterator, AsyncGenerator) or _inside_running_event_loop():
            return
        # The wrapped iterators are async generators that are only ever advanced, never sent to.
        generator = cast("AsyncGenerator[_ItemT, None]", iterator)
        with self._lock:
            loop = self._loop
            if loop is None:
                return
            future = asyncio.run_coroutine_threadsafe(_close_generator(generator), loop)
        future.result()

    # ── Protocol routes ────────────────────────────────────────────────

    def execute(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
        *,
        method_ref: str | None = None,
        method_id: str | None = None,
    ) -> PipelexExecuteResult:
        return self._run(
            self._async_client.execute(
                pipe_code=pipe_code,
                mthds_contents=mthds_contents,
                inputs=inputs,
                output_name=output_name,
                output_multiplicity=output_multiplicity,
                dynamic_output_concept_ref=dynamic_output_concept_ref,
                extra=extra,
                method_ref=method_ref,
                method_id=method_id,
            )
        )

    def start(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
        *,
        method_ref: str | None = None,
        method_id: str | None = None,
    ) -> PipelexRunResultStart:
        return self._run(
            self._async_client.start(
                pipe_code=pipe_code,
                mthds_contents=mthds_contents,
                inputs=inputs,
                output_name=output_name,
                output_multiplicity=output_multiplicity,
                dynamic_output_concept_ref=dynamic_output_concept_ref,
                extra=extra,
                method_ref=method_ref,
                method_id=method_id,
            )
        )

    def validate(
        self,
        mthds_contents: list[str] | None = None,
        allow_signatures: bool = False,
        mthds_sources: list[str] | None = None,
        render: list[str] | None = None,
        views: list[str] | None = None,
        *,
        method_ref: str | None = None,
        method_id: str | None = None,
    ) -> PipelexValidationResult:
        return self._run(
            self._async_client.validate(
                mthds_contents=mthds_contents,
                allow_signatures=allow_signatures,
                mthds_sources=mthds_sources,
                render=render,
                views=views,
                method_ref=method_ref,
                method_id=method_id,
            )
        )

    def validate_files(
        self,
        files: list[MthdsFile],
        allow_signatures: bool = False,
        render: list[str] | None = None,
        views: list[str] | None = None,
    ) -> PipelexValidationResult:
        return self._run(self._async_client.validate_files(files=files, allow_signatures=allow_signatures, render=render, views=views))

    def models(self, category: ModelCategory | None = None) -> ModelDeck:
        return self._run(self._async_client.models(category=category))

    def version(self) -> VersionInfo:
        return self._run(self._async_client.version())

    # ── Durable run lifecycle ──────────────────────────────────────────

    def get_run_status(self, run_id: str) -> RunRead:
        return self._run(self._async_client.get_run_status(run_id))

    def get_run_result(self, run_id: str) -> RunResultState:
        return self._run(self._async_client.get_run_result(run_id))

    def wait_for_result(self, run_id: str, options: WaitForResultOptions | None = None) -> RunResults:
        return self._run(self._async_client.wait_for_result(run_id, options=options))

    def start_and_wait(
        self,
        pipe_code: str | None = None,
        mthds_contents: list[str] | None = None,
        inputs: PipelineInputs | WorkingMemoryAbstract[StuffType] | None = None,
        output_name: str | None = None,
        output_multiplicity: VariableMultiplicity | None = None,
        dynamic_output_concept_ref: str | None = None,
        extra: dict[str, Any] | None = None,
        wait_options: WaitForResultOptions | None = None,
        *,
        method_ref: str | None = None,
        method_id: str | None = None,
    ) -> RunResults:
        return self._run(
            self._async_client.start_and_wait(
                pipe_code=pipe_code,
                mthds_contents=mthds_contents,
                inputs=inputs,
                output_name=output_name,
                output_multiplicity=output_multiplicity,
                dynamic_output_concept_ref=dynamic_output_concept_ref,
                extra=extra,
                wait_options=wait_options,
                method_ref=method_ref,
                method_id=method_id,
            )
        )

    # ── Product surface ────────────────────────────────────────────────

    def get_me(self) -> UserProfile:
        return self._run(self._async_client.get_me())

    def list_methods(self, *, q: str | None = None, limit: int | None = None, cursor: str | None = None) -> MethodPage:
        return self._run(self._async_client.list_methods(q=q, limit=limit, cursor=cursor))

    def iterate_methods(self, *, q: str | None = None, limit: int | None = None) -> Iterator[MethodSummary]:
        return self._iterate(self._async_client.iterate_methods(q=q, limit=limit))

    def get_method(self, method_id: str) -> MethodData:
        return self._run(self._async_client.get_method(method_id))

    def create_method(self, write_input: MethodWriteInput) -> MethodData:
        return self._run(self._async_client.create_method(write_input))

    def update_method(self, method_id: str, write_input: MethodWriteInput) -> MethodData:
        return self._run(self._async_client.update_method(method_id, write_input))

    def delete_method(self, method_id: str) -> MethodDeletionAccepted:
        return self._run(self._async_client.delete_method(method_id))

    def list_memberships(self) -> MembershipsResponse:
        return self._run(self._async_client.list_memberships())

    def create_organization(self, name: str) -> Membership:
        return self._run(self._async_client.create_organization(name))

    def rename_organization(self, org_id: str, name: str) -> Membership:
        return self._run(self._async_client.rename_organization(org_id, name))

    def get_subscription(self) -> SubscriptionResponse:
        return self._run(self._async_client.get_subscription())

    def list_plans(self) -> list[PlanView]:
        return self._run(self._async_client.list_plans())

    def list_invoices(self) -> list[InvoiceView]:
        return self._run(self._async_client.list_invoices())

    def create_checkout(self, plan: str) -> CheckoutResponse:
        return self._run(self._async_client.create_checkout(plan))

    def change_plan(self, plan: str) -> ChangePlanResponse:
        return self._run(self._async_client.change_plan(plan))

    def get_billing_portal(self) -> BillingPortalResponse:
        return self._run(self._async_client.get_billing_portal())

    def list_pipelex_api_keys(self) -> PipelexApiKeyList:
        return self._run(self._async_client.list_pipelex_api_keys())

    def create_pipelex_api_key(self, label: str) -> PipelexApiKeyCreated:
        return self._run(self._async_client.create_pipelex_api_key(label))

    def revoke_pipelex_api_key(self, key_id: str) -> None:
        self._run(self._async_client.revoke_pipelex_api_key(key_id))

    def rotate_pipelex_api_key(self, key_id: str) -> PipelexApiKeyCreated:
        return self._run(self._async_client.rotate_pipelex_api_key(key_id))

    def create_gateway_api_key(self, promo_code: str | None) -> GatewayApiKey:
        return self._run(self._async_client.create_gateway_api_key(promo_code))

    def get_gateway_api_key(self) -> GatewayApiKeyStatus:
        return self._run(self._async_client.get_gateway_api_key())

    def submit_onboarding(self, submission: OnboardingSubmission) -> None:
        self._run(self._async_client.submit_onboarding(submission))

    def resolve_storage_url(self, uri: str) -> ResolvedStorageUrl:
        return self._run(self._async_client.resolve_storage_url(uri))

    def upload(self, upload_input: UploadInput) -> UploadedFile:
        return self._run(self._async_client.upload(upload_input))

    # ── Crate routes ───────────────────────────────────────────────────

    def resolve(self, request: ResolveRequest) -> ResolveResponse:
        return self._run(self._async_client.resolve(request))

    def codegen(self, request: CodegenRequest) -> CodegenResponse:
        return self._run(self._async_client.codegen(request))

    # ── Input preparation ──────────────────────────────────────────────

    def upload_file(self, source: UploadSource, *, filename: str | None = None, content_type: str | None = None) -> UploadRecord:
        return self._run(self._async_client.upload_file(source, filename=filename, content_type=content_type))

    def prepare_inputs(
        self,
        *,
        files: list[MthdsFileItem] | None = None,
        method_ref: str | None = None,
        method_id: str | None = None,
        pipe_ref: str | None = None,
        inputs: dict[str, Any],
    ) -> PreparedInputs:
        return self._run(self._async_client.prepare_inputs(files=files, method_ref=method_ref, method_id=method_id, pipe_ref=pipe_ref, inputs=inputs))

    # ── Run records ────────────────────────────────────────────────────

    def list_runs(
        self,
        method_id: str,
        *,
        created_from: str | None = None,
        created_to: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> RunPage:
        return self._run(self._async_client.list_runs(method_id, created_from=created_from, created_to=created_to, limit=limit, cursor=cursor))

    def iterate_runs(
        self,
        method_id: str,
        *,
        created_from: str | None = None,
        created_to: str | None = None,
        limit: int | None = None,
    ) -> Iterator[PipelineRun]:
        return self._iterate(self._async_client.iterate_runs(method_id, created_from=created_from, created_to=created_to, limit=limit))

    def get_run_detail(self, run_id: str) -> RunDetail:
        return self._run(self._async_client.get_run_detail(run_id))

    def update_run(self, run_id: str, update_input: UpdateRunInput) -> None:
        self._run(self._async_client.update_run(run_id, update_input))

    # ── Health ─────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        return self._run(self._async_client.health())


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    """The facade's loop thread: own the loop until `close()` stops it."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _inside_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _in_event_loop_message(call_name: str) -> str:
    return (
        f"{call_name}() was called from a thread that is running an event loop, where a blocking call would freeze "
        "that loop for the whole request. Use PipelexAPIClient and await the call there instead."
    )


async def _next_item(iterator: AsyncIterator[_ItemT]) -> list[_ItemT]:
    """The iterator's next item in a one-element list, or an empty list once it is exhausted.

    Exhaustion is returned rather than raised because `StopAsyncIteration` is not an exception
    to carry across the thread boundary: the sync generator turns the empty list into its own
    ordinary return.
    """
    try:
        return [await anext(iterator)]
    except StopAsyncIteration:
        return []


async def _close_generator(generator: AsyncGenerator[Any, Any]) -> None:
    await generator.aclose()
