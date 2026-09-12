"""`codegen.lock` and the artifact path rules: the set-level half of a generated codegen tree.

Mirrors `pipelex/codegen/lock.py`. A stamp lets a lone file testify about itself; the lock records the
generated artifact *set* (each artifact's path and body hash, plus the crate fingerprint and engine
version the set was generated against), so the one drift a stamp cannot see, a deleted concept whose
stale file lingers, is still caught. A tree writer reads the *previous* lock to learn which files it
tracked and may prune; the offline check reads the lock to find a missing or an orphaned artifact.

The lock text is never produced here. `/v1/codegen` returns it already encoded and it is written
verbatim, because re-serializing it would change the bytes a check compares.

## Path rules

A `/v1/codegen` response names every artifact's path, and a writer that joined an unvetted path onto
its output root could be routed out of the tree, by a `..` component, an absolute or drive-prefixed
path, or a symbolic link, to a place no stamp guards and no check looks. The rules below are the
reference's, so a path this SDK refuses is one `pipelex codegen types` refuses too.

## Lock versions

The lock is a cross-language interchange format read with `extra="forbid"`, and it carries
`lock_version` so that a format change is named rather than reported as an opaque shape error. A reader
refuses a version it does not know *before* validating the key set, and says which side to upgrade. A
lock with no `lock_version` key is version 1 by definition, since the field arrived with version 1.
"""

import os
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NoReturn
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipelex_sdk._pydantic_utils import empty_list_factory_of
from pipelex_sdk.codegen_stamp import STAMPABLE_SUFFIXES
from pipelex_sdk.errors import CodegenError, CodegenLockError

CODEGEN_LOCK_FILENAME = "codegen.lock"
"""The one filename a codegen lock is written as and read from."""

CODEGEN_LOCK_VERSION = 1
"""The lock format version this SDK reads, mirroring pipelex's `CODEGEN_LOCK_VERSION`."""


class CodegenLockEntry(BaseModel):
    """One tracked artifact: its path relative to the lock, and the hash of its body below the stamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    content_hash: str


class CodegenLock(BaseModel):
    """The generated artifact set of one output root, keyed to the crate and engine it was built against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lock_version: int = 1
    crate_fingerprint: str
    engine_version: str
    artifacts: list[CodegenLockEntry] = Field(default_factory=empty_list_factory_of(CodegenLockEntry))

    def paths(self) -> set[str]:
        """The set of tracked artifact paths, relative to the lock."""
        return set(validate_artifact_paths(entry.path for entry in self.artifacts))


def parse_lock(content: str) -> CodegenLock:
    """Parse the text of a `codegen.lock`.

    Raises `CodegenLockError` for malformed TOML, a shape the format does not define, or a `lock_version`
    this SDK cannot read. Raises a plain `CodegenError` for an unsafe or duplicate artifact path, which is
    a containment violation rather than corrupt state.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        msg = f"Malformed codegen lock: {exc}"
        raise CodegenLockError(msg) from exc
    _reject_unknown_lock_version(data)
    try:
        lock = CodegenLock.model_validate(data)
    except ValidationError as exc:
        msg = f"Malformed codegen lock: {exc}"
        raise CodegenLockError(msg) from exc
    validate_artifact_paths(entry.path for entry in lock.artifacts)
    return lock


def load_lock(lock_path: Path) -> CodegenLock | None:
    """Read and parse the lock at `lock_path`, or return `None` when no file is there.

    A lock that exists but cannot be read, or whose bytes are not UTF-8, is a `CodegenLockError` like any
    other malformed lock.
    """
    if not lock_path.is_file():
        return None
    try:
        content = lock_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"Unreadable codegen lock at '{lock_path}': {exc}"
        raise CodegenLockError(msg) from exc
    try:
        return parse_lock(content)
    except CodegenLockError as exc:
        msg = f"Codegen lock at '{lock_path}': {exc}"
        raise CodegenLockError(msg) from exc


def validate_artifact_path(path: str) -> Path:
    """Validate one artifact path, from a response or a lock, and return its relative filesystem form."""
    if not path:
        _raise_path_error(path, reason="path is empty")
    if "\\" in path:
        _raise_path_error(path, reason="backslashes are not allowed; use forward slashes")
    if any(category(character).startswith("C") for character in path):
        _raise_path_error(path, reason="control characters are not allowed")

    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive or windows_path.root:
        _raise_path_error(path, reason="absolute paths and drive prefixes are not allowed")

    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _raise_path_error(path, reason="empty, '.', and '..' path components are not allowed")

    relative_path = Path(*parts)
    if relative_path.suffix not in STAMPABLE_SUFFIXES:
        expected = ", ".join(sorted(STAMPABLE_SUFFIXES))
        _raise_path_error(path, reason=f"unsupported artifact suffix (expected one of: {expected})")
    return relative_path


def validate_artifact_paths(paths: Iterable[str]) -> dict[str, Path]:
    """Validate a collection of artifact paths and reject a duplicate."""
    validated: dict[str, Path] = {}
    for path in paths:
        if path in validated:
            _raise_path_error(path, reason="duplicate artifact path")
        validated[path] = validate_artifact_path(path)
    return validated


def resolve_artifact_path(*, root: Path, artifact_path: str) -> Path:
    """Resolve a validated artifact path beneath `root` without following any symbolic link."""
    return resolve_output_path(root=root, relative_path=validate_artifact_path(artifact_path))


def resolve_output_path(*, root: Path, relative_path: Path) -> Path:
    """Resolve one output file beneath `root`, refusing a symbolic link anywhere on the way to it.

    The root itself must not be a symbolic link and, when it exists, must be a directory. Every component
    below it must not be a symbolic link, the resolved destination must stay inside the root, and a
    destination that already exists must be a regular file.
    """
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        _raise_path_error(str(relative_path), reason="internal output path must be a canonical relative path")

    requested_root = Path(os.path.normpath(root.absolute()))
    if requested_root.is_symlink():
        _raise_path_error(str(requested_root), reason="output root must not be a symbolic link")
    normalized_root = requested_root.resolve(strict=False)
    if normalized_root.exists() and not normalized_root.is_dir():
        _raise_path_error(str(normalized_root), reason="output root exists but is not a directory")

    destination = normalized_root / relative_path
    _reject_symlink_components(root=normalized_root, relative_path=relative_path)
    if not destination.resolve(strict=False).is_relative_to(normalized_root):
        _raise_path_error(str(destination), reason=f"resolved path escapes output root '{normalized_root}'")
    if destination.exists() and not destination.is_file():
        _raise_path_error(str(destination), reason="output destination exists but is not a regular file")
    return destination


def _reject_symlink_components(*, root: Path, relative_path: Path) -> None:
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            _raise_path_error(str(root / relative_path), reason=f"symbolic link component is not allowed: '{current}'")


def _raise_path_error(path: str, *, reason: str) -> NoReturn:
    msg = f"Unsafe codegen artifact path '{path}': {reason}."
    raise CodegenError(msg)


def _reject_unknown_lock_version(data: dict[str, Any]) -> None:
    """Refuse a lock whose format version this SDK cannot read, before its key set is validated.

    The ordering is the point: `extra="forbid"` would otherwise reject a future lock as a shape error over
    a key its writer was entitled to add, instead of naming the version and saying which side to upgrade.
    """
    # No key at all means the lock predates the field, which is version 1 by definition. It still goes
    # through the comparison, or the day the constant moves every such lock would skip the gate.
    raw_version = data.get("lock_version", 1)
    # `bool` is an `int` subclass and `True == 1`, so a boolean would otherwise read as version 1.
    is_version_number = isinstance(raw_version, int) and not isinstance(raw_version, bool)
    if is_version_number and raw_version == CODEGEN_LOCK_VERSION:
        return

    reason: str
    if is_version_number and raw_version > CODEGEN_LOCK_VERSION:
        reason = f"it declares lock_version {raw_version}, so upgrade pipelex-sdk to a build that reads it"
    else:
        reason = f"lock_version {raw_version!r} is not a known codegen lock format version"
    msg = f"Unsupported codegen lock: this SDK reads lock_version {CODEGEN_LOCK_VERSION}, but {reason}."
    raise CodegenLockError(msg)
