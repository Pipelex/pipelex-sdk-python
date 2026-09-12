"""`run_codegen_check`: the offline drift verdict over a generated tree and its lock.

The tree under `data/real_codegen_tree/` is genuine engine output — `pipelex codegen types --target
python-pydantic` over the cookbook's `documents` method, engine 0.57.0, copied in byte for byte (the
artifact carries a `.txt` suffix only so this repo's linters leave it alone; every test renames it back to
`models.py`, which is the path the lock tracks, and `.gitattributes` pins its line endings).

What that fixture re-proves on every run is that **this** implementation reads real engine bytes as
current, and what it cannot re-prove is agreement with `pipelex`, because this package must not depend on
it — which is the whole point of the module. Its hashes are self-proving, so the fixture cannot rot
unnoticed: recompute SHA-256 over the body below the fence and it equals both the stamp's recorded value
and the lock's. Cross-implementation parity was established once, by an out-of-tree harness running both
implementations in separate virtualenvs; `docs/architecture.md` records what that covered and why
re-proving it per run belongs to the workspace's `conformance/` suite rather than here.

The smaller fixtures below are the same grammar at a size a reader can hold: a real stamp fence whose
`content_hash` is the true SHA-256 of the body under it, and a real lock tracking the same hashes.
"""

import hashlib
import json
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

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
    """Materialize the genuine `pipelex codegen types` tree, artifact back under the path the lock tracks.

    The copy folds any CRLF back to LF. `.gitattributes` pins the fixture to LF, so in a correct checkout
    this is a no-op — but a clone made before that pin, or with `core.autocrlf=true`, holds CRLF bytes, and
    then `test_a_real_tree_checked_out_with_crlf_is_still_current` would double every carriage return and
    fail over its own fixture rather than over the code. The materialized tree is the engine's bytes either
    way.
    """
    (root / "codegen.lock").write_bytes(_to_lf((_REAL_TREE / "codegen.lock").read_bytes()))
    (root / "models.py").write_bytes(_to_lf((_REAL_TREE / "models.py.txt").read_bytes()))
    return root / "models.py"


def _to_lf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


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


class TestTheReportItself:
    """What a caller reads off the report, including what survives serializing it."""

    def test_the_verdict_survives_model_dump(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        report = run_codegen_check(root=tmp_path)
        # A CI gate that serializes the report is the natural shape for one, and a bare property would drop
        # the verdict out of it silently while every other field came through.
        assert report.model_dump()["is_current"] is True
        assert json.loads(report.model_dump_json())["is_current"] is True

    def test_the_verdict_survives_model_dump_when_the_tree_has_drifted(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        (tmp_path / "models.py").unlink()
        report = run_codegen_check(root=tmp_path)
        assert report.model_dump()["is_current"] is False
        assert json.loads(report.model_dump_json())["drifts"][0]["category"] == "missing"

    def test_the_reported_fingerprint_is_the_locks_header_and_is_not_cross_checked(self, tmp_path: Path) -> None:
        older, newer = "a" * 64, "b" * 64
        body_a, body_b = "A = 1\n", "B = 1\n"
        _write(tmp_path, "a.py", _stamped(body_a).replace(_FINGERPRINT, older))
        _write(tmp_path, "b.py", _stamped(body_b).replace(_FINGERPRINT, newer))
        _write(tmp_path, "codegen.lock", _lock(("a.py", body_a), ("b.py", body_b)).replace(_FINGERPRINT, newer))
        report = run_codegen_check(root=tmp_path)
        # Two artifacts generated against different crates, each body matching its own stamp and the lock.
        # The check is pure hashing, so it reports current and surfaces only what the lock header claims —
        # it never compares the artifacts' own stamped fingerprints with the header or with each other.
        # `pipelex codegen check` answers identically; the blind spot is the algorithm's, not this port's.
        assert report.is_current
        assert report.crate_fingerprint == newer


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

    def test_an_unreadable_locked_artifact_is_a_no_verdict_error_not_a_drift(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        (tmp_path / "models.py").chmod(0o000)
        try:
            # Bytes that are not UTF-8 are a verdict — not generated output — but bytes that cannot be read
            # are the absence of one. Reporting this as hand-edited would be a wrong verdict, and letting the
            # `PermissionError` out would break the one class a CI caller has to catch.
            with pytest.raises(CodegenLockError, match="Unreadable file under the codegen output root"):
                run_codegen_check(root=tmp_path)
        finally:
            (tmp_path / "models.py").chmod(0o644)

    def test_an_entry_the_walk_cannot_stat_is_a_no_verdict_error(self, tmp_path: Path, mocker: MockerFixture) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        # `iterdir` is not the only syscall in the walk: classifying an entry stats it, and that fails on a
        # path longer than the platform allows — a real 1400-deep tree does exactly this on macOS — or under
        # a component that has become unsearchable. The depth reproduction is platform-dependent, so the
        # failure is injected at the same call, for the walked entry alone rather than for every `is_dir`.
        real_is_dir = Path.is_dir

        def failing_is_dir(self: Path) -> bool:
            if self.name == "models.py":
                raise OSError(63, "File name too long")
            return real_is_dir(self)

        mocker.patch.object(Path, "is_dir", failing_is_dir)
        with pytest.raises(CodegenLockError, match="Unreadable entry under the codegen output root"):
            run_codegen_check(root=tmp_path)

    def test_an_unreadable_directory_under_the_root_is_a_no_verdict_error(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        _write(tmp_path, "sub/stale.py", _stamped("STALE = 1\n"))
        (tmp_path / "sub").chmod(0o000)
        try:
            # Treating an unlistable directory as an empty one would hide exactly the stale artifact the
            # orphan scan exists to find.
            with pytest.raises(CodegenLockError, match="Unreadable directory under the codegen output root"):
                run_codegen_check(root=tmp_path)
        finally:
            (tmp_path / "sub").chmod(0o755)


class TestParityWithTheReference:
    """States where a reader and a writer must answer differently, and where the two readers must agree."""

    def test_a_locked_artifact_whose_parent_is_a_regular_file_is_missing_not_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", _lock(("nested/models.py", "A = 1\n")))
        _write(tmp_path, "nested", "I am a file where a directory should be.\n")
        report = run_codegen_check(root=tmp_path)
        # `pipelex codegen check` reports `missing` here, so this reader must too. Refusing the tree is a
        # *writer's* guard — it must not start writing into a blocked path — and the check inherited it by
        # sharing the writer's resolver. To a reader the state says only that no file can be at that path.
        assert _categories(report) == [("nested/models.py", "missing")]
        assert report.drifts[0].detail == "Locked artifact is absent on disk."

    def test_a_symbolic_link_component_is_still_refused_on_the_read_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", _lock(("nested/models.py", "A = 1\n")))
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "nested").symlink_to(tmp_path / "elsewhere")
        # Relaxing the directory guard must not relax containment: a link on the way to an artifact still
        # routes the read out of the tree, and is still refused.
        with pytest.raises(CodegenLockError, match="symbolic link component is not allowed"):
            run_codegen_check(root=tmp_path)

    def test_a_python_artifact_declaring_a_source_encoding_is_hand_edited(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        # Every header line still starts with `#`, and the body below the fence is untouched, so both the
        # stamp hash and the locked hash still agree. But PEP 263 lets line 2 choose the codec CPython
        # decodes the file with, and under `raw_unicode_escape` a header value carrying the six literal
        # characters of a backslash-u-000a escape becomes a newline — so the text after it is a statement
        # that runs on import, in a region no hash covers. Without the gate this tree reads as current
        # while executing injected code.
        injected = "# note: " + chr(92) + "u000aINJECTED = True"
        stamped = (
            _stamped("A = 1\n")
            .replace(
                "# >>> pipelex-codegen-stamp >>>\n",
                "# >>> pipelex-codegen-stamp >>>\n# coding: raw_unicode_escape\n",
            )
            .replace("# options: {}\n", f"# options: {{}}\n{injected}\n")
        )
        _write(tmp_path, "models.py", stamped)
        assert _categories(run_codegen_check(root=tmp_path)) == [("models.py", "hand-edited")]

    def test_a_coding_declaration_below_pep_263s_two_line_window_is_not_a_drift(self, tmp_path: Path) -> None:
        _tree(tmp_path, ("models.py", "A = 1\n"))
        # CPython reads the declaration on the first two lines and no further, so one below them changes
        # nothing about how the file decodes. Reporting it would be a drift the state does not justify.
        _write(tmp_path, "models.py", _stamped("A = 1\n").replace("# options: {}\n", "# options: {}\n# coding: utf-8\n"))
        assert run_codegen_check(root=tmp_path).is_current

    def test_every_no_verdict_condition_is_catchable_as_a_codegen_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "codegen.lock", "lock_version = = 1\n")
        with pytest.raises(CodegenError):
            run_codegen_check(root=tmp_path)
