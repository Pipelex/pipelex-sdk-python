"""Package version, derived from the installed distribution metadata.

`__version__` is read from the installed `pipelex-sdk` distribution (via
`importlib.metadata`), so there is no hardcoded constant to drift from the
`pyproject.toml` source of truth — the analogue of the JS SDK's `SDK_VERSION`
sync guard, but with nothing to keep in sync by hand. `tests/unit/test_version.py`
asserts the resolved value matches the version declared in `pyproject.toml`,
catching a stale install (e.g. an editable tree whose metadata was not refreshed
after a bump).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: The PyPI distribution name (NOT the import package `pipelex_sdk`).
PACKAGE_NAME = "pipelex-sdk"

try:
    __version__: str = version(PACKAGE_NAME)
except PackageNotFoundError:
    # Running from a source tree that was never installed (no dist metadata).
    __version__ = "0.0.0"
