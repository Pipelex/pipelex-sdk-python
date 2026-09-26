"""The artifact stack — the pure walk, the bulk resolve, the bounded fetch and the download.

Ports `pipelex-sdk-js/tests/artifacts.test.ts` branch for branch, with the Python surface's own
shapes: the client is a fake satisfying the operations' `Protocol`s, and the object store is an
`httpx.MockTransport` injected at the one seam the module opens for it (`_new_storage_client`), so
every case runs at the httpx boundary and no real socket is ever opened.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from pipelex_sdk.artifact_models import (
    ArtifactItemError,
    ArtifactLocation,
    ArtifactScope,
    BulkResolvedStorageUrls,
    DownloadArtifactsOptions,
    DownloadedArtifact,
    FetchArtifactOptions,
    ResolvedArtifact,
)
from pipelex_sdk.artifacts import (
    artifact_filename,
    collect_artifacts,
    download_artifacts,
    fetch_artifact,
    is_storage_reference,
    locate_artifacts,
    resolve_artifacts,
)
from pipelex_sdk.error_models import RunErrorReport
from pipelex_sdk.errors import (
    ApiResponseError,
    ApiUnreachableError,
    ArtifactAuthenticationError,
    ArtifactFetchError,
    ArtifactOperationError,
    FieldNotIncludedError,
    RunFailedError,
    RunStillRunningError,
    ScopeUnavailableError,
)
from pipelex_sdk.runs import RunResultCompleted, RunResultFailed, RunResultRunning, RunResults, RunStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

    from pipelex_sdk.client import PipelexAPIClient
    from pipelex_sdk.runs import RunResultState
    from tests.unit.conftest import ResponseBuilder, SendPatcher

_RUN_ID = "run-01J"
_URI_PNG = "pipelex-storage://org_1/runs/01J/outputs/illustration.png"
_URI_PDF = "pipelex-storage://org_1/runs/01J/outputs/report.pdf"
_URI_INPUT = "pipelex-storage://org_1/uploads/brief.pdf"
#: A key whose last segment carries no extension, so the content type decides it.
_URI_BARE = "pipelex-storage://org_1/runs/01J/outputs/report"
_STORE = "https://store.example.com"
_PDF_BYTES = b"%PDF-1.4 tiny"
_PNG_BYTES = b"\x89PNG tiny"


# ── Fakes and builders ───────────────────────────────────────────────


class _FakeClient:
    """The two client methods the artifact operations call, scripted per test and recorded."""

    def __init__(
        self,
        *,
        resolve: Callable[[list[str]], BulkResolvedStorageUrls] | None = None,
        run_result: RunResultState | None = None,
    ) -> None:
        self._resolve = resolve
        self._run_result = run_result
        self.resolve_calls: list[list[str]] = []
        self.run_result_calls: list[str] = []

    async def resolve_storage_urls_bulk(self, uris: list[str]) -> BulkResolvedStorageUrls:
        self.resolve_calls.append(list(uris))
        if self._resolve is None:
            msg = "this test did not script a resolve answer"
            raise AssertionError(msg)
        return self._resolve(list(uris))

    async def get_run_result(self, run_id: str) -> RunResultState:
        self.run_result_calls.append(run_id)
        if self._run_result is None:
            msg = "this test did not script a run result"
            raise AssertionError(msg)
        return self._run_result


def _resolved(
    uri: str, *, url: str | None = None, expires_in_seconds: float = 900.0, content_type: str | None = "application/pdf"
) -> ResolvedArtifact:
    """One resolved item, its link live for `expires_in_seconds` from now."""
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat().replace("+00:00", "Z")
    return ResolvedArtifact(
        uri=uri, url=url or f"{_STORE}/{uri.rsplit('/', maxsplit=1)[-1]}?sig=fresh", expires_at=expires_at, content_type=content_type
    )


def _refused(uri: str, *, code: str = "forbidden", detail: str = "Another organization owns this reference.") -> ResolvedArtifact:
    """One refused item — the three link fields null, the verdict on `error`."""
    return ResolvedArtifact(uri=uri, url=None, expires_at=None, content_type=None, error=ArtifactItemError(code=code, detail=detail))


def _answer(*items: ResolvedArtifact) -> BulkResolvedStorageUrls:
    return BulkResolvedStorageUrls(items=list(items))


def _resolver(*items: ResolvedArtifact) -> Callable[[list[str]], BulkResolvedStorageUrls]:
    """A resolve script answering one item per requested reference, matched by uri."""
    by_uri = {item.uri: item for item in items}

    def _resolve(uris: list[str]) -> BulkResolvedStorageUrls:
        return _answer(*[by_uri[uri] for uri in uris])

    return _resolve


def _api_error(status: int) -> ApiResponseError:
    return ApiResponseError(
        f"API POST /v1/resolve-storage-url/bulk failed ({status})",
        api_url=_STORE,
        status=status,
        status_text="Forbidden",
        response_body="{}",
    )


def _patch_storage(mocker: MockerFixture, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Route every object-store fetch through a mock transport, and record the requests it saw."""
    seen: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_recording), timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)

    mocker.patch("pipelex_sdk.artifacts._new_storage_client", _factory)
    return seen


def _serving(payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """A store that answers every link with the same body, `Content-Length` declared by httpx."""

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers=headers or {"content-type": "application/pdf"})

    return _handler


def _streamed(chunks: list[bytes], *, delay_seconds: float = 0.0, headers: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """A store that answers with a chunked body and therefore declares no length."""

    def _handler(_: httpx.Request) -> httpx.Response:
        async def _body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
                yield chunk

        return httpx.Response(200, content=_body(), headers=headers or {"content-type": "application/pdf"})

    return _handler


#: Says a test wants the `working_memory` key left out of the body altogether, which is what the
#: platform does for a key the results read did not carry — distinct from relaying it as null.
_ABSENT_KEY = object()


def _results(main_stuff: Any, *, working_memory: Any = _ABSENT_KEY, run_id: str = _RUN_ID) -> RunResults:
    """A `RunResults` whose `working_memory` key is present only when the test says so."""
    body: dict[str, Any] = {"pipeline_run_id": run_id, "main_stuff": main_stuff}
    if working_memory is not _ABSENT_KEY:
        body["working_memory"] = working_memory
    return RunResults.model_validate(body)


def _at(path: str, uri: str = _URI_PNG) -> ArtifactLocation:
    """A location at one path, for the naming rule's tests."""
    return ArtifactLocation(uri=uri, found_at=[path])


def _content(uri: str) -> dict[str, Any]:
    """A produced file as the runtime serializes it: the durable reference beside an expiring link."""
    return {"url": uri, "public_url": f"{_STORE}/signed-by-the-runtime?sig=stale"}


class TestArtifacts:
    # ── locate_artifacts / collect_artifacts ─────────────────────────

    def test_locates_every_reference_once_in_discovery_order_with_every_path_it_sits_at(self) -> None:
        walked = {
            "image": _content(_URI_PNG),
            "pages": [{"url": _URI_PNG}, {"deeper": {"url": _URI_PDF}}, _URI_INPUT],
            "text": "not a reference",
            "count": 3,
            "nothing": None,
        }
        assert locate_artifacts(walked) == [
            ArtifactLocation(uri=_URI_PNG, found_at=["$.image.url", "$.pages[0].url"]),
            ArtifactLocation(uri=_URI_PDF, found_at=["$.pages[1].deeper.url"]),
            ArtifactLocation(uri=_URI_INPUT, found_at=["$.pages[2]"]),
        ]
        assert collect_artifacts(walked) == [location.uri for location in locate_artifacts(walked)]

    @pytest.mark.parametrize(
        ("walked", "expected"),
        [
            (_URI_PNG, "$"),
            ({"url": _URI_PNG}, "$.url"),
            ({"items": [{"url": _URI_PNG}]}, "$.items[0].url"),
            ([[_URI_PNG]], "$[0][0]"),
        ],
    )
    def test_roots_a_path_at_the_walked_value_itself(self, walked: Any, expected: str) -> None:
        assert locate_artifacts(walked) == [ArtifactLocation(uri=_URI_PNG, found_at=[expected])]

    def test_writes_an_identifier_key_dotted_and_any_other_as_a_json_string_in_brackets(self) -> None:
        keys = ["_ok", "a key", "2nd", 'say "hi"\\', "", "café 📷", "line\nbreak", "\u2028", "\ud800"]
        walked = {key: f"pipelex-storage://org_1/{index_key}.png" for index_key, key in enumerate(keys)}

        # `JSON.stringify`'s escaping, so a path reads the same from the JS twin: the quote, the
        # backslash, the control characters and a lone surrogate are escaped, and nothing else is.
        assert [location.found_at[0] for location in locate_artifacts(walked)] == [
            "$._ok",
            '$["a key"]',
            '$["2nd"]',
            '$["say \\"hi\\"\\\\"]',
            '$[""]',
            '$["café 📷"]',
            '$["line\\nbreak"]',
            '$["\u2028"]',
            '$["\\ud800"]',
        ]

    def test_walks_a_pydantic_model_by_its_dumped_keys(self) -> None:
        assert locate_artifacts(_results({"picture": _content(_URI_PNG)})) == [
            ArtifactLocation(uri=_URI_PNG, found_at=["$.main_stuff.picture.url"]),
        ]

    def test_reads_a_key_that_is_not_a_string_as_the_string_it_prints_as(self) -> None:
        assert locate_artifacts({7: _URI_PNG}) == [ArtifactLocation(uri=_URI_PNG, found_at=['$["7"]'])]

    def test_collects_every_reference_once_in_discovery_order(self) -> None:
        walked = {
            "picture": _content(_URI_PNG),
            "items": [{"doc": _content(_URI_PDF)}, {"doc": _content(_URI_PNG)}],
            "note": "no file here",
        }
        assert collect_artifacts(walked) == [_URI_PNG, _URI_PDF]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (f"see {_URI_PNG} for the picture", []),
            ("pipelex-storage://", []),
            ("s3://org_1/runs/01J/outputs/x.png", []),
            (_URI_PNG, [_URI_PNG]),
        ],
    )
    def test_counts_a_string_only_when_it_is_a_reference(self, value: str, expected: list[str]) -> None:
        assert collect_artifacts(value) == expected
        assert [location.uri for location in locate_artifacts(value)] == expected
        assert is_storage_reference(value) is (expected != [])

    def test_ignores_values_that_carry_no_reference(self) -> None:
        for value in (None, 42, {"a": [1, 2.5, True, None]}, {"note": f"Saved as {_URI_PDF}."}):
            assert collect_artifacts(value) == []
            assert locate_artifacts(value) == []

    def test_walks_a_pydantic_model_as_well_as_a_parsed_body(self) -> None:
        results = _results({"picture": _content(_URI_PNG)})
        assert collect_artifacts(results) == [_URI_PNG]

    # ── artifact_filename ────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("path", "scope", "expected"),
        [
            # The walked value itself, or its `url`, takes the scope's name.
            ("$", ArtifactScope.MAIN_STUFF, "main_stuff.png"),
            ("$.url", ArtifactScope.MAIN_STUFF, "main_stuff.png"),
            ("$.url", ArtifactScope.WORKING_MEMORY, "working_memory.png"),
            # A list member is named after the envelope and its index.
            ("$.items[0].url", ArtifactScope.MAIN_STUFF, "items-0.png"),
            ("$[2].url", ArtifactScope.MAIN_STUFF, "2.png"),
            # A nested field is named after its whole path.
            ("$.rooms[3].staged_photo.url", ArtifactScope.MAIN_STUFF, "rooms-3-staged_photo.png"),
            # Only a final `url` key is dropped: another key, or a `url` key that is not final, is kept.
            ("$.photo.src", ArtifactScope.MAIN_STUFF, "photo-src.png"),
            ("$.photo.URL", ArtifactScope.MAIN_STUFF, "photo-URL.png"),
            ("$.url.original", ArtifactScope.MAIN_STUFF, "url-original.png"),
            ("$.links.url[0]", ArtifactScope.MAIN_STUFF, "links-url-0.png"),
            ('$["url"]', ArtifactScope.MAIN_STUFF, "main_stuff.png"),
            # A key that is not an identifier is reduced to `[A-Za-z0-9_]`, dashes and dots included.
            ('$["a key"].url', ArtifactScope.MAIN_STUFF, "a_key.png"),
            ('$["staged-photo.v2"].url', ArtifactScope.MAIN_STUFF, "staged_photo_v2.png"),
            ('$["café 📷"].url', ArtifactScope.MAIN_STUFF, "caf___.png"),
            ('$["2nd"]', ArtifactScope.MAIN_STUFF, "2nd.png"),
            ('$[""].url', ArtifactScope.MAIN_STUFF, "main_stuff.png"),
        ],
    )
    def test_names_a_file_after_the_field_it_fills(self, path: str, scope: ArtifactScope, expected: str) -> None:
        assert artifact_filename(_at(path), "image/png", scope) == expected

    @pytest.mark.parametrize(
        "path",
        ['$["../../etc/passwd"]', '$[".."].url', '$["."]', '$[".env"]', '$["a\\u0000b\\nc"]', '$["C:\\\\Windows"]'],
    )
    def test_cannot_name_anything_outside_the_directory_nor_a_hidden_file(self, path: str) -> None:
        name = artifact_filename(_at(path, "pipelex-storage://x/.."), None, ArtifactScope.MAIN_STUFF)
        assert re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9]+)?", name)

    def test_reduces_a_traversal_key_to_one_plain_name(self) -> None:
        assert artifact_filename(_at('$["../../etc/passwd"]', _URI_BARE), None, ArtifactScope.MAIN_STUFF) == "______etc_passwd"

    def test_keeps_the_tail_of_a_long_name_dropping_whole_leading_segments_first(self) -> None:
        segments = [f"segment_{index_segment:02d}" for index_segment in range(20)]
        name = artifact_filename(_at(f"$.{'.'.join(segments)}.url"), None, ArtifactScope.MAIN_STUFF)

        assert len(name) <= 128
        assert name == f"{'-'.join(segments[9:])}.png"

    def test_cuts_a_single_segment_still_too_long_keeping_the_extension(self) -> None:
        long_key = "a" * 300
        assert artifact_filename(_at(f"$.short.{long_key}.url"), None, ArtifactScope.MAIN_STUFF) == f"{'a' * 124}.png"
        assert artifact_filename(_at(f"$.{long_key}", _URI_BARE), None, ArtifactScope.MAIN_STUFF) == "a" * 128

    @pytest.mark.parametrize(
        ("uri", "content_type", "expected"),
        [
            # The storage key's extension first, then the content type's, else none.
            (_URI_PNG, "application/pdf", "cover.png"),
            (_URI_BARE, "application/pdf", "cover.pdf"),
            (_URI_BARE, "image/png; charset=binary", "cover.png"),
            (_URI_BARE, "application/x-unknown", "cover"),
            (_URI_BARE, None, "cover"),
            # The key's last segment, percent-decoded, its query and fragment dropped.
            ("pipelex-storage://x/hello%20world.pdf?token=1#frag", None, "cover.pdf"),
            ("pipelex-storage://x/photo%2Epng", None, "cover.png"),
            ("pipelex-storage://a/..\\..\\secret.txt", None, "cover.txt"),
            # Reduced to `[A-Za-z0-9]`, never a leading dot, never empty, never long.
            ("pipelex-storage://x/photo.P-N_G", None, "cover.PNG"),
            ("pipelex-storage://x/.env", None, "cover"),
            ("pipelex-storage://x/report.", None, "cover"),
            (f"pipelex-storage://x/stem.{'z' * 300}", None, "cover"),
            (f"pipelex-storage://x/stem.{'z' * 300}", "text/csv", "cover.csv"),
            # Where `decodeURIComponent` would throw — a stray `%`, bytes that are not UTF-8 — the
            # segment is read as typed, so both SDKs find the same extension.
            ("pipelex-storage://x/bad%zz.pdf", None, "cover.pdf"),
            ("pipelex-storage://x/a%2Eb%zz", None, "cover"),
            ("pipelex-storage://x/photo.p%FFng", None, "cover.pFFng"),
        ],
    )
    def test_takes_the_extension_from_the_storage_key_then_the_content_type(self, uri: str, content_type: str | None, expected: str) -> None:
        assert artifact_filename(_at("$.cover.url", uri), content_type, ArtifactScope.MAIN_STUFF) == expected

    @pytest.mark.parametrize(
        ("path", "uri", "expected"),
        [
            ("$.aux.url", _URI_PNG, "aux_.png"),
            ("$.NUL", _URI_PNG, "NUL_.png"),
            ("$.Com1.url", _URI_PNG, "Com1_.png"),
            ("$.lpt9", _URI_BARE, "lpt9_"),
            ('$[""].con.url', _URI_PNG, "con_.png"),
            # The cap drops every leading segment and leaves the device name alone.
            (f"$.{'x' * 130}.prn.url", _URI_PNG, "prn_.png"),
            # Only the whole stem is a device name: a join or a longer word is not one.
            ("$.a.nul.url", _URI_PNG, "a-nul.png"),
            ("$.auxiliary.url", _URI_PNG, "auxiliary.png"),
            ("$.com10.url", _URI_PNG, "com10.png"),
        ],
    )
    def test_suffixes_a_stem_windows_reserves_for_a_device(self, path: str, uri: str, expected: str) -> None:
        assert artifact_filename(_at(path, uri), None, ArtifactScope.MAIN_STUFF) == expected

    @pytest.mark.parametrize(
        "location",
        [
            ArtifactLocation(uri=_URI_PNG, found_at=[]),
            _at("rooms.url"),
            _at("$.rooms["),
            _at("$[-1]"),
            _at('$["unterminated]'),
            _at('$["\\x"]'),
            _at("$.a b"),
            # The old signature's bare uri.
            cast("ArtifactLocation", _URI_PNG),
        ],
    )
    def test_refuses_a_location_whose_first_path_is_not_in_the_walks_notation(self, location: ArtifactLocation) -> None:
        with pytest.raises(ArtifactOperationError):
            artifact_filename(location, None, ArtifactScope.MAIN_STUFF)

    @pytest.mark.parametrize("scope", ["other", 0])
    def test_refuses_an_unknown_scope(self, scope: object) -> None:
        with pytest.raises(ArtifactOperationError, match='"scope" must be'):
            artifact_filename(_at("$.url"), None, cast("ArtifactScope", scope))

    def test_names_every_location_the_walk_writes_through_the_round_trip_of_its_notation(self) -> None:
        walked = {
            'say "hi"\\': {"url": "pipelex-storage://org_1/a.png"},
            "line\nbreak": {"url": "pipelex-storage://org_1/b.png"},
            "\u2028": {"url": "pipelex-storage://org_1/c.png"},
            "[0]": {"url": "pipelex-storage://org_1/d.png"},
            "\ud800": {"url": "pipelex-storage://org_1/e.png"},
        }
        names = [artifact_filename(location, None, ArtifactScope.MAIN_STUFF) for location in locate_artifacts(walked)]
        assert names == ["say__hi__.png", "line_break.png", "_.png", "_0_.png", "_.png"]

    def test_takes_a_verdict_item_as_its_location(self) -> None:
        item = DownloadedArtifact(uri=_URI_BARE, found_at=["$.report.url"], content_type="application/pdf", error=None)
        assert artifact_filename(item, item.content_type, ArtifactScope.MAIN_STUFF) == "report.pdf"

    # ── resolve_artifacts ────────────────────────────────────────────

    def test_resolves_a_list_within_the_bound_in_one_call_and_keeps_request_order(self) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PNG), _refused(_URI_PDF)))
        items = asyncio.run(resolve_artifacts(client, [_URI_PNG, _URI_PDF, _URI_PNG]))

        assert client.resolve_calls == [[_URI_PNG, _URI_PDF, _URI_PNG]]
        assert [item.uri for item in items] == [_URI_PNG, _URI_PDF, _URI_PNG]
        assert items[0].error is None
        assert items[1].error is not None
        assert items[1].error.code == "forbidden"
        assert items[1].url is None

    def test_chunks_a_longer_list_at_the_routes_bound(self) -> None:
        uris = [f"pipelex-storage://org_1/f{index}.pdf" for index in range(250)]
        client = _FakeClient(resolve=lambda chunk: _answer(*[_resolved(uri) for uri in chunk]))
        items = asyncio.run(resolve_artifacts(client, uris))

        assert [len(call) for call in client.resolve_calls] == [100, 100, 50]
        assert [item.uri for item in items] == uris

    def test_makes_no_request_for_an_empty_list(self) -> None:
        client = _FakeClient()
        assert asyncio.run(resolve_artifacts(client, [])) == []
        assert client.resolve_calls == []

    def test_refuses_a_malformed_answer_rather_than_misattributing_verdicts(self) -> None:
        client = _FakeClient(resolve=lambda _: _answer(_resolved(_URI_PNG)))
        with pytest.raises(ArtifactOperationError, match="1 item"):
            asyncio.run(resolve_artifacts(client, [_URI_PNG, _URI_PDF]))

    def test_lets_a_whole_request_refusal_propagate_unchanged(self) -> None:
        def _refuse(_: list[str]) -> BulkResolvedStorageUrls:
            raise _api_error(404)

        client = _FakeClient(resolve=_refuse)
        with pytest.raises(ApiResponseError) as caught:
            asyncio.run(resolve_artifacts(client, [_URI_PNG]))
        assert caught.value.status == 404

    # ── fetch_artifact ───────────────────────────────────────────────

    def test_fetches_a_fresh_link_with_no_credentials_and_relays_the_stores_response(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        seen = _patch_storage(mocker, _serving(_PDF_BYTES, headers={"content-type": "application/pdf", "x-store": "yes"}))

        async def _read() -> tuple[int, bytes, str | None]:
            async with fetch_artifact(client, _URI_PDF) as stream:
                return stream.status_code, await stream.read(), stream.headers.get("x-store")

        status, body, store_header = asyncio.run(_read())

        assert status == 200
        assert body == _PDF_BYTES
        assert store_header == "yes"
        assert len(seen) == 1
        assert "authorization" not in seen[0].headers
        assert str(seen[0].url).startswith(f"{_STORE}/report.pdf")
        # The link came from the route, never from the content's embedded `public_url`.
        assert "sig=fresh" in str(seen[0].url)

    def test_raises_the_routes_per_reference_refusal_as_a_typed_fetch_error(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_refused(_URI_PDF, code="invalid_storage_uri", detail="Not a reference.")))
        _patch_storage(mocker, _serving(_PDF_BYTES))

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF):
                pass

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "invalid_storage_uri"
        assert caught.value.uri == _URI_PDF

    def test_refuses_a_plain_http_link_by_default_and_accepts_it_on_request(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF, url="http://localhost:9000/report.pdf")))
        seen = _patch_storage(mocker, _serving(_PDF_BYTES))

        async def _read(options: FetchArtifactOptions | None) -> bytes:
            async with fetch_artifact(client, _URI_PDF, options) as stream:
                return await stream.read()

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read(None))
        assert caught.value.code == "plain_http_refused"
        assert seen == []

        assert asyncio.run(_read(FetchArtifactOptions(allow_http=True))) == _PDF_BYTES
        assert len(seen) == 1

    @pytest.mark.parametrize("url", ["ftp://store/x.pdf", "not-a-url", "https://user:pass@store/x.pdf", "https://[::1", "https://host:abc/file"])
    def test_refuses_an_unusable_link_before_any_request(self, mocker: MockerFixture, url: str) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF, url=url)))
        seen = _patch_storage(mocker, _serving(_PDF_BYTES))

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF):
                pass

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "unsupported_url"
        assert seen == []

    @pytest.mark.parametrize(
        ("status", "code"),
        [(302, "redirect_refused"), (401, "store_refused"), (403, "store_refused"), (404, "not_found"), (410, "not_found"), (500, "store_error")],
    )
    def test_maps_the_stores_statuses_onto_the_fetch_codes(self, mocker: MockerFixture, status: int, code: str) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(b"", status=status, headers={"location": f"{_STORE}/elsewhere"}))

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF):
                pass

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == code
        assert caught.value.status == status

    def test_refuses_a_declared_oversize_without_reading_the_body(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(b"x" * 64))

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF, FetchArtifactOptions(max_bytes=32)):
                pass

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "too_large"

    def test_cuts_a_body_that_crosses_the_cap_mid_stream(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _streamed([b"a" * 16, b"b" * 16, b"c" * 16]))

        async def _read() -> list[bytes]:
            chunks: list[bytes] = []
            async with fetch_artifact(client, _URI_PDF, FetchArtifactOptions(max_bytes=20)) as stream:
                async for chunk in stream.aiter_bytes(chunk_size=16):
                    chunks.append(chunk)
            return chunks

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "too_large"

    def test_times_out_an_exchange_that_outlives_its_budget(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _streamed([b"a", b"b"], delay_seconds=0.2))

        async def _read() -> bytes:
            async with fetch_artifact(client, _URI_PDF, FetchArtifactOptions(timeout_seconds=0.05)) as stream:
                return await stream.read()

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "timeout"

    def test_reports_a_transport_failure_as_a_network_fault(self, mocker: MockerFixture) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))

        def _broken(request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg, request=request)

        _patch_storage(mocker, _broken)

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF):
                pass

        with pytest.raises(ArtifactFetchError) as caught:
            asyncio.run(_read())
        assert caught.value.code == "network"

    @pytest.mark.parametrize(("name", "value"), [("max_bytes", 0), ("timeout_seconds", -1)])
    def test_refuses_nonsense_bounds_before_resolving_anything(self, name: str, value: float) -> None:
        client = _FakeClient()

        async def _read() -> None:
            async with fetch_artifact(client, _URI_PDF, FetchArtifactOptions.model_validate({name: value})):
                pass

        with pytest.raises(ArtifactOperationError, match=name):
            asyncio.run(_read())
        assert client.resolve_calls == []

    def test_drops_a_content_encoding_httpx_already_decoded_with_its_length(self, mocker: MockerFixture) -> None:
        packed = gzip.compress(_PDF_BYTES)
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(packed, headers={"content-type": "application/pdf", "content-encoding": "gzip"}))

        async def _read() -> tuple[bytes, httpx.Headers]:
            async with fetch_artifact(client, _URI_PDF) as stream:
                return await stream.read(), stream.headers

        body, headers = asyncio.run(_read())
        assert body == _PDF_BYTES
        assert "content-encoding" not in headers
        assert "content-length" not in headers

    @pytest.mark.parametrize("coding", ["exi", "x-gzip"])
    def test_keeps_an_encoding_httpx_did_not_decode_beside_its_body(self, mocker: MockerFixture, coding: str) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(_PDF_BYTES, headers={"content-type": "application/pdf", "content-encoding": coding}))

        async def _read() -> httpx.Headers:
            async with fetch_artifact(client, _URI_PDF) as stream:
                return stream.headers

        headers = asyncio.run(_read())
        assert headers["content-encoding"] == coding
        assert headers["content-length"] == str(len(_PDF_BYTES))

    # ── download_artifacts ───────────────────────────────────────────

    def test_takes_exactly_one_of_run_id_or_results(self, tmp_path: Path) -> None:
        client = _FakeClient()
        results = _results({"picture": _content(_URI_PNG)})

        with pytest.raises(ArtifactOperationError, match="exactly one"):
            asyncio.run(download_artifacts(client, dir_path=tmp_path))
        with pytest.raises(ArtifactOperationError, match="exactly one"):
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=_RUN_ID, results=results))
        # An empty run id names nothing, so it is neither selector.
        with pytest.raises(ArtifactOperationError, match="exactly one"):
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=""))

    @pytest.mark.parametrize(("name", "value"), [("concurrency", 0), ("max_total_bytes", 0), ("max_bytes", -1), ("timeout_seconds", 0)])
    def test_validates_the_download_bounds(self, tmp_path: Path, name: str, value: float) -> None:
        client = _FakeClient()
        with pytest.raises(ArtifactOperationError, match=name):
            asyncio.run(
                download_artifacts(
                    client,
                    dir_path=tmp_path,
                    results=_results({"picture": _content(_URI_PNG)}),
                    options=DownloadArtifactsOptions.model_validate({name: value}),
                )
            )

    def test_raises_run_still_running_with_the_retry_hint(self, tmp_path: Path) -> None:
        client = _FakeClient(run_result=RunResultRunning(pipeline_run_id=_RUN_ID, retry_after_seconds=7))
        with pytest.raises(RunStillRunningError, match="retry in 7s"):
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=_RUN_ID))

    def test_raises_run_failed_for_a_run_that_ended_without_a_result(self, tmp_path: Path) -> None:
        report = RunErrorReport(error_type="SandboxProvisioningError", message="Snapshot is building", error_domain="runtime", retryable=True)
        failed = RunResultFailed(pipeline_run_id=_RUN_ID, status=RunStatus.FAILED, message="the run failed", error=report)
        client = _FakeClient(run_result=failed)
        with pytest.raises(RunFailedError) as caught:
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=_RUN_ID))
        assert caught.value.status == RunStatus.FAILED
        assert caught.value.run_id == _RUN_ID
        assert caught.value.error == report

    def test_raises_field_not_included_when_the_scope_key_was_never_relayed(self, tmp_path: Path) -> None:
        client = _FakeClient()
        with pytest.raises(FieldNotIncludedError) as caught:
            asyncio.run(
                download_artifacts(
                    client,
                    dir_path=tmp_path,
                    results=_results({"picture": _content(_URI_PNG)}),
                    options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),
                )
            )
        assert caught.value.field_name == "working_memory"

    def test_raises_scope_unavailable_when_the_key_was_relayed_as_null(self, tmp_path: Path) -> None:
        client = _FakeClient()
        with pytest.raises(ScopeUnavailableError) as caught:
            asyncio.run(
                download_artifacts(
                    client,
                    dir_path=tmp_path,
                    results=_results({"picture": _content(_URI_PNG)}, working_memory=None),
                    options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),
                )
            )
        assert caught.value.scope == ArtifactScope.WORKING_MEMORY
        assert caught.value.run_id == _RUN_ID

    def test_answers_an_empty_walk_over_a_present_scope_as_a_verdict(self, tmp_path: Path) -> None:
        client = _FakeClient()
        target = tmp_path / "out"
        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results({"text": "no file here"})))

        assert verdict.scope == ArtifactScope.MAIN_STUFF
        assert verdict.artifacts == []
        assert verdict.saved_paths == []
        assert verdict.all_saved is True
        assert client.resolve_calls == []
        assert not target.exists()

    def test_reads_by_run_id_resolves_in_one_bulk_call_and_saves_every_file(self, mocker: MockerFixture, tmp_path: Path) -> None:
        results = _results({"items": [_content(_URI_PNG), _content(_URI_PDF)]})
        client = _FakeClient(
            resolve=_resolver(_resolved(_URI_PNG, content_type="image/png"), _resolved(_URI_PDF)),
            run_result=RunResultCompleted(pipeline_run_id=_RUN_ID, result=results),
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            payload = _PNG_BYTES if request.url.path.endswith(".png") else _PDF_BYTES
            return httpx.Response(200, content=payload)

        _patch_storage(mocker, _handler)
        target = tmp_path / "out"
        verdict = asyncio.run(download_artifacts(client, dir_path=target, run_id=_RUN_ID))

        assert client.run_result_calls == [_RUN_ID]
        assert client.resolve_calls == [[_URI_PNG, _URI_PDF]]
        assert verdict.all_saved is True
        assert [artifact.uri for artifact in verdict.artifacts] == [_URI_PNG, _URI_PDF]
        assert [artifact.size for artifact in verdict.artifacts] == [len(_PNG_BYTES), len(_PDF_BYTES)]
        assert [artifact.content_type for artifact in verdict.artifacts] == ["image/png", "application/pdf"]
        assert [artifact.found_at for artifact in verdict.artifacts] == [["$.items[0].url"], ["$.items[1].url"]]
        assert verdict.saved_paths == [str(target / "items-0.png"), str(target / "items-1.pdf")]
        assert (target / "items-0.png").read_bytes() == _PNG_BYTES
        assert (target / "items-1.pdf").read_bytes() == _PDF_BYTES

    def test_takes_results_in_hand_without_re_reading_and_creates_the_directory(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        target = tmp_path / "nested" / "out"
        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results({"doc": _content(_URI_PDF)})))

        assert client.run_result_calls == []
        assert verdict.all_saved is True
        assert target.is_dir()

    def test_walks_working_memory_when_asked_echoed_inputs_included(self, mocker: MockerFixture, tmp_path: Path) -> None:
        results = _results(
            {"text": "round trip"},
            working_memory={
                "root": {
                    "doc": {"concept": "native.PDF", "content": _content(_URI_PDF)},
                    "picture": {"concept": "native.Image", "content": _content(_URI_PNG)},
                },
                "aliases": {},
            },
        )
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF), _resolved(_URI_PNG, content_type="image/png")))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        target = tmp_path / "out"
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=results,
                options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),
            )
        )

        assert verdict.scope == ArtifactScope.WORKING_MEMORY
        assert [artifact.uri for artifact in verdict.artifacts] == [_URI_PDF, _URI_PNG]
        assert [artifact.found_at for artifact in verdict.artifacts] == [["$.root.doc.content.url"], ["$.root.picture.content.url"]]
        assert verdict.all_saved is True
        assert verdict.saved_paths == [str(target / "root-doc-content.pdf"), str(target / "root-picture-content.png")]

    def test_never_overwrites_a_name_already_on_disk(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        target = tmp_path / "out"
        target.mkdir()
        (target / "main_stuff.pdf").write_bytes(b"do not touch me")
        (target / "main_stuff-1.pdf").write_bytes(b"nor me")

        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results(_content(_URI_PDF))))

        assert verdict.saved_paths == [str(target / "main_stuff-2.pdf")]
        assert (target / "main_stuff.pdf").read_bytes() == b"do not touch me"
        assert (target / "main_stuff-1.pdf").read_bytes() == b"nor me"

    def test_names_each_file_after_the_field_it_fills_and_reports_every_path_on_both_arms(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(
            resolve=_resolver(
                _resolved(_URI_INPUT),
                _resolved(_URI_PNG, content_type="image/png"),
                _refused(_URI_PDF, detail="another organization"),
            )
        )
        _patch_storage(mocker, _serving(_PNG_BYTES))
        target = tmp_path / "out"
        main_stuff = {
            "rooms": [
                {"original_photo": _content(_URI_INPUT), "staged_photo": _content(_URI_PNG)},
                {"original_photo": _content(_URI_PDF)},
            ],
            "cover": _content(_URI_PNG),
        }

        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results(main_stuff)))

        assert [(artifact.uri, artifact.found_at, artifact.path) for artifact in verdict.artifacts] == [
            (_URI_INPUT, ["$.rooms[0].original_photo.url"], str(target / "rooms-0-original_photo.pdf")),
            (_URI_PNG, ["$.rooms[0].staged_photo.url", "$.cover.url"], str(target / "rooms-0-staged_photo.png")),
            (_URI_PDF, ["$.rooms[1].original_photo.url"], None),
        ]
        assert sorted(entry.name for entry in target.iterdir()) == ["rooms-0-original_photo.pdf", "rooms-0-staged_photo.png"]

    def test_tells_apart_two_paths_that_reduce_to_one_name_with_the_suffix_rule(self, mocker: MockerFixture, tmp_path: Path) -> None:
        second = "pipelex-storage://org_1/runs/01J/outputs/second.png"
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PNG), _resolved(second)))
        _patch_storage(mocker, _serving(_PNG_BYTES))
        target = tmp_path / "out"

        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=_results({"staged photo": _content(_URI_PNG), "staged-photo": _content(second)}),
                options=DownloadArtifactsOptions(concurrency=1),
            )
        )

        assert verdict.saved_paths == [str(target / "staged_photo.png"), str(target / "staged_photo-1.png")]

    def test_saves_every_file_under_the_name_artifact_filename_gives_its_location(self, mocker: MockerFixture, tmp_path: Path) -> None:
        uris = ["pipelex-storage://org_1/a.png", "pipelex-storage://org_1/b", "pipelex-storage://org_1/c.pdf"]
        client = _FakeClient(resolve=_resolver(*[_resolved(uri, content_type=None) for uri in uris]))
        _patch_storage(mocker, _serving(_PNG_BYTES))
        target = tmp_path / "out"
        main_stuff = {'say "hi"\\': {"url": uris[0]}, "line\nbreak": [{"url": uris[1]}], "url": uris[2]}

        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results(main_stuff)))

        predicted = [artifact_filename(location, None, ArtifactScope.MAIN_STUFF) for location in locate_artifacts(main_stuff)]
        assert predicted == ["say__hi__.png", "line_break-0", "main_stuff.pdf"]
        assert verdict.saved_paths == [str(target / name) for name in predicted]
        # A verdict item is a location too, and names its own file.
        assert [artifact_filename(artifact, artifact.content_type, verdict.scope) for artifact in verdict.artifacts] == predicted

    def test_refuses_an_unknown_scope_before_reading_anything(self, tmp_path: Path) -> None:
        client = _FakeClient()
        # Pydantic refuses the unknown scope at construction; only an unvalidated model reaches here.
        options = DownloadArtifactsOptions.model_construct(scope=cast("ArtifactScope", "everything"))

        with pytest.raises(ArtifactOperationError, match='"scope" must be'):
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=_RUN_ID, options=options))
        assert client.run_result_calls == []

    def test_keeps_a_per_reference_refusal_as_that_items_error_beside_the_saved_ones(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_refused(_URI_PNG), _resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        verdict = asyncio.run(
            download_artifacts(client, dir_path=tmp_path / "out", results=_results({"items": [_content(_URI_PNG), _content(_URI_PDF)]}))
        )

        assert verdict.all_saved is False
        first = verdict.artifacts[0]
        assert first.error is not None
        assert first.error.code == "forbidden"
        assert first.path is None
        assert first.found_at == ["$.items[0].url"]
        assert verdict.artifacts[1].error is None
        assert verdict.saved_paths == [str(tmp_path / "out" / "items-1.pdf")]

    def test_re_resolves_a_link_that_has_expired_by_the_time_its_task_reaches_it(self, mocker: MockerFixture, tmp_path: Path) -> None:
        answers = [
            _answer(_resolved(_URI_PDF, url=f"{_STORE}/stale.pdf", expires_in_seconds=1.0)),
            _answer(_resolved(_URI_PDF, url=f"{_STORE}/fresh.pdf")),
        ]
        client = _FakeClient(resolve=lambda _: answers.pop(0))
        seen = _patch_storage(mocker, _serving(_PDF_BYTES))
        verdict = asyncio.run(download_artifacts(client, dir_path=tmp_path / "out", results=_results({"doc": _content(_URI_PDF)})))

        assert client.resolve_calls == [[_URI_PDF], [_URI_PDF]]
        assert [str(request.url) for request in seen] == [f"{_STORE}/fresh.pdf"]
        assert verdict.all_saved is True

    @pytest.mark.parametrize("failure", [_api_error(500), ApiUnreachableError("host unreachable", api_url=_STORE, code="ENOTFOUND")])
    def test_marks_an_item_whose_expired_link_cannot_be_re_resolved(self, mocker: MockerFixture, tmp_path: Path, failure: Exception) -> None:
        calls: list[int] = []

        def _resolve(uris: list[str]) -> BulkResolvedStorageUrls:
            calls.append(len(uris))
            if len(calls) == 1:
                return _answer(_resolved(_URI_PDF, expires_in_seconds=1.0))
            raise failure

        client = _FakeClient(resolve=_resolve)
        _patch_storage(mocker, _serving(_PDF_BYTES))
        verdict = asyncio.run(download_artifacts(client, dir_path=tmp_path / "out", results=_results({"doc": _content(_URI_PDF)})))

        assert verdict.all_saved is False
        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "resolve_failed"

    def test_raises_a_credential_failure_on_the_first_resolve_with_an_all_aborted_verdict(self, tmp_path: Path) -> None:
        def _refuse(_: list[str]) -> BulkResolvedStorageUrls:
            raise _api_error(403)

        client = _FakeClient(resolve=_refuse)
        with pytest.raises(ArtifactAuthenticationError) as caught:
            asyncio.run(download_artifacts(client, dir_path=tmp_path / "out", results=_results({"items": [_content(_URI_PNG), _content(_URI_PDF)]})))

        verdict = caught.value.verdict
        assert caught.value.status == 403
        assert verdict.saved_paths == []
        assert [artifact.error.code for artifact in verdict.artifacts if artifact.error is not None] == ["aborted", "aborted"]
        assert [artifact.found_at for artifact in verdict.artifacts] == [["$.items[0].url"], ["$.items[1].url"]]

    def test_raises_a_credential_failure_part_way_through_carrying_the_verdict_so_far(self, mocker: MockerFixture, tmp_path: Path) -> None:
        def _resolve(uris: list[str]) -> BulkResolvedStorageUrls:
            if len(uris) > 1:
                return _answer(_resolved(_URI_PDF), _resolved(_URI_PNG, expires_in_seconds=1.0))
            raise _api_error(401)

        client = _FakeClient(resolve=_resolve)
        _patch_storage(mocker, _serving(_PDF_BYTES))
        with pytest.raises(ArtifactAuthenticationError) as caught:
            asyncio.run(
                download_artifacts(
                    client,
                    dir_path=tmp_path / "out",
                    results=_results({"items": [_content(_URI_PDF), _content(_URI_PNG)]}),
                    options=DownloadArtifactsOptions(concurrency=1),
                )
            )

        verdict = caught.value.verdict
        assert caught.value.status == 401
        assert len(verdict.saved_paths) == 1
        assert verdict.artifacts[0].error is None
        second = verdict.artifacts[1].error
        assert second is not None
        assert second.code == "aborted"
        assert "credential failure" in second.detail

    def test_refuses_a_declared_oversize_and_leaves_nothing_behind(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(b"x" * 64))
        target = tmp_path / "out"
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=_results({"doc": _content(_URI_PDF)}),
                options=DownloadArtifactsOptions(max_bytes=32),
            )
        )

        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "too_large"
        assert list(target.iterdir()) == []

    def test_cuts_an_undeclared_oversize_mid_stream_and_unlinks_the_partial_file(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _streamed([b"a" * 16, b"b" * 16, b"c" * 16]))
        target = tmp_path / "out"
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=_results({"doc": _content(_URI_PDF)}),
                options=DownloadArtifactsOptions(max_bytes=20),
            )
        )

        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "too_large"
        assert list(target.iterdir()) == []

    def test_enforces_the_total_cap_on_each_later_item_by_its_own_size(self, mocker: MockerFixture, tmp_path: Path) -> None:
        third = "pipelex-storage://org_1/runs/01J/outputs/third.pdf"
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF), _resolved(_URI_PNG), _resolved(third)))
        _patch_storage(mocker, _serving(b"x" * 8))
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=tmp_path / "out",
                results=_results({"items": [_content(_URI_PDF), _content(_URI_PNG), _content(third)]}),
                options=DownloadArtifactsOptions(concurrency=1, max_total_bytes=10),
            )
        )

        codes = [artifact.error.code if artifact.error is not None else None for artifact in verdict.artifacts]
        assert codes == [None, "total_limit_exceeded", "total_limit_exceeded"]
        refused = verdict.artifacts[2].error
        assert refused is not None
        assert refused.detail.startswith("Saving this")
        assert len(verdict.saved_paths) == 1

    def test_a_refused_item_never_skips_a_smaller_one_that_still_fits(self, mocker: MockerFixture, tmp_path: Path) -> None:
        big = "pipelex-storage://org_1/runs/01J/outputs/big.pdf"
        small = "pipelex-storage://org_1/runs/01J/outputs/small.pdf"
        client = _FakeClient(resolve=_resolver(_resolved(big), _resolved(small)))

        def _sized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (16 if "big" in request.url.path else 2))

        _patch_storage(mocker, _sized)
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=tmp_path / "out",
                results=_results({"items": [_content(big), _content(small)]}),
                options=DownloadArtifactsOptions(concurrency=1, max_total_bytes=10),
            )
        )

        codes = [artifact.error.code if artifact.error is not None else None for artifact in verdict.artifacts]
        assert codes == ["total_limit_exceeded", None]
        assert verdict.artifacts[1].size == 2
        assert len(verdict.saved_paths) == 1

    def test_enforces_the_total_cap_mid_stream_on_an_undeclared_body(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _streamed([b"a" * 8, b"b" * 8]))
        target = tmp_path / "out"
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=_results({"doc": _content(_URI_PDF)}),
                options=DownloadArtifactsOptions(max_total_bytes=10),
            )
        )

        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "total_limit_exceeded"
        assert list(target.iterdir()) == []

    def test_keeps_a_store_refusal_as_the_items_error(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(b"", status=404))
        verdict = asyncio.run(download_artifacts(client, dir_path=tmp_path / "out", results=_results({"doc": _content(_URI_PDF)})))

        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "not_found"
        assert verdict.all_saved is False

    def test_reports_a_file_that_cannot_be_created_as_write_failed(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        mocker.patch("pipelex_sdk.artifacts._open_unique_file", side_effect=OSError("no space left on device"))
        target = tmp_path / "out"
        verdict = asyncio.run(download_artifacts(client, dir_path=target, results=_results({"doc": _content(_URI_PDF)})))

        error = verdict.artifacts[0].error
        assert error is not None
        assert error.code == "write_failed"
        assert list(target.iterdir()) == []

    def test_raises_for_a_directory_that_cannot_be_created(self, tmp_path: Path) -> None:
        blocked = tmp_path / "a-file"
        blocked.write_bytes(b"not a directory")
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        with pytest.raises(ArtifactOperationError, match="cannot be created or used"):
            asyncio.run(download_artifacts(client, dir_path=blocked, results=_results({"doc": _content(_URI_PDF)})))

    def test_lets_a_deployment_without_the_bulk_route_surface_as_the_transport_error(self, tmp_path: Path) -> None:
        def _refuse(_: list[str]) -> BulkResolvedStorageUrls:
            raise _api_error(404)

        client = _FakeClient(resolve=_refuse)
        with pytest.raises(ApiResponseError) as caught:
            asyncio.run(download_artifacts(client, dir_path=tmp_path / "out", results=_results({"doc": _content(_URI_PDF)})))
        assert caught.value.status == 404

    def test_holds_the_concurrency_bound_across_the_pipeline(self, mocker: MockerFixture, tmp_path: Path) -> None:
        uris = [f"pipelex-storage://org_1/runs/01J/outputs/f{index}.pdf" for index in range(6)]
        client = _FakeClient(resolve=_resolver(*[_resolved(uri) for uri in uris]))
        live = {"now": 0, "peak": 0}

        def _handler(_: httpx.Request) -> httpx.Response:
            async def _body() -> AsyncIterator[bytes]:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
                await asyncio.sleep(0.02)
                yield _PDF_BYTES
                live["now"] -= 1

            return httpx.Response(200, content=_body())

        _patch_storage(mocker, _handler)
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=tmp_path / "out",
                results=_results({"items": [_content(uri) for uri in uris]}),
                options=DownloadArtifactsOptions(concurrency=2),
            )
        )

        assert verdict.all_saved is True
        assert live["peak"] == 2

    def test_unlinks_the_partial_file_when_the_download_is_cancelled(self, mocker: MockerFixture, tmp_path: Path) -> None:
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF)))
        _patch_storage(mocker, _streamed([b"a" * 8] * 20, delay_seconds=0.05))
        target = tmp_path / "out"

        async def _cancel_midway() -> bool:
            task = asyncio.create_task(download_artifacts(client, dir_path=target, results=_results({"doc": _content(_URI_PDF)})))
            await asyncio.sleep(0.12)
            assert (target / "doc.pdf").exists()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return True
            return False

        assert asyncio.run(_cancel_midway()) is True
        assert list(target.iterdir()) == []

    # ── the client's own methods ─────────────────────────────────────

    def test_the_client_posts_the_bulk_route_and_serves_the_whole_stack(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        item = _resolved(_URI_PDF).model_dump()
        spy = patch_send(api_client, wire_response(200, json_body={"items": [item]}))
        seen = _patch_storage(mocker, _serving(_PDF_BYTES))
        target = tmp_path / "out"

        verdict = asyncio.run(api_client.download_artifacts(dir_path=target, results=_results({"doc": _content(_URI_PDF)})))

        assert verdict.all_saved is True
        assert verdict.saved_paths == [str(target / "doc.pdf")]
        method, url = spy.call_args.args[0], spy.call_args.args[1]
        assert method == "POST"
        assert url.endswith("/v1/resolve-storage-url/bulk")
        assert json.loads(spy.call_args.kwargs["content"]) == {"uris": [_URI_PDF]}
        # The store was reached on its own client, carrying none of the API client's credential.
        assert "authorization" not in seen[0].headers

    def test_the_clients_fetch_artifact_yields_the_same_bounded_stream(
        self,
        api_client: PipelexAPIClient,
        wire_response: ResponseBuilder,
        patch_send: SendPatcher,
        mocker: MockerFixture,
    ) -> None:
        item = _resolved(_URI_PDF).model_dump()
        patch_send(api_client, wire_response(200, json_body={"items": [item]}))
        _patch_storage(mocker, _serving(_PDF_BYTES))

        async def _read() -> bytes:
            async with api_client.fetch_artifact(_URI_PDF) as stream:
                return await stream.read()

        assert asyncio.run(_read()) == _PDF_BYTES
