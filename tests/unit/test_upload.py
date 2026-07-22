"""`upload_file` — the single-asset upload convenience over the raw `upload()` wire call.
Pins accepted asset forms (path str/Path, bytes), the client-side record assembly
(uri/filename/content_type/size), base64 correctness, and the mapping of raw transport
errors onto the semantic preparation errors.

Ports `pipelex-sdk-js/tests/upload.test.ts`. Uses a fake `upload` client double for the
logic, plus one wiring test through the real `PipelexAPIClient` with a mocked `_send`.
"""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import (
    ApiResponseError,
    ApiUnreachableError,
    InvalidLocalSourceError,
    RejectedAssetError,
    UnsupportedUploadCapabilityError,
    UploadAuthenticationError,
    UploadTransportError,
)
from pipelex_sdk.product_models import UploadedFile, UploadInput
from pipelex_sdk.upload import _to_asset_bytes, upload_file

_BASE_URL = "http://localhost:8081"


class _FakeUploadClient:
    """A fake `upload` client: records the wire body, returns a canned URI (or raises)."""

    def __init__(self, *, uri: str = "pipelex-storage://user/assets/abc.bin", error: Exception | None = None) -> None:
        self.calls: list[UploadInput] = []
        self._uri = uri
        self._error = error

    async def upload(self, upload_input: UploadInput) -> UploadedFile:
        if self._error is not None:
            raise self._error
        self.calls.append(upload_input)
        return UploadedFile(uri=self._uri, filename=upload_input.filename)


def _api_error(status: int, server_message: str = "boom") -> ApiResponseError:
    return ApiResponseError(
        f"HTTP {status}", api_url=f"{_BASE_URL}/v1/upload", status=status, status_text="Error", response_body="", server_message=server_message
    )


class TestUploadFile:
    def test_uploads_bytes_with_base64_and_full_record(self) -> None:
        client = _FakeUploadClient()
        data = bytes([1, 2, 3, 4, 5])

        record = asyncio.run(upload_file(client, data, filename="blob.png", content_type="image/png"))

        assert len(client.calls) == 1
        assert client.calls[0].data == base64.b64encode(data).decode("ascii")
        assert client.calls[0].content_type == "image/png"
        assert record.uri == "pipelex-storage://user/assets/abc.bin"
        assert record.filename == "blob.png"
        assert record.content_type == "image/png"
        assert record.size == 5

    def test_reads_a_local_path_str_deriving_filename_and_mime(self, tmp_path: Path) -> None:
        client = _FakeUploadClient()
        path = tmp_path / "diagram.png"
        path.write_bytes(bytes([10, 20, 30]))

        record = asyncio.run(upload_file(client, str(path)))

        assert client.calls[0].filename == "diagram.png"
        assert client.calls[0].content_type == "image/png"
        assert record.size == 3

    def test_accepts_a_pathlib_path(self, tmp_path: Path) -> None:
        client = _FakeUploadClient()
        path = tmp_path / "report.pdf"
        path.write_bytes(bytes([1, 2]))

        record = asyncio.run(upload_file(client, path))

        assert client.calls[0].filename == "report.pdf"
        assert client.calls[0].content_type == "application/pdf"
        assert record.size == 2

    def test_missing_path_raises_invalid_local_source(self, tmp_path: Path) -> None:
        client = _FakeUploadClient()
        missing = tmp_path / "nope.png"
        with pytest.raises(InvalidLocalSourceError):
            asyncio.run(upload_file(client, missing))

    def test_413_maps_to_rejected_asset(self) -> None:
        client = _FakeUploadClient(error=_api_error(413, "too big"))
        with pytest.raises(RejectedAssetError) as exc_info:
            asyncio.run(upload_file(client, bytes([1]), filename="big.pdf"))
        assert exc_info.value.filename == "big.pdf"
        assert exc_info.value.status == 413

    @pytest.mark.parametrize("status", [401, 403])
    def test_401_403_map_to_upload_authentication(self, status: int) -> None:
        client = _FakeUploadClient(error=_api_error(status))
        with pytest.raises(UploadAuthenticationError):
            asyncio.run(upload_file(client, bytes([1])))

    def test_404_maps_to_unsupported_capability(self) -> None:
        client = _FakeUploadClient(error=_api_error(404))
        with pytest.raises(UnsupportedUploadCapabilityError):
            asyncio.run(upload_file(client, bytes([1])))

    @pytest.mark.parametrize(
        "error",
        [
            _api_error(500),
            ApiUnreachableError("down", api_url=_BASE_URL, code="ECONNREFUSED"),
        ],
    )
    def test_non_semantic_failures_map_to_transport(self, error: Exception) -> None:
        client = _FakeUploadClient(error=error)
        with pytest.raises(UploadTransportError):
            asyncio.run(upload_file(client, bytes([1])))

    def test_reads_the_local_file_off_the_event_loop(self, mocker: MockerFixture, tmp_path: Path) -> None:
        # The (possibly large) file read is offloaded via asyncio.to_thread so it never blocks
        # the event loop. `wraps` keeps the real behavior; we only assert the offload happened.
        to_thread_spy = mocker.patch("pipelex_sdk.upload.asyncio.to_thread", wraps=asyncio.to_thread)
        client = _FakeUploadClient()
        path = tmp_path / "shot.png"
        path.write_bytes(bytes([1, 2, 3, 4]))

        record = asyncio.run(upload_file(client, path))

        assert to_thread_spy.await_count == 1
        await_args = to_thread_spy.await_args
        assert await_args is not None
        assert await_args.args[0] is _to_asset_bytes
        assert record.size == 4  # real behavior preserved through the wrapped call

    def test_wires_through_the_real_client(self, mocker: MockerFixture) -> None:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        uploaded = {"uri": "pipelex-storage://user/assets/z.bin", "filename": "x.png"}
        request = httpx.Request("POST", f"{_BASE_URL}/v1/upload")
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=httpx.Response(200, json=uploaded, request=request)))

        record = asyncio.run(client.upload_file(bytes([1, 2, 3]), filename="x.png", content_type="image/png"))

        call = send.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == f"{_BASE_URL}/v1/upload"
        body = json.loads(call.kwargs["content"])
        assert body["filename"] == "x.png"
        assert body["data"] == base64.b64encode(bytes([1, 2, 3])).decode("ascii")
        assert record.uri == "pipelex-storage://user/assets/z.bin"
        assert record.size == 3
