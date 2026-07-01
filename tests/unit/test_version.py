"""Version sync guard — the runtime `__version__` must match the `pyproject.toml` source of truth."""

import re
from pathlib import Path

import pytest

from pipelex_sdk.version import __version__

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+")


def _declared_version() -> str:
    """The `[project].version` declared in pyproject.toml, read without a TOML dependency.

    `^version` (MULTILINE) anchors at the line start, so it never matches the
    `python_version` / `target-version` / `required-version` tooling keys.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "no [project].version found in pyproject.toml"
    return match.group(1)


class TestVersion:
    def test_version_is_semver(self) -> None:
        assert _SEMVER.match(__version__), f"__version__ {__version__!r} is not a semver string"

    def test_version_matches_pyproject(self) -> None:
        # A stale install (editable tree whose dist metadata was not refreshed after a
        # bump) would misreport the SDK to consumers doing diagnostics/compat checks.
        if __version__ == "0.0.0":
            pytest.skip("pipelex-sdk distribution metadata not installed; run `make install`")
        assert __version__ == _declared_version()
