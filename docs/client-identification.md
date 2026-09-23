# Client identification (`User-Agent`)

Every request this SDK sends to the Pipelex API carries a `User-Agent` header that says which program made it. The platform reads that header to tell a run started from an SDK script apart from one started by the web app, the MCP server, the CLI or a hand-written `curl`, and records the result in product analytics and its access log. The convention is shared by every first-party client and is fixed by the workspace spec `docs/specs/client-identification.md`; this page describes how this SDK follows it.

## What the header contains

The value is a list of product tokens, outermost first: the integrator's own name when one is given, then this SDK, then the `mthds` library whose transport `PipelexAPIClient` inherits, then the Python runtime with its operating system and architecture.

```
acme-invoicer/1.4.0 pipelex-sdk-python/0.11.0 mthds-python/0.15.0 python/3.12.4 (linux; x86_64)
```

- `pipelex-sdk-python/<version>` carries the installed `pipelex-sdk` distribution's version, read through `importlib.metadata`, so it cannot drift from the package that ships.
- `mthds-python/<version>` carries the installed `mthds` distribution's version. When that metadata cannot be read, the token is omitted rather than guessed.
- `python/<major.minor.micro> (<os>; <arch>)` reads `sys.version_info`, `platform.system().lower()` and `platform.machine()`. A platform value that is empty or not a valid token is left out of the comment, and the comment is dropped when neither is readable; the runtime token always stays.

The header is built once, when the client is constructed, and is exposed as `client.user_agent`. It is a default header of the one `httpx.AsyncClient` that `start_client` creates, so every API request carries it, authenticated or anonymous, including `health`, uploads and the product routes. The object-store fetches of the artifact stack use their own client and are left with httpx's default `User-Agent`, because that traffic goes to a third party.

The header is self-declared and unauthenticated. It is for analytics and diagnostics only, and the platform never uses it to decide authorization, rate limits or entitlements.

## Naming your application with `app_info`

An integrator can put its own name in front of the SDK's tokens by passing an `AppInfo`, shaped like Stripe's `appInfo`:

```python
from pipelex_sdk.client import PipelexAPIClient
from pipelex_sdk.user_agent import AppInfo

client = PipelexAPIClient(
    app_info=AppInfo(name="acme-invoicer", version="1.4.0", details=["batch"], url="https://acme.example"),
)
# client.user_agent starts with "acme-invoicer/1.4.0 (batch; +https://acme.example) pipelex-sdk-python/..."
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | An RFC 9110 token (letters, digits and the `tchar` punctuation, with no space, slash, parenthesis or semicolon), such as `acme-invoicer` |
| `version` | no | A token, such as `1.4.0` |
| `url` | no | A URL, rendered in the comment as `+url`; it must be visible ASCII and may not contain whitespace, parentheses, backslashes or semicolons |
| `details` | no | A list of comment parameters, each a token or `token=value`, where the value is a token or a `name/version` product |

It renders as `name/version (<details>; +url)`, dropping `/version` when there is no version and the comment when there are neither details nor a URL. An empty `version`, `url` or `details` counts as absent rather than invalid, so `version=""` is stored as `None`. An invalid field is refused when the `AppInfo` is constructed, with a `pydantic.ValidationError`, which is a `ValueError`; it is never silently dropped or rewritten. A header longer than the spec's 512-character ceiling is refused with a `ValueError` when the client is constructed.

Do not put a secret, a user identifier, an email address or a hostname in `app_info`: the header is logged and analysed.

## Relation to `mthds`

The spec places the header builder of the `mthds` library in `mthds.runners.api.user_agent`. The `mthds` version this SDK pins does not ship it yet, so `pipelex_sdk.user_agent` builds the whole header itself and mirrors the public shape the `mthds` builder has: an `AppInfo` model with `name`, `version`, `url` and `details`, and a `ValueError` on an invalid token.
