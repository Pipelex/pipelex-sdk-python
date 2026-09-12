"""`run_codegen_check`: the offline drift verdict over a generated tree and its lock.

The tree under `data/real_codegen_tree/` is genuine engine output — `pipelex codegen types --target
python-pydantic` over the cookbook's `documents` method, engine 0.57.0, copied in byte for byte (the
artifact carries a `.txt` suffix only so this repo's linters leave it alone; every test renames it back to
`models.py`, which is the path the lock tracks). A verdict reached over those bytes is the verdict
`pipelex codegen check` reaches over the same tree, which is the whole contract this module is held to.

The smaller fixtures below are the same grammar at a size a reader can hold: a real stamp fence whose
`content_hash` is the true SHA-256 of the body under it, and a real lock tracking the same hashes.
"""

import hashlib
import shutil
from pathlib import Path

import pytest

from pipelex_sdk.codegen_check import CodegenCheckReport, DriftCategory, run_codegen_check
from pipelex_sdk.errors import CodegenError, CodegenLockError

_REAL_TREE = Path(__file__).parent / "data" / "real_codegen_tree"
_FINGERPRINT = "f" * 64
_ENGINE_VERSION = "0.57.0"


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _stamped(body: str, *, comment_prefix: str = "#", recorded_hash: str | None = None, projection: str = "types / python-pydantic") -> str:
    """A stamped artifact in pipelex's exact fence grammar, recording the true hash of `body` by default."""
    return (
        f"{comment_prefix} >>> pipelex-codegen-stamp >>>\n"
        f"{comment_prefix} crate_fingerprint: {_FINGERPRINT}\n"
        f"{comment_prefix} engine_version: {_ENGINE_VERSION}\n"
        f"{comment_prefix} projection: {projection}\n"
        f"{comment_prefix} options: {{}}\n"
        f"{comment_prefix} content_hash: {recorded_hash if recorded_hash is not None else _hash(body)}\n"
        f"{comment_prefix} <<< pipelex-codegen-stamp <<<\n"
        f"{body}"
    )


def _lock(*entries: tuple[str, str]) -> str:
    """A `codegen.lock` in the encoding `pipelex` writes, tracking `(path, body)` pairs by their true hash."""
    artifacts = "".join(f'\n[[artifacts]]\npath = "{path}"\ncontent_hash = "{_hash(body)}"\n' for path, body in entries)
    return (
        "# codegen.lock — generated artifact set (Pipelex codegen). Do not edit by hand.\n\n"
        f'lock_version = 1\ncrate_fingerprint = "{_FINGERPRINT}"\nengine_version = "{_ENGINE_VERSION}"\n{artifacts}'
    )


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _tree(root: Path, *entries: tuple[str, str]) -> None:
    """Write a current tree: each `(path, body)` stamped, and a lock tracking exactly those bodies."""
    for relative, body in entries:
        comment_prefix = "#" if relative.endswith(".py") else "//"
        _write(root, relative, _stamped(body, comment_prefix=comment_prefix))
    _write(root, "codegen.lock", _lock(*entries))


def _real_tree(root: Path) -> Path:
    """Materialize the genuine `pipelex codegen types` tree, artifact back under the path the lock tracks."""
    shutil.copyfile(_REAL_TREE / "codegen.lock", root / "codegen.lock")
    shutil.copyfile(_REAL_TREE / "models.py.txt", root / "models.py")
    return root / "models.py"


def _categories(report: CodegenCheckReport) -> list[tuple[str, str]]:
    return [(drift.path, drift.category) for drift in report.drifts]


class TestRealGeneratedTree:
    """The verdict over bytes a real `pipelex codegen types` run wrote."""

    def test_a_real_generated_tree_is_current(self, tmp_path: Path) -> None:
        _real_tree(tmp_path)
        report = run_codegen_check(root=tmp_path)
        assert report.is_current
        assert report.lock_found
        assert report.drifts == []

    def test_the_report_surfaces_the_locks_crate_fingerprint_and_engine_version(self, tmp_path: Path) -> None:
        _real_tree(tmp_path)
        report = run_codegen_check(root=tmp_path)
        # The caller compares these against a live `codegen()` response to close the question the offline
        # check cannot ask: whether the tree still matches what the method resolves to.
        assert report.crate_fingerprint == "38d02d151de391f760bcaa1bf1c376cd617a598964f13db2fcfab0930e1322d1"
        assert report.engine_version == "0.57.0"

    def test_one_appended_line_in_a_real_artifact_is_a_hand_edit(self, tmp_path: Path) -> None:
        artifact = _real_tree(tmp_path)
        artifact.write_text(artifact.read_text(encoding="utf-8") + "\nSNUCK_IN = True\n", encoding="utf-8")
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "hand-edited")]
        assert report.drifts[0].detail == "Body was edited below the stamp (stamp hash no longer matches)."

    def test_a_real_tree_checked_out_with_crlf_is_still_current(self, tmp_path: Path) -> None:
        artifact = _real_tree(tmp_path)
        artifact.write_bytes(artifact.read_bytes().replace(b"\n", b"\r\n"))
        # Universal-newline translation, as pipelex's reader applies it: a Windows checkout is not a drift.
        assert run_codegen_check(root=tmp_path).is_current


class TestCurrentTree:
    def test_a_multi_artifact_nested_tree_is_current(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"), ("nested/deep/schemas.ts", "export const A = 1;\n"))
        assert run_codegen_check(root=tmp_path).is_current

    def test_a_non_stampable_sidecar_beside_the_lock_is_ignored(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "sources.json", '{"method": "documents"}\n')
        assert run_codegen_check(root=tmp_path).is_current

    def test_an_unstamped_hand_authored_sibling_is_ignored(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "helper.py", "HELPER = 2\n")
        assert run_codegen_check(root=tmp_path).is_current

    def test_a_tree_with_no_lock_is_not_checked_and_is_not_current(self, tmp_path: Path) -> None:
        _write(tmp_path, "models.py", _stamped("A = 1\n"))
        report = run_codegen_check(root=tmp_path)
        # No lock is nothing to check rather than a drift — but `is_current` still refuses to vouch for it.
        assert report.lock_found is False
        assert report.drifts == []
        assert report.is_current is False
        assert report.crate_fingerprint is None
        assert report.engine_version is None

    def test_a_lock_tracking_nothing_over_an_empty_tree_is_current(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", _lock())
        assert run_codegen_check(root=tmp_path).is_current


class TestDriftCategories:
    def test_a_locked_artifact_absent_on_disk_is_missing(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        (tmp_path / "models.py").unlink()
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "missing")]
        assert report.drifts[0].detail == "Locked artifact is absent on disk."

    def test_a_body_off_the_locked_hash_whose_stamp_still_agrees_is_modified(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        # Restamped, so the file is self-consistent and only the lock disagrees — which is what a stale
        # tree regenerated from a newer crate looks like.
        _write(tmp_path, "models.py", _stamped("A = 2\n"))
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "modified")]
        assert report.drifts[0].detail == "Body no longer matches the locked hash — regenerate."

    def test_a_body_edited_below_an_untouched_stamp_is_hand_edited(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", _stamped("A = 1\nEDIT = True\n", recorded_hash=_hash("A = 1\n")))
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "hand-edited")]
        assert report.drifts[0].detail == "Body was edited below the stamp (stamp hash no longer matches)."

    def test_a_locked_artifact_whose_stamp_was_stripped_is_hand_edited(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", "A = 1\n")
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "hand-edited")]
        assert report.drifts[0].detail == "Stamp header is missing or unparseable."

    def test_a_locked_artifact_that_is_not_utf8_is_hand_edited(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        (tmp_path / "models.py").write_bytes(b"# >>> pipelex-codegen-stamp >>>\n\xff\xfe\n")
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "hand-edited")]
        assert report.drifts[0].detail == "File is not valid UTF-8 — not generated output."

    def test_a_stamped_file_the_lock_does_not_track_is_an_orphan(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "stale.py", _stamped("STALE = 1\n"))
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("stale.py", "orphan")]
        assert report.drifts[0].detail == "Stamped generated file not tracked by the lock — stale; remove or regenerate."

    def test_an_orphan_is_found_at_any_depth(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "a/b/c/stale.ts", _stamped("export const STALE = 1;\n", comment_prefix="//"))
        assert _categories(run_codegen_check(root=tmp_path)) == [("a/b/c/stale.ts", "orphan")]

    @pytest.mark.parametrize("skipped_dir", ["node_modules", ".venv", "__pycache__", ".git", "dist"])
    def test_an_orphan_inside_a_vendor_or_cache_directory_is_not_scanned(self, tmp_path: Path, skipped_dir: str) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, f"{skipped_dir}/stale.py", _stamped("STALE = 1\n"))
        assert run_codegen_check(root=tmp_path).is_current

    def test_a_symlinked_orphan_is_skipped_but_its_target_is_still_found(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        target = _write(tmp_path, "stale.py", _stamped("STALE = 1\n"))
        (tmp_path / "linked.py").symlink_to(target)
        # The scan reads whatever shares the output root; a link parked beside the tree is not the tree's.
        assert _categories(run_codegen_check(root=tmp_path)) == [("stale.py", "orphan")]


class TestStampParsing:
    """What `parse_stamped` refuses, every refusal reported as a hand edit."""

    def test_an_uncommented_line_inside_the_fence_is_refused(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        stamped = _stamped("A = 1\n").replace("# <<< pipelex-codegen-stamp <<<", "import os\n# <<< pipelex-codegen-stamp <<<")
        _write(tmp_path, "models.py", stamped)
        # The hash covers only the body BELOW the fence, so an executable line hiding inside a "DO NOT
        # EDIT" block would otherwise verify as pristine.
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    def test_a_line_separator_hiding_a_statement_in_a_field_is_refused(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("nested.ts", "export const A = 1;\n"))
        stamped = _stamped("export const A = 1;\n", comment_prefix="//").replace("// options: {}", "// options: {}\u2028import os")
        _write(tmp_path, "nested.ts", stamped)
        # U+2028 terminates a `//` comment in ECMAScript, so splitting on "\n" alone would read this as one
        # commented line while the JavaScript engine reads two and runs the second.
        assert _categories(run_codegen_check(root=tmp_path)) == [("nested.ts", "hand-edited")]

    def test_an_unterminated_fence_is_refused(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", "# >>> pipelex-codegen-stamp >>>\n# content_hash: x\nA = 1\n")
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    def test_a_byte_order_mark_above_the_fence_is_refused(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        (tmp_path / "models.py").write_bytes(b"\xef\xbb\xbf" + (tmp_path / "models.py").read_bytes())
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    @pytest.mark.parametrize("options", ["not-json", "[1, 2]", "null", '{"a": NaN}', '{"a": Infinity}'])
    def test_options_that_are_not_a_json_object_are_refused(self, tmp_path: Path, options: str) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", _stamped("A = 1\n").replace("# options: {}", f"# options: {options}"))
        # `NaN` / `Infinity` are the interesting pair: Python's `json` accepts them and conformant parsers
        # do not, so a stamp only Python could read is not a valid stamp.
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    @pytest.mark.parametrize("projection", ["onlyonepart", " / python-pydantic", "types / ", ""])
    def test_a_malformed_projection_line_is_refused(self, tmp_path: Path, projection: str) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", _stamped("A = 1\n", projection=projection))
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    def test_a_missing_projection_line_is_refused(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", _stamped("A = 1\n").replace("# projection: types / python-pydantic\n", ""))
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    @pytest.mark.parametrize("projection", ["futurekind / python-pydantic", "types / future-target", "types / python-pydantic / domain.some_pipe"])
    def test_projection_axes_outside_this_sdks_vocabulary_are_accepted(self, tmp_path: Path, projection: str) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "models.py", _stamped("A = 1\n", projection=projection))
        # The one documented relaxation against `pipelex`, inherited from `@pipelex/sdk`: the axes must be
        # present and well-formed, not members of a vocabulary an SDK copy is free to lag. Validating them
        # would report every artifact of a tree generated by a newer engine as hand-edited.
        assert run_codegen_check(root=tmp_path).is_current


class TestDriftOrdering:
    def test_locked_drifts_come_first_in_path_order_then_orphans_in_path_order(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("zz.py", "Z = 1\n"), ("aa.py", "A = 1\n"), ("mm/nested.py", "M = 1\n"))
        (tmp_path / "zz.py").unlink()
        (tmp_path / "aa.py").unlink()
        (tmp_path / "mm" / "nested.py").unlink()
        _write(tmp_path, "zzz-orphan.py", _stamped("O = 1\n"))
        _write(tmp_path, "aaa-orphan.py", _stamped("O = 1\n"))
        assert _categories(run_codegen_check(root=tmp_path)) == [
            ("aa.py", "missing"),
            ("mm/nested.py", "missing"),
            ("zz.py", "missing"),
            ("aaa-orphan.py", "orphan"),
            ("zzz-orphan.py", "orphan"),
        ]

    def test_a_file_that_is_both_hand_edited_and_off_the_locked_hash_drifts_once_as_the_hand_edit(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        # Body changed AND the stamp left recording the old hash: both conditions trip, one drift is reported.
        _write(tmp_path, "models.py", _stamped("A = 99\n", recorded_hash=_hash("A = 1\n")))
        report = run_codegen_check(root=tmp_path)
        assert _categories(report) == [("models.py", "hand-edited")]
        assert report.drifts[0].category is DriftCategory.HAND_EDITED


class TestNoVerdict:
    """A lock or a tree the check cannot reason about at all — an error, never a drift."""

    def test_a_malformed_lock_raises(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", "lock_version = = 1\n")
        with pytest.raises(CodegenLockError, match="Malformed codegen lock"):
            run_codegen_check(root=tmp_path)

    def test_a_future_lock_version_names_the_side_to_upgrade(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", _lock().replace("lock_version = 1", "lock_version = 2"))
        with pytest.raises(CodegenLockError, match="upgrade pipelex-sdk"):
            run_codegen_check(root=tmp_path)

    def test_an_unknown_lock_key_raises(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", _lock() + "\nfuture_key = 3\n")
        with pytest.raises(CodegenLockError, match="Malformed codegen lock"):
            run_codegen_check(root=tmp_path)

    @pytest.mark.parametrize("tracked_path", ["../escape.py", "/etc/passwd.py", "models.txt", "./models.py", "nested/../models.py"])
    def test_a_lock_tracking_an_unsafe_path_raises_rather_than_drifting(self, tmp_path: Path, tracked_path: str) -> None:
        _write(tmp_path, "codegen.lock", _lock(("models.py", "A = 1\n")).replace('path = "models.py"', f'path = "{tracked_path}"'))
        # `CodegenLockError` subclasses `CodegenError`, and the path refusal is wrapped as the former so a
        # consumer has one no-verdict class to catch.
        with pytest.raises(CodegenLockError, match="Unsafe codegen artifact"):
            run_codegen_check(root=tmp_path)

    def test_a_symbolic_link_at_a_locked_artifacts_path_raises(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        elsewhere = _write(tmp_path, "elsewhere.txt", (tmp_path / "models.py").read_text(encoding="utf-8"))
        (tmp_path / "models.py").unlink()
        (tmp_path / "models.py").symlink_to(elsewhere)
        with pytest.raises(CodegenLockError, match="symbolic link component is not allowed"):
            run_codegen_check(root=tmp_path)

    def test_an_output_root_that_is_a_regular_file_raises(self, tmp_path: Path) -> None:
        root = _write(tmp_path, "not-a-directory", "")
        with pytest.raises(CodegenLockError, match="exists but is not a directory"):
            run_codegen_check(root=root)

    def test_every_no_verdict_condition_is_catchable_as_a_codegen_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", "lock_version = = 1\n")
        with pytest.raises(CodegenError):
            run_codegen_check(root=tmp_path)
