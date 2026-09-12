"""The offline codegen drift check: pure hashing over a tree and its lock — no engine, no network, no key.

`client.codegen()` returns stamped artifacts plus a `codegen.lock`, and `write_codegen_tree` puts them on
disk; a project that commits that tree needs a CI gate over it. This is that gate, and it is the reason a
Python consumer of the hosted API needs no `pipelex` install to have one: before this existed, the only
offline gate available was the `pipelex` CLI — the whole runtime, which is exactly the dependency a
hosted-API consumer took this SDK to avoid. `@pipelex/sdk` has carried the TypeScript half since v0.12.0;
this is its counterpart.

**What it proves, and what it cannot.** It proves the tree and its lock still agree with each other: no
artifact edited, none missing, none lingering. Whether the tree still matches what the *method* resolves
to is a second question, and answering it needs the engine — so the check never asks it. A caller closes
that gap by comparing `CodegenCheckReport.crate_fingerprint` against a live `codegen()` response. This
split is the point: regeneration is a **dev action** (it needs the engine), the check is the **CI action**
(it needs only hashes), so an upstream template improvement never reddens a consumer's CI.

**A mirror, deliberately.** The algorithm is `pipelex.codegen.check.run_codegen_check`, and the spec pins
it as pure hashing precisely so that every client — the CLI, an SDK, a short CI script — reaches the same
verdict over the same bytes. The drift `detail` sentences are kept verbatim for the same reason: a
consumer moving between `pipelex codegen check` and this function reads the same report. The one
documented relaxation is in `pipelex_sdk.codegen_stamp.parse_stamped`, which does not match the
projection axes against this SDK's vocabulary.

The algorithm, per the codegen spec's "Offline check algorithm":

1. For each artifact the lock tracks, locate the file and recompute the hash of the body below its stamp;
   absent is a `missing` drift, a mismatch is a `modified` drift.
2. A locked file whose stamp is gone, unparseable or self-inconsistent is `hand-edited`.
3. A stamped file the lock does not track is an `orphan` — the stale-artifact class a per-file stamp
   cannot catch on its own, which is the whole reason the lock exists beside the stamps.
"""

from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from pipelex_sdk._pydantic_utils import empty_list_factory_of
from pipelex_sdk.codegen_lock import CODEGEN_LOCK_FILENAME, CodegenLock, load_lock, resolve_artifact_path, resolve_output_path
from pipelex_sdk.codegen_stamp import STAMPABLE_SUFFIXES, comment_prefix_for, compute_content_hash, has_stamp, parse_stamped
from pipelex_sdk.errors import CodegenError, CodegenLockError

_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
"""Vendor / VCS / cache directories the orphan scan never descends into, mirroring pipelex's list.

They never hold generated output, so pruning them keeps a check run at a project root from reading — and
choking on — unrelated files beneath them.
"""

_MISSING_DETAIL = "Locked artifact is absent on disk."
_NO_STAMP_DETAIL = "Stamp header is missing or unparseable."
_STAMP_MISMATCH_DETAIL = "Body was edited below the stamp (stamp hash no longer matches)."
_MODIFIED_DETAIL = "Body no longer matches the locked hash — regenerate."
_ORPHAN_DETAIL = "Stamped generated file not tracked by the lock — stale; remove or regenerate."
_NOT_UTF8_DETAIL = "File is not valid UTF-8 — not generated output."


class DriftCategory(StrEnum):
    """The kind of drift found for one artifact — the canonical wire values, shared with pipelex."""

    MISSING = "missing"
    """Tracked by the lock, absent on disk (a deleted-artifact drift)."""

    MODIFIED = "modified"
    """Present, and its stamp is self-consistent, but its body hash no longer matches the locked hash."""

    HAND_EDITED = "hand-edited"
    """Present, but its stamp is missing, unparseable or self-inconsistent (edited below the stamp)."""

    ORPHAN = "orphan"
    """A stamped generated file on disk that the lock does not track (a stale lingering artifact)."""


class CodegenDrift(BaseModel):
    """One drifting artifact: its path relative to the lock, the drift category, and a human detail."""

    model_config = ConfigDict(frozen=True)

    path: str
    category: DriftCategory
    detail: str


class CodegenCheckReport(BaseModel):
    """The structured verdict of one offline check over one output root."""

    model_config = ConfigDict(frozen=True)

    lock_found: bool
    """Whether a `codegen.lock` was there at all. No lock is not a drift — it is nothing to check."""

    drifts: list[CodegenDrift] = Field(default_factory=empty_list_factory_of(CodegenDrift))
    """Every drift found: locked-artifact drifts first in ascending path order, then orphans in that order."""

    crate_fingerprint: str | None = None
    """The lock header's crate fingerprint, or `None` when no lock was found.

    Surfaced so a caller can compare a committed tree against a live `codegen()` response's
    `crate_fingerprint` — the engine-needing comparison this check deliberately never makes itself.
    """

    engine_version: str | None = None
    """The lock header's `pipelex` engine version, or `None` when no lock was found — same purpose."""

    @property
    def is_current(self) -> bool:
        """Whether the generated tree is in sync: a lock was found and no drift was detected."""
        return self.lock_found and not self.drifts


def run_codegen_check(*, root: Path) -> CodegenCheckReport:
    """Run the offline drift check over `root`, the directory holding `codegen.lock`.

    Pass the same directory `write_codegen_tree` wrote into; the two are counterparts, and a tree that
    writer produced from a `codegen()` response is current by construction.

    The drift order is part of the contract, so a second implementation can mirror it exactly: every
    locked-artifact drift comes first, in ascending order of the full relative path compared as a plain
    string, then every orphan, ordered by that same rule. A locked path yields **at most one** drift, and
    `hand-edited` outranks `modified` when a file is both self-inconsistent and off the locked hash.

    Artifacts are read in text mode, so Python's universal-newline translation folds a CRLF and a lone CR
    into an LF before anything is hashed. That is not a convenience but what keeps this check in agreement
    with `pipelex codegen check`, whose reader does the same: a tree generated on Windows, or checked out
    under `core.autocrlf=true`, is current to both readers rather than hand-edited to one.

    Raises `CodegenLockError` for a no-verdict condition — a lock that is malformed, unreadable or of a
    `lock_version` this SDK does not know, or a tree whose paths are not safe and canonical. A drift is a
    verdict and rides the report; this is the absence of one.
    """
    try:
        lock_path = resolve_output_path(root=root, relative_path=Path(CODEGEN_LOCK_FILENAME))
        safe_root = lock_path.parent
        lock = load_lock(lock_path)
        if lock is None:
            return CodegenCheckReport(lock_found=False)

        drifts: list[CodegenDrift] = []
        drifts += _check_locked_artifacts(root=safe_root, lock=lock)
        drifts += _find_orphans(root=safe_root, lock=lock)
        return CodegenCheckReport(
            lock_found=True,
            drifts=drifts,
            crate_fingerprint=lock.crate_fingerprint,
            engine_version=lock.engine_version,
        )
    except CodegenLockError:
        # Already precise and actionable — re-wrapping it as an unsafe tree would bury the one thing the
        # reader needs to know. `CodegenLockError` subclasses `CodegenError`, so this clause comes first.
        raise
    except CodegenError as exc:
        msg = f"Unsafe codegen artifact tree at '{root}': {exc}"
        raise CodegenLockError(msg) from exc


def _check_locked_artifacts(*, root: Path, lock: CodegenLock) -> list[CodegenDrift]:
    drifts: list[CodegenDrift] = []
    for path, locked_hash in sorted(lock.hash_by_path().items()):
        file_path = resolve_artifact_path(root=root, artifact_path=path)
        if not file_path.is_file():
            drifts.append(CodegenDrift(path=path, category=DriftCategory.MISSING, detail=_MISSING_DETAIL))
            continue
        drift = _check_present_artifact(path=path, file_path=file_path, locked_hash=locked_hash)
        if drift is not None:
            drifts.append(drift)
    return drifts


def _check_present_artifact(*, path: str, file_path: Path, locked_hash: str) -> CodegenDrift | None:
    """The drift for one locked artifact that is present, or `None` when it is current.

    The precedence is the point: returning early makes `hand-edited` outrank `modified`, so a hand edit —
    which trips both the stamp check and the lock check — is reported once, as the hand edit.
    """
    text = _read_text_or_none(file_path)
    if text is None:
        return CodegenDrift(path=path, category=DriftCategory.HAND_EDITED, detail=_NOT_UTF8_DETAIL)
    # Never raises: a path the lock tracks was suffix-validated when the lock was parsed.
    parsed = parse_stamped(text, comment_prefix=comment_prefix_for(path))
    if parsed is None:
        return CodegenDrift(path=path, category=DriftCategory.HAND_EDITED, detail=_NO_STAMP_DETAIL)
    body_hash = compute_content_hash(parsed.body)
    if parsed.content_hash != body_hash:
        return CodegenDrift(path=path, category=DriftCategory.HAND_EDITED, detail=_STAMP_MISMATCH_DETAIL)
    if body_hash != locked_hash:
        return CodegenDrift(path=path, category=DriftCategory.MODIFIED, detail=_MODIFIED_DETAIL)
    return None


def _find_orphans(*, root: Path, lock: CodegenLock) -> list[CodegenDrift]:
    tracked = lock.paths()
    orphans: list[CodegenDrift] = []
    for file_path in _iter_stampable_files(directory=root):
        relative = file_path.relative_to(root).as_posix()
        if relative in tracked:
            continue
        text = _read_text_or_none(file_path)
        if text is not None and has_stamp(text, comment_prefix=comment_prefix_for(relative)):
            orphans.append(CodegenDrift(path=relative, category=DriftCategory.ORPHAN, detail=_ORPHAN_DETAIL))
    return sorted(orphans, key=lambda drift: drift.path)


def _iter_stampable_files(*, directory: Path) -> Iterator[Path]:
    """Yield stampable files under `directory`, pre-order and deterministic, pruning vendor / VCS dirs.

    Symbolic links are skipped rather than refused, as in the reference: the orphan scan reads whatever
    happens to share the output root, and a link parked beside the tree is not the tree's fault. A link at
    an artifact's own path is a different matter and `resolve_artifact_path` still refuses it.
    """
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in _SKIP_DIRS:
                yield from _iter_stampable_files(directory=entry)
        elif entry.suffix in STAMPABLE_SUFFIXES:
            yield entry


def _read_text_or_none(path: Path) -> str | None:
    """Read UTF-8 text, or `None` when the bytes are not UTF-8 — in which case it is not generated output.

    Text mode on purpose: see `run_codegen_check` on universal-newline translation.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
