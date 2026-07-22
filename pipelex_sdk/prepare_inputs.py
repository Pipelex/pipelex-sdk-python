"""`prepare_inputs` — signature-driven input preparation. Resolves the target pipe's
declared inputs via the explicit inputs template, interprets the caller's compact inputs
top-down against it, uploads the file-bearing values, and returns rewritten inputs
(canonical content carrying `pipelex-storage://` in `url`) plus one upload record per
prepared asset. Python counterpart of `pipelex-sdk-js`'s `prepareInputs`.

The classification mirrors the runtime: `pipelex`'s `input_normalizer` walks
Image/Document contents (recognized by their `url`-bearing shape, incl. nested in
structured content) and `resolve_uri` decides upload vs pass-through. The declared
signature comes from the explicit template (`build_inputs`, `explicit=True`), whose
canonical content shape is the classifier — the file signal is a value that is a dict
containing a `url` key. See the shared behavior matrix (`wip/upload/behavior-matrix.md`)
and `docs/input-preparation.md`.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import unquote

from pydantic import BaseModel

from pipelex_sdk.build_models import BuildInputsRequest, BuildInputsResponse, CrateInvalidReport, MthdsFileItem
from pipelex_sdk.errors import InputPreparationError
from pipelex_sdk.upload import UploadRecord, UploadSource, upload_file

if TYPE_CHECKING:
    from pipelex_sdk.product_models import UploadedFile, UploadInput

PIPELEX_STORAGE_SCHEME = "pipelex-storage://"
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class PreparedInputs(BaseModel):
    """The result of `prepare_inputs`: rewritten inputs (copy-on-write) plus upload records.

    `inputs` is a copy of the caller's inputs with each file-bearing value rewritten to
    canonical content carrying `pipelex-storage://` in `url`. `uploads` carries one record
    per uploaded asset — pass-through references (http(s), existing storage URIs) produce none.
    """

    inputs: dict[str, Any]
    uploads: list[UploadRecord]


class _PrepareClient(Protocol):
    """The client surface `prepare_inputs` needs: raw `upload` plus the `build_inputs` signature source."""

    async def upload(self, upload_input: UploadInput) -> UploadedFile: ...

    async def build_inputs(self, request: BuildInputsRequest) -> BuildInputsResponse: ...


class _PrepareContext:
    """Mutable state threaded through one preparation walk."""

    def __init__(self, client: _PrepareClient) -> None:
        self.client = client
        self.uploads: list[UploadRecord] = []
        # Dedup by source identity: same source (str/bytes/Path value) uploads once.
        self.dedup: dict[UploadSource, str] = {}


def _is_file_content(node: Any) -> bool:
    """A canonical Image/Document content is a dict carrying a `url` key."""
    return isinstance(node, dict) and "url" in node


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a `data:` URL into bytes plus its MIME type."""
    comma = data_url.find(",")
    if comma < 0:
        msg = f"Malformed data URL (no comma separator): {data_url[:32]}…"
        raise InputPreparationError(msg)
    header = data_url[5:comma]  # strip "data:"
    payload = data_url[comma + 1 :]
    content_type = header.split(";")[0] or "application/octet-stream"
    if ";base64" in header.lower():
        return base64.b64decode(payload), content_type
    return unquote(payload).encode("utf-8"), content_type


async def _do_resolve_source(ctx: _PrepareContext, source: Any) -> str:
    """Resolve one source to the URL/URI to write."""
    if isinstance(source, str):
        if source.startswith(PIPELEX_STORAGE_SCHEME):
            return source  # already prepared
        if _HTTP_URL_RE.match(source):
            return source  # reachable URL — pass through
        if source.startswith("data:"):
            data, content_type = _decode_data_url(source)
            record = await upload_file(ctx.client, data, content_type=content_type)
            ctx.uploads.append(record)
            return record.uri
        # Anything else is a local filesystem path.
        record = await upload_file(ctx.client, source)
        ctx.uploads.append(record)
        return record.uri
    if isinstance(source, (bytes, Path)):
        record = await upload_file(ctx.client, source)
        ctx.uploads.append(record)
        return record.uri
    # An unrecognized value sits at a file-bearing position (neither a source string,
    # bytes/Path, nor a canonical {url} content dict). Fail with a typed error rather than
    # passing an unusable value through to a later run.
    msg = (
        "Unsupported value at a file input: expected a path (str/Path), bytes, a data URL, "
        f"an http(s)/pipelex-storage:// URL, or canonical {{url}} content; got {type(source).__name__}."
    )
    raise InputPreparationError(msg)


async def _resolve_source(ctx: _PrepareContext, source: Any) -> str:
    """Resolve a source, deduped by identity (same source uploads once)."""
    hashable = isinstance(source, (str, bytes, Path))
    if hashable and source in ctx.dedup:
        return ctx.dedup[source]
    resolved = await _do_resolve_source(ctx, source)
    if hashable:
        ctx.dedup[source] = resolved
    return resolved


async def _resolve_file_position(ctx: _PrepareContext, caller_value: Any) -> Any:
    """Resolve a value known to sit at a file position into canonical content with a rewritten `url`."""
    if isinstance(caller_value, dict) and "url" in caller_value:
        content = cast("dict[str, Any]", caller_value)
        resolved = await _resolve_source(ctx, content["url"])
        return {**content, "url": resolved}
    resolved = await _resolve_source(ctx, caller_value)
    return {"url": resolved}


async def _resolve_node(ctx: _PrepareContext, template_node: Any, caller_value: Any) -> Any:
    """Template-guided walk: a template node that is canonical file content marks a file position."""
    if _is_file_content(template_node):
        return await _resolve_file_position(ctx, caller_value)
    if isinstance(template_node, list) and template_node:
        element_template = cast("list[Any]", template_node)[0]
        if isinstance(caller_value, list):
            items = cast("list[Any]", caller_value)
            return [await _resolve_node(ctx, element_template, item) for item in items]
        return caller_value  # shape mismatch — leave it for the run to reject
    if isinstance(template_node, dict) and isinstance(caller_value, dict):
        template_dict = cast("dict[str, Any]", template_node)
        caller_dict = cast("dict[str, Any]", caller_value)
        result: dict[str, Any] = dict(caller_dict)
        for key in template_dict:
            if key in caller_dict:
                result[key] = await _resolve_node(ctx, template_dict[key], caller_dict[key])
        return result
    return caller_value  # scalar (text/number/…) or shape mismatch — pass through


async def prepare_inputs(
    client: _PrepareClient,
    *,
    files: list[MthdsFileItem],
    pipe_ref: str | None = None,
    inputs: dict[str, Any],
) -> PreparedInputs:
    """Prepare a pipe's inputs: upload local/byte/data-URL assets at the signature's
    file-bearing positions and return copy-on-write rewritten inputs plus upload records.

    HTTP(S) URLs and existing `pipelex-storage://` URIs pass through unchanged. All failures
    are raised before any run is created. The declared signature is resolved from the inline
    `files` closure; a closure that does not resolve raises `InputPreparationError`. No-verdict
    conditions from the signature route (unknown `pipe_ref`, auth, server fault) surface as the
    build route's `ApiResponseError`.
    """
    report = await client.build_inputs(BuildInputsRequest(files=files, pipe_ref=pipe_ref, format="json", explicit=True))
    if isinstance(report, CrateInvalidReport):
        first = report.validation_errors[0].message if report.validation_errors else report.message
        msg = f"Cannot prepare inputs: the method signature did not resolve — {first}"
        raise InputPreparationError(msg)
    if report.format != "json" or report.inputs is None:
        msg = f'Cannot prepare inputs: expected a JSON inputs template, got "{report.format}".'
        raise InputPreparationError(msg)
    template = report.inputs

    ctx = _PrepareContext(client)
    rewritten = dict(inputs)
    for name, caller_value in inputs.items():
        entry = template.get(name)
        if not isinstance(entry, dict) or "content" not in entry:
            # Not a declared input (or an unexpected envelope) — pass through untouched.
            continue
        content = cast("dict[str, Any]", entry)["content"]
        rewritten[name] = await _resolve_node(ctx, content, caller_value)

    return PreparedInputs(inputs=rewritten, uploads=ctx.uploads)
