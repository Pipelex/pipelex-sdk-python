"""`codegen.lock` parsing and the artifact path rules, mirroring `pipelex/codegen/lock.py`.

The lock is a cross-language interchange format: `pipelex` writes it and this SDK reads it. These tests pin the
version gate (named before the key set is validated), the closed key set, and the split between a malformed lock,
which is a `CodegenLockError`, and an unsafe path inside a lock, which is a plain `CodegenError`.
"""

from pathlib import Path

import pytest

from pipelex_sdk.codegen_lock import CODEGEN_LOCK_VERSION, load_lock, parse_lock, validate_artifact_path
from pipelex_sdk.errors import CodegenError, CodegenLockError

# Real `pipelex.codegen.lock.encode_lock` output, header comment included.
_PIPELEX_LOCK = (
    "# codegen.lock — generated artifact set (Pipelex codegen). Do not edit by hand.\n\n"
    "lock_version = 1\n"
    'crate_fingerprint = "abc"\n'
    'engine_version = "0.55.0"\n\n'
    '[[artifacts]]\npath = "models.py"\ncontent_hash = "111"\n\n'
    '[[artifacts]]\npath = "nested/extra.ts"\ncontent_hash = "222"\n'
)
_LOCK_WITHOUT_VERSION = 'crate_fingerprint = "abc"\nengine_version = "0.55.0"\n'


class TestCodegenLock:
    def test_parses_a_pipelex_lock(self) -> None:
        lock = parse_lock(_PIPELEX_LOCK)

        assert lock.lock_version == CODEGEN_LOCK_VERSION
        assert lock.crate_fingerprint == "abc"
        assert lock.engine_version == "0.55.0"
        assert [(entry.path, entry.content_hash) for entry in lock.artifacts] == [("models.py", "111"), ("nested/extra.ts", "222")]
        assert lock.paths() == {"models.py", "nested/extra.ts"}

    def test_a_lock_without_lock_version_is_version_one(self) -> None:
        lock = parse_lock(_LOCK_WITHOUT_VERSION)

        assert lock.lock_version == 1
        assert lock.artifacts == []

    def test_a_newer_lock_version_names_the_upgrade_before_the_key_set_is_checked(self) -> None:
        future = "lock_version = 2\nsome_future_key = true\n" + _LOCK_WITHOUT_VERSION

        with pytest.raises(CodegenLockError, match="declares lock_version 2, so upgrade pipelex-sdk"):
            parse_lock(future)

    @pytest.mark.parametrize("bad_version", ["true", '"1"', "0"])
    def test_a_version_that_is_not_a_known_number_is_refused(self, bad_version: str) -> None:
        with pytest.raises(CodegenLockError, match="is not a known codegen lock format version"):
            parse_lock(f"lock_version = {bad_version}\n" + _LOCK_WITHOUT_VERSION)

    def test_an_unknown_key_is_a_malformed_lock(self) -> None:
        with pytest.raises(CodegenLockError, match="Malformed codegen lock"):
            parse_lock(_LOCK_WITHOUT_VERSION + "extra = 1\n")

    def test_malformed_toml_is_a_malformed_lock(self) -> None:
        with pytest.raises(CodegenLockError, match="Malformed codegen lock"):
            parse_lock("this is [not toml")

    @pytest.mark.parametrize("tracked", [["../escape.py"], ["models.py", "models.py"]])
    def test_an_unsafe_or_duplicate_tracked_path_is_a_containment_error_not_a_lock_error(self, tracked: list[str]) -> None:
        entries = "".join(f'\n[[artifacts]]\npath = "{path}"\ncontent_hash = "0"\n' for path in tracked)

        with pytest.raises(CodegenError, match="Unsafe codegen artifact path") as exc_info:
            parse_lock(_LOCK_WITHOUT_VERSION + entries)

        assert not isinstance(exc_info.value, CodegenLockError)

    def test_load_lock_returns_none_when_there_is_no_lock(self, tmp_path: Path) -> None:
        assert load_lock(tmp_path / "codegen.lock") is None

    def test_load_lock_names_the_path_of_a_malformed_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "codegen.lock"
        lock_path.write_bytes(b"\xff\xfe not utf-8")

        with pytest.raises(CodegenLockError, match="Unreadable codegen lock at"):
            load_lock(lock_path)

    def test_validate_artifact_path_returns_the_relative_filesystem_form(self) -> None:
        assert validate_artifact_path("nested/deeper/models.ts") == Path("nested", "deeper", "models.ts")
