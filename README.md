# pipelex-sdk

The Python client for the [Pipelex](https://www.pipelex.com) hosted API.

`pipelex-sdk` is the Python counterpart of [`@pipelex/sdk`](https://www.npmjs.com/package/@pipelex/sdk), exactly as [`mthds`](https://pypi.org/project/mthds/) (the `mthds-python` package) is the Python counterpart of the `mthds` npm package. It is the **hosted superset**: the five normative MTHDS Protocol routes (inherited from `mthds`) **plus** the durable run lifecycle **plus** the Pipelex product surface (methods, organizations, billing, API keys, onboarding, storage, run records).

One-way dependency: `pipelex-sdk → mthds`.

## Status

Early development. The public surface is being built phase by phase; see `docs/architecture.md`.

## Install

```bash
pip install pipelex-sdk
```

## Usage

The client is async-only (httpx `AsyncClient` under the hood) and constructs from the environment:

```python
from pipelex_sdk.client import PipelexAPIClient

async with PipelexAPIClient() as client:
    ...
```

Credentials resolve from `PIPELEX_API_KEY` / `PIPELEX_API_URL`, falling back to `MTHDS_API_KEY` / `MTHDS_API_URL` (and `~/.mthds/config`). A token is optional — anonymous access works against the protocol routes; product routes require authentication. The default base URL is `https://api.pipelex.com`.

There is no barrel import: import from the full module path (e.g. `from pipelex_sdk.client import PipelexAPIClient`). The quickstart and the full list of public import paths will be documented here as the surface lands.

## Development

```bash
make install      # create the venv and install all extras (resolves `mthds` from ../mthds-python)
make agent-check  # fix-imports + format + lint + pyright + mypy
make agent-test   # run the test suite quietly (prints only on failure)
make check        # full gate: agent-check aggregate + unused-imports + pylint
```

See `CLAUDE.md` for the coding standards and `docs/architecture.md` for the design.

## License

MIT — see [LICENSE](./LICENSE).
