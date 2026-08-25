"""Tests for the paged-list iterators — `iterate_methods` and `iterate_runs`.

Both follow `next_cursor` until the server says stop, but they stop on *different* signals,
and the difference is in the server rather than the client: `q` is a post-read filter over a
bounded index slice, so a method page can be empty with a live cursor; the run date bounds are
index key conditions, so a run page never is. These pin both rules, the stuck-cursor guard, and
the runaway backstop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

import pytest

from pipelex_sdk.errors import PagingNotTerminatingError

if TYPE_CHECKING:
    from pytest_mock import MockerFixture, MockType

    from pipelex_sdk.client import PipelexAPIClient
    from pipelex_sdk.product_models import MethodSummary, PipelineRun
    from tests.unit.conftest import ResponseBuilder, SendPatcher


def _method(method_id: str) -> dict[str, Any]:
    return {"method_id": method_id, "name": f"Method {method_id}", "created_at": "t"}


def _run(run_id: str) -> dict[str, Any]:
    return {"pipeline_run_id": run_id, "method_id": "m1", "pipe_code": "p", "status": "RUNNING", "created_at": "t"}


def _cursors_sent(send: MockType) -> list[str | None]:
    """The `cursor` query value of every request the spy recorded, in order."""
    cursors: list[str | None] = []
    for call in send.call_args_list:
        url = cast("str", call.args[1])
        query: dict[str, list[str]] = parse_qs(urlparse(url).query)
        values = query.get("cursor")
        cursors.append(values[0] if values else None)
    return cursors


async def _drain_methods(client: PipelexAPIClient, **kwargs: Any) -> list[MethodSummary]:
    return [summary async for summary in client.iterate_methods(**kwargs)]


async def _drain_runs(client: PipelexAPIClient, method_id: str) -> list[PipelineRun]:
    return [pipeline_run async for pipeline_run in client.iterate_runs(method_id)]


class TestClientPaging:
    def test_iterate_methods_follows_the_cursor_and_stops_on_none(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        send = patch_send(
            api_client,
            wire_response(200, json_body={"items": [_method("m1")], "next_cursor": "c1"}),
            wire_response(200, json_body={"items": [_method("m2")], "next_cursor": None}),
        )

        summaries = asyncio.run(_drain_methods(api_client))

        assert [summary.method_id for summary in summaries] == ["m1", "m2"]
        # The cursor sent on page N+1 is exactly the `next_cursor` received on page N.
        assert _cursors_sent(send) == [None, "c1"]

    def test_iterate_methods_continues_through_an_empty_page_with_a_live_cursor(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        """`q` filters after the index read, so an empty page means "keep going", not "done"."""
        patch_send(
            api_client,
            wire_response(200, json_body={"items": [], "next_cursor": "c1"}),
            wire_response(200, json_body={"items": [], "next_cursor": "c2"}),
            wire_response(200, json_body={"items": [_method("m9")], "next_cursor": None}),
        )

        summaries = asyncio.run(_drain_methods(api_client, q="needle"))

        assert [summary.method_id for summary in summaries] == ["m9"]

    def test_iterate_methods_stops_on_an_unchanged_cursor_without_re_yielding(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        """A server that hands back the cursor it was sent stops the loop *before* yielding."""
        patch_send(
            api_client,
            wire_response(200, json_body={"items": [_method("m1")], "next_cursor": "stuck"}),
            wire_response(200, json_body={"items": [_method("m1")], "next_cursor": "stuck"}),
        )

        summaries = asyncio.run(_drain_methods(api_client))

        assert [summary.method_id for summary in summaries] == ["m1"]

    def test_iterate_methods_raises_past_the_page_ceiling(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
        mocker: MockerFixture,
    ) -> None:
        """The backstop raises rather than returning: a truncated list is the bug paging removed."""
        mocker.patch("pipelex_sdk.client._MAX_LIST_PAGES", 2)
        pages = [wire_response(200, json_body={"items": [_method(f"m{index}")], "next_cursor": f"c{index}"}) for index in range(5)]
        patch_send(api_client, *pages)

        with pytest.raises(PagingNotTerminatingError) as exc_info:
            asyncio.run(_drain_methods(api_client))

        assert exc_info.value.page_limit == 2

    def test_iterate_runs_stops_on_an_empty_page(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        """Run date bounds are index key conditions, so an empty page really is the end."""
        patch_send(
            api_client,
            wire_response(200, json_body={"items": [_run("r1")], "next_cursor": "c1"}),
            wire_response(200, json_body={"items": [], "next_cursor": "c2"}),
            wire_response(200, json_body={"items": [_run("r99")], "next_cursor": None}),
        )

        runs = asyncio.run(_drain_runs(api_client, "m1"))

        assert [pipeline_run.pipeline_run_id for pipeline_run in runs] == ["r1"]

    def test_iterate_runs_follows_the_cursor_and_stops_on_none(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        send = patch_send(
            api_client,
            wire_response(200, json_body={"items": [_run("r1")], "next_cursor": "c1"}),
            wire_response(200, json_body={"items": [_run("r2")], "next_cursor": None}),
        )

        runs = asyncio.run(_drain_runs(api_client, "m1"))

        assert [pipeline_run.pipeline_run_id for pipeline_run in runs] == ["r1", "r2"]
        assert _cursors_sent(send) == [None, "c1"]

    def test_iterate_runs_stops_on_an_unchanged_cursor_without_re_yielding(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
    ) -> None:
        patch_send(
            api_client,
            wire_response(200, json_body={"items": [_run("r1")], "next_cursor": "stuck"}),
            wire_response(200, json_body={"items": [_run("r1")], "next_cursor": "stuck"}),
        )

        runs = asyncio.run(_drain_runs(api_client, "m1"))

        assert [pipeline_run.pipeline_run_id for pipeline_run in runs] == ["r1"]
