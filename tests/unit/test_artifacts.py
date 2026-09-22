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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from pipelex_sdk.artifact_models import (
    ArtifactItemError,
    ArtifactScope,
    BulkResolvedStorageUrls,
    DownloadArtifactsOptions,
    FetchArtifactOptions,
    ResolvedArtifact,
)
from pipelex_sdk.artifacts import (
    artifact_filename,
    collect_artifacts,
    download_artifacts,
    fetch_artifact,
    is_storage_reference,
    resolve_artifacts,
)
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


def _content(uri: str) -> dict[str, Any]:
    """A produced file as the runtime serializes it: the durable reference beside an expiring link."""
    return {"url": uri, "public_url": f"{_STORE}/signed-by-the-runtime?sig=stale"}


class TestArtifacts:
    # ── collect_artifacts ────────────────────────────────────────────

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
        assert is_storage_reference(value) is (expected != [])

    def test_ignores_values_that_carry_no_reference(self) -> None:
        assert collect_artifacts(None) == []
        assert collect_artifacts(42) == []
        assert collect_artifacts({"a": [1, 2.5, True, None]}) == []

    def test_walks_a_pydantic_model_as_well_as_a_parsed_body(self) -> None:
        results = _results({"picture": _content(_URI_PNG)})
        assert collect_artifacts(results) == [_URI_PNG]

    # ── artifact_filename ────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("uri", "content_type", "expected"),
        [
            ("pipelex-storage://org_1/runs/01J/outputs/report.pdf", "application/pdf", "report.pdf"),
            # Path separators are the split point, so no traversal and no absolute path survives.
            ("pipelex-storage://org_1/../../etc/passwd", None, "passwd"),
            # An encoded traversal is one segment, decoded after the split: the separators it hid
            # become underscores and the leading dots go, so it still names a file in the directory.
            ("pipelex-storage://org_1/x/..%2F..%2Fetc%2Fpasswd", None, "etc_passwd"),
            ("pipelex-storage://org_1/x/a\\b\\c.txt", None, "c.txt"),
            # A leading dot is stripped, so no hidden file; odd characters are neutralized.
            ("pipelex-storage://org_1/.bashrc", None, "bashrc"),
            ("pipelex-storage://org_1/my file (1).png", "image/png", "my_file__1_.png"),
            # Percent-decoded, with the query and fragment dropped.
            ("pipelex-storage://org_1/a%20b.pdf?sig=x#frag", None, "a_b.pdf"),
            # The extension comes from the content type only when the key carries none.
            ("pipelex-storage://org_1/outputs/report", "application/pdf", "report.pdf"),
            ("pipelex-storage://org_1/outputs/report.bin", "application/pdf", "report.bin"),
            ("pipelex-storage://org_1/outputs/report", "image/png; charset=binary", "report.png"),
            ("pipelex-storage://org_1/outputs/report", "application/x-unknown", "report"),
            ("pipelex-storage://org_1/outputs/report", None, "report"),
            # Nothing usable in the key: the numbered fallback, one-based.
            ("pipelex-storage://", None, "artifact-3"),
            ("pipelex-storage://org_1/___", None, "artifact-3"),
        ],
    )
    def test_derives_a_filename_that_can_only_name_a_file_in_the_directory(self, uri: str, content_type: str | None, expected: str) -> None:
        assert artifact_filename(uri, content_type, 2) == expected

    def test_caps_the_filename_length_keeping_the_extension(self) -> None:
        name = artifact_filename(f"pipelex-storage://org_1/{'a' * 400}.pdf", None, 0)
        assert len(name) == 128
        assert name.endswith(".pdf")

    def test_drops_an_extension_that_alone_exceeds_the_cap(self) -> None:
        name = artifact_filename(f"pipelex-storage://org_1/name.{'z' * 400}", None, 0)
        assert len(name) == 128
        assert name.startswith("name.")

    # ── resolve_artifacts ────────────────────────────────────────────

    def test_caps_the_length_after_the_guessed_extension(self) -> None:
        name = artifact_filename(f"pipelex-storage://org_1/{'a' * 200}", "image/jpeg", 0)

        assert len(name) <= 128
        assert name.endswith(".jpg")

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
        client = _FakeClient(run_result=RunResultFailed(pipeline_run_id=_RUN_ID, status=RunStatus.FAILED, message="the run failed"))
        with pytest.raises(RunFailedError) as caught:
            asyncio.run(download_artifacts(client, dir_path=tmp_path, run_id=_RUN_ID))
        assert caught.value.status == RunStatus.FAILED
        assert caught.value.run_id == _RUN_ID

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
        assert verdict.saved_paths == [str(target / "illustration.png"), str(target / "report.pdf")]
        assert (target / "illustration.png").read_bytes() == _PNG_BYTES
        assert (target / "report.pdf").read_bytes() == _PDF_BYTES

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
        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=tmp_path / "out",
                results=results,
                options=DownloadArtifactsOptions(scope=ArtifactScope.WORKING_MEMORY),
            )
        )

        assert verdict.scope == ArtifactScope.WORKING_MEMORY
        assert [artifact.uri for artifact in verdict.artifacts] == [_URI_PDF, _URI_PNG]
        assert verdict.all_saved is True

    def test_never_overwrites_a_name_already_on_disk(self, mocker: MockerFixture, tmp_path: Path) -> None:
        other = "pipelex-storage://org_1/runs/01J/second/report.pdf"
        client = _FakeClient(resolve=_resolver(_resolved(_URI_PDF), _resolved(other)))
        _patch_storage(mocker, _serving(_PDF_BYTES))
        target = tmp_path / "out"
        target.mkdir()
        (target / "report.pdf").write_bytes(b"do not touch me")

        verdict = asyncio.run(
            download_artifacts(
                client,
                dir_path=target,
                results=_results({"items": [_content(_URI_PDF), _content(other)]}),
                options=DownloadArtifactsOptions(concurrency=1),
            )
        )

        assert verdict.saved_paths == [str(target / "report-1.pdf"), str(target / "report-2.pdf")]
        assert (target / "report.pdf").read_bytes() == b"do not touch me"

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
        assert verdict.artifacts[1].error is None
        assert len(verdict.saved_paths) == 1

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
            assert (target / "report.pdf").exists()
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
        assert verdict.saved_paths == [str(target / "report.pdf")]
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
