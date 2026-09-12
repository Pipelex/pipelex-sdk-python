"""The stamp grammar both codegen readers depend on, mirroring `pipelex/codegen/stamp.py`: which file types are
stamped, in which comment syntax, the begin-line predicate that decides whether a file on disk belongs to
codegen, and the parser plus content hash that turn one stamped file into a drift verdict.
"""

import hashlib

import pytest

from pipelex_sdk.codegen_stamp import (
    STAMPABLE_SUFFIXES,
    comment_prefix_for,
    compute_content_hash,
    has_stamp,
    is_stampable_artifact_path,
    parse_stamped,
)
from pipelex_sdk.errors import CodegenError

_BODY = "from pydantic import BaseModel\n"


def _stamped(*, comment_prefix: str = "#", body: str = _BODY, content_hash: str | None = None) -> str:
    return (
        f"{comment_prefix} >>> pipelex-codegen-stamp >>>\n"
        f"{comment_prefix} crate_fingerprint: {'f' * 64}\n"
        f"{comment_prefix} engine_version: 0.57.0\n"
        f"{comment_prefix} projection: types / python-pydantic\n"
        f"{comment_prefix} options: {{}}\n"
        f"{comment_prefix} content_hash: {content_hash if content_hash is not None else compute_content_hash(body)}\n"
        f"{comment_prefix} <<< pipelex-codegen-stamp <<<\n"
        f"{body}"
    )


class TestCodegenStamp:
    def test_the_stampable_suffixes_are_python_and_typescript(self) -> None:
        assert frozenset({".py", ".ts"}) == STAMPABLE_SUFFIXES

    @pytest.mark.parametrize(("artifact_path", "expected_prefix"), [("models.py", "#"), ("nested/schemas.ts", "//")])
    def test_comment_prefix_follows_the_suffix(self, artifact_path: str, expected_prefix: str) -> None:
        assert comment_prefix_for(artifact_path) == expected_prefix

    def test_an_unstampable_file_type_has_no_comment_prefix(self) -> None:
        with pytest.raises(CodegenError, match="not stampable"):
            comment_prefix_for("sources.json")

    @pytest.mark.parametrize(
        ("content", "comment_prefix", "expected"),
        [
            ("# >>> pipelex-codegen-stamp >>>\n# <<< pipelex-codegen-stamp <<<\nbody\n", "#", True),
            ("// >>> pipelex-codegen-stamp >>>\n// <<< pipelex-codegen-stamp <<<\nbody\n", "//", True),
            ("// >>> pipelex-codegen-stamp >>>\nbody\n", "#", False),
            ("\n# >>> pipelex-codegen-stamp >>>\nbody\n", "#", False),
            ("handwritten = True\n", "#", False),
        ],
    )
    def test_has_stamp_reads_only_the_opening_line(self, content: str, comment_prefix: str, expected: bool) -> None:
        assert has_stamp(content, comment_prefix=comment_prefix) is expected

    @pytest.mark.parametrize(
        ("artifact_path", "expected"),
        [
            ("models.py", True),
            ("nested/deep/schemas.ts", True),
            ("sources.json", False),
            ("codegen.lock", False),
            ("README", False),
            (".py", False),
            ("archive.tar.py", True),
        ],
    )
    def test_the_stampable_predicate_follows_the_suffix(self, artifact_path: str, expected: bool) -> None:
        # Public so a caller's tree walk filters exactly as the check does; a suffixless name and a dotfile
        # both have no suffix, so neither can ever be an artifact.
        assert is_stampable_artifact_path(artifact_path) is expected

    def test_the_content_hash_is_lowercase_sha256_hex_over_utf8_bytes(self) -> None:
        body = "X = 'é'\n"
        assert compute_content_hash(body) == hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_parse_stamped_returns_the_recorded_hash_and_the_body_byte_exactly(self) -> None:
        parsed = parse_stamped(_stamped(), comment_prefix="#")
        assert parsed is not None
        assert parsed.body == _BODY
        assert parsed.content_hash == compute_content_hash(_BODY)

    def test_parse_stamped_does_not_verify_the_hash_it_reports(self) -> None:
        parsed = parse_stamped(_stamped(content_hash="0" * 64), comment_prefix="#")
        # Splitting and verifying are separate steps: the caller compares, which is what lets the check tell
        # a self-inconsistent stamp apart from a body that merely drifted off the lock.
        assert parsed is not None
        assert parsed.content_hash == "0" * 64

    def test_parse_stamped_keeps_an_empty_body_empty(self) -> None:
        parsed = parse_stamped(_stamped(body=""), comment_prefix="#")
        assert parsed is not None
        assert parsed.body == ""

    def test_parse_stamped_reads_the_typescript_fence(self) -> None:
        parsed = parse_stamped(_stamped(comment_prefix="//", body="export const A = 1;\n"), comment_prefix="//")
        assert parsed is not None
        assert parsed.body == "export const A = 1;\n"

    def test_parse_stamped_refuses_a_fence_in_the_wrong_comment_syntax(self) -> None:
        assert parse_stamped(_stamped(comment_prefix="//"), comment_prefix="#") is None

    @pytest.mark.parametrize(
        "content",
        [
            "handwritten = True\n",
            "\n# >>> pipelex-codegen-stamp >>>\n# <<< pipelex-codegen-stamp <<<\nbody\n",
            "# >>> pipelex-codegen-stamp >>>\n# projection: types / python-pydantic\nbody\n",
        ],
    )
    def test_parse_stamped_refuses_a_missing_or_unterminated_fence(self, content: str) -> None:
        assert parse_stamped(content, comment_prefix="#") is None
