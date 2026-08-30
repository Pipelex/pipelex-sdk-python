"""`prepare_inputs` — signature-driven input preparation. Names the method three ways,
resolves the target pipe's declared inputs from the standard's input-form descriptor,
interprets the caller's inputs top-down against it, uploads the file-bearing values, and
returns rewritten inputs (canonical content carrying `pipelex-storage://` in `url`) plus one
upload record per prepared asset. Python counterpart of `pipelex-sdk-js`'s `prepareInputs`.

The signature comes from ONE `POST /v1/validate` asking for `views: ["input_form"]`, and the
walk is discriminated on each descriptor node's declared `kind` — never on the shape of a
value. That is the whole point: the previous source, the explicit inputs template, marked a
file position by rendering a `{"url": …}` dict, which is a side effect of a field being NAMED
`url` rather than of its concept being an Image or a Document. Two positions were misread as a
result — an OPTIONAL nested file field, which the required-only template never rendered, was
left un-uploaded and its local path travelled to the runner as a literal string; and a text
field merely named `url` was read from disk and uploaded. The descriptor states the resolved
kind at every depth and includes optional fields, so both are gone.

See `docs/input-preparation.md`, and the design of record in
`pipelex-sdk-js/wip/prepare-inputs-selectors/design.md`.
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
    InputForm,
    InputFormItem,
    ListItem,
    NumberItem,
    ObjectItem,
    ProseItem,
    TextItem,
    UnknownItem,
)
from pydantic import BaseModel

from pipelex_sdk.errors import InputPreparationError
from pipelex_sdk.upload import UploadRecord, UploadSource, upload_file
from pipelex_sdk.validation_models import VALIDATION_VIEW_INPUT_FORM, PipelexInvalidReport, PipelexValidationReport, PipelexValidationResult

if TYPE_CHECKING:
    from pipelex_sdk.crate_models import MthdsFileItem
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
    """The client surface `prepare_inputs` needs: raw `upload` plus `validate` as the
    signature source. Typed as `PipelexAPIClient.validate`'s own signature so the client
    satisfies it structurally.
    """

    async def upload(self, upload_input: UploadInput) -> UploadedFile: ...

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
    ) -> PipelexValidationResult: ...


class _PrepareContext:
    """Mutable state threaded through one preparation walk."""

    def __init__(self, client: _PrepareClient) -> None:
        self.client = client
        self.uploads: list[UploadRecord] = []
        # Dedup by source identity: same source (str/bytes/Path value) uploads once.
        self.dedup: dict[UploadSource, str] = {}


def _non_empty_string(value: object) -> str | None:
    """A trimmed non-empty string, or `None` — the "empty is absent" rule.

    Deliberately local rather than reusing `client.py`'s `_normalized_selector`: that helper
    is private to the client boundary and raises `PipelineRequestError`, where every failure
    of this module owes an `InputPreparationError`.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


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
    a real one without tripping the XOR. The check lives here because this module is what
    composes the `validate` call, and it runs BEFORE any request.
    """
    selected_files = files or None
    selected_method_ref = _non_empty_string(method_ref)
    selected_method_id = _non_empty_string(method_id)

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


async def _fetch_signature(
    client: _PrepareClient,
    *,
    files: list[MthdsFileItem] | None,
    method_ref: str | None,
    method_id: str | None,
) -> PipelexValidationReport:
    """Ask `validate` for the signature, whatever the selector, and hand back the valid report.

    `allow_signatures=True` on purpose: preparation needs a pipe's DECLARED inputs, and a
    bundle mid-authoring with an unresolved signature elsewhere must not be refused inputs for
    a pipe whose inputs are declared — whether the bundle runs is the run's verdict, not
    preparation's. An `is_valid: false` arm still means the closure does not load, which IS a
    preparation failure.

    No timeout override for a `method_ref`: `validate` already rides the 20-minute blocking
    ceiling, and the internal 3-minute fetch budget exists to RAISE the ~30s poll-ceiling
    routes, not to lower this one.
    """
    views = [VALIDATION_VIEW_INPUT_FORM]
    result: PipelexValidationResult
    if files is not None:
        contents = [file_item.content for file_item in files]
        # `validate_files`' rule: label every content once any file names a source, so the
        # server never sees a length-mismatched `mthds_sources` array.
        sources: list[str] | None
        if any(file_item.source is not None for file_item in files):
            sources = [file_item.source or f"inline://file-{index + 1}.mthds" for index, file_item in enumerate(files)]
        else:
            sources = None
        result = await client.validate(contents, True, sources, None, views)
    else:
        result = await client.validate(None, True, None, None, views, method_ref=method_ref, method_id=method_id)

    if isinstance(result, PipelexInvalidReport):
        first = result.validation_errors[0].message if result.validation_errors else result.message
        msg = f"Cannot prepare inputs: the method signature did not resolve — {first}"
        raise InputPreparationError(msg)
    return result


def _blueprint_main_pipe_ref(blueprint: dict[str, Any]) -> str | None:
    """The bundle blueprint's declared `main_pipe`, qualified by its `domain` when authored bare.

    Every read is defensive: `bundle_blueprint` is carried opaquely by this SDK on purpose —
    its schema is the runtime's, not ours — so a shape that does not match falls through
    rather than raising.
    """
    main_pipe = _non_empty_string(blueprint.get("main_pipe"))
    if main_pipe is None:
        return None
    if "." in main_pipe:
        return main_pipe
    domain = _non_empty_string(blueprint.get("domain"))
    return f"{domain}.{main_pipe}" if domain is not None else None


def _select_pipe_ref(report: PipelexValidationReport, input_form: InputForm, requested: str | None) -> str:
    """Pick the pipe whose descriptor guides the walk.

    `validate` has no pipe selector — its report describes every pipe, keyed by qualified
    `pipe_ref` — so the choice is made here, in the order `docs/input-preparation.md`
    documents: an explicit qualified `pipe_ref`, then the report's typed resolved default,
    then the bundle's declared `main_pipe`, then the single pipe, else an error naming the
    candidates.
    """
    refs = list(input_form)
    candidates = ", ".join(refs) if refs else "(none — the closure declares no pipes)"

    if requested is not None:
        if "." not in requested:
            msg = (
                "Cannot prepare inputs: `pipe_ref` must be qualified (`domain.pipe_code`), got the bare "
                f'"{requested}". The method declares: {candidates}.'
            )
            raise InputPreparationError(msg)
        if requested not in input_form:
            msg = f'Cannot prepare inputs: the method declares no pipe "{requested}". It declares: {candidates}.'
            raise InputPreparationError(msg)
        return requested

    # The typed resolved default, when the runner serves it (manifest-aware for a `method_ref`
    # package, which is why it outranks the blueprint read below).
    typed_default = _non_empty_string(report.default_pipe_ref)
    if typed_default is not None and typed_default in input_form:
        return typed_default

    blueprint_default = _blueprint_main_pipe_ref(report.bundle_blueprint)
    if blueprint_default is not None and blueprint_default in input_form:
        return blueprint_default

    if len(refs) == 1:
        return refs[0]

    msg = f"Cannot prepare inputs: the method declares no single default pipe, so `pipe_ref` is required. It declares: {candidates}."
    raise InputPreparationError(msg)


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
        client: The client supplying `upload` and `validate`.
        files: The method closure inline. Exactly one of `files` / `method_ref` / `method_id`.
        method_ref: A published method's address —
            `github.com/<owner>/<repo>[/<selector>][@<tag>]` — resolved by the runner.
        method_id: A stored method's hosted catalog id (`mt_…`), resolved by the platform.
            A pure pass-through: nothing is expanded client-side.
        pipe_ref: The target pipe as a QUALIFIED `domain.pipe_code`. Omit it to default —
            see "Pipe selection" in `docs/input-preparation.md`. A bare `pipe_code` is
            refused: the descriptor is keyed by qualified refs, and search is a run-route
            affordance this helper deliberately does not grow.
        inputs: The caller's inputs (variable name → value), compact or explicit-envelope
            per input.

    Returns:
        `PreparedInputs` — a copy of `inputs` with each file-bearing value rewritten to
        canonical content carrying `pipelex-storage://` in `url`, plus one `UploadRecord`
        per uploaded asset.

    Raises:
        InputPreparationError: No selector or several; the closure did not resolve; the
            report carries no descriptor; the pipe could not be selected; or a value at a
            file position is unusable. HTTP(S) URLs and existing `pipelex-storage://` URIs
            pass through unchanged, and every failure is raised BEFORE any run is created.
        ApiResponseError: A no-verdict condition from `/v1/validate` — a malformed selector,
            an unknown or foreign-org `method_id` (`404`), a stored method with no source, a
            fetch failure at the address.
    """
    selected_files, selected_method_ref, selected_method_id = _resolve_selector(files=files, method_ref=method_ref, method_id=method_id)
    report = await _fetch_signature(client, files=selected_files, method_ref=selected_method_ref, method_id=selected_method_id)

    input_form = report.input_form
    if input_form is None:
        # Never a silent degrade to "no uploads": without the descriptor there is no
        # signature to prepare against.
        msg = (
            "Cannot prepare inputs: the validate report carries no `input_form` descriptor — the signature "
            'preparation reads. The descriptor rides `views: ["input_form"]` on pipelex-api >= 0.18.0; '
            "point the client at a runner that serves it."
        )
        raise InputPreparationError(msg)

    selected_pipe_ref = _select_pipe_ref(report, input_form, _non_empty_string(pipe_ref))
    declared = {field.name: field for field in input_form[selected_pipe_ref].fields}

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
