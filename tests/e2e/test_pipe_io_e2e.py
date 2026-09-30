"""`pipe_io` and `prepare_inputs` on `POST /v1/pipe-io`, exercised against a LIVE API (no mocks).

Run it with `make e2e-test` against any API that serves the route — a local `pipelex-api` or the
hosted platform:

    PIPELEX_E2E_BASE_URL=http://127.0.0.1:8082 make e2e-test
    PIPELEX_E2E_BASE_URL=https://api-dev.pipelex.com PIPELEX_API_KEY=plx_sk_… make e2e-test

The whole module skips when `PIPELEX_E2E_BASE_URL` is unset, so `make agent-test`, which does not
collect this directory at all, never reaches it. The inline `files` and `method_ref` cases need only
the runner. The hosted `method_id` case needs the platform's catalog, and skips unless
`PIPELEX_API_KEY` is set: a bare runner answers without a key, and has no catalog to resolve an id
against. No case uploads a file, since a bare runner serves no `/v1/upload`; the upload leg rides
`test_artifacts_e2e.py` on the platform.

What the unit suite cannot prove: that the answer the models parse, the selection the route makes,
and the refusal `prepare_inputs` maps are the ones a real server produces.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

import pytest
from mthds.protocol.input_form import DocumentField, ObjectField

from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.crate_models import CrateInvalidReport, MthdsFileItem, PipeIORequest, PipeIOValidReport
from pipelex_sdk.errors import InputPreparationError
from pipelex_sdk.product_models import MethodWriteInput

if TYPE_CHECKING:
    from pipelex_sdk.crate_models import PipeIOResponse
    from pipelex_sdk.prepare_inputs import PreparedInputs

_BASE_URL = os.environ.get("PIPELEX_E2E_BASE_URL", "")
_API_KEY = os.environ.get("PIPELEX_API_KEY", "")

pytestmark = pytest.mark.skipif(not _BASE_URL, reason="live leg: set PIPELEX_E2E_BASE_URL to run it")

#: A published package whose entry pipe is named in its `METHODS.toml` alone.
_METHOD_REF = "github.com/Pipelex/methods/documents@v0.1.0"
_DOCUMENT_URL = "https://example.com/brief.pdf"

#: One domain declaring its entry pipe: a Document, a Text, and a structured input with an optional
#: nested Image — no inference, and every declared input read by the template.
_ENTRY_BUNDLE = '''domain = "smoke_pipe_io"
main_pipe = "echo"

[concept.Dossier]
description = "A dossier"

[concept.Dossier.structure]
title = { type = "text", description = "Title", required = true }
cover = { type = "concept", concept_ref = "native.Image", description = "Cover" }

[pipe.echo]
type = "PipeCompose"
description = "Echo the note beside a document and a dossier"
inputs = { doc = "Document", note = "Text", dossier = "Dossier" }
output = "Text"
template = """
$note

@doc

@dossier
"""
'''

#: Two pipes and no `main_pipe`: the route cannot select an entry pipe without being told one.
_NO_ENTRY_BUNDLE = """domain = "smoke_pipe_io_open"

[pipe.first]
type = "PipeCompose"
description = "Echo a note"
inputs = { note = "Text" }
output = "Text"
template = "$note"

[pipe.second]
type = "PipeCompose"
description = "Echo a topic"
inputs = { topic = "Text" }
output = "Text"
template = "$topic"
"""


def _entry_domain_bundle(domain: str) -> str:
    """One domain declaring `run` as its `main_pipe`: two of them make the entry pipe ambiguous."""
    return f"""domain = "{domain}"
main_pipe = "run"

[pipe.run]
type = "PipeCompose"
description = "Echo a note"
inputs = {{ note = "Text" }}
output = "Text"
template = "$note"
"""


_ENTRY_FILES = [MthdsFileItem(content=_ENTRY_BUNDLE, source="smoke_pipe_io.mthds")]
_NO_ENTRY_FILES = [MthdsFileItem(content=_NO_ENTRY_BUNDLE, source="smoke_pipe_io_open.mthds")]
_SEVERAL_ENTRY_FILES = [
    MthdsFileItem(content=_entry_domain_bundle("smoke_pipe_io_alpha"), source="alpha.mthds"),
    MthdsFileItem(content=_entry_domain_bundle("smoke_pipe_io_beta"), source="beta.mthds"),
]


def _client() -> PipelexAPIClient:
    return PipelexAPIClient(api_key=_API_KEY or None, base_url=_BASE_URL)


def _pipe_io(request: PipeIORequest) -> PipeIOResponse:
    async def _call() -> PipeIOResponse:
        async with _client() as client:
            return await client.pipe_io(request)

    return asyncio.run(_call())


class TestPipeIOLive:
    # ── The route ────────────────────────────────────────────────────

    def test_selects_the_declared_entry_pipe_and_types_its_artifacts(self) -> None:
        report = _pipe_io(PipeIORequest(files=_ENTRY_FILES))

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref == "smoke_pipe_io.echo"
        assert report.default_pipe_ref == "smoke_pipe_io.echo"
        assert report.is_runnable is True
        assert report.pending_signatures == []
        assert report.files is None
        assert set(report.pipe_io_contracts) == set(report.input_form) == set(report.output_form) == {"smoke_pipe_io.echo"}
        fields = report.input_form["smoke_pipe_io.echo"].fields
        assert [field.name for field in fields] == ["doc", "note", "dossier"]
        assert isinstance(fields[0], DocumentField)
        dossier = fields[2]
        assert isinstance(dossier, ObjectField)
        assert [(field.name, field.required) for field in dossier.fields] == [("title", True), ("cover", False)]

    def test_describes_a_method_with_no_entry_pipe_under_all_pipes_and_echoes_its_files(self) -> None:
        report = _pipe_io(PipeIORequest(files=_NO_ENTRY_FILES, all_pipes=True, include_files=True))

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref is None
        assert report.default_pipe_ref is None
        assert set(report.input_form) == {"smoke_pipe_io_open.first", "smoke_pipe_io_open.second"}
        assert report.files == _NO_ENTRY_FILES

    def test_an_invalid_closure_is_the_crate_verdict(self) -> None:
        broken = [MthdsFileItem(content=_ENTRY_BUNDLE.replace('type = "PipeCompose"', 'type = "PipeNope"'), source="broken.mthds")]

        report = _pipe_io(PipeIORequest(files=broken, include_files=True))

        assert isinstance(report, CrateInvalidReport)
        assert report.validation_errors

    def test_a_method_ref_selects_the_manifest_entry_pipe(self) -> None:
        report = _pipe_io(PipeIORequest(method_ref=_METHOD_REF, include_files=True))

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref is not None
        assert report.pipe_ref == report.default_pipe_ref
        assert report.files is not None
        assert all(item.source is not None and item.source.endswith(".mthds") for item in report.files)

    # ── prepare_inputs on the route ──────────────────────────────────

    def test_prepare_inputs_walks_the_descriptor_the_route_selected(self) -> None:
        async def _prepare() -> PreparedInputs:
            async with _client() as client:
                return await client.prepare_inputs(
                    files=_ENTRY_FILES,
                    inputs={"doc": _DOCUMENT_URL, "note": "hi", "dossier": {"title": "t", "cover": "https://example.com/c.png"}},
                )

        prepared = asyncio.run(_prepare())

        # Both file positions — the top-level Document and the OPTIONAL nested Image — are
        # recognized from the descriptor and wrapped as canonical content; http(s) is not uploaded.
        assert prepared.inputs == {
            "doc": {"url": _DOCUMENT_URL},
            "note": "hi",
            "dossier": {"title": "t", "cover": {"url": "https://example.com/c.png"}},
        }
        assert prepared.uploads == []

    def test_prepare_inputs_by_method_ref_needs_no_pipe_ref(self) -> None:
        # The package names its entry pipe in its manifest alone, which the route reads.
        async def _prepare() -> PreparedInputs:
            async with _client() as client:
                return await client.prepare_inputs(method_ref=_METHOD_REF, inputs={})

        prepared = asyncio.run(_prepare())

        assert prepared.inputs == {}
        assert prepared.uploads == []

    @pytest.mark.parametrize(
        ("files", "pipe_ref", "named"),
        [
            (_ENTRY_FILES, "smoke_pipe_io.absent", "smoke_pipe_io.absent"),
            (_NO_ENTRY_FILES, None, "main_pipe"),
            (_SEVERAL_ENTRY_FILES, None, "smoke_pipe_io_alpha.run, smoke_pipe_io_beta.run"),
        ],
    )
    def test_prepare_inputs_maps_a_refused_selection(self, files: list[MthdsFileItem], pipe_ref: str | None, named: str) -> None:
        # Needs the runner to type the refusal with its entry-lookup `error_type` (pipelex-api >= 0.33.1);
        # the server's `detail` names what was refused, the candidates included.
        async def _prepare() -> PreparedInputs:
            async with _client() as client:
                return await client.prepare_inputs(files=files, pipe_ref=pipe_ref, inputs={})

        with pytest.raises(InputPreparationError, match="the pipe could not be selected") as exc_info:
            asyncio.run(_prepare())
        assert named in str(exc_info.value)

    # ── The hosted catalog selector ──────────────────────────────────

    @pytest.mark.skipif(not _API_KEY, reason="hosted leg: a stored method needs the platform catalog and PIPELEX_API_KEY")
    def test_a_method_id_resolves_through_the_catalog(self) -> None:
        async def _by_id() -> tuple[PipeIOResponse, PreparedInputs]:
            async with _client() as client:
                method = await client.create_method(MethodWriteInput(name=f"sdk-python-e2e-pipe-io-{time.time_ns()}", mthds=_ENTRY_BUNDLE))
                try:
                    report = await client.pipe_io(PipeIORequest(method_id=method.method_id, include_files=True))
                    prepared = await client.prepare_inputs(method_id=method.method_id, inputs={"doc": _DOCUMENT_URL, "note": "hi"})
                finally:
                    await client.delete_method(method.method_id)
            return report, prepared

        report, prepared = asyncio.run(_by_id())

        assert isinstance(report, PipeIOValidReport)
        assert report.pipe_ref == "smoke_pipe_io.echo"
        assert report.files is not None
        assert [item.content for item in report.files] == [_ENTRY_BUNDLE]
        assert prepared.inputs == {"doc": {"url": _DOCUMENT_URL}, "note": "hi"}
