# Changelog

All notable changes to `pipelex-sdk` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repository scaffold: packaging (`pyproject.toml`), tooling (`Makefile`, ruff/pyright/mypy/pylint config mirroring `mthds-python`), and the empty `pipelex_sdk` package.
- `PipelexAPIClient` (subclass of `mthds`'s `MthdsAPIClient`): Pipelex-branded construction (resolves `PIPELEX_API_KEY` / `PIPELEX_API_URL`, falling back to the `mthds` resolver; token optional for anonymous access; host-only base-URL validation; origin URL for `health`).
- Transport extension layer: `_request_product` (typed `ApiResponseError` mapping, empty-body tolerant, PUT/PATCH/DELETE), `_request_json` (plainer error regime), transport-failure mapping to `ApiUnreachableError`, and the `problem+json` error-body parser.
- Errors: `ApiResponseError` (with the RFC 9457 `code` discriminant) and `ApiUnreachableError`, both deriving from the protocol-base `PipelineRequestError`.
- Durable run lifecycle (`pipelex_sdk/runs.py` + client methods): owned run-lifecycle models (`RunStatus`, `RunRead`, `RunResults`, the discriminated `RunResultState`, `WaitForResultOptions`, `PollInfo`) and the polling surface `get_run_status` / `get_run_result` / `wait_for_result`, mapping the platform's `202`/`503`/`200`/`409` results semantics to a typed union.
- `start_and_wait` self-heals across hosted and bare runners: a cached `GET /v1/version` handshake picks the durable start+poll path on the hosted API and falls back to the blocking `POST /v1/execute` on a bare runner (including the case where a base-only version response hides a missing run store — `start` then surfaces `RunLifecycleUnavailableError` before any run is created). This closes a gap versus `mthds-python` (whose `start_and_wait` raises on a bare runner).
- Lifecycle errors `RunFailedError`, `RunTimeoutError`, `RunLifecycleUnavailableError`; `RunStillRunningError` (the protocol `execute()` 202-degrade error) re-exported from `mthds` so all run/lifecycle errors share one import home.
