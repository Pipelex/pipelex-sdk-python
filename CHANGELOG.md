# Changelog

All notable changes to `pipelex-sdk` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repository scaffold: packaging (`pyproject.toml`), tooling (`Makefile`, ruff/pyright/mypy/pylint config mirroring `mthds-python`), and the empty `pipelex_sdk` package.
- `PipelexAPIClient` (subclass of `mthds`'s `MthdsAPIClient`): Pipelex-branded construction (resolves `PIPELEX_API_KEY` / `PIPELEX_API_URL`, falling back to the `mthds` resolver; token optional for anonymous access; host-only base-URL validation; origin URL for `health`).
- Transport extension layer: `_request_product` (typed `ApiResponseError` mapping, empty-body tolerant, PUT/PATCH/DELETE), `_request_json` (plainer error regime), transport-failure mapping to `ApiUnreachableError`, and the `problem+json` error-body parser.
- Errors: `ApiResponseError` (with the RFC 9457 `code` discriminant) and `ApiUnreachableError`, both deriving from the protocol-base `PipelineRequestError`.
