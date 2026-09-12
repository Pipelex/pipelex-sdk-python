"""The stamp facts a tree writer depends on: which file types are stamped, in which comment syntax, and the
begin-line predicate that decides whether a file on disk belongs to codegen (mirroring `pipelex/codegen/stamp.py`).
"""

import pytest

from pipelex_sdk.codegen_stamp import STAMPABLE_SUFFIXES, comment_prefix_for, has_stamp
from pipelex_sdk.errors import CodegenError


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
