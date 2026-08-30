"""`prepare_inputs` — signature-driven input preparation over the input-form descriptor.

Cases derive from the shared behavior matrix (`wip/upload/behavior-matrix.md`) and port
`pipelex-sdk-js/tests/prepare-inputs.test.ts`: file-bearing positions come from the DESCRIPTOR's
declared kind (`document` / `image`), assets are uploaded and rewritten to `pipelex-storage://`
in `url`, http(s)/storage references pass through, dedup keys on source identity, and the call
is copy-on-write.

The fake client returns a canned `PipelexValidationReport` from `validate` and records the call,
so the request shape is asserted and not just the outcome; one wiring test drives the real client.
"""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mthds.protocol.input_form import (
    DocumentField,
    DocumentItem,
    ImageField,
    InputFormField,
    ListField,
    ObjectField,
    PipeInputFormDescriptor,
    TextField,
    UnknownField,
)
from mthds.protocol.pipe_io_contracts import PresenceMarker
from pytest_mock import MockerFixture

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.crate_models import MthdsFileItem
from pipelex_sdk.errors import ApiResponseError, InputPreparationError, RejectedAssetError
from pipelex_sdk.prepare_inputs import prepare_inputs
from pipelex_sdk.product_models import UploadedFile, UploadInput
from pipelex_sdk.validation_models import PipelexInvalidReport, PipelexValidationReport, PipelexValidationResult

_BASE_URL = "http://localhost:8081"
_FILES = [MthdsFileItem(content='domain = "demo"')]
_PIPE_REF = "demo.main"


def _required(**kwargs: Any) -> dict[str, Any]:
    """The pipe-slot facts every TOP-LEVEL field must state (`required` restates `presence`)."""
    return {"required": True, "presence": PresenceMarker.PLAIN, "gating": True, **kwargs}


def _optional(**kwargs: Any) -> dict[str, Any]:
    """An optional slot: `required: false`, `presence: optional`, and it never gates."""
    return {"required": False, "presence": PresenceMarker.OPTIONAL, "gating": False, **kwargs}


def _form(*fields: InputFormField, pipe_ref: str = _PIPE_REF) -> dict[str, PipeInputFormDescriptor]:
    return {pipe_ref: PipeInputFormDescriptor(fields=list(fields))}


def _report(
    input_form: dict[str, PipeInputFormDescriptor] | None,
    *,
    bundle_blueprint: dict[str, Any] | None = None,
    default_pipe_ref: str | None = None,
) -> PipelexValidationReport:
    return PipelexValidationReport(
        is_valid=True,
        bundle_blueprint=bundle_blueprint if bundle_blueprint is not None else {},
        default_pipe_ref=default_pipe_ref,
        input_form=input_form,
    )


class _FakePrepareClient:
    """Fake client: `validate` returns the given report and records the call; `upload` counts calls."""

    def __init__(self, result: PipelexValidationResult, *, upload_error: Exception | None = None) -> None:
        self._result = result
        self._upload_error = upload_error
        self.upload_calls: list[UploadInput] = []
        self.validate_calls: list[dict[str, Any]] = []
        self._counter = 0

    async def validate(
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
        self.validate_calls.append(
            {
                "mthds_contents": mthds_contents,
                "allow_signatures": allow_signatures,
                "mthds_sources": mthds_sources,
                "views": views,
                "method_ref": method_ref,
                "method_id": method_id,
            }
        )
        return self._result

    async def upload(self, upload_input: UploadInput) -> UploadedFile:
        if self._upload_error is not None:
            raise self._upload_error
        self._counter += 1
        self.upload_calls.append(upload_input)
        return UploadedFile(uri=f"pipelex-storage://user/assets/{self._counter}.bin", filename=upload_input.filename)


def _image_client(name: str = "photo", **upload_error: Any) -> _FakePrepareClient:
    return _FakePrepareClient(_report(_form(ImageField(name=name, **_required()))), **upload_error)


class TestPrepareInputs:
    # ── The signature call ────────────────────────────────────────────────

    def test_asks_validate_for_the_input_form_view(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, files=_FILES, inputs={}))

        call = client.validate_calls[0]
        assert call["views"] == ["input_form"]
        assert call["allow_signatures"] is True
        assert call["mthds_contents"] == ['domain = "demo"']
        # No file names a source, so none is synthesized — the server never sees a
        # length-mismatched `mthds_sources` array.
        assert call["mthds_sources"] is None

    def test_labels_every_content_once_any_file_names_a_source(self) -> None:
        client = _image_client()
        files = [MthdsFileItem(content="a"), MthdsFileItem(content="b", source="b.mthds")]

        asyncio.run(prepare_inputs(client, files=files, inputs={}))

        assert client.validate_calls[0]["mthds_sources"] == ["inline://file-1.mthds", "b.mthds"]

    def test_method_ref_is_a_server_side_pass_through(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, method_ref="github.com/Pipelex/methods/documents", inputs={}))

        call = client.validate_calls[0]
        assert call["method_ref"] == "github.com/Pipelex/methods/documents"
        assert call["mthds_contents"] is None
        assert call["views"] == ["input_form"]

    def test_method_id_is_a_server_side_pass_through(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, method_id="mt_abc123", inputs={}))

        call = client.validate_calls[0]
        assert call["method_id"] == "mt_abc123"
        assert call["mthds_contents"] is None

    # ── The three selectors ───────────────────────────────────────────────

    def test_no_selector_is_refused_before_any_request(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="no method selector"):
            asyncio.run(prepare_inputs(client, inputs={"photo": bytes([1])}))
        assert client.validate_calls == []
        assert client.upload_calls == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"files": _FILES, "method_ref": "github.com/o/r"},
            {"files": _FILES, "method_id": "mt_1"},
            {"method_ref": "github.com/o/r", "method_id": "mt_1"},
        ],
    )
    def test_several_selectors_are_refused_before_any_request(self, kwargs: dict[str, Any]) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="exactly one method selector"):
            asyncio.run(prepare_inputs(client, inputs={}, **kwargs))
        assert client.validate_calls == []

    def test_empty_selectors_are_absent_beside_a_real_one(self) -> None:
        # `files=[]` and a blank `method_id` select nothing, so they may sit beside a real
        # `method_ref` without tripping the XOR — the run options' empty-as-absent rule.
        client = _image_client()

        asyncio.run(prepare_inputs(client, files=[], method_ref="github.com/o/r", method_id="   ", inputs={}))

        assert client.validate_calls[0]["method_ref"] == "github.com/o/r"

    def test_only_empty_selectors_is_no_selector(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="no method selector"):
            asyncio.run(prepare_inputs(client, files=[], method_ref="", inputs={}))

    # ── Pipe selection ────────────────────────────────────────────────────

    def test_uses_the_single_declared_pipe_when_no_ref_is_given(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_typed_default_pipe_ref_outranks_the_blueprint(self) -> None:
        input_form = {
            "demo.first": PipeInputFormDescriptor(fields=[TextField(name="photo", **_required())]),
            "demo.second": PipeInputFormDescriptor(fields=[ImageField(name="photo", **_required())]),
        }
        client = _FakePrepareClient(_report(input_form, bundle_blueprint={"domain": "demo", "main_pipe": "first"}, default_pipe_ref="demo.second"))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        # `demo.second` declares `photo` as an image; `demo.first` declares it as text.
        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_falls_back_to_the_blueprint_main_pipe_qualified_by_its_domain(self) -> None:
        input_form = {
            "demo.first": PipeInputFormDescriptor(fields=[ImageField(name="photo", **_required())]),
            "demo.second": PipeInputFormDescriptor(fields=[TextField(name="photo", **_required())]),
        }
        client = _FakePrepareClient(_report(input_form, bundle_blueprint={"domain": "demo", "main_pipe": "first"}))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_explicit_pipe_ref_wins(self) -> None:
        input_form = {
            "demo.first": PipeInputFormDescriptor(fields=[TextField(name="photo", **_required())]),
            "demo.second": PipeInputFormDescriptor(fields=[ImageField(name="photo", **_required())]),
        }
        client = _FakePrepareClient(_report(input_form, default_pipe_ref="demo.first"))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref="demo.second", inputs={"photo": bytes([1])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_bare_pipe_ref_is_refused_naming_the_qualified_candidates(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="must be qualified") as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref="main", inputs={}))
        assert _PIPE_REF in str(exc_info.value)

    def test_unknown_pipe_ref_is_refused_naming_the_candidates(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="declares no pipe") as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref="demo.absent", inputs={}))
        assert _PIPE_REF in str(exc_info.value)

    def test_several_pipes_and_no_default_is_an_honest_refusal(self) -> None:
        # The manifest-only `main_pipe` gap: a fetched package may name its entry pipe in
        # METHODS.toml alone, which the report never carries. The error lists the candidates
        # so the caller's fix is one line.
        input_form = {
            "demo.first": PipeInputFormDescriptor(fields=[]),
            "demo.second": PipeInputFormDescriptor(fields=[]),
        }
        client = _FakePrepareClient(_report(input_form))

        with pytest.raises(InputPreparationError, match="no single default pipe") as exc_info:
            asyncio.run(prepare_inputs(client, method_ref="github.com/Pipelex/methods/documents", inputs={}))
        assert "demo.first, demo.second" in str(exc_info.value)

    # ── The descriptor-guided walk ────────────────────────────────────────

    def test_uploads_top_level_image_bytes(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1, 2, 3])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert len(prepared.uploads) == 1
        assert prepared.uploads[0].uri == "pipelex-storage://user/assets/1.bin"

    def test_passes_http_url_through(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "https://example.com/real.png"}))

        assert prepared.inputs == {"photo": {"url": "https://example.com/real.png"}}
        assert prepared.uploads == []
        assert client.upload_calls == []

    def test_passes_existing_storage_uri_through(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "pipelex-storage://user/assets/already.png"}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/already.png"}}
        assert prepared.uploads == []

    def test_decodes_and_uploads_data_url(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "data:image/png;base64,AQIDBA=="}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert client.upload_calls[0].content_type == "image/png"
        assert client.upload_calls[0].data == "AQIDBA=="

    @pytest.mark.parametrize(
        "data_url",
        [
            "data:image/png;base64,AQI",  # bad padding — binascii.Error
            "data:image/png;base64,AQID!!!!",  # non-alphabet junk — rejected by validate=True
        ],
    )
    def test_malformed_base64_data_url_raises_typed_error(self, data_url: str) -> None:
        # A malformed base64 data URL must surface as the typed `InputPreparationError`
        # (never a raw binascii.Error), and must never upload silently-corrupted bytes.
        client = _image_client()

        with pytest.raises(InputPreparationError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": data_url}))
        assert client.upload_calls == []

    def test_percent_encoded_binary_data_url_keeps_exact_bytes(self) -> None:
        # A non-base64 data URL carrying percent-encoded binary must upload its exact bytes;
        # decoding as UTF-8 text first would corrupt any byte >= 0x80 (e.g. %FF).
        client = _image_client()

        asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "data:application/octet-stream,%00%ff%01"}))

        assert base64.b64decode(client.upload_calls[0].data) == bytes([0x00, 0xFF, 0x01])

    def test_uploads_each_element_of_a_declared_list(self) -> None:
        client = _FakePrepareClient(_report(_form(ListField(name="exhibits", item=DocumentItem(required=True), **_required()))))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"exhibits": [bytes([1]), bytes([2])]}))

        assert prepared.inputs == {"exhibits": [{"url": "pipelex-storage://user/assets/1.bin"}, {"url": "pipelex-storage://user/assets/2.bin"}]}
        assert len(prepared.uploads) == 2

    def test_leaves_text_input_untouched(self) -> None:
        client = _FakePrepareClient(_report(_form(TextField(name="question", **_required()))))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"question": "notes/summary.txt"}))

        assert prepared.inputs == {"question": "notes/summary.txt"}
        assert client.upload_calls == []

    def test_uploads_only_the_nested_image_of_a_structured_input(self) -> None:
        dossier = ObjectField(
            name="dossier",
            fields=[TextField(name="title", required=True), ImageField(name="cover", required=True)],
            **_required(),
        )
        client = _FakePrepareClient(_report(_form(dossier)))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": {"title": "Q3 report", "cover": bytes([7, 7])}}))

        assert prepared.inputs == {"dossier": {"title": "Q3 report", "cover": {"url": "pipelex-storage://user/assets/1.bin"}}}
        assert len(prepared.uploads) == 1

    def test_preserves_sibling_keys_of_canonical_file_content(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": {"url": bytes([1]), "mime_type": "image/png"}}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin", "mime_type": "image/png"}}

    def test_copies_through_object_keys_the_descriptor_does_not_name(self) -> None:
        dossier = ObjectField(name="dossier", fields=[ImageField(name="cover", required=True)], **_required())
        client = _FakePrepareClient(_report(_form(dossier)))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": {"cover": bytes([1]), "note": "kept"}}))

        assert prepared.inputs["dossier"]["note"] == "kept"

    def test_dedups_by_source_identity(self) -> None:
        client = _FakePrepareClient(_report(_form(ListField(name="exhibits", item=DocumentItem(required=True), **_required()))))
        shared = bytes([9, 9, 9])

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"exhibits": [shared, shared]}))

        assert len(client.upload_calls) == 1
        exhibits = prepared.inputs["exhibits"]
        assert exhibits[0]["url"] == exhibits[1]["url"]

    def test_is_copy_on_write(self) -> None:
        client = _image_client()
        original = {"photo": bytes([1, 2, 3])}

        asyncio.run(prepare_inputs(client, files=_FILES, inputs=original))

        assert original["photo"] == bytes([1, 2, 3])

    def test_passes_through_undeclared_input(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": "https://example.com/p.png", "stray": "left alone"}))

        assert prepared.inputs["stray"] == "left alone"

    def test_uploads_real_local_path(self, tmp_path: Path) -> None:
        client = _image_client()
        path = tmp_path / "shot.png"
        path.write_bytes(bytes([1, 2, 3, 4]))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": str(path)}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert client.upload_calls[0].content_type == "image/png"

    def test_a_shape_mismatch_passes_through_for_the_run_to_reject(self) -> None:
        # A scalar where the descriptor declares an object: preparation never second-guesses
        # the signature, so the value rides through and the run answers for it.
        dossier = ObjectField(name="dossier", fields=[ImageField(name="cover", required=True)], **_required())
        client = _FakePrepareClient(_report(_form(dossier)))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": "not an object"}))

        assert prepared.inputs == {"dossier": "not an object"}
        assert client.upload_calls == []

    # ── The two misclassifications of L-260826-ddd843 ─────────────────────

    def test_uploads_an_optional_top_level_file_field_when_supplied(self) -> None:
        client = _FakePrepareClient(_report(_form(DocumentField(name="appendix", **_optional()))))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"appendix": bytes([3])}))

        assert prepared.inputs == {"appendix": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_uploads_an_optional_nested_file_field(self) -> None:
        # First edge: the required-only inputs template never rendered an optional nested
        # file field, so its position was invisible and the caller's local path travelled to
        # the runner as a literal string. The descriptor states `required: false` and the
        # walk enters it.
        dossier = ObjectField(
            name="dossier",
            fields=[TextField(name="title", required=True), ImageField(name="cover", required=False)],
            **_required(),
        )
        client = _FakePrepareClient(_report(_form(dossier)))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": {"title": "t", "cover": bytes([7])}}))

        assert prepared.inputs["dossier"]["cover"] == {"url": "pipelex-storage://user/assets/1.bin"}

    def test_does_not_read_a_text_field_merely_named_url_from_disk(self) -> None:
        # Second edge: the template marked a file position by rendering a `url`-bearing dict —
        # a side effect of the field's NAME, not of its concept — so a path-shaped text value
        # was uploaded. `kind: "text"` ends that.
        client = _FakePrepareClient(_report(_form(TextField(name="url", **_required()))))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"url": "notes/summary.txt"}))

        assert prepared.inputs == {"url": "notes/summary.txt"}
        assert client.upload_calls == []

    def test_does_not_enter_a_dynamic_input(self) -> None:
        # A `Dynamic` / `Composite` input is `kind: "unknown"` — the standard's escape hatch —
        # and the walk does not enter it, so a canonical file dict nested inside is NOT
        # uploaded. Uploading on the strength of a `url` key is the value-shape guess this
        # walk removes; such a caller uses `upload_file` first and passes the storage URI.
        client = _FakePrepareClient(_report(_form(UnknownField(name="data", **_required()))))
        nested = {"text": "hi", "images": [{"url": "https://mock/i.png"}]}

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"data": nested}))

        assert prepared.inputs == {"data": nested}
        assert client.upload_calls == []

    def test_does_not_path_interpret_a_bare_string_at_a_dynamic_input(self) -> None:
        client = _FakePrepareClient(_report(_form(UnknownField(name="freeform", **_required()))))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"freeform": "resembles/a/path"}))

        assert prepared.inputs == {"freeform": "resembles/a/path"}
        assert client.upload_calls == []

    # ── The explicit `{concept, content}` envelope ────────────────────────

    def test_unwraps_and_rewraps_the_explicit_envelope(self) -> None:
        client = _image_client()

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": {"concept": "native.Image", "content": bytes([1])}}))

        # The concept annotation rides through to the run; only `content` is rewritten.
        assert prepared.inputs == {"photo": {"concept": "native.Image", "content": {"url": "pipelex-storage://user/assets/1.bin"}}}

    def test_walks_inside_an_envelope_carrying_a_structured_content(self) -> None:
        dossier = ObjectField(
            name="dossier",
            fields=[TextField(name="title", required=True), ImageField(name="cover", required=True)],
            **_required(),
        )
        client = _FakePrepareClient(_report(_form(dossier)))
        envelope = {"concept": "demo.Dossier", "content": {"title": "t", "cover": bytes([7])}}

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"dossier": envelope}))

        assert prepared.inputs["dossier"]["concept"] == "demo.Dossier"
        assert prepared.inputs["dossier"]["content"]["cover"] == {"url": "pipelex-storage://user/assets/1.bin"}

    def test_a_dict_that_is_not_exactly_concept_and_content_is_not_an_envelope(self) -> None:
        # The envelope test matches the runtime's `_is_explicit`: keys EXACTLY `concept` and
        # `content`. A third key means it is ordinary content, not an envelope.
        client = _image_client()

        with pytest.raises(InputPreparationError, match="Unsupported value at a file input"):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": {"concept": "x", "content": bytes([1]), "extra": 1}}))

    # ── Failures, all raised before any run exists ────────────────────────

    def test_raises_for_unrecognized_value_at_file_position(self) -> None:
        client = _image_client()

        # A plain object that is neither a canonical {url} content nor bytes — a realistic
        # caller typo — must surface as a typed error, not pass through unresolved.
        with pytest.raises(InputPreparationError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": {"mimeType": "image/png", "bytes": [1, 2, 3]}}))
        assert client.upload_calls == []

    def test_raises_when_the_signature_does_not_resolve(self) -> None:
        invalid = PipelexInvalidReport(is_valid=False, message="closure did not validate", validation_errors=[])
        client = _FakePrepareClient(invalid)

        with pytest.raises(InputPreparationError, match="the method signature did not resolve"):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

    def test_a_report_without_the_descriptor_is_an_error_not_a_silent_no_op(self) -> None:
        # Never a silent degrade to "no uploads": without the descriptor there is no signature
        # to prepare against, and the caller's local path would travel to the runner verbatim.
        client = _FakePrepareClient(_report(None))

        with pytest.raises(InputPreparationError, match="carries no `input_form` descriptor"):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))
        assert client.upload_calls == []

    def test_surfaces_rejected_asset_before_returning(self) -> None:
        error = ApiResponseError(
            "HTTP 413", api_url=f"{_BASE_URL}/v1/upload", status=413, status_text="Payload Too Large", response_body="", server_message="too big"
        )
        client = _image_client(upload_error=error)

        with pytest.raises(RejectedAssetError):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

    # ── Wiring ────────────────────────────────────────────────────────────

    def test_wires_through_the_real_client(self, mocker: MockerFixture) -> None:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        validate_body = {
            "is_valid": True,
            "bundle_blueprint": {},
            "input_form": {_PIPE_REF: {"fields": [{"kind": "image", "name": "photo", "required": True, "presence": "plain", "gating": True}]}},
        }
        upload_body = {"uri": "pipelex-storage://user/assets/1.bin", "filename": "upload.bin"}
        request = httpx.Request("POST", f"{_BASE_URL}/x")
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    httpx.Response(200, json=validate_body, request=request),
                    httpx.Response(200, json=upload_body, request=request),
                ]
            ),
        )

        prepared = asyncio.run(client.prepare_inputs(files=_FILES, inputs={"photo": bytes([1, 2, 3])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}
        assert len(prepared.uploads) == 1
        assert send.await_args_list[0].args[1] == f"{_BASE_URL}/v1/validate"

    def test_wires_a_method_ref_through_the_real_client(self, mocker: MockerFixture) -> None:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        validate_body = {
            "is_valid": True,
            "bundle_blueprint": {},
            "input_form": {_PIPE_REF: {"fields": [{"kind": "text", "name": "question", "required": True, "presence": "plain", "gating": True}]}},
        }
        request = httpx.Request("POST", f"{_BASE_URL}/x")
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=httpx.Response(200, json=validate_body, request=request)))

        asyncio.run(client.prepare_inputs(method_ref="github.com/o/r", inputs={"question": "hi"}))

        body = json.loads(send.await_args_list[0].kwargs["content"])
        assert body["method_ref"] == "github.com/o/r"
        assert body["views"] == ["input_form"]
        assert body["allow_signatures"] is True
        assert "mthds_contents" not in body
