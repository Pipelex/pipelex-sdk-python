"""The codegen stamp header: the grammar that lets a lone generated file testify about itself.

Mirrors `pipelex/codegen/stamp.py`, the reference for that grammar. Every generated artifact opens with a
fenced comment block (`# >>> pipelex-codegen-stamp >>>` … `# <<< pipelex-codegen-stamp <<<` in Python, the
same fence behind `//` in TypeScript) recording the crate fingerprint, the engine version, the projection
and a hash of the body below the fence. Two readers in this SDK live off it.

The **writer** needs two facts and nothing more. The stampable suffixes are the only file types that can
ever be an artifact, which makes them part of the path rules in `pipelex_sdk.codegen_lock`. And
`has_stamp`, the begin-line predicate, decides whether a file already on disk belongs to codegen: the
writer overwrites or prunes a file only when it does.

The **offline check** needs the rest: `parse_stamped` splits a file into the hash its stamp recorded and
the body that hash covers, and `compute_content_hash` recomputes it. That is the whole of the drift
verdict for one file — no engine, no network, no key.

One deliberate relaxation against the reference, inherited from `@pipelex/sdk`'s port: the projection
line must be *present and well-formed*, but its `kind` / `target` values are not checked against this
SDK's vocabulary. `pipelex` validates them against its own enums, which cannot lag its own emitter; an
SDK copy can, and rejecting an unknown-but-valid future `kind` would report every artifact in the tree
as hand-edited. For today's vocabulary the two readers are identical.

Nothing here builds or rewrites a stamp. The server stamps, and the SDK keeps the bytes it was sent.
"""

import hashlib
import json
from pathlib import PurePosixPath
from typing import NoReturn

from pydantic import BaseModel, ConfigDict

from pipelex_sdk.errors import CodegenError

_BEGIN_MARKER = ">>> pipelex-codegen-stamp >>>"
_END_MARKER = "<<< pipelex-codegen-stamp <<<"

_COMMENT_PREFIX_BY_SUFFIX = {".py": "#", ".ts": "//"}

STAMPABLE_SUFFIXES = frozenset(_COMMENT_PREFIX_BY_SUFFIX)
"""The file suffixes codegen stamps, mirroring pipelex's `STAMPABLE_SUFFIXES`.

Public so a caller filters a tree walk exactly as the check does: a file whose suffix is not here can
never be an artifact and can never be an orphan, so it is skipped rather than refused — which is what
lets a project park a sidecar such as `sources.json` beside the lock.
"""


class ParsedStamp(BaseModel):
    """A stamp read back off a file: the hash it recorded, and the body text that hash covers."""

    model_config = ConfigDict(frozen=True)

    content_hash: str
    """The `content_hash` field as the stamp spells it — recomputed from `body` by the offline check."""

    body: str
    """Everything below the end-marker line, byte-exact, so rehashing it reproduces the recorded value."""


def is_stampable_artifact_path(artifact_path: str) -> bool:
    """Whether `artifact_path` names a file type codegen stamps, and therefore one the check considers."""
    return PurePosixPath(artifact_path).suffix in STAMPABLE_SUFFIXES


def comment_prefix_for(artifact_path: str) -> str:
    """The line-comment prefix an artifact is stamped in, by suffix (`.py` → `#`, `.ts` → `//`)."""
    prefix = _COMMENT_PREFIX_BY_SUFFIX.get(PurePosixPath(artifact_path).suffix)
    if prefix is None:
        expected = ", ".join(sorted(STAMPABLE_SUFFIXES))
        msg = f"No codegen stamp syntax for '{artifact_path}': its file type is not stampable (expected one of: {expected})."
        raise CodegenError(msg)
    return prefix


def has_stamp(content: str, *, comment_prefix: str) -> bool:
    """Whether `content` opens with a codegen stamp block in the given comment syntax."""
    return content.startswith(f"{comment_prefix} {_BEGIN_MARKER}")


def compute_content_hash(body: str) -> str:
    """The canonical content hash of a generated body: lowercase SHA-256 hex over its UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_stamped(content: str, *, comment_prefix: str) -> ParsedStamp | None:
    """Split a stamped file into the hash its stamp recorded and the body below the fence, or `None`.

    `None` means there is no stamp this reader will trust — a missing or unterminated fence, a header
    line that is not a comment, a malformed projection, or options that are not a JSON object — and the
    check reports such a file as hand-edited. The body is everything after the end-marker line,
    byte-exact, so recomputing its hash reproduces the value the stamp recorded.
    """
    begin_line = f"{comment_prefix} {_BEGIN_MARKER}"
    end_line = f"{comment_prefix} {_END_MARKER}"
    if not content.startswith(begin_line):
        return None
    end_index = content.find(f"\n{end_line}\n")
    if end_index == -1:
        return None
    header_region = content[len(begin_line) + 1 : end_index]
    body = content[end_index + len(end_line) + 2 :]

    # Every line the emitter ever writes inside the fence carries the comment prefix, so anything else in
    # there was injected by hand — and it would otherwise verify as pristine, since the hash covers only
    # the body below the fence. An executable line hiding inside a "DO NOT EDIT" block is not a valid stamp.
    #
    # `splitlines` is the deliberate split, as in the reference: it also breaks on U+2028, U+2029 and
    # U+0085, and the first two terminate a `//` comment in ECMAScript. Split on `"\n"` alone, a `.ts`
    # header carrying a raw U+2028 followed by a statement is one prefixed line to this gate and two lines
    # to the JavaScript engine — so the check would report the file current while it executes the injected
    # code, since the header itself is not hashed.
    if any(not line.startswith(comment_prefix) for line in header_region.splitlines()):
        return None

    fields = _parse_fields(header_region, comment_prefix=comment_prefix)
    projection = fields.get("projection")
    if projection is None or not _is_well_formed_projection(projection):
        return None
    if not _is_json_object(fields.get("options", "{}")):
        return None
    return ParsedStamp(content_hash=fields.get("content_hash", ""), body=body)


def _parse_fields(header_region: str, *, comment_prefix: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    # The same split rule as the gate in `parse_stamped`, and it has to stay the same one: a narrower
    # split here would rejoin a line the gate had already split, so a field value would swallow the
    # injected text the gate exists to catch.
    for raw_line in header_region.splitlines():
        # `parse_stamped` has already rejected any line without the prefix, so stripping it is unconditional.
        stripped = raw_line[len(comment_prefix) :].strip()
        key, separator, value = stripped.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _is_well_formed_projection(projection: str) -> bool:
    """`<kind> / <target>`, with an optional trailing ` / <pipe_ref>` for a per-pipe projection.

    Shape only: see the module docstring on why the axes' values are not matched against this SDK's
    vocabulary.
    """
    parts = [segment.strip() for segment in projection.split("/")]
    return len(parts) >= 2 and parts[0] != "" and parts[1] != ""


def _is_json_object(options_raw: str) -> bool:
    try:
        # The stamp header is a cross-language interchange format, so a stamp only Python can read is not
        # a valid stamp: `parse_constant` turns `NaN` / `Infinity` / `-Infinity` — which Python's `json`
        # accepts and conformant parsers refuse — into the `ValueError` below, as the reference does.
        loaded = json.loads(options_raw, parse_constant=_reject_json_constant)
    except ValueError:  # JSONDecodeError is a subclass, so this one clause covers malformed JSON too
        return False
    return isinstance(loaded, dict)


def _reject_json_constant(value: str) -> NoReturn:
    msg = f"Non-standard JSON constant in stamp options: {value}"
    raise ValueError(msg)
