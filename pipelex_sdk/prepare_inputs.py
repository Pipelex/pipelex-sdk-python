"""`prepare_inputs` — signature-driven input preparation. Names the method three ways,
resolves the target pipe's declared inputs from the standard's input-form descriptor,
interprets the caller's inputs top-down against it, uploads the file-bearing values, and
returns rewritten inputs (canonical content carrying `pipelex-storage://` in `url`) plus one
upload record per prepared asset. Python counterpart of `pipelex-sdk-js`'s `prepareInputs`.

The signature comes from ONE `POST /v1/pipe-io`, which selects the pipe server-side and returns
its input-form descriptor with no dry run, and the walk is discriminated on each descriptor
node's declared `kind` — never on the shape of a value. That is the whole point: the previous
source, the explicit inputs template, marked a file position by rendering a `{"url": …}` dict,
which is a side effect of a field being NAMED `url` rather than of its concept being an Image or
a Document. Two positions were misread as a result — an OPTIONAL nested file field, which the
required-only template never rendered, was left un-uploaded and its local path travelled to the
runner as a literal string; and a text field merely named `url` was read from disk and uploaded. The descriptor states the resolved
kind at every depth and includes optional fields, so both are gone.

See `docs/input-preparation.md`. The design of record is shared with `@pipelex/sdk` and
tracked as L-260829-300c50 in the workspace ledger.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import unquote_to_bytes

from mthds.protocol.input_form import (
    BooleanItem,
    DateItem,
    DocumentItem,
    EnumItem,
    ImageItem,
    InputFormItem,
    ListItem,
    NumberItem,
    ObjectItem,
    ProseItem,
    TextItem,
    UnknownItem,
)
from pydantic import BaseModel

from pipelex_sdk.crate_models import CrateInvalidReport, PipeIORequest, PipeIOValidReport
from pipelex_sdk.errors import ApiResponseError, InputPreparationError
from pipelex_sdk.upload import UploadRecord, UploadSource, upload_file

if TYPE_CHECKING:
    from pipelex_sdk.crate_models import MthdsFileItem, PipeIOResponse
    from pipelex_sdk.product_models import UploadedFile, UploadInput

PIPELEX_STORAGE_SCHEME = "pipelex-storage://"
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# How `/v1/pipe-io` says it refused the pipe selection, which `_fetch_signature` turns into an
# `InputPreparationError`: a `422` whose `error_type` is one of the engine's entry-lookup errors.
# `EntryPipeNotFoundError` is an unknown `pipe_ref`, or no `pipe_ref` and a method declaring no
# entry pipe; `EntryPipeAmbiguousError` is a code matching pipes in several domains, or several
# `main_pipe` declarations. Every other `422` — a malformed body, a `method_ref` that does not
# parse or fetch, a stored method with no source — is not a selection and stays the
# `ApiResponseError` it is. The names are the runner's exception classes; they live here alone,
# so a rename upstream is a one-line edit.
_HTTP_UNPROCESSABLE_ENTITY = 422
_PIPE_SELECTION_ERROR_TYPES: frozenset[str] = frozenset({"EntryPipeNotFoundError", "EntryPipeAmbiguousError"})
# The problem-document member a selection refusal may carry its candidate qualified refs in.
_CANDIDATES_MEMBER = "candidates"


class PreparedInputs(BaseModel):
    """The result of `prepare_inputs`: rewritten inputs (copy-on-write) plus upload records.

    `inputs` is a copy of the caller's inputs with each file-bearing value rewritten to
    canonical content carrying `pipelex-storage://` in `url`. `uploads` carries one record
    per uploaded asset — pass-through references (http(s), existing storage URIs) produce none.
    """

    inputs: dict[str, Any]
    uploads: list[UploadRecord]


class _PrepareClient(Protocol):
    """The client surface `prepare_inputs` needs: raw `upload` plus `pipe_io` as the
    signature source. Typed as `PipelexAPIClient`'s own signatures so the client satisfies it
    structurally.
    """

    async def upload(self, upload_input: UploadInput) -> UploadedFile: ...

    async def pipe_io(self, request: PipeIORequest) -> PipeIOResponse: ...


class _PrepareContext:
    """Mutable state threaded through one preparation walk."""

    def __init__(self, client: _PrepareClient) -> None:
        self.client = client
        self.uploads: list[UploadRecord] = []
        # Dedup by source identity: same source (str/bytes/Path value) uploads once.
        self.dedup: dict[UploadSource, str] = {}


def _caller_selector(value: object, *, argument: str) -> str | None:
    """A caller-supplied selector, trimmed — `None` when absent, refused when not a string.

    The "empty is absent" rule, plus a boundary check. Coercing a non-string to `None` here
    would read `method_ref=123` as an absent selector and let it fall through to another one —
    defeating the exactly-one check this whole surface rests on — and would let a non-string
    `pipe_ref` silently take the default pipe instead of the one the caller named. Both are
    caller mistakes, and a caller mistake owes an `InputPreparationError` raised before any
    request.

    Deliberately local rather than reusing `client.py`'s `_normalized_selector`: that helper
    is private to the client boundary and raises `PipelineRequestError`, where every failure
    of this module owes an `InputPreparationError`.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    msg = f"Cannot prepare inputs: `{argument}` must be a string, got {type(value).__name__}."
    raise InputPreparationError(msg)


def _is_file_content(node: Any) -> bool:
    """A canonical Image/Document content is a dict carrying a `url` key.

    A value-shape helper only, consulted at a position the DESCRIPTOR already declared a
    file. It is no longer a classifier: reading it as one is the defect this module removed.
    """
    return isinstance(node, dict) and "url" in node


def _is_explicit_envelope(value: Any) -> bool:
    """The explicit `{concept, content}` input envelope — keys EXACTLY `concept` and `content`.

    Matches the runtime's `_is_explicit` (`input_shaper.py`), so an agent that filled an
    explicit template can hand it straight back. Anything else is a compact value.
    """
    if not isinstance(value, dict):
        return False
    return set(cast("dict[str, Any]", value)) == {"concept", "content"}


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a `data:` URL into bytes plus its MIME type.

    A base64 payload is decoded with `validate=True` so junk characters are rejected rather
    than silently discarded (which would upload corrupted bytes), and a decode failure (bad
    padding or non-alphabet input) surfaces as a typed `InputPreparationError` — never a raw
    `binascii.Error` escaping the preparation contract. A non-base64 payload decodes straight
    to bytes via `unquote_to_bytes`, so percent-encoded binary keeps its exact bytes (decoding
    it as UTF-8 text first would corrupt any byte ≥ 0x80).
    """
    comma = data_url.find(",")
    if comma < 0:
        msg = f"Malformed data URL (no comma separator): {data_url[:32]}…"
        raise InputPreparationError(msg)
    header = data_url[5:comma]  # strip "data:"
    payload = data_url[comma + 1 :]
    content_type = header.split(";")[0] or "application/octet-stream"
    if ";base64" in header.lower():
        try:
            decoded = base64.b64decode(payload, validate=True)
        except binascii.Error as exc:
            msg = f"Malformed data URL: the base64 payload is not valid ({exc})."
            raise InputPreparationError(msg) from exc
        return decoded, content_type
    return unquote_to_bytes(payload), content_type


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
    if _is_file_content(caller_value):
        content = cast("dict[str, Any]", caller_value)
        resolved = await _resolve_source(ctx, content["url"])
        return {**content, "url": resolved}
    resolved = await _resolve_source(ctx, caller_value)
    return {"url": resolved}


async def _resolve_node(ctx: _PrepareContext, node: InputFormItem, caller_value: Any) -> Any:
    """Descriptor-guided walk, discriminated on the node's declared kind.

    - `document` / `image` — a file position, whatever the value's shape;
    - `object` — walk the declared `fields` by name; keys the descriptor does not name are
      copied through untouched. An OPTIONAL field is walked when present, which is what
      makes an optional nested file reachable at all;
    - `list` — walk `item` against each element;
    - every other kind — pass through at any depth. `unknown` is the standard's escape hatch
      for a `Dynamic` / `Composite` input and is deliberately NOT entered: the signature
      declares no file there, and uploading on the strength of a `url` key is the value-shape
      guess this walk removes. Such a caller uploads with `upload_file` first and passes the
      storage URI.

    A caller value whose shape disagrees with the node (a scalar at an `object`, a non-list at
    a `list`) passes through for the run to reject — preparation never second-guesses the
    signature. The match is over the item classes rather than over `kind`, because each
    per-kind `*Field` derives from its `*Item`: one set of patterns covers both the named
    layer (top level, `object.fields`) and the nameless one (`list.item`), and it narrows the
    node for the type checker where matching on `node.kind` would not.
    """
    match node:
        case DocumentItem() | ImageItem():
            return await _resolve_file_position(ctx, caller_value)
        case ObjectItem():
            if not isinstance(caller_value, dict):
                return caller_value
            caller_dict = cast("dict[str, Any]", caller_value)
            result: dict[str, Any] = dict(caller_dict)
            for field in node.fields:
                if field.name in caller_dict:
                    result[field.name] = await _resolve_node(ctx, field, caller_dict[field.name])
            return result
        case ListItem():
            if not isinstance(caller_value, list):
                return caller_value
            elements = cast("list[Any]", caller_value)
            return [await _resolve_node(ctx, node.item, element) for element in elements]
        case TextItem() | ProseItem() | DateItem() | NumberItem() | BooleanItem() | EnumItem() | UnknownItem():
            return caller_value


def _resolve_selector(
    *,
    files: list[MthdsFileItem] | None,
    method_ref: str | None,
    method_id: str | None,
) -> tuple[list[MthdsFileItem] | None, str | None, str | None]:
    """Normalize the three selectors and check that exactly one remains.

    Empty is absent — `files=[]`, `method_ref=""`, `method_id="  "` — mirroring the run
    options' rule and the `CrateRequestBase` normalizers, so an empty selector may sit beside
    a real one without tripping the XOR. A non-string `method_ref` / `method_id` is NOT absent
    but refused, so a mistyped selector cannot slip past the XOR as a silent `None`. The check
    lives here, rather than in `PipeIORequest`'s own validator, so that it raises the
    `InputPreparationError` this module owes, and it runs BEFORE any request.
    """
    selected_files = files or None
    selected_method_ref = _caller_selector(method_ref, argument="method_ref")
    selected_method_id = _caller_selector(method_id, argument="method_id")

    given: list[str] = []
    if selected_files is not None:
        given.append("`files`")
    if selected_method_ref is not None:
        given.append("`method_ref`")
    if selected_method_id is not None:
        given.append("`method_id`")

    if not given:
        msg = (
            "Cannot prepare inputs: no method selector. Supply exactly one of `files` (an inline MTHDS "
            "closure), `method_ref` (a published method's address) or `method_id` (a stored method's "
            "catalog id)."
        )
        raise InputPreparationError(msg)
    if len(given) > 1:
        msg = (
            f"Cannot prepare inputs: {' and '.join(given)} were both given. Supply exactly one method "
            "selector — `files`, `method_ref` or `method_id`."
        )
        raise InputPreparationError(msg)
    return selected_files, selected_method_ref, selected_method_id


def _checked_pipe_ref(pipe_ref: object) -> str | None:
    """The caller's `pipe_ref`, normalized — `None` when absent, refused when bare or not a string.

    Qualified-only is this helper's contract: the descriptor is keyed by qualified refs, and a
    searched `pipe_code` is a run-route affordance preparation does not grow. The route would
    still resolve a bare code across domains today — the pipe-selector rule that refuses one
    server-side has not reached the runner's shared selection yet — so the refusal stays here,
    raised before any request.
    """
    requested = _caller_selector(pipe_ref, argument="pipe_ref")
    if requested is not None and "." not in requested:
        msg = f'Cannot prepare inputs: `pipe_ref` must be qualified (`domain.pipe_code`), got the bare "{requested}".'
        raise InputPreparationError(msg)
    return requested


def _is_pipe_selection_refusal(exc: ApiResponseError) -> bool:
    """Whether a `/v1/pipe-io` error is the route refusing the pipe selection, and nothing else."""
    return exc.status == _HTTP_UNPROCESSABLE_ENTITY and exc.error_type in _PIPE_SELECTION_ERROR_TYPES


def _selection_refusal_reason(exc: ApiResponseError) -> str:
    """The server's reason for a refused selection, with its candidates when the body lists them.

    The reason is the problem's `detail`. A candidate list the body carries as its own member is
    appended unless the detail already names every candidate, as the engine's ambiguity message
    does, so the refs are never repeated. Read defensively: a member that is not a list of
    strings is ignored rather than trusted.
    """
    reason = exc.server_message or exc.title or exc.response_body or exc.status_text
    raw_candidates: object = exc.problem.get(_CANDIDATES_MEMBER) if exc.problem is not None else None
    if not isinstance(raw_candidates, list):
        return reason
    candidates = [candidate for candidate in cast("list[object]", raw_candidates) if isinstance(candidate, str)]
    if not candidates or all(candidate in reason for candidate in candidates):
        return reason
    return f"{reason} Candidates: {', '.join(candidates)}."


async def _fetch_signature(client: _PrepareClient, *, request: PipeIORequest) -> PipeIOValidReport:
    """Ask `pipe_io` for the selected pipe's signature and hand back the valid report.

    The route selects the pipe — the request's `pipe_ref`, else a fetched package manifest's
    `main_pipe`, else the closure's single `main_pipe` declaration — so this module keeps no
    selection chain of its own, and a package that names its entry pipe in its manifest alone
    is selected like any other.

    The route runs no dry run. Preparation needs a pipe's DECLARED inputs, which static
    validation settles, so a pending signature elsewhere in the method does not refuse inputs
    to a pipe whose inputs are declared — whether the method runs is the run's verdict, not
    preparation's. An `is_valid: false` arm still means the closure does not load, which IS a
    preparation failure.

    A refused selection — a `422` whose `error_type` is an entry-lookup error (see
    `_PIPE_SELECTION_ERROR_TYPES`) — becomes an `InputPreparationError` carrying the server's
    reason and its candidates, with the `ApiResponseError` kept as its `__cause__` for a caller
    who needs the whole problem document. Every other non-2xx propagates as the
    `ApiResponseError` it is.
    """
    try:
        response = await client.pipe_io(request)
    except ApiResponseError as exc:
        if not _is_pipe_selection_refusal(exc):
            raise
        msg = f"Cannot prepare inputs: the pipe could not be selected — {_selection_refusal_reason(exc)}"
        raise InputPreparationError(msg) from exc

    if isinstance(response, CrateInvalidReport):
        first = response.validation_errors[0].message if response.validation_errors else response.message
        msg = f"Cannot prepare inputs: the method signature did not resolve — {first}"
        raise InputPreparationError(msg)
    return response


async def prepare_inputs(
    client: _PrepareClient,
    *,
    files: list[MthdsFileItem] | None = None,
    method_ref: str | None = None,
    method_id: str | None = None,
    pipe_ref: str | None = None,
    inputs: dict[str, Any],
) -> PreparedInputs:
    """Prepare a pipe's inputs: upload local/byte/data-URL assets at the signature's
    file-bearing positions and return copy-on-write rewritten inputs plus upload records.

    Args:
        client: The client supplying `upload` and `pipe_io`.
        files: The method closure inline. Exactly one of `files` / `method_ref` / `method_id`.
        method_ref: A published method's address —
            `github.com/<owner>/<repo>[/<selector>][@<tag>]` — resolved by the runner.
        method_id: A stored method's hosted catalog id (`mt_…`), resolved by the platform.
            A pure pass-through: nothing is expanded client-side.
        pipe_ref: The target pipe as a QUALIFIED `domain.pipe_code`. Omit it and the route
            selects the method's entry pipe — see "Pipe selection" in
            `docs/input-preparation.md`. A bare `pipe_code` is refused before any request:
            the descriptor is keyed by qualified refs, and search is a run-route affordance
            this helper deliberately does not grow.
        inputs: The caller's inputs (variable name → value), compact or explicit-envelope
            per input.

    Returns:
        `PreparedInputs` — a copy of `inputs` with each file-bearing value rewritten to
        canonical content carrying `pipelex-storage://` in `url`, plus one `UploadRecord`
        per uploaded asset.

    Raises:
        InputPreparationError: No selector or several; a selector or `pipe_ref` that is not a
            string; a bare `pipe_ref`; the closure did not resolve; the route refused the pipe
            selection with the runner's entry-lookup `error_type` — an unknown `pipe_ref`, or
            no `pipe_ref` and a method declaring no single entry pipe — carrying the server's
            reason, with the `ApiResponseError` as its `__cause__`; or a value at a file
            position is unusable. HTTP(S) URLs and
            existing `pipelex-storage://` URIs pass through unchanged, and every failure is
            raised BEFORE any run is created.
        ApiResponseError: Any other no-verdict condition from `/v1/pipe-io` — an unknown or
            foreign-org `method_id` or no package at a `method_ref` address (`404`), a
            `method_ref` that does not parse or fetch or a stored method with no source
            (`422`), a registry-form `method_ref` (`501`), auth, a server fault, or an API that
            does not serve the route at all.
    """
    selected_files, selected_method_ref, selected_method_id = _resolve_selector(files=files, method_ref=method_ref, method_id=method_id)
    # Checked here rather than after the round-trip, so a mistyped or bare `pipe_ref` is refused
    # on the same pre-request boundary as a mistyped selector.
    requested_pipe_ref = _checked_pipe_ref(pipe_ref)
    request = PipeIORequest(files=selected_files, method_ref=selected_method_ref, method_id=selected_method_id, pipe_ref=requested_pipe_ref)
    report = await _fetch_signature(client, request=request)

    selected_pipe_ref = report.pipe_ref
    descriptor = report.input_form.get(selected_pipe_ref) if selected_pipe_ref is not None else None
    if descriptor is None:
        # The route promises the selected pipe's descriptor on every single-pipe valid answer.
        # Never a silent degrade to "no uploads": without it there is no signature to prepare
        # against, and the caller's local paths would travel to the runner verbatim.
        msg = f"Cannot prepare inputs: the pipe-io answer carries no input-form descriptor for the selected pipe ({selected_pipe_ref!r})."
        raise InputPreparationError(msg)
    declared = {field.name: field for field in descriptor.fields}

    ctx = _PrepareContext(client)
    rewritten = dict(inputs)
    for name, caller_value in inputs.items():
        field = declared.get(name)
        if field is None:
            # Not a declared input — pass through untouched.
            continue
        if _is_explicit_envelope(caller_value):
            # Unwrap, walk the content against the same node, re-wrap: the concept annotation
            # rides through to the run, which accepts the envelope as an input.
            envelope = cast("dict[str, Any]", caller_value)
            walked = await _resolve_node(ctx, field, envelope["content"])
            rewritten[name] = {**envelope, "content": walked}
        else:
            rewritten[name] = await _resolve_node(ctx, field, caller_value)

    return PreparedInputs(inputs=rewritten, uploads=ctx.uploads)
