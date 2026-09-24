"""The artifact round trip, exercised against a LIVE hosted platform (no mocks).

Run it with `make e2e-test` against a platform that serves upload, the durable run lifecycle and the
bulk resolve route (`POST /v1/resolve-storage-url/bulk`), with an API key set for it:

    PIPELEX_E2E_BASE_URL=https://api-dev.pipelex.com PIPELEX_API_KEY=plx_sk_… make e2e-test

The whole module skips when either variable is unset, so the unit suite's `make agent-test` never
reaches it — and `make agent-test` does not collect this directory at all.

What the unit suite cannot prove: that the wire shapes the SDK composes — the bulk request, the
per-item answer, the presigned link the store actually honours, the working-memory echo of an
uploaded input — are the ones a real platform produces. Every mock agrees with the client about the
field names; only a live exchange settles whether the platform does. The leg is the JS twin's
(`pipelex-sdk-js/tests/e2e/artifacts.e2e.ts`): prepare a small file, run a pass-through with no
inference, download over `working_memory`, and read the bytes back equal.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pipelex_sdk.artifact_models import ArtifactScope, DownloadArtifactsOptions
from pipelex_sdk.artifacts import artifact_filename, collect_artifacts, download_artifacts, fetch_artifact
from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.crate_models import MthdsFileItem

if TYPE_CHECKING:
    from pipelex_sdk.artifact_models import DownloadArtifactsResult

_BASE_URL = os.environ.get("PIPELEX_E2E_BASE_URL", "")
_API_KEY = os.environ.get("PIPELEX_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not _BASE_URL or not _API_KEY,
    reason="live leg: set PIPELEX_E2E_BASE_URL and PIPELEX_API_KEY to run it",
)

#: One domain, one main pipe, a Document input beside the Text it echoes — no inference.
_PASS_THROUGH_BUNDLE = """domain = "smoke_artifacts"
main_pipe = "echo_note"

[pipe.echo_note]
type = "PipeCompose"
description = "Echo the note beside a document, with no inference"
inputs = { doc = "Document", note = "Text" }
output = "Text"
template = "$note"
"""

#: A minimal PDF with a nonce of its own, so a swapped file could not pass. Nothing in the run reads it.
_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"%% nonce " + str(time.time()).encode("ascii") + b"\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)


class TestArtifactRoundTripLive:
    def test_brings_an_uploaded_input_back_down_byte_for_byte(self, tmp_path: Path) -> None:
        source = tmp_path / "brief.pdf"
        source.write_bytes(_PDF_BYTES)
        files = [MthdsFileItem(content=_PASS_THROUGH_BUNDLE, source="smoke_artifacts.mthds")]

        async def _round_trip() -> tuple[str, DownloadArtifactsResult]:
            async with PipelexAPIClient(api_key=_API_KEY, base_url=_BASE_URL) as client:
                # Up: the Document position is uploaded and rewritten to its storage reference.
                prepared = await client.prepare_inputs(files=files, inputs={"doc": str(source), "note": "round trip"})
                assert len(prepared.uploads) == 1
                uploaded = prepared.uploads[0].uri
                assert collect_artifacts(prepared.inputs) == [uploaded]

                # Across: a pass-through run echoes the input in its working memory.
                results = await client.start_and_wait(
                    pipe_code="smoke_artifacts.echo_note",
                    mthds_contents=[_PASS_THROUGH_BUNDLE],
                    inputs=prepared.inputs,
                )
                assert results.main_stuff == {"text": "round trip"}

                # Down: by run id, over working_memory, every link minted fresh.
                verdict = await download_artifacts(
                    client,
                    dir_path=tmp_path / "out",
                    run_id=results.pipeline_run_id,
                    options=DownloadArtifactsOptions(
                        scope=ArtifactScope.WORKING_MEMORY,
                        # The local stack's object store hands out plain http links; a hosted one never does.
                        allow_http=_BASE_URL.startswith("http://"),
                    ),
                )
                return uploaded, verdict

        uploaded, verdict = asyncio.run(_round_trip())

        assert verdict.scope == ArtifactScope.WORKING_MEMORY
        assert verdict.all_saved is True
        echoed = next(artifact for artifact in verdict.artifacts if artifact.uri == uploaded)
        assert echoed.error is None
        assert echoed.content_type == "application/pdf"
        assert echoed.size == len(_PDF_BYTES)
        assert echoed.path in verdict.saved_paths
        assert echoed.path is not None
        # The file is named after the working-memory field the echoed input sits in.
        assert echoed.found_at[0].startswith("$.")
        assert Path(echoed.path).name == artifact_filename(echoed, echoed.content_type, ArtifactScope.WORKING_MEMORY)
        assert Path(echoed.path).read_bytes() == _PDF_BYTES

    def test_resolves_through_the_bulk_route_and_refuses_a_malformed_reference_as_a_value(self) -> None:
        async def _resolve_and_fetch() -> None:
            async with PipelexAPIClient(api_key=_API_KEY, base_url=_BASE_URL) as client:
                record = await client.upload_file(_PDF_BYTES, filename="probe.pdf", content_type="application/pdf")
                resolved = await client.resolve_artifacts([record.uri, "pipelex-storage://"])

                assert len(resolved) == 2
                assert resolved[0].uri == record.uri
                assert resolved[0].error is None
                assert resolved[0].url is not None
                assert resolved[0].url.startswith(("http://", "https://"))
                assert resolved[1].error is not None
                assert resolved[1].url is None

                # The link the platform minted is honoured by the store: the bytes come back.
                options = DownloadArtifactsOptions(allow_http=_BASE_URL.startswith("http://"))
                async with fetch_artifact(client, record.uri, options) as stream:
                    assert stream.status_code == 200
                    assert await stream.read() == _PDF_BYTES

        asyncio.run(_resolve_and_fetch())
