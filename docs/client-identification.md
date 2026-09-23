# Client identification (`User-Agent`)

Every request this SDK sends to the Pipelex API carries a `User-Agent` header that says which program made it. The platform reads that header to tell a run started from an SDK script apart from one started by the web app, the MCP server, the CLI or a hand-written `curl`, and records the result in product analytics and its access log. The convention is shared by every first-party client and is fixed by the workspace spec `docs/specs/client-identification.md`; this page describes how this SDK follows it.

## What the header contains

The value is a list of product tokens, outermost first: the integrator's own name when one is given, then this SDK, then the `mthds` library whose transport `PipelexAPIClient` inherits, then the Python runtime with its operating system and architecture.

```
acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.16.0 python/3.12.4 (linux; x86_64)
```

- `pipelex-sdk-python/<version>` carries the installed `pipelex-sdk` distribution's version, read through `importlib.metadata`, so it cannot drift from the package that ships.
- `mthds-python/<version>` is the `mthds` library's own token, carrying the version that package reports; `mthds` adds it, not this SDK.
- `python/<major.minor.micro> (<os>; <arch>)` is built by `mthds` from `sys.version_info`, `platform.system().lower()` and `platform.machine()`. An empty platform value is left out of the comment, and the comment is dropped when neither is readable; the runtime token always stays.

The header is built once, when the client is constructed, and is exposed as `client.user_agent`; `client.app_info` holds the `AppInfo` it was built with. It is a default header of the one `httpx.AsyncClient` that `start_client` creates, so every API request carries it, authenticated or anonymous, including `health`, uploads and the product routes. The object-store fetches of the artifact stack use their own client and are left with httpx's default `User-Agent`, because that traffic goes to a third party.

The header is self-declared and unauthenticated. It is for analytics and diagnostics only, and the platform never uses it to decide authorization, rate limits or entitlements.

## Naming your application with `app_info`

An integrator can put its own name in front of the SDK's tokens by passing an `AppInfo`, shaped like Stripe's `appInfo`. `pipelex_sdk.user_agent.AppInfo` is the `mthds` class `mthds.runners.api.user_agent.AppInfo` re-exported, so either import names the same type:

```python
from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.user_agent import AppInfo

client = PipelexAPIClient(
    app_info=AppInfo(name="acme-invoicer", version="1.4.0", details=("batch",), url="https://acme.example"),
)
# client.user_agent starts with "acme-invoicer/1.4.0 (batch; +https://acme.example) pipelex-sdk-python/..."
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | An RFC 9110 token (letters, digits and the `tchar` punctuation, with no space, slash, parenthesis or semicolon), such as `acme-invoicer` |
| `version` | no | A token, such as `1.4.0` |
| `url` | no | A URL, rendered in the comment as `+url`; it must be visible ASCII and may not contain whitespace, parentheses, backslashes or semicolons |
| `details` | no | A tuple of comment parameters, each a token or `token=value`, where the value is a token or a `name/version` product. A list is accepted at run time and stored as a tuple, but the field is typed `tuple[str, ...]` |

It renders as `name/version (<details>; +url)`, dropping `/version` when there is no version and the comment when there are neither details nor a URL. An empty `version`, `url` or `details` counts as absent rather than invalid, so `version=""` is stored as `None`. An invalid field, or an unknown one, is refused when the `AppInfo` is constructed, with a `pydantic.ValidationError`, which is a `ValueError`; it is never silently dropped or rewritten. The model is frozen but not strict, so pydantic's usual coercions apply. A header longer than the spec's 512-character ceiling is refused with a `ValueError` when the client is constructed.

Do not put a secret, a user identifier, an email address or a hostname in `app_info`: the header is logged and analysed.

## Relation to `mthds`

The header builder belongs to the `mthds` library, in `mthds.runners.api.user_agent`, and `MthdsAPIClient` exposes a seam for the SDKs built on it. `PipelexAPIClient` overrides the class method `user_agent_sdk_tokens()` to return its own `pipelex-sdk-python/<version>` token (from `pipelex_sdk.user_agent.pipelex_sdk_token()`) in front of the base's `mthds-python/<version>`, and calls `init_user_agent(app_info)` from its `__init__`, which sets `app_info` and builds `user_agent`. It calls the seam rather than the base's `__init__` because that constructor reads the `mthds` resolver, which this client must not. The runtime token, the `AppInfo` validation and the 512-character ceiling are therefore the ones `mthds` applies, and `pipelex_sdk.user_agent` holds only this SDK's token name and the re-exported `AppInfo`.
