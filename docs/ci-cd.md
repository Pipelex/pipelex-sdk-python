# CI/CD

The GitHub Actions workflows under `.github/workflows/` mirror the `mthds-python` set, adapted to this repo's identity (the `Pipelex` GitHub org and the `pipelex-sdk` PyPI distribution). They split into PR gates that run on every pull request and a publish pipeline that runs when `main` advances.

## PR gates

| Workflow | Trigger | What it enforces |
| --- | --- | --- |
| `lint-check.yml` | `pull_request` | Runs `make merge-check-ruff-format`, `merge-check-ruff-lint`, `merge-check-pyright`, `merge-check-mypy` across the full Python matrix (3.10–3.14). A `lint-all` aggregator is the single required status check. |
| `tests-check.yml` | `pull_request` | Runs `make gha-tests` across the same matrix; `tests-all` aggregates. Concurrency-cancels superseded runs on the same branch. |
| `package-check.yml` | `pull_request` | `uv lock --locked` must leave `uv.lock` unchanged. |
| `changelog-check.yml` | `pull_request → main` | `CHANGELOG.md` must contain a `## [v<version>] - …` entry matching `pyproject.toml`'s `version`. |
| `version-check.yml` | `pull_request → main` | For `release/vX.Y.Z` source branches, the `pyproject.toml` version must equal the branch's version. |
| `guard-branches.yml` | `pull_request_target` | Branch-flow policy: only `release/vX.Y.Z → main`; only `fix|feature|refactor|chore|docs|ci-cd|… → release/*`, `pre-release/*`, or `dev`; external contributors may not edit workflow files. |
| `cla.yml` | `pull_request_target`, `issue_comment` | Contributor License Agreement check against the `Pipelex/cla-signatures` registry. Points at this repo's root `CLA.md`. |

The lint and test matrices use the repo `Makefile` targets, which honor `PYTHON_VERSION`, so each matrix leg provisions its own interpreter via `uv venv --python <version>`.

## Publish pipeline

`publish.yml` runs on `push` to `main` (every merge of a `release/vX.Y.Z` PR, which `guard-branches.yml` is what restricts what can land there). Three sequential jobs:

1. **build** — `python3 -m build` produces the sdist + wheel (`pipelex_sdk-<version>.{tar.gz,whl}`), uploaded as an artifact.
2. **publish-to-pypi** — Trusted Publishing (OIDC, `id-token: write`) to PyPI via `pypa/gh-action-pypi-publish`. The `pypi` environment is pinned to `https://pypi.org/p/pipelex-sdk`.
3. **github-release** — extracts the current version's notes from `CHANGELOG.md`, Sigstore-signs the artifacts, creates the `v<version>` GitHub Release (auto-flagged pre-release for PEP 440 `a`/`b`/`rc` suffixes), and uploads the signed artifacts.

## Required org/repo configuration

- **PyPI Trusted Publishing**: register `Pipelex/pipelex-sdk-python` as a trusted publisher for the `pipelex-sdk` project, environment `pypi`. No API token secret is needed.
- **CLA secrets** (org-level, shared with the other `Pipelex` Python repos): `CLA_GH_APP_ID`, `CLA_GH_APP_PRIVATE_KEY`. The GitHub App must have access to `cla-signatures` and this repo.

## Release flow (summary)

1. Branch `release/vX.Y.Z` off the integration branch; set `pyproject.toml` `version = "X.Y.Z"` and add a `## [vX.Y.Z] - YYYY-MM-DD` entry to `CHANGELOG.md`.
2. Open a PR into `main`. `version-check`, `changelog-check`, `lint-check`, `tests-check`, and `package-check` must pass.
3. Merge. The push to `main` triggers `publish.yml` → PyPI + a signed GitHub Release.
