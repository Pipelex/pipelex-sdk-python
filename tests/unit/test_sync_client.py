"""Tests for `SyncPipelexAPIClient` — the synchronous facade over `PipelexAPIClient`.

What is pinned here is the facade's own machinery, not the routes: calls run on one private
loop on one other thread, arguments and errors cross that boundary unchanged, several threads
share a facade without queueing, a caller inside an event loop is refused before anything is
sent, the paged iterators finalize when abandoned, and `close()` releases everything and leaves
the facade reusable. The one-to-one signature match with the async client is
`test_sync_client_parity.py`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import threading
import warnings
from typing import TYPE_CHECKING, Any, cast

import pytest
from mthds.protocol.exceptions import PipelineRequestError

from pipelex_sdk.errors import RunTimeoutError, SyncClientInEventLoopError
from pipelex_sdk.runs import WaitForResultOptions
from pipelex_sdk.sync_client import SyncPipelexAPIClient
from tests.unit.conftest import BASE_URL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator

    from pytest_mock import MockerFixture

    from tests.unit.conftest import ResponseBuilder, SendPatcher

# How long a test waits on a cross-thread handshake before failing instead of hanging.
_HANDSHAKE_TIMEOUT_SECONDS = 5.0


def _method(method_id: str) -> dict[str, Any]:
    return {"method_id": method_id, "name": f"Method {method_id}", "created_at": "t"}


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
            with pytest.raises(SyncClientInEventLoopError, match=r"PipelexAPIClient\.health\(\).*await"):
                asyncio.run(_async_caller())
            gc.collect()

        assert send.await_count == 0
        assert sync_api_client._loop is None
        assert not [warning for warning in caught if "never awaited" in str(warning.message)]

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
