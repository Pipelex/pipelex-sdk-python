"""`prepare_inputs` — signature-driven input preparation over the input-form descriptor.

Cases derive from the behavior this SDK shares with `@pipelex/sdk` (`docs/input-preparation.md`) and port
`pipelex-sdk-js/tests/prepare-inputs.test.ts`: file-bearing positions come from the DESCRIPTOR's
declared kind (`document` / `image`), assets are uploaded and rewritten to `pipelex-storage://`
in `url`, http(s)/storage references pass through, dedup keys on source identity, and the call
is copy-on-write.

The fake client returns a canned `/v1/pipe-io` answer from `pipe_io` and records the request, so the
request shape is asserted and not just the outcome; the wiring tests drive the real client, including
the typed selection refusal it must parse off the wire.
"""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, cast

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
from pipelex_sdk.crate_models import CrateInvalidReport, MthdsFileItem, PipeIORequest, PipeIOResponse, PipeIOValidReport
from pipelex_sdk.errors import ApiResponseError, InputPreparationError, RejectedAssetError
from pipelex_sdk.prepare_inputs import prepare_inputs
from pipelex_sdk.product_models import UploadedFile, UploadInput
from tests.unit.test_data import PipeIOBodies

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


def _report(input_form: dict[str, PipeInputFormDescriptor], *, pipe_ref: str | None = _PIPE_REF) -> PipeIOValidReport:
    """A single-pipe valid answer: the route resolved `pipe_ref` and keyed the descriptor by it."""
    return PipeIOValidReport(
        is_valid=True,
        pipe_ref=pipe_ref,
        pipe_io_contracts={},
        input_form=input_form,
        output_form={},
        default_pipe_ref=pipe_ref,
        pending_signatures=[],
        is_runnable=True,
    )


def _api_error(status: int, body: dict[str, Any]) -> ApiResponseError:
    """The `ApiResponseError` the real client raises for this answer, built through its own error seam."""
    client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
    response = httpx.Response(status, json=body, request=httpx.Request("POST", f"{_BASE_URL}/v1/pipe-io"))
    with pytest.raises(ApiResponseError) as exc_info:
        client._raise_api_response_error(method="POST", endpoint="pipe-io", response=response)
    return exc_info.value


class _FakePrepareClient:
    """Fake client: `pipe_io` returns the given answer (or raises) and records the request; `upload` counts calls."""

    def __init__(
        self,
        result: PipeIOResponse | None = None,
        *,
        pipe_io_error: ApiResponseError | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        self._result = result
        self._pipe_io_error = pipe_io_error
        self._upload_error = upload_error
        self.upload_calls: list[UploadInput] = []
        self.pipe_io_calls: list[PipeIORequest] = []
        self._counter = 0

    async def pipe_io(self, request: PipeIORequest) -> PipeIOResponse:
        self.pipe_io_calls.append(request)
        if self._pipe_io_error is not None:
            raise self._pipe_io_error
        assert self._result is not None
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

    def test_asks_pipe_io_for_the_pipe_the_route_selects(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, files=_FILES, inputs={}))

        # One call, one pipe, no echo: the route selects the pipe when none is named.
        assert client.pipe_io_calls == [PipeIORequest(files=_FILES)]
        request = client.pipe_io_calls[0]
        assert request.pipe_ref is None
        assert request.all_pipes is False
        assert request.include_files is False

    def test_passes_the_files_through_as_given(self) -> None:
        # The route takes the crate envelope, so each file keeps its own `source` — none is synthesized.
        client = _image_client()
        files = [MthdsFileItem(content="a"), MthdsFileItem(content="b", source="b.mthds")]

        asyncio.run(prepare_inputs(client, files=files, inputs={}))

        assert client.pipe_io_calls[0].files == files

    def test_method_ref_is_a_server_side_pass_through(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, method_ref="github.com/Pipelex/methods/documents", inputs={}))

        assert client.pipe_io_calls == [PipeIORequest(method_ref="github.com/Pipelex/methods/documents")]

    def test_method_id_is_a_server_side_pass_through(self) -> None:
        client = _image_client()

        asyncio.run(prepare_inputs(client, method_id="mt_abc123", inputs={}))

        assert client.pipe_io_calls == [PipeIORequest(method_id="mt_abc123")]

    # ── The three selectors ───────────────────────────────────────────────

    def test_no_selector_is_refused_before_any_request(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="no method selector"):
            asyncio.run(prepare_inputs(client, inputs={"photo": bytes([1])}))
        assert client.pipe_io_calls == []
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
        assert client.pipe_io_calls == []

    def test_empty_selectors_are_absent_beside_a_real_one(self) -> None:
        # `files=[]` and a blank `method_id` select nothing, so they may sit beside a real
        # `method_ref` without tripping the XOR — the run options' empty-as-absent rule.
        client = _image_client()

        asyncio.run(prepare_inputs(client, files=[], method_ref="github.com/o/r", method_id="   ", inputs={}))

        assert client.pipe_io_calls == [PipeIORequest(method_ref="github.com/o/r")]

    def test_only_empty_selectors_is_no_selector(self) -> None:
        client = _image_client()

        with pytest.raises(InputPreparationError, match="no method selector"):
            asyncio.run(prepare_inputs(client, files=[], method_ref="", inputs={}))

    @pytest.mark.parametrize(
        ("kwargs", "argument", "type_name"),
        [
            ({"method_ref": 123}, "method_ref", "int"),
            ({"method_id": ["mt_1"]}, "method_id", "list"),
            ({"method_ref": True}, "method_ref", "bool"),
        ],
    )
    def test_a_non_string_selector_is_refused_rather_than_read_as_absent(self, kwargs: dict[str, Any], argument: str, type_name: str) -> None:
        # Empty is absent, but a WRONG TYPE is not: coercing it to `None` would let the XOR
        # pass on `files` alone and prepare against a method the caller did not name.
        client = _image_client()

        with pytest.raises(InputPreparationError) as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={}, **kwargs))

        assert str(exc_info.value) == f"Cannot prepare inputs: `{argument}` must be a string, got {type_name}."
        assert client.pipe_io_calls == []
        assert client.upload_calls == []

    def test_a_non_string_pipe_ref_is_refused_rather_than_silently_defaulted(self) -> None:
        # Read as absent, it would let the route select the default pipe: the pipe the caller
        # named would vanish without a word. Refused on the pre-request boundary.
        client = _image_client()

        with pytest.raises(InputPreparationError) as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref=cast("str", 123), inputs={}))

        assert str(exc_info.value) == "Cannot prepare inputs: `pipe_ref` must be a string, got int."
        assert client.pipe_io_calls == []

    # ── Pipe selection: the route's ───────────────────────────────────────

    def test_reads_the_descriptor_of_the_pipe_the_route_resolved(self) -> None:
        # No `pipe_ref`: the route's chain picks `demo.second`, and the walk follows its
        # descriptor, which declares `photo` as an image.
        client = _FakePrepareClient(_report(_form(ImageField(name="photo", **_required()), pipe_ref="demo.second"), pipe_ref="demo.second"))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_an_explicit_pipe_ref_is_sent_to_the_route(self) -> None:
        client = _FakePrepareClient(_report(_form(ImageField(name="photo", **_required()), pipe_ref="demo.second"), pipe_ref="demo.second"))

        prepared = asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref=" demo.second ", inputs={"photo": bytes([1])}))

        # Trimmed on the way out, like every caller-supplied selector.
        assert client.pipe_io_calls == [PipeIORequest(files=_FILES, pipe_ref="demo.second")]
        assert prepared.inputs == {"photo": {"url": "pipelex-storage://user/assets/1.bin"}}

    def test_a_bare_pipe_ref_is_refused_before_any_request(self) -> None:
        # The runner would still resolve a bare code across domains; preparation is
        # qualified-only, so it refuses before spending the round-trip.
        client = _image_client()

        with pytest.raises(InputPreparationError) as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, pipe_ref="main", inputs={}))

        assert str(exc_info.value) == 'Cannot prepare inputs: `pipe_ref` must be qualified (`domain.pipe_code`), got the bare "main".'
        assert client.pipe_io_calls == []

    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            (PipeIOBodies.UNKNOWN_PIPE_REFUSAL, "Pipe 'smoke.absent' not found in the submitted closure."),
            (
                PipeIOBodies.AMBIGUOUS_PIPE_REFUSAL,
                "No `pipe_ref` was given and the closure declares several `main_pipe`s (alpha.run, beta.run) — name the pipe explicitly.",
            ),
        ],
    )
    def test_a_refused_selection_is_an_input_preparation_error(self, body: dict[str, Any], detail: str) -> None:
        refusal = _api_error(422, body)
        client = _FakePrepareClient(pipe_io_error=refusal)

        with pytest.raises(InputPreparationError) as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        assert str(exc_info.value) == f"Cannot prepare inputs: the pipe could not be selected — {detail}"
        # The whole problem document stays reachable for a caller who needs it.
        assert exc_info.value.__cause__ is refusal
        assert client.upload_calls == []

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (422, PipeIOBodies.REQUEST_SHAPE_REFUSAL),
            (422, {**PipeIOBodies.UNKNOWN_PIPE_REFUSAL, "error_type": "MethodRefFetchError"}),
            (404, {"detail": "Unknown method", "code": "not_found"}),
            (500, {"detail": "PipeIOContractError", "error_type": "PipeIOContractError"}),
        ],
    )
    def test_every_other_error_is_left_as_it_is(self, status: int, body: dict[str, Any]) -> None:
        # Only the entry-lookup errors are a selection; a `422` of any other type is not.
        error = _api_error(status, body)
        client = _FakePrepareClient(pipe_io_error=error)

        with pytest.raises(ApiResponseError) as exc_info:
            asyncio.run(prepare_inputs(client, method_id="mt_1", inputs={}))

        assert exc_info.value is error

    @pytest.mark.parametrize(
        ("input_form", "pipe_ref"),
        [
            ({}, _PIPE_REF),
            (_form(ImageField(name="photo", **_required()), pipe_ref="demo.other"), _PIPE_REF),
            (_form(ImageField(name="photo", **_required())), None),
        ],
    )
    def test_an_answer_without_the_selected_descriptor_is_an_error_not_a_silent_no_op(
        self, input_form: dict[str, PipeInputFormDescriptor], pipe_ref: str | None
    ) -> None:
        # Never a silent degrade to "no uploads": without the descriptor there is no signature
        # to prepare against, and the caller's local path would travel to the runner verbatim.
        client = _FakePrepareClient(_report(input_form, pipe_ref=pipe_ref))

        with pytest.raises(InputPreparationError, match="carries no input-form descriptor for the selected pipe"):
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))
        assert client.upload_calls == []

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
        invalid = CrateInvalidReport.model_validate(PipeIOBodies.INVALID)
        client = _FakePrepareClient(invalid)

        with pytest.raises(InputPreparationError) as exc_info:
            asyncio.run(prepare_inputs(client, files=_FILES, inputs={"photo": bytes([1])}))

        assert (
            str(exc_info.value)
            == "Cannot prepare inputs: the method signature did not resolve — Input 'doc' is declared but never read by the template."
        )

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
        upload_body = {"uri": "pipelex-storage://user/assets/1.bin", "filename": "upload.bin"}
        request = httpx.Request("POST", f"{_BASE_URL}/x")
        send = mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                side_effect=[
                    httpx.Response(200, json=PipeIOBodies.VALID, request=request),
                    httpx.Response(200, json=upload_body, request=request),
                ]
            ),
        )

        prepared = asyncio.run(
            client.prepare_inputs(
                files=[MthdsFileItem(content='domain = "smoke"', source="smoke.mthds")],
                inputs={"doc": bytes([1, 2, 3]), "note": "hi", "dossier": {"title": "t"}},
            )
        )

        assert prepared.inputs == {"doc": {"url": "pipelex-storage://user/assets/1.bin"}, "note": "hi", "dossier": {"title": "t"}}
        assert len(prepared.uploads) == 1
        first_call = send.await_args_list[0]
        assert first_call.args[1] == f"{_BASE_URL}/v1/pipe-io"
        assert json.loads(first_call.kwargs["content"]) == {
            "files": [{"content": 'domain = "smoke"', "source": "smoke.mthds"}],
            "all_pipes": False,
            "include_files": False,
        }
        assert first_call.kwargs["request_timeout"] == 30.0

    def test_wires_a_method_ref_through_the_real_client(self, mocker: MockerFixture) -> None:
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        request = httpx.Request("POST", f"{_BASE_URL}/x")
        send = mocker.patch.object(client, "_send", mocker.AsyncMock(return_value=httpx.Response(200, json=PipeIOBodies.VALID, request=request)))

        asyncio.run(client.prepare_inputs(method_ref="github.com/o/r", pipe_ref="smoke.echo", inputs={"note": "hi"}))

        call = send.await_args_list[0]
        assert json.loads(call.kwargs["content"]) == {
            "method_ref": "github.com/o/r",
            "pipe_ref": "smoke.echo",
            "all_pipes": False,
            "include_files": False,
        }
        # The server may clone the repository before it answers.
        assert call.kwargs["request_timeout"] == 180.0

    def test_wires_a_refused_selection_off_the_wire(self, mocker: MockerFixture) -> None:
        # The mapping reads `error_type` off the problem document the real client parses.
        client = PipelexAPIClient(api_key="test-token", base_url=_BASE_URL)
        request = httpx.Request("POST", f"{_BASE_URL}/v1/pipe-io")
        mocker.patch.object(
            client,
            "_send",
            mocker.AsyncMock(
                return_value=httpx.Response(
                    422, json=PipeIOBodies.UNKNOWN_PIPE_REFUSAL, headers={"content-type": "application/problem+json"}, request=request
                )
            ),
        )

        with pytest.raises(InputPreparationError, match="the pipe could not be selected") as exc_info:
            asyncio.run(client.prepare_inputs(files=_FILES, pipe_ref="smoke.absent", inputs={}))

        cause = exc_info.value.__cause__
        assert isinstance(cause, ApiResponseError)
        assert cause.error_type == "EntryPipeNotFoundError"
