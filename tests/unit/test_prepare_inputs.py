"""`prepare_inputs` — signature-driven input preparation. Cases derive from the shared
behavior matrix (`wip/upload/behavior-matrix.md`): file-bearing positions are found from the
explicit template's canonical content shape (a `{"url": …}` dict), assets are uploaded and
rewritten to `pipelex-storage://` in `url`, http(s)/storage references pass through, dedup
keys on source identity, and the call is copy-on-write.

Ports `pipelex-sdk-js/tests/prepare-inputs.test.ts`. The fake client returns a canned explicit
template from `build_inputs` and a counting `upload`; one wiring test drives the real client.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_mock import MockerFixture

from pipelex_sdk.build_models import BuildInputsRequest, BuildInputsResponse, BuildInputsValidReport, CrateInvalidReport, MthdsFileItem
from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.errors import ApiResponseError, InputPreparationError, RejectedAssetError
from pipelex_sdk.prepare_inputs import prepare_inputs
from pipelex_sdk.product_models import UploadedFile, UploadInput

_BASE_URL = "http://localhost:8081"
_FILES = [MthdsFileItem(content='domain = "demo"')]


def _entry(concept: str, content: Any) -> dict[str, Any]:
    return {"concept": concept, "content": content}


class _FakePrepareClient:
    """Fake client: `build_inputs` returns the given envelope template; `upload` counts calls."""

    def __init__(self, template: dict[str, Any], *, report: BuildInputsResponse | None = None, upload_error: Exception | None = None) -> None:
        self._template = template
        self._report = report
        self._upload_error = upload_error
        self.upload_calls: list[UploadInput] = []
        self._counter = 0

    async def build_inputs(self, request: BuildInputsRequest) -> BuildInputsResponse:
        if self._report is not None:
            return self._report
        return BuildInputsValidReport(is_valid=True, pipe_ref="demo.main", message="ok", format="json", explicit=True, inputs=self._template)

    async def upload(self, upload_input: UploadInput) -> UploadedFile:
        if self._upload_error is not None:
            raise self._upload_error
        self._counter += 1
        self.upload_calls.append(upload_input)
        return UploadedFile(uri=f"pipelex-storage://user/assets/{self._counter}.bin", filename=upload_input.filename)


class TestPrepareInputs:
    def test_uploads_top_level_image_bytes(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1, 2, 3])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert len(prepared.uploads) == 1
        assert prepared.uploads[0].uri == "pipelex-storage://user/assets/1.bin"

    def test_passes_http_url_through(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "https://example.com/real.png"}))

        assert prepared.inputs == {"photo": {"url": "https://example.com/real.png"}}
        assert prepared.uploads == []
        assert client.upload_calls == []

    def test_passes_existing_storage_uri_through(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "pipelex-storage://user/assets/already.png"}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/already.png"}}
        assert prepared.uploads == []

    def test_decodes_and_uploads_data_url(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "data:image/png;base64,AQIDBA=="}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert client.upload_calls[0].content_type == "image/png"
        assert client.upload_calls[0].data == "AQIDBA=="

    def test_uploads_each_element_of_declared_multiple(self) -> None:
        client = _FakePrepareClient({"exhibits": _entry("demo.Exhibit", [{"url": "https://mock/d.pdf"}])})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"exhibits": [bytes([1]), bytes([2])]}))

        assert prepared.inputs == {"exhibits": [{"url": "pipelex-storage://user/assets/1.bin"}, {"url": "pipelex-storage://user/assets/2.bin"}]}
        assert len(prepared.uploads) == 2

    def test_leaves_text_input_untouched(self) -> None:
        client = _FakePrepareClient({"question": _entry("demo.Question", {"text": "text_value"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"question": "notes/summary.txt"}))

        assert prepared.inputs == {"question": "notes/summary.txt"}
        assert client.upload_calls == []

    def test_uploads_only_nested_image_of_structured_input(self) -> None:
        client = _FakePrepareClient(
            {"dossier": _entry("demo.Dossier", {"title": "title_value", "cover": {"url": "https://mock/c.png", "mime_type": "image/png"}})}
        )

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": {"title": "Q3 report", "cover": bytes([7, 7])}}))

        assert prepared.inputs == {"dossier": {"title": "Q3 report", "cover": {"url": "pipelex-storage://user/assets/1.bin"}}}
        assert len(prepared.uploads) == 1

    def test_does_not_path_interpret_bare_string_at_dynamic_input(self) -> None:
        client = _FakePrepareClient({"freeform": _entry("native.Anything", {"whatever": "value"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"freeform": "resembles/a/path"}))

        assert prepared.inputs == {"freeform": "resembles/a/path"}
        assert client.upload_calls == []

    def test_uploads_canonical_image_nested_in_dynamic(self) -> None:
        client = _FakePrepareClient({"data": _entry("native.Composite", {"text": "t", "images": [{"url": "https://mock/i.png"}]})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"data": {"text": "hi", "images": [bytes([5])]}}))

        assert prepared.inputs == {"data": {"text": "hi", "images": [{"url": "pipelex-storage://user/assets/1.bin"}]}}
        assert len(prepared.uploads) == 1

    def test_dedups_by_source_identity(self) -> None:
        client = _FakePrepareClient({"exhibits": _entry("demo.Exhibit", [{"url": "https://mock/d.pdf"}])})
        shared = bytes([9, 9, 9])

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"exhibits": [shared, shared]}))

        assert len(client.upload_calls) == 1
        exhibits = prepared.inputs["exhibits"]
        assert exhibits[0]["url"] == exhibits[1]["url"]

    def test_is_copy_on_write(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})
        original = {"photo": bytes([1, 2, 3])}

        asyncio.run(prepare_inputs(client, files=_FILES, inputs=original))

        assert original["photo"] == bytes([1, 2, 3])

    def test_passes_through_undeclared_input(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "https://example.com/p.png", "stray": "left alone"}))

        assert prepared.inputs["stray"] == "left alone"

    def test_uploads_real_local_path(self, tmp_path: Path) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})
        path = tmp_path / "shot.png"
        path.write_bytes(bytes([1, 2, 3, 4]))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": str(path)}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert client.upload_calls[0].content_type == "image/png"

    def test_raises_for_unrecognized_value_at_file_position(self) -> None:
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})})

        # A plain object that is neither a canonical {url} content nor bytes — a realistic
        # caller typo — must surface as a typed error, not pass through unresolved.
        with pytest.raises(InputPreparationError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": {"mimeType": "image/png", "bytes": [1, 2, 3]}}))
        assert client.upload_calls == []

    def test_raises_when_signature_does_not_resolve(self) -> None:
        report = CrateInvalidReport(is_valid=False, message="closure did not validate", validation_errors=[])
        client = _FakePrepareClient({}, report=report)

        with pytest.raises(InputPreparationError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

    def test_surfaces_rejected_asset_before_returning(self) -> None:
        error = ApiResponseError(
            "HTTP 413", api_url=f"{_BASE_URL}/v1/upload", status=413, status_text="Payload Too Large", response_body="", server_message="too big"
        )
        client = _FakePrepareClient({"photo": _entry("demo.Photo", {"url": "https://mock/p.png"})}, upload_error=error)

        with pytest.raises(RejectedAssetError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

    def test_wires_through_the_real_client(self, mocker: MockerFixture) -> None:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        build_body = {
            "is_valid": True,
            "pipe_ref": "demo.main",
            "message": "ok",
            "format": "json",
            "explicit": True,
            "inputs": {"photo": {"concept": "demo.Photo", "content": {"url": "https://mock/p.png"}}},
        }
        upload_body = {"uri": "pipelex-storage://user/assets/1.bin", "filename": "upload.bin"}
        request = httpx.Request("POST", f"{_BASE_URL}/x")
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    httpx.Response(200, json=build_body, request=request),
                    httpx.Response(200, json=upload_body, request=request),
                ]
            ),
        )

        prepared = asyncio.run(client.prepare_inputs(files=_FILES, inputs={"photo": bytes([1, 2, 3])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert len(prepared.uploads) == 1
