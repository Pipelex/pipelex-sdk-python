"""This SDK's part of the `User-Agent` every request to the Pipelex API carries.

The header follows the workspace client-identification spec: product tokens, outermost first —
the integrator's `app_info`, then this SDK, then the `mthds` library whose transport it inherits,
then the Python runtime and its `(<os>; <arch>)` comment:

    acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.16.0 python/3.12.4 (linux; x86_64)

The builder, the runtime token and `AppInfo` belong to `mthds.runners.api.user_agent`; this module
only contributes this SDK's own token and re-exports `AppInfo`, so `from pipelex_sdk.user_agent
import AppInfo` names the very class `MthdsAPIClient` accepts. `PipelexAPIClient` puts the token in
front of the base's through the `user_agent_sdk_tokens()` seam.
"""

from __future__ import annotations

from mthds.runners.api.user_agent import AppInfo, product_token

from pipelex_sdk.version import __version__

__all__ = ["SDK_TOKEN_NAME", "AppInfo", "pipelex_sdk_token"]

#: This SDK's product-token name in the spec's closed registry (the repo name, not the PyPI name).
SDK_TOKEN_NAME = "pipelex-sdk-python"


def pipelex_sdk_token() -> str:
    """This SDK's own product token, `pipelex-sdk-python/<package version>`."""
    return product_token(SDK_TOKEN_NAME, __version__)
