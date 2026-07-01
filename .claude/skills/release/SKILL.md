---
name: release
description: >
  Automates the pipelex-sdk-python release workflow: bumps the version in pyproject.toml, finalizes the CHANGELOG.md Unreleased section, runs quality checks, regenerates uv.lock, creates a release/vX.Y.Z branch, commits, pushes, and opens a PR to main. Use when user says "release", "cut a release", "bump version", "prepare a release", "make a release", "ship it", "create release branch", or any variation of shipping a new version of the pipelex-sdk Python package. The user can optionally provide changelog content inline when invoking the skill (e.g. "/release Added the storage routes"), which will be used as the changelog entry for this version.
---

# pipelex-sdk-python Release Workflow

This skill handles the full release cycle for the `pipelex-sdk` Python package (import package `pipelex_sdk`, the `pipelex-sdk-python` repo). A release is a `release/vX.Y.Z` branch that PRs into `main`; merging to `main` triggers `publish.yml`, which builds the wheel, publishes it to PyPI as `pipelex-sdk` via Trusted Publishing (OIDC, no token), and creates a Sigstore-signed GitHub release from the changelog notes.

## Files touched

- **`pyproject.toml`** — the `version` field (line 3, under `[project]`)
- **`CHANGELOG.md`** — add `## [vX.Y.Z] - YYYY-MM-DD` entry (convert the `## [Unreleased]` section if present)
- **`uv.lock`** — regenerated via `make li` (lock + install)

## Workflow

### 1. Pre-flight checks

- Read the current version from `pyproject.toml`.
- Read `CHANGELOG.md` to understand the current state (this repo keeps a `## [Unreleased]` section at the top).
- Run `git status` and `git log origin/main..HEAD` to assess the working tree:
  - If there are **uncommitted changes** (staged or unstaged), warn the user and ask whether to commit them as part of the release, stash them, or abort.
  - If there are **unpushed commits** on the current branch, list them so the user is aware — these will be included in the release branch.

### 2. Determine the bump type

Ask the user which kind of version bump they want — **patch**, **minor**, or **major** — unless they already specified it. Show the current version and what the new version would be for each option so the choice is concrete.

While the package is pre-1.0 (`0.y.z`), treat the `0.MINOR.PATCH` segments the way the project has been using them: a breaking change bumps the minor, a backward-compatible feature or fix bumps the patch. If the changelog for this release contains a `### Breaking Changes` section (or otherwise describes a breaking change), steer the user toward at least a minor bump — this matches the repo's "pre-1.0 breaking changes → minor version bump" rule.

### 3. Run quality checks

Run `make agent-check`. This is the gate — if it fails, stop and report the errors so they can be fixed before retrying. Do not proceed past this step on failure.

### 4. Ensure we're on the right branch

The release branch must be named `release/vX.Y.Z` where X.Y.Z is the **new** version. The CI guards in this repo are strict about this:

- `guard-branches.yml` (`gate-main`) rejects any source branch other than `release/vX.Y.Z` merging into `main`.
- `version-check.yml` rejects a mismatch between the branch name and the `pyproject.toml` version.

Both guards match the **exact** regex `release/v[0-9]+\.[0-9]+\.[0-9]+` (strict three-segment semver, no suffix). All file modifications (changelog, version bump, lock) must happen on this branch.

- If already on `release/vX.Y.Z` matching the new version, stay on it.
- If on `dev`, `main`, or any other branch, create and switch to `release/vX.Y.Z` from the current HEAD.
- If on a `release/` branch for a **different** version, warn the user and ask how to proceed.

### 5. Finalize the changelog

Add a new version entry for the release. This repo uses the workspace-wide `## [vX.Y.Z]` header convention (the changelog and publish workflows key off it).

1. If there is an `## [Unreleased]` section, **convert it**: remove the `## [Unreleased]` heading (and any blank lines that immediately follow it) and replace it with the new `## [vX.Y.Z] - YYYY-MM-DD` heading. Any content that was under `[Unreleased]` becomes the content of the new version.
2. If there is no `[Unreleased]` section, insert the new version heading directly after the `# Changelog` intro block.
3. **Never recreate an `[Unreleased]` heading.** After a release the changelog should contain only concrete version entries — the next change adds a fresh `## [Unreleased]` section organically when someone starts the next cycle.
4. If the user provided changelog content when invoking the skill (e.g. `/release Added the storage routes`), **merge** that content with any existing `[Unreleased]` content (do not discard either source). Format the combined content under the appropriate headings — this repo uses `### Breaking Changes`, `### Added`, `### Changed`, `### Fixed`, `### Removed` — inferring headings from the content when possible.
5. If the release has no changelog content yet (neither from an `[Unreleased]` section nor from inline user input), ask the user what to include before proceeding.
6. The result should look like:

```markdown
# Changelog

All notable changes to `pipelex-sdk` are documented here. ...

## [vX.Y.Z] - YYYY-MM-DD

### Changed
- ...

## [vPREVIOUS] - PREVIOUS-DATE
...
```

### 6. Bump the version in pyproject.toml

Edit `pyproject.toml` line 3 (`version = "..."` under `[project]`) to the new version string. Only change the version field — don't touch anything else.

### 7. Lock dependencies

Run `make li` to regenerate `uv.lock` and reinstall. This ensures the lockfile reflects the new version in `pyproject.toml`. The `package-check.yml` CI job runs `uv lock --locked` and fails the PR if `uv.lock` is out of sync, so this step is not optional. If it fails, stop and report the error.

### 8. Commit and push

Stage all release-related changes. This includes at minimum `pyproject.toml`, `CHANGELOG.md`, and `uv.lock`, plus any other files the user chose to include in step 1 (e.g. previously uncommitted work that belongs in this release).

Commit with the message:

```
Release vX.Y.Z
```

Push the branch to origin with `-u` to set up tracking.

### 9. Open a PR

Create a pull request targeting `main` with:

- **Title:** `Release vX.Y.Z`
- **Body:** Include:
  - The changelog entries for this version (copied from CHANGELOG.md)
  - A note about the version bump from old to new

Use this format for the PR body:

```markdown
## Release vX.Y.Z

Bumps version from `A.B.C` to `X.Y.Z`.

### Changelog

<paste the changelog entries for this version here>
```

Report the PR URL back to the user, and remind them that **merging the PR into `main` is what publishes** — `publish.yml` builds the wheel, pushes it to PyPI as `pipelex-sdk` (Trusted Publishing), and cuts the Sigstore-signed GitHub release automatically. Nothing publishes until the PR is merged.

## Important details

- The version follows semver: `MAJOR.MINOR.PATCH`.
- Always confirm the bump type with the user before making changes.
- If `make agent-check` fails, the release is blocked — help the user fix the issues rather than skipping the checks.
- The CI gates a `release/vX.Y.Z` → `main` PR with:
  - `version-check.yml` — the `pyproject.toml` version must match the `release/vX.Y.Z` branch name.
  - `changelog-check.yml` — `CHANGELOG.md` must contain a `## [vX.Y.Z] -` entry for the new version.
  - `package-check.yml` — `uv.lock` must be in sync with `pyproject.toml` (`uv lock --locked`).
  - `tests-check.yml` — the test matrix must pass on every supported Python version (3.10 through 3.14).
  - `lint-check.yml` — ruff format, ruff lint, pyright, and mypy merge checks across the same Python matrix (the same gates as `make agent-check`).
  - `guard-branches.yml` — only `release/vX.Y.Z` branches may target `main`.
  - `cla.yml` — the PR author must have signed the Pipelex CLA (maintainers are allow-listed; an external first-time author will be prompted to sign before the PR can merge).
- All checks must pass for the PR to be mergeable, so getting the changelog, version, and lockfile right is critical.
- **Pre-release versions are not supported through this flow.** Unlike `mthds-python`, this repo's `guard-branches.yml` (`gate-main`) and `version-check.yml` both match the exact regex `release/v[0-9]+\.[0-9]+\.[0-9]+` — a PEP 440 suffix (`a`/`b`/`rc`, e.g. `0.2.0rc1`) on a `release/v0.2.0rc1` branch would be **rejected** by the branch guard even though `publish.yml` can detect pre-releases. Stick to strict three-segment versions for the `release/vX.Y.Z` → `main` flow; raise it with the user if they ask for a pre-release.
- Today's date for the changelog entry: use the current date in `YYYY-MM-DD` format.
