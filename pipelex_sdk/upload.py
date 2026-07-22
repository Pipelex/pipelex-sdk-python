"""`upload_file` — the single-asset upload convenience over the raw `upload()` wire
call. Accepts `str`/`pathlib.Path` filesystem paths and raw `bytes`, and returns an
`UploadRecord` assembled client-side. Python counterpart of `pipelex-sdk-js`'s
`uploadFile`. See `docs/input-preparation.md`.

The MIME type and size are known client-side at upload time, so the record is built
without extending the `/v1/upload` response.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from pipelex_sdk.errors import (
    ApiResponseError,
    ApiUnreachableError,
    InputPreparationError,
    InvalidLocalSourceError,
    RejectedAssetError,
    UnsupportedUploadCapabilityError,
    UploadAuthenticationError,
    UploadTransportError,
)
from pipelex_sdk.product_models import UploadedFile, UploadInput

DEFAULT_CONTENT_TYPE = "application/octet-stream"
DEFAULT_FILENAME = "upload.bin"

# A local asset `upload_file` accepts. A bare string in `upload_file` is a filesystem
# path; `prepare_inputs` classifies strings (url vs path) before they reach here.
UploadSource = str | Path | bytes


class UploadRecord(BaseModel):
    """The record `upload_file` returns for a prepared asset. Beyond the source identity it
    guarantees the resulting `uri`, the MIME `content_type`, the `size` in bytes, and the
    `filename`. A content checksum is deliberately not included — best-effort at most, and
    within-preparation dedup keys on source identity.
    """

    uri: str
    filename: str
    content_type: str
    size: int


class _UploadClient(Protocol):
    """The client surface `upload_file` needs — the raw base64 `upload` wire call."""

    async def upload(self, upload_input: UploadInput) -> UploadedFile: ...


def _guess_content_type(filename: str) -> str:
    """MIME guess from a filename extension; `application/octet-stream` when unknown."""
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or DEFAULT_CONTENT_TYPE


def _read_path(path: Path) -> bytes:
    """Read a filesystem path into bytes, mapping read failures to `InvalidLocalSourceError`."""
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = f'Local file cannot be read: "{path}" ({type(exc).__name__}).'
        raise InvalidLocalSourceError(msg, source=str(path)) from exc


def _to_asset_bytes(source: UploadSource, filename: str | None, content_type: str | None) -> tuple[bytes, str, str]:
    """Normalize any accepted asset form into bytes plus a filename and MIME."""
    if isinstance(source, bytes):
        resolved_name = filename or DEFAULT_FILENAME
        return source, resolved_name, content_type or _guess_content_type(resolved_name)
    path = Path(source)
    data = _read_path(path)
    resolved_name = filename or path.name or DEFAULT_FILENAME
    return data, resolved_name, content_type or _guess_content_type(resolved_name)


def _map_upload_error(error: ApiResponseError | ApiUnreachableError, filename: str) -> InputPreparationError:
    """Translate a raw `upload()` transport error into the matching preparation error."""
    if isinstance(error, ApiUnreachableError):
        msg = f'Upload of "{filename}" could not reach the Pipelex API ({error.code or "unreachable"}).'
        return UploadTransportError(msg)
    match error.status:
        case 413:
            detail = error.server_message or "asset exceeds the service size limit"
            return RejectedAssetError(f'The server rejected "{filename}": {detail}.', filename=filename, status=error.status)
        case 401 | 403:
            return UploadAuthenticationError(
                f'Upload of "{filename}" was not authorized ({error.status}). Check the configured Pipelex API key.',
                status=error.status,
            )
        case 404:
            return UnsupportedUploadCapabilityError(
                "The configured Pipelex deployment does not support file upload (no /v1/upload route). Upload is a hosted Pipelex capability."
            )
        case _:
            detail = error.server_message or error.status_text
            return UploadTransportError(f'Upload of "{filename}" failed ({error.status}): {detail}.')


async def upload_file(
    client: _UploadClient,
    source: UploadSource,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> UploadRecord:
    """Upload one local asset and return its `UploadRecord`.

    `source` is a filesystem path (`str`/`Path`) or raw `bytes`. Maps the raw `upload()`
    transport errors onto the semantic input-preparation errors: a `413` is a rejected
    asset, `401`/`403` an auth failure, `404` an unsupported upload capability, an
    unreachable host a transport failure.
    """
    # Offload the (possibly large) synchronous file read off the event loop — `read_bytes`
    # releases the GIL during the underlying os.read, so other coroutines run during disk I/O.
    # base64 stays inline on purpose: CPython's binascii holds the GIL, so threading it would
    # not free the loop (and this matches the JS SDK, which also reads off-loop but encodes inline).
    data, resolved_name, resolved_type = await asyncio.to_thread(_to_asset_bytes, source, filename, content_type)
    encoded = base64.b64encode(data).decode("ascii")
    try:
        uploaded = await client.upload(UploadInput(filename=resolved_name, data=encoded, content_type=resolved_type))
    except (ApiResponseError, ApiUnreachableError) as exc:
        raise _map_upload_error(exc, resolved_name) from exc
    return UploadRecord(uri=uploaded.uri, filename=uploaded.filename, content_type=resolved_type, size=len(data))
