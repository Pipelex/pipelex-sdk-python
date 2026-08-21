# pipelex-sdk

The Python client for the [Pipelex](https://www.pipelex.com) hosted API.

`pipelex-sdk` is the Python counterpart of [`@pipelex/sdk`](https://www.npmjs.com/package/@pipelex/sdk), exactly as [`mthds`](https://pypi.org/project/mthds/) (the `mthds-python` package) is the Python counterpart of the `mthds` npm package. It is the **hosted superset**: the five normative MTHDS Protocol routes (inherited from `mthds`) **plus** the durable run lifecycle **plus** the Pipelex product surface (methods, organizations, billing, API keys, onboarding, storage, run records).

One-way dependency: `pipelex-sdk → mthds`.

## Install

```bash
pip install pipelex-sdk
```

## Configuration

The **API key** resolves, in order: explicit `api_key` argument → `PIPELEX_API_KEY` → anonymous. The token is **optional** — anonymous access works against the protocol routes (e.g. a local bare runner); the product routes return `401`.

The **base URL** resolves, in order: explicit `base_url` argument → `PIPELEX_BASE_URL` → the hosted default `https://api.pipelex.com`. The base URL is host-only (no path/query/fragment); every endpoint composes as `{base}/v1/{endpoint}`.

The SDK never reads the `mthds` resolver (`MTHDS_API_KEY` / `MTHDS_BASE_URL` / `~/.mthds/config`) — those settings configure the vendor-neutral `mthds` tooling and whichever runner it targets, not this Pipelex client.

`request_timeout_seconds` (constructor argument, default 20 min) sets the per-instance blocking-execute ceiling the inherited protocol routes (`execute` / `start` / `validate` / `models` / `version`) use.

The client is **async-only** (httpx `AsyncClient`) and is an async context manager.

## Quickstart

```python
from pipelex_sdk.client import PipelexAPIClient


async def main() -> None:
    async with PipelexAPIClient() as client:
        # 1. Validate an MTHDS bundle. The verdict is always returned (never raised):
        #    a 200 discriminated on `is_valid`, carrying `rendered_markdown`.
        report = await client.validate([bundle_text])
        print(report.rendered_markdown)
        if not report.is_valid:
            return

        # 2. Run a method end-to-end. `start_and_wait` self-heals across runner kinds:
        #    durable start+poll on the hosted API, blocking execute on a bare runner.
        result = await client.start_and_wait(
            pipe_code="my_pipe",
            inputs={"topic": "quantum computing"},
        )

        # 3. Read the output. Every completed run delivers a resolved `main_stuff`
        #    (the full working memory also rides `pipe_output` on the blocking path).
        print(result.main_stuff)
```

### Long runs: start + poll explicitly

Behind the hosted gateway, a synchronous `execute()` is cut off at ~30s and surfaces a `PipelineExecuteTimeoutError` pointing here. For long methods, drive the durable lifecycle yourself — the run survives client disconnects and is resumable by `pipeline_run_id`:

```python
ack = await client.start(pipe_code="long_pipe", inputs={...})
result = await client.wait_for_result(ack.pipeline_run_id)
```

### Product routes: branch on `err.code`, not the HTTP status

The hosted product routes raise a typed `ApiResponseError` carrying the RFC 9457 `code` discriminant. Branch on `err.code`, which is decoupled from the transport status:

```python
from pipelex_sdk.errors import ApiResponseError

try:
    created = await client.create_pipelex_api_key(label="ci")
    print(created.api_key)  # plaintext — returned only once
except ApiResponseError as exc:
    if exc.code == "pipelex_api_key_limit_reached":
        print("Per-account key limit reached — revoke an old key first.")
    else:
        raise
```

## Public import paths (no barrel)

There is no barrel import — package `__init__.py` files stay empty. Import each symbol from its module:

- **Client & construction** — `from pipelex_sdk.client import PipelexAPIClient, DEFAULT_API_BASE_URL, MthdsFile`
- **Run lifecycle types** — `from pipelex_sdk.runs import RunStatus, RunPublic, RunRead, RunResults, RunResultState, WaitForResultOptions, PollInfo`
- **Product wire models** — `from pipelex_sdk.product_models import UserProfile, MethodData, MethodWriteInput, Membership, MembershipsResponse, SubscriptionResponse, PlanView, InvoiceView, OnboardingSubmission, UploadInput, UploadedFile, PipelineRun, ...`
- **Typed errors** — `from pipelex_sdk.errors import ApiResponseError, ApiUnreachableError, PipelineExecuteTimeoutError, RunFailedError, RunTimeoutError, RunLifecycleUnavailableError, RunStillRunningError`
- **Version** — `from pipelex_sdk.version import __version__`
- **Protocol surface** (the MTHDS standard's wire types) comes from the `mthds` dependency — e.g. `from mthds.protocol.exceptions import PipelineRequestError`, `from mthds.runners.api.models import PipelexValidationResult`.

## Development

```bash
make install      # create the venv and install all extras (resolves `mthds` from ../mthds-python)
make agent-check  # fix-imports + format + lint + pyright + mypy
make agent-test   # run the test suite quietly (prints only on failure)
make check        # full gate: agent-check aggregate + unused-imports + pylint
```

See `CLAUDE.md` for the coding standards and `docs/architecture.md` for the design (including the parity map against `@pipelex/sdk`).

## License

MIT — see [LICENSE](./LICENSE).
