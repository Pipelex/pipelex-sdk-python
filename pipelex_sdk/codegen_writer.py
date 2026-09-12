"""Write a `/v1/codegen` response to disk verbatim: the tree a local `pipelex codegen types` run writes.

`client.codegen()` returns stamped artifacts and their `codegen.lock`; `write_codegen_tree` is the last
step, and it puts those bytes on disk and does nothing else. Every artifact is written exactly as the
server sent it, at its declared `path`, and the lock is written as `lock_filename`: no reformatting, no
re-serialized lock, no newline translation. That byte-for-byte fidelity is what makes the tree identical
to a local `pipelex codegen types` run, and therefore what lets the offline codegen check pass on it.
Reformatting an artifact or rebuilding the lock breaks that trust chain.

The discipline is `pipelex`'s own `write_stamped_projection` (`pipelex/codegen/emission.py`), applied to
a projection the server has already stamped:

- **Validate before writing.** A `lock_filename` other than `codegen.lock`, an unsafe or duplicate
  artifact path, a lock that cannot be read or does not track exactly the artifacts, a symbolic link or
  a regular file on the way to a destination, a destination that is not a regular file, or a previously
  tracked path about to be pruned that fails those same checks refuses the whole tree before the first
  byte is written. The lock and prune checks go further than `pipelex`, which builds its lock from the
  files it writes, so the two cannot disagree, and which resolves prune targets only after writing.
- **Never overwrite a file codegen does not own.** A file already at an artifact's path is replaced only
  when its content is already identical, when the previous lock tracked it, or when it carries a stamp.
- **Write only what changed.** An already-current file is left alone, so regenerating over a current
  tree is a true no-op: no mtime churn and clean diffs.
- **Prune what dropped out.** A file the previous lock tracked, that the new set no longer contains and
  that still carries its stamp, is deleted, so a removed concept never lingers as a stale artifact. A
  file whose stamp was removed by hand is left alone, and so is any file the lock never tracked.
- **Write the lock last.**

Everything else is project policy and stays with the caller: which methods to generate, where each tree
goes, and whatever a project writes beside the tree. The function takes one response and one directory.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from pipelex_sdk.codegen_lock import CODEGEN_LOCK_FILENAME, load_lock, parse_lock, resolve_artifact_path, resolve_output_path, validate_artifact_paths
from pipelex_sdk.codegen_stamp import comment_prefix_for, has_stamp
from pipelex_sdk.crate_models import CodegenValidReport
from pipelex_sdk.errors import CodegenError, CodegenLockError


class CodegenTreeWriteReport(BaseModel):
    """What one tree write did to the output directory. Every path is relative to that directory."""

    model_config = ConfigDict(frozen=True)

    written: list[str] = Field(default_factory=list)
    """Artifacts whose content changed and were written, in response order."""

    unchanged: list[str] = Field(default_factory=list)
    """Artifacts that were already current and were left untouched, in response order."""

    removed: list[str] = Field(default_factory=list)
    """Stamped artifacts the previous lock tracked that dropped out of the set and were deleted, sorted."""

    lock_written: bool
    """Whether `codegen.lock` changed and was written."""


def write_codegen_tree(report: CodegenValidReport, *, output_dir: Path) -> CodegenTreeWriteReport:
    """Write a valid `/v1/codegen` report into `output_dir`, verbatim.

    Branch on `is_valid` first: only the valid arm carries a tree. Raises `CodegenError` before anything
    is written when the report, its lock or the directory is unsafe, and when a file codegen does not own
    sits at an artifact's path. The report's lock must parse and track exactly its artifacts, because it is
    what the next run trusts for ownership and pruning. A previous lock that cannot be read is replaced,
    and nothing is pruned on its account; a previous lock tracking an unsafe path is refused.
    """
    if report.lock_filename != CODEGEN_LOCK_FILENAME:
        msg = (
            f"Refusing to write a codegen tree whose lock is named '{report.lock_filename}': this SDK writes and reads "
            f"'{CODEGEN_LOCK_FILENAME}' only, so a lock under another name would guard nothing. Upgrade pipelex-sdk."
        )
        raise CodegenError(msg)
    validate_artifact_paths(artifact.path for artifact in report.artifacts)
    _validate_response_lock(report)

    lock_path = resolve_output_path(root=output_dir, relative_path=Path(CODEGEN_LOCK_FILENAME))
    output_root = lock_path.parent
    previous_paths = _previous_tracked_paths(lock_path)

    destinations: dict[str, Path] = {}
    for artifact in report.artifacts:
        destinations[artifact.path] = resolve_artifact_path(root=output_root, artifact_path=artifact.path)
    # Resolved now rather than while pruning: a tracked path that has since become a directory or moved
    # behind a symbolic link must refuse the tree before the first write, not after the artifacts changed.
    stale_destinations: dict[str, Path] = {}
    for stale_path in sorted(previous_paths - set(destinations)):
        stale_destinations[stale_path] = resolve_artifact_path(root=output_root, artifact_path=stale_path)
    _preflight_destinations(report=report, destinations=destinations, previous_paths=previous_paths)

    written: list[str] = []
    unchanged: list[str] = []
    for artifact in report.artifacts:
        if _write_if_changed(path=destinations[artifact.path], content=artifact.content):
            written.append(artifact.path)
        else:
            unchanged.append(artifact.path)

    removed = _prune_delisted(stale_destinations=stale_destinations)
    lock_written = _write_if_changed(path=lock_path, content=report.lock)

    return CodegenTreeWriteReport(written=written, unchanged=unchanged, removed=removed, lock_written=lock_written)


def _validate_response_lock(report: CodegenValidReport) -> None:
    """Refuse a report whose lock cannot be read or does not track exactly its artifacts, before any write.

    `pipelex` builds its lock from the files it is writing, so the two agree by construction. Here they
    arrive as two independent fields, and the lock written now is what the next run trusts: a path it
    tracks may be overwritten without a stamp and pruned. A lock tracking a path the report never wrote
    would hand that ownership to a file codegen never produced, a lock this SDK cannot parse would switch
    pruning off on the next run, and one tracking an unsafe path would make every later write refuse.
    """
    try:
        lock = parse_lock(report.lock)
    except CodegenLockError as exc:
        msg = f"Refusing to write a codegen tree whose lock this SDK cannot read: {exc}"
        raise CodegenError(msg) from exc
    artifact_paths = {artifact.path for artifact in report.artifacts}
    lock_paths = lock.paths()
    if lock_paths != artifact_paths:
        msg = (
            "Refusing to write a codegen tree whose lock does not track exactly its artifacts: "
            f"untracked artifacts {sorted(artifact_paths - lock_paths)}, tracked paths with no artifact {sorted(lock_paths - artifact_paths)}."
        )
        raise CodegenError(msg)


def _previous_tracked_paths(lock_path: Path) -> set[str]:
    try:
        lock = load_lock(lock_path)
    except CodegenLockError:
        # The new response is authoritative and replaces a corrupt or unreadable prior lock. Without a
        # trustworthy artifact set there is nothing safe to prune, so the write proceeds with none. An
        # unsafe tracked path is a plain `CodegenError` instead, and propagates: it is a containment
        # violation, and recovering from it would weaken the boundary.
        return set()
    return lock.paths() if lock is not None else set()


def _preflight_destinations(*, report: CodegenValidReport, destinations: dict[str, Path], previous_paths: set[str]) -> None:
    """Refuse to replace a file codegen does not own, before any artifact is written."""
    for artifact in report.artifacts:
        destination = destinations[artifact.path]
        existing = _read_bytes_or_none(destination)
        if existing is None or existing == artifact.content.encode("utf-8"):
            continue
        if artifact.path in previous_paths or _is_stamped(existing, artifact_path=artifact.path):
            continue
        msg = f"Refusing to overwrite unowned file '{destination}'. Move it or choose a different codegen output directory."
        raise CodegenError(msg)


def _prune_delisted(*, stale_destinations: dict[str, Path]) -> list[str]:
    """Delete files the previous lock tracked and the new set dropped, but only those still carrying a stamp.

    `stale_destinations` maps each such path, in sorted order, to the destination the preflight resolved.
    """
    removed: list[str] = []
    for relative_path, stale_path in stale_destinations.items():
        existing = _read_bytes_or_none(stale_path)
        if existing is None:
            continue
        if _is_stamped(existing, artifact_path=relative_path):
            stale_path.unlink()
            removed.append(relative_path)
    return removed


def _write_if_changed(*, path: Path, content: str) -> bool:
    """Write `content` to `path` as UTF-8 bytes, only when they differ from what is there. Returns whether it wrote.

    Bytes rather than text on purpose: text mode would translate every line feed into the platform's line
    separator, and the same response written on Windows and on Linux would then be two different trees.
    """
    encoded = content.encode("utf-8")
    if _read_bytes_or_none(path) == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return True


def _read_bytes_or_none(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    return path.read_bytes()


def _is_stamped(content: bytes, *, artifact_path: str) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # A stamp is UTF-8 text, so bytes that do not decode cannot open with one.
        return False
    return has_stamp(text, comment_prefix=comment_prefix_for(artifact_path))
