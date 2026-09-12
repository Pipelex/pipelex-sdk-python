"""`write_codegen_tree`: a `/v1/codegen` response on disk, byte for byte.

The pipelex fixtures below are real engine output, produced by `pipelex.codegen.emission.build_stamped_projection`,
the pure half of the `write_stamped_projection` a local `pipelex codegen types` run calls. A tree these tests
accept is therefore the tree that run writes. The behaviour pinned here is that function's: validate everything
before writing, never overwrite a file codegen does not own, write only what changed, prune what the previous lock
tracked and the new set dropped, and write the lock last.
"""

import os
from pathlib import Path

import pytest

from pipelex_sdk.codegen_writer import write_codegen_tree
from pipelex_sdk.crate_models import CodegenValidReport, GeneratedArtifact
from pipelex_sdk.errors import CodegenError, CodegenLockError

_FINGERPRINT = "f" * 64

_PIPELEX_MODELS_PY = (
    "# >>> pipelex-codegen-stamp >>>\n"
    f"# crate_fingerprint: {_FINGERPRINT}\n"
    "# engine_version: 0.55.0\n"
    "# projection: types / python-pydantic\n"
    "# options: {}\n"
    "# content_hash: d3ae42f924ea654dc34df7a2199ef4c42833b0189758af122f8a2bddcbb1f358\n"
    "# <<< pipelex-codegen-stamp <<<\n"
    "from pydantic import BaseModel\n\n\nclass Invoice(BaseModel):\n    total: float\n"
)
_PIPELEX_EXTRA_PY = (
    "# >>> pipelex-codegen-stamp >>>\n"
    f"# crate_fingerprint: {_FINGERPRINT}\n"
    "# engine_version: 0.55.0\n"
    "# projection: types / python-pydantic\n"
    "# options: {}\n"
    "# content_hash: bb97e71874d3a97080723f8984bfb17af8bf40d80f22f1d7d2223784cb3e8a2e\n"
    "# <<< pipelex-codegen-stamp <<<\n"
    "X = 'é'\n"
)
_PIPELEX_LOCK = (
    "# codegen.lock — generated artifact set (Pipelex codegen). Do not edit by hand.\n\n"
    "lock_version = 1\n"
    f'crate_fingerprint = "{_FINGERPRINT}"\n'
    'engine_version = "0.55.0"\n\n'
    '[[artifacts]]\npath = "models.py"\ncontent_hash = "d3ae42f924ea654dc34df7a2199ef4c42833b0189758af122f8a2bddcbb1f358"\n\n'
    '[[artifacts]]\npath = "nested/extra.py"\ncontent_hash = "bb97e71874d3a97080723f8984bfb17af8bf40d80f22f1d7d2223784cb3e8a2e"\n'
)


def _stamped(body: str) -> str:
    """A Python artifact behind a stamp fence. The recorded hash is a placeholder: the writer never reads it."""
    return f"# >>> pipelex-codegen-stamp >>>\n# content_hash: {'0' * 64}\n# <<< pipelex-codegen-stamp <<<\n{body}"


def _lock_tracking(*paths: str) -> str:
    entries = "".join(f'\n[[artifacts]]\npath = "{path}"\ncontent_hash = "{"0" * 64}"\n' for path in paths)
    return f'lock_version = 1\ncrate_fingerprint = "{_FINGERPRINT}"\nengine_version = "0.55.0"\n{entries}'


def _report(artifacts: dict[str, str], *, lock: str | None = None, lock_filename: str = "codegen.lock") -> CodegenValidReport:
    return CodegenValidReport(
        is_valid=True,
        kind="types",
        target="python-pydantic",
        crate_fingerprint=_FINGERPRINT,
        engine_version="0.55.0",
        artifacts=[GeneratedArtifact(path=path, content=content) for path, content in artifacts.items()],
        lock=lock if lock is not None else _lock_tracking(*artifacts),
        lock_filename=lock_filename,
        message="ok",
    )


def _pipelex_report() -> CodegenValidReport:
    return _report({"models.py": _PIPELEX_MODELS_PY, "nested/extra.py": _PIPELEX_EXTRA_PY}, lock=_PIPELEX_LOCK)


class TestCodegenWriter:
    # ── Writing verbatim ─────────────────────────────────────────────

    def test_writes_every_artifact_at_its_path_and_the_lock_as_lock_filename_byte_for_byte(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"

        result = write_codegen_tree(_pipelex_report(), output_dir=output_dir)

        assert (output_dir / "models.py").read_bytes() == _PIPELEX_MODELS_PY.encode("utf-8")
        assert (output_dir / "nested" / "extra.py").read_bytes() == _PIPELEX_EXTRA_PY.encode("utf-8")
        assert (output_dir / "codegen.lock").read_bytes() == _PIPELEX_LOCK.encode("utf-8")
        assert result.written == ["models.py", "nested/extra.py"]
        assert result.unchanged == []
        assert result.removed == []
        assert result.lock_written is True

    def test_writes_nothing_else_into_the_output_directory(self, tmp_path: Path) -> None:
        write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        on_disk = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
        assert on_disk == ["codegen.lock", "models.py", "nested/extra.py"]

    def test_keeps_line_endings_and_non_ascii_bytes_exactly_as_sent(self, tmp_path: Path) -> None:
        """Text mode would rewrite each line feed as the platform separator; the tree must not depend on the platform."""
        content = _stamped("crlf = 'a'\r\nlf = 'é'\n")

        write_codegen_tree(_report({"models.py": content}), output_dir=tmp_path)

        assert (tmp_path / "models.py").read_bytes() == content.encode("utf-8")

    def test_rewriting_a_current_tree_touches_nothing(self, tmp_path: Path) -> None:
        write_codegen_tree(_pipelex_report(), output_dir=tmp_path)
        tracked = [tmp_path / "models.py", tmp_path / "nested" / "extra.py", tmp_path / "codegen.lock"]
        for path in tracked:
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))

        result = write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert result.written == []
        assert result.unchanged == ["models.py", "nested/extra.py"]
        assert result.lock_written is False
        assert [path.stat().st_mtime_ns for path in tracked] == [1_000_000_000] * len(tracked)

    def test_rewrites_only_the_artifact_that_changed(self, tmp_path: Path) -> None:
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "other.py": _stamped("B = 1\n")}), output_dir=tmp_path)

        result = write_codegen_tree(_report({"models.py": _stamped("A = 2\n"), "other.py": _stamped("B = 1\n")}), output_dir=tmp_path)

        assert result.written == ["models.py"]
        assert result.unchanged == ["other.py"]
        assert (tmp_path / "models.py").read_text(encoding="utf-8") == _stamped("A = 2\n")

    # ── Pruning ──────────────────────────────────────────────────────

    def test_prunes_a_delisted_artifact_that_still_carries_its_stamp(self, tmp_path: Path) -> None:
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "gone/old.py": _stamped("OLD = 1\n")}), output_dir=tmp_path)

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.removed == ["gone/old.py"]
        assert not (tmp_path / "gone" / "old.py").exists()
        assert (tmp_path / "codegen.lock").read_text(encoding="utf-8") == _lock_tracking("models.py")

    def test_keeps_a_delisted_artifact_whose_stamp_was_removed_by_hand(self, tmp_path: Path) -> None:
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "old.py": _stamped("OLD = 1\n")}), output_dir=tmp_path)
        (tmp_path / "old.py").write_text("OLD = 1  # adopted by hand\n", encoding="utf-8")

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.removed == []
        assert (tmp_path / "old.py").read_text(encoding="utf-8") == "OLD = 1  # adopted by hand\n"

    def test_leaves_a_stamped_file_the_previous_lock_never_tracked(self, tmp_path: Path) -> None:
        """Only the previous lock's set is pruned, as pipelex does; the offline check is what reports such a file."""
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)
        (tmp_path / "copied.py").write_text(_stamped("COPIED = 1\n"), encoding="utf-8")

        result = write_codegen_tree(_report({"models.py": _stamped("A = 2\n")}), output_dir=tmp_path)

        assert result.removed == []
        assert (tmp_path / "copied.py").exists()

    def test_skips_a_tracked_artifact_that_is_already_gone(self, tmp_path: Path) -> None:
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "old.py": _stamped("OLD = 1\n")}), output_dir=tmp_path)
        (tmp_path / "old.py").unlink()

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.removed == []

    def test_a_corrupt_previous_lock_is_replaced_and_prunes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "codegen.lock").write_text("this is [not toml", encoding="utf-8")
        (tmp_path / "old.py").write_text(_stamped("OLD = 1\n"), encoding="utf-8")

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.removed == []
        assert result.lock_written is True
        assert (tmp_path / "old.py").exists()
        assert (tmp_path / "codegen.lock").read_text(encoding="utf-8") == _lock_tracking("models.py")

    def test_a_previous_lock_of_an_unknown_version_is_replaced(self, tmp_path: Path) -> None:
        (tmp_path / "codegen.lock").write_text(_lock_tracking("old.py").replace("lock_version = 1", "lock_version = 2"), encoding="utf-8")

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.lock_written is True
        assert (tmp_path / "codegen.lock").read_text(encoding="utf-8") == _lock_tracking("models.py")

    def test_refuses_a_previous_lock_that_tracks_an_unsafe_path(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (tmp_path / "escape.py").write_text(_stamped("VICTIM = 1\n"), encoding="utf-8")
        (output_dir / "codegen.lock").write_text(_lock_tracking("../escape.py"), encoding="utf-8")

        with pytest.raises(CodegenError, match="Unsafe codegen artifact path") as exc_info:
            write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=output_dir)

        assert not isinstance(exc_info.value, CodegenLockError)
        assert (tmp_path / "escape.py").exists()
        assert not (output_dir / "models.py").exists()

    def test_refuses_a_tracked_path_that_became_a_directory_before_writing_anything(self, tmp_path: Path) -> None:
        """Prune targets are resolved in the preflight: refusing one while pruning would leave rewritten artifacts beside the old lock."""
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "old.py": _stamped("OLD = 1\n")}), output_dir=tmp_path)
        previous_lock = (tmp_path / "codegen.lock").read_bytes()
        (tmp_path / "old.py").unlink()
        (tmp_path / "old.py").mkdir()

        with pytest.raises(CodegenError, match="not a regular file"):
            write_codegen_tree(_report({"models.py": _stamped("A = 2\n")}), output_dir=tmp_path)

        assert (tmp_path / "models.py").read_text(encoding="utf-8") == _stamped("A = 1\n")
        assert (tmp_path / "codegen.lock").read_bytes() == previous_lock

    def test_refuses_a_tracked_path_behind_a_symlink_before_writing_anything(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), "sub/old.py": _stamped("OLD = 1\n")}), output_dir=output_dir)
        elsewhere = tmp_path / "elsewhere"
        (output_dir / "sub").rename(elsewhere)
        (output_dir / "sub").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(CodegenError, match="symbolic link component is not allowed"):
            write_codegen_tree(_report({"models.py": _stamped("A = 2\n")}), output_dir=output_dir)

        assert (elsewhere / "old.py").exists()
        assert (output_dir / "models.py").read_text(encoding="utf-8") == _stamped("A = 1\n")

    # ── Ownership ────────────────────────────────────────────────────

    def test_refuses_to_overwrite_an_unowned_file_and_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "extra.py").write_text("handwritten = True\n", encoding="utf-8")

        with pytest.raises(CodegenError, match="Refusing to overwrite unowned file"):
            write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert (tmp_path / "nested" / "extra.py").read_text(encoding="utf-8") == "handwritten = True\n"
        assert not (tmp_path / "models.py").exists()
        assert not (tmp_path / "codegen.lock").exists()

    def test_an_unowned_file_already_identical_to_the_artifact_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "models.py").write_bytes(_PIPELEX_MODELS_PY.encode("utf-8"))

        result = write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert result.unchanged == ["models.py"]

    def test_overwrites_a_stamped_file_the_previous_lock_never_tracked(self, tmp_path: Path) -> None:
        """Ownership reads the stamp's begin line only. `pipelex` also requires the header to parse, and refuses this file (L-260912-bb83ca)."""
        (tmp_path / "models.py").write_text(_stamped("STALE = 1\n"), encoding="utf-8")

        result = write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert "models.py" in result.written
        assert (tmp_path / "models.py").read_bytes() == _PIPELEX_MODELS_PY.encode("utf-8")

    def test_overwrites_an_unstamped_file_the_previous_lock_tracked(self, tmp_path: Path) -> None:
        write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)
        (tmp_path / "models.py").write_text("A = 1  # stamp stripped by a formatter\n", encoding="utf-8")

        result = write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}), output_dir=tmp_path)

        assert result.written == ["models.py"]
        assert (tmp_path / "models.py").read_text(encoding="utf-8") == _stamped("A = 1\n")

    # ── Refusals before the first byte ───────────────────────────────

    def test_refuses_a_lock_filename_other_than_codegen_lock(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"

        with pytest.raises(CodegenError, match=r"whose lock is named 'types\.lock'"):
            write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}, lock_filename="types.lock"), output_dir=output_dir)

        assert not output_dir.exists()

    @pytest.mark.parametrize(
        "unsafe_path",
        ["", "../escape.py", "/absolute.py", "C:/drive.py", "nested\\windows.py", "nested//empty.py", "./dot.py", "notes.txt", "control\x00.py"],
    )
    def test_refuses_an_unsafe_artifact_path_and_writes_nothing(self, tmp_path: Path, unsafe_path: str) -> None:
        output_dir = tmp_path / "generated"

        with pytest.raises(CodegenError, match="Unsafe codegen artifact path"):
            write_codegen_tree(_report({"models.py": _stamped("A = 1\n"), unsafe_path: _stamped("X = 1\n")}, lock=""), output_dir=output_dir)

        assert not output_dir.exists()

    @pytest.mark.parametrize(
        ("artifact_paths", "lock_paths"),
        [(["models.py"], ["models.py", "hand.py"]), (["models.py", "other.py"], ["models.py"])],
    )
    def test_refuses_a_response_whose_lock_does_not_track_exactly_its_artifacts(
        self, tmp_path: Path, artifact_paths: list[str], lock_paths: list[str]
    ) -> None:
        """The written lock is what the next run trusts: a path it tracks may be overwritten without a stamp, and pruned."""
        output_dir = tmp_path / "generated"
        report = _report({path: _stamped("X = 1\n") for path in artifact_paths}, lock=_lock_tracking(*lock_paths))

        with pytest.raises(CodegenError, match="does not track exactly its artifacts"):
            write_codegen_tree(report, output_dir=output_dir)

        assert not output_dir.exists()

    @pytest.mark.parametrize("lock", ["this is [not toml", _lock_tracking("models.py").replace("lock_version = 1", "lock_version = 2")])
    def test_refuses_a_response_lock_this_sdk_cannot_read(self, tmp_path: Path, lock: str) -> None:
        """Unlike a corrupt previous lock, which is replaced, a response lock that cannot be read is refused: writing it would switch pruning off."""
        output_dir = tmp_path / "generated"

        with pytest.raises(CodegenError, match="whose lock this SDK cannot read") as exc_info:
            write_codegen_tree(_report({"models.py": _stamped("A = 1\n")}, lock=lock), output_dir=output_dir)

        assert not isinstance(exc_info.value, CodegenLockError)
        assert not output_dir.exists()

    def test_refuses_a_response_lock_that_tracks_an_unsafe_path(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        report = _report({"models.py": _stamped("A = 1\n")}, lock=_lock_tracking("models.py", "../escape.py"))

        with pytest.raises(CodegenError, match=r"Unsafe codegen artifact path '\.\./escape\.py'"):
            write_codegen_tree(report, output_dir=output_dir)

        assert not output_dir.exists()

    def test_refuses_a_duplicate_artifact_path(self, tmp_path: Path) -> None:
        report = _report({"models.py": _stamped("A = 1\n")})
        duplicated = report.model_copy(update={"artifacts": [*report.artifacts, GeneratedArtifact(path="models.py", content=_stamped("A = 2\n"))]})

        with pytest.raises(CodegenError, match="duplicate artifact path"):
            write_codegen_tree(duplicated, output_dir=tmp_path / "generated")

        assert not (tmp_path / "generated").exists()

    def test_refuses_a_symlinked_output_root(self, tmp_path: Path) -> None:
        real_root = tmp_path / "real"
        real_root.mkdir()
        (tmp_path / "link").symlink_to(real_root, target_is_directory=True)

        with pytest.raises(CodegenError, match="output root must not be a symbolic link"):
            write_codegen_tree(_pipelex_report(), output_dir=tmp_path / "link")

        assert list(real_root.iterdir()) == []

    def test_refuses_a_symlink_on_the_way_to_a_destination(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (output_dir / "nested").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(CodegenError, match="symbolic link component is not allowed"):
            write_codegen_tree(_pipelex_report(), output_dir=output_dir)

        assert list(elsewhere.iterdir()) == []
        assert not (output_dir / "models.py").exists()

    def test_refuses_a_file_where_an_artifact_needs_a_directory_before_writing_anything(self, tmp_path: Path) -> None:
        (tmp_path / "nested").write_text("not a directory\n", encoding="utf-8")

        with pytest.raises(CodegenError, match="a component on the way to it is not a directory"):
            write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert not (tmp_path / "models.py").exists()
        assert not (tmp_path / "codegen.lock").exists()

    def test_refuses_a_destination_that_is_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "models.py").mkdir()

        with pytest.raises(CodegenError, match="not a regular file"):
            write_codegen_tree(_pipelex_report(), output_dir=tmp_path)

        assert not (tmp_path / "codegen.lock").exists()

    def test_refuses_an_output_root_that_is_a_file(self, tmp_path: Path) -> None:
        output_file = tmp_path / "generated"
        output_file.write_text("", encoding="utf-8")

        with pytest.raises(CodegenError, match="output root exists but is not a directory"):
            write_codegen_tree(_pipelex_report(), output_dir=output_file)
