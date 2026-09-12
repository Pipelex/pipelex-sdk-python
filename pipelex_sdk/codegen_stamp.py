"""The codegen stamp header, as far as a tree writer needs it.

Mirrors `pipelex/codegen/stamp.py`, the reference for the stamp grammar. Every generated artifact opens
with a fenced comment block (`# >>> pipelex-codegen-stamp >>>` … `# <<< pipelex-codegen-stamp <<<` in
Python, the same fence behind `//` in TypeScript) recording the crate fingerprint, the engine version,
the projection and a hash of the body below the fence.

A writer needs two facts from that grammar and nothing more. The stampable suffixes are the only file
types that can ever be an artifact, which makes them part of the path rules in
`pipelex_sdk.codegen_lock`. And the begin-line predicate decides whether a file already on disk belongs
to codegen: the writer overwrites or prunes a file only when it does, and the offline check calls a
stamped file the lock does not track an orphan by that same predicate, so the two agree on ownership by
construction.

Nothing here builds or rewrites a stamp. The server stamps, and the SDK keeps the bytes it was sent.
"""

from pathlib import PurePosixPath

from pipelex_sdk.errors import CodegenError

_BEGIN_MARKER = ">>> pipelex-codegen-stamp >>>"

_COMMENT_PREFIX_BY_SUFFIX = {".py": "#", ".ts": "//"}

STAMPABLE_SUFFIXES = frozenset(_COMMENT_PREFIX_BY_SUFFIX)
"""The file suffixes codegen stamps, mirroring pipelex's `STAMPABLE_SUFFIXES`."""


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
