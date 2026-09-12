"""Tests for `SyncPipelexAPIClient` — the synchronous facade over `PipelexAPIClient`.

What is pinned here is the facade's own machinery, not the routes: calls run on one private
loop on one other thread, arguments and errors cross that boundary unchanged, several threads
share a facade without queueing, a caller inside an event loop is refused before anything is
sent, the paged iterators finalize when abandoned, interrupts and exits reach the caller as they
were raised, and `close()` releases everything, holds off the calls made meanwhile, and leaves
the facade reusable. The one-to-one match with the async client is `test_sync_client_parity.py`.

A test whose regression would leave a thread blocked for good runs that call through
`_outcome_in_thread`, and builds its own facade instead of taking the fixture, so a regression
fails the test instead of hanging the suite at teardown.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import subprocess
import sys
import textwrap
import threading
import warnings
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from mthds.protocol.exceptions import PipelineRequestError

from pipelex_sdk.errors import RunTimeoutError, SyncClientInEventLoopError
from pipelex_sdk.runs import PollInfo, WaitForResultOptions
from pipelex_sdk.sync_client import SyncPipelexAPIClient
from tests.unit.conftest import BASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Generator

    from pytest_mock import MockerFixture

    from tests.unit.conftest import ResponseBuilder, SendPatcher

# How long a test waits on a cross-thread handshake before failing instead of hanging.
_HANDSHAKE_TIMEOUT_SECONDS = 5.0
# How long a child interpreter gets to run a short script and exit before the test fails.
_SUBPROCESS_TIMEOUT_SECONDS = 60.0


def _method(method_id: str) -> dict[str, Any]:
    return {"method_id": method_id, "name": f"Method {method_id}", "created_at": "t"}


def _outcome_in_thread(call: Callable[[], object]) -> object:
    """Run a call on a daemon thread and return what it returned or raised.

    A call still blocked after the handshake timeout fails the test rather than hanging it, and
    the stuck thread, being a daemon, does not keep the test run from exiting.
    """
    outcome: list[object] = []

    def _target() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # the test inspects whatever crossed the thread boundary
            outcome.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(_HANDSHAKE_TIMEOUT_SECONDS)
    assert outcome, "the call was still blocked"
    return outcome[0]


class _Lock(Protocol):
    def acquire(self, blocking: bool = ..., timeout: float = ...) -> bool: ...

    def release(self) -> None: ...


class _LockReportingContention:
    """Stands in for the facade's lock, and reports that a caller found it held before waiting for it."""

    def __init__(self, lock: _Lock, found_held: threading.Event) -> None:
        self._lock = lock
        self._found_held = found_held

    def __enter__(self) -> bool:
        if not self._lock.acquire(blocking=False):
            self._found_held.set()
            self._lock.acquire()
        return True

    def __exit__(self, *exc_info: object) -> None:
        self._lock.release()


class TestSyncClient:
    def test_a_call_runs_through_the_real_transport(
        self,
        sync_api_client: SyncPipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        send = patch_send(sync_api_client._async_client, wire_response(200, json_body={"status": "ok"}))

        assert sync_api_client.health() == {"status": "ok"}
        assert send.await_count == 1
        assert send.call_args.args == ("GET", f"{BASE_URL}/health")

    def test_arguments_reach_the_async_twin_unchanged(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        sentinel = object()
        start_and_wait = mocker.patch.object(sync_api_client._async_client, "start_and_wait", mocker.AsyncMock(return_value=sentinel))
        wait_options = WaitForResultOptions(interval_seconds=0.5)

        result = sync_api_client.start_and_wait(
            pipe_code="my_pipe",
            inputs={"topic": "quantum computing"},
            extra={"custom": True},
            wait_options=wait_options,
            method_id="mt_123",
        )

        assert result is sentinel
        start_and_wait.assert_awaited_once_with(
            pipe_code="my_pipe",
            mthds_contents=None,
            inputs={"topic": "quantum computing"},
            output_name=None,
            output_multiplicity=None,
            dynamic_output_concept_ref=None,
            extra={"custom": True},
            wait_options=wait_options,
            method_ref=None,
            method_id="mt_123",
        )

    def test_the_async_error_is_raised_as_is(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        run_timeout = RunTimeoutError("run r1 did not finish", run_id="r1", timeout_seconds=1.0)
        mocker.patch.object(sync_api_client._async_client, "wait_for_result", mocker.AsyncMock(side_effect=run_timeout))

        with pytest.raises(RunTimeoutError) as exc_info:
            sync_api_client.wait_for_result("r1")

        assert exc_info.value is run_timeout

    def test_every_call_runs_on_one_loop_on_one_other_thread(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        seen: list[tuple[int, asyncio.AbstractEventLoop]] = []

        async def _record() -> dict[str, Any]:
            await asyncio.sleep(0)
            seen.append((threading.get_ident(), asyncio.get_running_loop()))
            return {}

        mocker.patch.object(sync_api_client._async_client, "health", mocker.AsyncMock(side_effect=_record))

        sync_api_client.health()
        sync_api_client.health()

        assert len(seen) == 2
        assert seen[0] == seen[1]
        assert seen[0][0] != threading.get_ident()

    def test_calls_from_several_threads_run_concurrently(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        """The first call waits for the second to have run: if calls queued, it would time out."""
        first_started = threading.Event()
        second_ran: list[asyncio.Event] = []
        outcomes: list[str] = []

        async def _wait_for_the_second(run_id: str) -> str:
            released = asyncio.Event()
            second_ran.append(released)
            first_started.set()
            await asyncio.wait_for(released.wait(), timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            return run_id

        async def _release_the_first(run_id: str) -> str:
            await asyncio.sleep(0)
            second_ran[0].set()
            return run_id

        mocker.patch.object(sync_api_client._async_client, "get_run_status", mocker.AsyncMock(side_effect=_wait_for_the_second))
        mocker.patch.object(sync_api_client._async_client, "get_run_result", mocker.AsyncMock(side_effect=_release_the_first))

        def _first() -> None:
            outcomes.append(str(sync_api_client.get_run_status("first")))

        first_thread = threading.Thread(target=_first)
        first_thread.start()
        assert first_started.wait(_HANDSHAKE_TIMEOUT_SECONDS)
        outcomes.append(str(sync_api_client.get_run_result("second")))
        first_thread.join(_HANDSHAKE_TIMEOUT_SECONDS)

        assert sorted(outcomes) == ["first", "second"]

    def test_a_call_inside_a_running_event_loop_is_refused_before_anything_is_sent(
        self,
        sync_api_client: SyncPipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        send = patch_send(sync_api_client._async_client, wire_response(200, json_body={"status": "ok"}))

        async def _async_caller() -> None:
            await asyncio.sleep(0)
            sync_api_client.health()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(SyncClientInEventLoopError, match=r"^SyncPipelexAPIClient\.health\(\).*await"):
                asyncio.run(_async_caller())
            gc.collect()

        assert send.await_count == 0
        assert sync_api_client._loop is None
        assert not [warning for warning in caught if "never awaited" in str(warning.message)]

    def test_a_refusal_names_the_facade_method_that_was_called(self, sync_api_client: SyncPipelexAPIClient) -> None:
        """Inherited routes and the paged iterators included, where the coroutine's own name would mislead."""

        async def _async_caller() -> None:
            await asyncio.sleep(0)
            with pytest.raises(SyncClientInEventLoopError, match=r"^SyncPipelexAPIClient\.models\(\)"):
                sync_api_client.models()
            with pytest.raises(SyncClientInEventLoopError, match=r"^SyncPipelexAPIClient\.iterate_methods\(\)"):
                next(sync_api_client.iterate_methods())

        asyncio.run(_async_caller())

    def test_close_inside_a_running_event_loop_is_refused(self, sync_api_client: SyncPipelexAPIClient) -> None:
        async def _async_caller() -> None:
            await asyncio.sleep(0)
            sync_api_client.close()

        with pytest.raises(SyncClientInEventLoopError, match=r"SyncPipelexAPIClient\.close\(\)"):
            asyncio.run(_async_caller())

    def test_iterate_methods_follows_every_page(
        self,
        sync_api_client: SyncPipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        patch_send(
            sync_api_client._async_client,
            wire_response(200, json_body={"items": [_method("m1"), _method("m2")], "next_cursor": "c1"}),
            wire_response(200, json_body={"items": [_method("m3")], "next_cursor": None}),
        )

        method_ids = [summary.method_id for summary in sync_api_client.iterate_methods(limit=2)]

        assert method_ids == ["m1", "m2", "m3"]

    def test_abandoning_an_iterator_finalizes_the_async_generator(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        finalized: list[bool] = []

        async def _endless_methods(**_kwargs: Any) -> AsyncIterator[str]:
            try:
                while True:
                    await asyncio.sleep(0)
                    yield "m"
            finally:
                finalized.append(True)

        mocker.patch.object(sync_api_client._async_client, "iterate_methods", _endless_methods)

        iterator = sync_api_client.iterate_methods()
        assert next(iterator) == "m"
        # The public type is `Iterator`; closing it early is the generator protocol a `break` triggers.
        cast("Generator[Any, None, None]", iterator).close()

        assert finalized == [True]

    def test_an_interrupt_during_a_page_fetch_reaches_the_caller_as_raised(
        self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture
    ) -> None:
        """The interrupt comes out of the blocking wait once the page fetch is in flight, where a real Ctrl+C lands.

        The page's cleanup spans several loop steps, as releasing an httpx response does: closing the
        async generator while that cleanup still runs raised a RuntimeError in place of the interrupt.
        """
        fetching = threading.Event()
        unwound = threading.Event()

        async def _pages(**_kwargs: Any) -> AsyncIterator[str]:
            yield "m1"
            try:
                fetching.set()
                await asyncio.Event().wait()
                yield "never"
            finally:
                for _ in range(3):
                    await asyncio.sleep(0)
                unwound.set()

        mocker.patch.object(sync_api_client._async_client, "iterate_methods", _pages)
        real_result = concurrent.futures.Future[Any].result
        blocking_waits: list[None] = []

        def _interrupt_the_second_wait(future: concurrent.futures.Future[Any], timeout: float | None = None) -> Any:
            blocking_waits.append(None)
            if len(blocking_waits) == 2:
                assert fetching.wait(_HANDSHAKE_TIMEOUT_SECONDS)
                raise KeyboardInterrupt
            return real_result(future, timeout)

        mocker.patch.object(concurrent.futures.Future, "result", autospec=True, side_effect=_interrupt_the_second_wait)

        iterator = sync_api_client.iterate_methods()
        assert next(iterator) == "m1"
        with pytest.raises(KeyboardInterrupt):
            next(iterator)

        assert unwound.wait(_HANDSHAKE_TIMEOUT_SECONDS)

    def test_a_system_exit_raised_on_the_loop_reaches_the_caller_and_the_loop_survives(self, mocker: MockerFixture) -> None:
        client = SyncPipelexAPIClient(api_key="test-token", base_url=BASE_URL)

        async def _poll_once(_run_id: str, options: WaitForResultOptions | None = None) -> None:
            await asyncio.sleep(0)
            if options is not None and options.on_poll is not None:
                options.on_poll(PollInfo(attempt=1, elapsed_seconds=0.0))

        def _exit_from_the_callback(_info: PollInfo) -> None:
            raise SystemExit(3)

        mocker.patch.object(client._async_client, "wait_for_result", mocker.AsyncMock(side_effect=_poll_once))
        mocker.patch.object(client._async_client, "health", mocker.AsyncMock(return_value={"status": "ok"}))

        outcome = _outcome_in_thread(lambda: client.wait_for_result("r1", options=WaitForResultOptions(on_poll=_exit_from_the_callback)))

        assert isinstance(outcome, SystemExit)
        assert outcome.code == 3
        assert _outcome_in_thread(client.health) == {"status": "ok"}
        client.close()

    def test_an_iterator_left_open_across_close_raises_rather_than_ending_early(
        self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture
    ) -> None:
        async def _five_methods(**_kwargs: Any) -> AsyncIterator[str]:
            for index in range(5):
                await asyncio.sleep(0)
                yield f"m{index}"

        mocker.patch.object(sync_api_client._async_client, "iterate_methods", _five_methods)

        iterator = sync_api_client.iterate_methods()
        assert next(iterator) == "m0"
        sync_api_client.close()

        with pytest.raises(concurrent.futures.CancelledError, match="was closed while this"):
            next(iterator)

    def test_an_iterator_collected_while_its_facade_holds_the_lock_does_not_deadlock(self, mocker: MockerFixture) -> None:
        client = SyncPipelexAPIClient(api_key="test-token", base_url=BASE_URL)

        async def _endless_methods(**_kwargs: Any) -> AsyncIterator[str]:
            while True:
                await asyncio.sleep(0)
                yield "m"

        mocker.patch.object(client._async_client, "iterate_methods", _endless_methods)
        mocker.patch.object(client._async_client, "health", mocker.AsyncMock(return_value={"status": "ok"}))
        real_submit = asyncio.run_coroutine_threadsafe

        def _collect_then_submit(coroutine: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> concurrent.futures.Future[Any]:
            gc.collect()  # stands in for an automatic collection, which any allocation made under the lock can trigger
            return real_submit(coroutine, loop)

        # Automatic collection is off so the abandoned iterator is collected under the lock, not earlier.
        gc.disable()
        try:
            iterator = client.iterate_methods()
            assert next(iterator) == "m"
            cycle: list[object] = [iterator]
            cycle.append(cycle)
            del iterator, cycle
            mocker.patch.object(asyncio, "run_coroutine_threadsafe", side_effect=_collect_then_submit)
            outcome = _outcome_in_thread(client.health)
        finally:
            gc.enable()

        assert outcome == {"status": "ok"}
        client.close()

    def test_an_iterator_left_in_a_module_global_does_not_keep_the_interpreter_from_exiting(self) -> None:
        """At exit the iterator is collected after the daemon loop thread has stopped; waiting on it hung the process."""
        script = textwrap.dedent(
            f"""
            import asyncio
            from pipelex_sdk.sync_client import SyncPipelexAPIClient

            async def _endless_methods(**_kwargs):
                while True:
                    await asyncio.sleep(0)
                    yield "m"

            client = SyncPipelexAPIClient(api_key="test-token", base_url="{BASE_URL}")
            client._async_client.iterate_methods = _endless_methods
            methods = client.iterate_methods()
            next(methods)
            """
        )

        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)

        assert completed.returncode == 0, completed.stderr

    def test_a_call_made_while_close_runs_waits_for_it_then_starts_a_fresh_loop(
        self,
        sync_api_client: SyncPipelexAPIClient,
        mocker: MockerFixture,
    ) -> None:
        """Calling in the middle of a close started a second loop on the client the first was still closing."""
        closing = threading.Event()
        close_released: list[asyncio.Event] = []
        caller_settled = threading.Event()
        events: list[str] = []

        async def _slow_close() -> None:
            if close_released:  # a later close, the fixture's teardown among them, has nothing to wait for
                return
            released = asyncio.Event()
            close_released.append(released)
            closing.set()
            await released.wait()
            events.append("closed")

        async def _health() -> dict[str, Any]:
            await asyncio.sleep(0)
            events.append("health")
            return {}

        mocker.patch.object(sync_api_client._async_client, "health", mocker.AsyncMock(side_effect=_health))
        mocker.patch.object(sync_api_client._async_client, "close", mocker.AsyncMock(side_effect=_slow_close))
        sync_api_client.health()
        events.clear()
        first_loop = sync_api_client._loop
        assert first_loop is not None

        closer = threading.Thread(target=sync_api_client.close)
        closer.start()
        assert closing.wait(_HANDSHAKE_TIMEOUT_SECONDS)
        mocker.patch.object(sync_api_client, "_lock", _LockReportingContention(sync_api_client._lock, caller_settled))

        def _caller() -> None:
            try:
                sync_api_client.health()
            finally:
                caller_settled.set()

        caller = threading.Thread(target=_caller)
        caller.start()
        # Settled: the caller found the lock held, or ran its whole call without having to wait.
        assert caller_settled.wait(_HANDSHAKE_TIMEOUT_SECONDS)
        first_loop.call_soon_threadsafe(close_released[0].set)
        closer.join(_HANDSHAKE_TIMEOUT_SECONDS)
        caller.join(_HANDSHAKE_TIMEOUT_SECONDS)

        assert events == ["closed", "health"]

    def test_close_releases_everything_and_the_facade_is_reusable(
        self,
        sync_api_client: SyncPipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        patch_send(
            sync_api_client._async_client,
            wire_response(200, json_body={"status": "ok"}),
            wire_response(200, json_body={"status": "again"}),
        )
        sync_api_client.health()
        sync_api_client._async_client.start_client()  # the real transport is patched out, so open the HTTP client by hand
        first_thread = sync_api_client._loop_thread
        assert first_thread is not None

        sync_api_client.close()

        assert not first_thread.is_alive()
        assert sync_api_client._loop is None
        assert sync_api_client._async_client.client is None
        assert sync_api_client.health() == {"status": "again"}
        assert sync_api_client._loop_thread is not None
        assert sync_api_client._loop_thread is not first_thread

    def test_close_cancels_a_call_in_flight_on_another_thread(self, sync_api_client: SyncPipelexAPIClient, mocker: MockerFixture) -> None:
        started = threading.Event()
        raised: list[BaseException] = []

        async def _never_finishes(_run_id: str) -> None:
            started.set()
            await asyncio.Event().wait()

        mocker.patch.object(sync_api_client._async_client, "get_run_status", mocker.AsyncMock(side_effect=_never_finishes))

        def _blocked_caller() -> None:
            try:
                sync_api_client.get_run_status("r1")
            except BaseException as exc:  # the test inspects whatever crossed the thread boundary
                raised.append(exc)

        caller_thread = threading.Thread(target=_blocked_caller)
        caller_thread.start()
        assert started.wait(_HANDSHAKE_TIMEOUT_SECONDS)

        sync_api_client.close()
        caller_thread.join(_HANDSHAKE_TIMEOUT_SECONDS)

        assert not caller_thread.is_alive()
        assert len(raised) == 1
        assert isinstance(raised[0], concurrent.futures.CancelledError)

    def test_the_context_manager_closes_on_exit(self, wire_response: ResponseBuilder, patch_send: SendPatcher) -> None:
        with SyncPipelexAPIClient(api_key="test-token", base_url=BASE_URL) as client:
            patch_send(client._async_client, wire_response(200, json_body={"status": "ok"}))
            client.health()
            loop_thread = client._loop_thread
            assert loop_thread is not None

        assert client._loop is None
        assert not loop_thread.is_alive()

    def test_closing_an_unused_facade_twice_does_nothing(self) -> None:
        client = SyncPipelexAPIClient(api_key="test-token", base_url=BASE_URL)

        client.close()
        client.close()

        assert client._loop is None
        assert client._loop_thread is None

    def test_construction_is_the_async_clients(self) -> None:
        client = SyncPipelexAPIClient(api_key="test-token", base_url=f"{BASE_URL}/", request_timeout_seconds=30.0)

        assert client.base_url == BASE_URL
        assert client.origin_url == BASE_URL
        assert client.request_timeout_seconds == 30.0
        with pytest.raises(PipelineRequestError):
            SyncPipelexAPIClient(base_url=f"{BASE_URL}/v1")
