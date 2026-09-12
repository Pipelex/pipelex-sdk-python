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

    @pytest.mark.parametrize("declaration", ["# coding: raw_unicode_escape", "# -*- coding: unicode_escape -*-", "#coding=utf-8"])
    def test_parse_stamped_refuses_a_python_artifact_that_declares_a_source_encoding(self, declaration: str) -> None:
        stamped = _stamped().replace(
            "# >>> pipelex-codegen-stamp >>>\n",
            f"# >>> pipelex-codegen-stamp >>>\n{declaration}\n",
        )
        # The prefix gate proves every header line opens with a comment marker in the bytes on disk. A PEP 263
        # declaration makes that proof worthless, because CPython decodes the file before tokenizing it and the
        # declaration chooses the codec: under an escape-decoding codec a header value can carry characters that
        # become a line break plus a statement, in the one region no hash covers. All three spellings here are
        # ones CPython honours, and the emitter writes none of them.
        assert parse_stamped(stamped, comment_prefix="#") is None

    def test_parse_stamped_accepts_a_coding_declaration_below_the_first_two_lines(self) -> None:
        stamped = _stamped().replace("# options: {}\n", "# options: {}\n# coding: raw_unicode_escape\n")
        # CPython honours the declaration on the first two lines only, so one below them decodes nothing
        # differently. Refusing it would report a drift the state does not justify.
        assert parse_stamped(stamped, comment_prefix="#") is not None

    def test_parse_stamped_does_not_apply_the_python_encoding_rule_to_typescript(self) -> None:
        body = "export const A = 1;\n"
        stamped = _stamped(comment_prefix="//", body=body).replace(
            "// >>> pipelex-codegen-stamp >>>\n",
            "// >>> pipelex-codegen-stamp >>>\n// coding: raw_unicode_escape\n",
        )
        # TypeScript has no source-encoding declaration, so the line is an ordinary header field there. Every
        # line terminator ECMAScript honours is one `splitlines` already breaks on, which is the `.ts` half.
        parsed = parse_stamped(stamped, comment_prefix="//")
        assert parsed is not None
        assert parsed.body == body
