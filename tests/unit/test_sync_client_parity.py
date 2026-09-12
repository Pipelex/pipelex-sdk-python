"""The sync facade mirrors the async client one-to-one, and this module is what keeps it so.

Every public coroutine and async generator on `PipelexAPIClient` — inherited protocol routes
included — must have a same-named method on `SyncPipelexAPIClient` with an identical parameter
list and the same return annotation, an `AsyncIterator[X]` becoming `Iterator[X]`. A method
added to the async client without its twin fails here rather than shipping a facade that
silently lags, and the facade may not grow a method the async client does not have.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.sync_client import SyncPipelexAPIClient

if TYPE_CHECKING:
    from collections.abc import Callable

_ASYNC_ITERATOR_PREFIX = "AsyncIterator["
_SYNC_ITERATOR_PREFIX = "Iterator["


def _async_surface() -> dict[str, Callable[..., Any]]:
    surface: dict[str, Callable[..., Any]] = {}
    for name in dir(PipelexAPIClient):
        if name.startswith("_"):
            continue
        member = getattr(PipelexAPIClient, name)
        if inspect.iscoroutinefunction(member) or inspect.isasyncgenfunction(member):
            surface[name] = member
    return surface


def _sync_methods() -> dict[str, Callable[..., Any]]:
    methods: dict[str, Callable[..., Any]] = {}
    for name in dir(SyncPipelexAPIClient):
        if name.startswith("_"):
            continue
        member = inspect.getattr_static(SyncPipelexAPIClient, name)
        if inspect.isfunction(member):
            methods[name] = member
    return methods


_ASYNC_METHOD_NAMES = sorted(_async_surface())


class TestSyncClientParity:
    def test_every_async_method_has_a_sync_twin(self) -> None:
        missing = sorted(set(_async_surface()) - set(_sync_methods()))
        assert missing == []

    def test_the_facade_adds_no_method_the_async_client_lacks(self) -> None:
        extra = sorted(set(_sync_methods()) - set(_async_surface()))
        assert extra == []

    @pytest.mark.parametrize("method_name", _ASYNC_METHOD_NAMES)
    def test_the_signature_matches(self, method_name: str) -> None:
        async_method = _async_surface()[method_name]
        sync_method = _sync_methods()[method_name]
        async_signature = inspect.signature(async_method)
        sync_signature = inspect.signature(sync_method)

        assert list(sync_signature.parameters.values()) == list(async_signature.parameters.values())

        expected_return = async_signature.return_annotation
        if inspect.isasyncgenfunction(async_method):
            assert str(expected_return).startswith(_ASYNC_ITERATOR_PREFIX)
            expected_return = _SYNC_ITERATOR_PREFIX + str(expected_return).removeprefix(_ASYNC_ITERATOR_PREFIX)
        assert sync_signature.return_annotation == expected_return
