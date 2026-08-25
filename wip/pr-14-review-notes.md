# PR #14 — deferred review findings

Findings from the pre-landing review of [PR #14](https://github.com/Pipelex/pipelex-sdk-python/pull/14) (`feature/Typed-method-id-run-option`, reviewed at `301d96e`). Each item below was verified against the code and, where the claim was about the built artifact, against a locally built sdist. None of them blocks landing the branch; each is deferred because acting on it reaches outside what this branch set out to change.

The findings that *were* acted on during the review are recorded in `CHANGELOG.md` under `[Unreleased]`, not here.

---

## 1. The published sdist carries the repo's internal planning documents

**Status:** Confirmed, pre-existing, widened by this branch. Deferred because the fix is a packaging change to `pyproject.toml`, which this branch does not touch and which has release implications worth deciding on their own.

`pyproject.toml` declares `build-backend = "hatchling.build"` (`pyproject.toml:40`) and carries no `[tool.hatch.build.targets.sdist]` section, so hatchling falls back to including everything the VCS does not ignore. An sdist built at the review point confirms it. The listing below is that build — version 0.5.0, at `301d96e` — and is kept as it was taken rather than re-run, so it predates this very file and is not the current release's archive:

```
$ uv build --sdist
$ tar -tzf dist/pipelex_sdk-0.5.0.tar.gz
pipelex_sdk-0.5.0/CLAUDE.md
pipelex_sdk-0.5.0/TODOS.md
pipelex_sdk-0.5.0/Makefile
pipelex_sdk-0.5.0/uv.lock
pipelex_sdk-0.5.0/docs/HANDOFF.md
pipelex_sdk-0.5.0/wip/pr-11-review-notes.md
pipelex_sdk-0.5.0/wip/updates.md
pipelex_sdk-0.5.0/tests/...
```

`CLAUDE.md`, `wip/pr-11-review-notes.md`, `docs/HANDOFF.md`, the `Makefile` and the whole `tests/` tree already shipped this way before the branch, so this is not a regression it introduced. What the branch adds is `TODOS.md` and `wip/updates.md` — the tracker and the design — which together are a substantial share of the archive and are addressed to reviewers of this PR rather than to anyone installing the package. `docs/HANDOFF.md` is a related case already on PyPI: it describes creating this repo from scratch and reads as rot to anyone who finds it in a release.

Nothing breaks — an sdist is not what `pip install` normally consumes, and none of these files is importable — so this is about what a public package says about itself, not about correctness.

**If picked up:** declare an explicit sdist include list (or an exclude list covering `wip/`, `TODOS.md`, `CLAUDE.md`, `Makefile` and `docs/HANDOFF.md`), decide deliberately whether `tests/` should stay (some consumers value a testable sdist), and land it with a release rather than inside a feature branch.

## 2. The client class docstring dates its surfaces by build-plan phase

**Status:** Confirmed, pre-existing. Deferred because Phase 2 of this branch scoped its citation sweep to bare workspace-private *paths*, and widening that scope mid-branch was a judgement call the tracker declined elsewhere for the same reason.

`pipelex_sdk/client.py:176` and `pipelex_sdk/client.py:178` describe the run lifecycle as "(added in Phase 2)" and the product surface as "(added in Phase 3)". Those phase numbers refer to the original build plan for this package. They travel to PyPI in the class docstring of the one class every consumer instantiates, where they resolve to nothing — the same failure mode as the repo-relative spec paths Phase 2 replaced, in a different spelling.

**If picked up:** replace each marker with what the reader actually needs (the release the surface shipped in, or nothing at all), and sweep for the same pattern elsewhere in the shipped modules.

## 3. `start_and_wait` documents fewer exceptions than it propagates

**Status:** Confirmed, pre-existing. Deferred as too small to justify widening this branch's diff.

`pipelex_sdk/client.py` documents `Raises: RunFailedError` and `RunTimeoutError` on `start_and_wait`, but the method reaches `_merge_hosted_run_extensions` on both of its paths — the durable one through `start` and the fallback through `_execute_blocking` → `execute` — so it also propagates `PipelineRequestError` for a reserved key on `extra` and, since this branch, for a non-string `method_id`. The `Raises:` sections of `execute` and `start` were corrected during this review; `start_and_wait` was left alone because its omission predates the branch and is not about anything the branch changed.

**If picked up:** add the `PipelineRequestError` line to `start_and_wait`, and while there check `wait_for_result` and the product methods for the same drift.

## 4. `PipelineRun.pipe_statuses` is a contract no server fills, in three repos at once

**Status:** Confirmed, pre-existing, and the most consequential item here. Deferred because it cannot be resolved inside this repo: removing the field locally would break the parity invariant this package is built on, and the decision belongs to whoever owns the run wire contract.

`pipelex_sdk/product_models.py` declares `pipe_statuses: dict[str, PipeStatus] | None = None` on `PipelineRun`, with `PipeStatus` as its supporting enum. The platform never sends it. `RunPublic` — the model FastAPI serializes `GET /v1/runs` and `GET /v1/runs/{id}` through — declares no such field (`pipelex-server/shared/src/pipelex_shared/schemas/run.py:180`), and a response model strips whatever it does not declare. A `grep -rn "pipe_statuses"` over the entire `pipelex-server` monorepo returns nothing at all, so no route, worker or Lambda writes it either. The field therefore reads `None` on every run this SDK will ever parse, and a consumer branching on it gets a silently empty answer rather than an error.

The same dead field exists in the two sibling repos, which is what makes it a workspace question rather than a local cleanup:

- `pipelex-sdk-js/src/product-models.ts:322` — `pipe_statuses?: Record<string, PipeStatus> | null;`, with the enum at `:291`. This SDK is a port of that one, so dropping the field here alone would introduce exactly the parity gap `docs/architecture.md` → "Parity with `@pipelex/sdk`" exists to prevent.
- `pipelex-app/src/types/run.ts:24` — the same declaration, and `pipelex-app/src/components/method/run-history-list.tsx:269` renders a row of per-pipe progress dots gated on `{run.pipe_statuses && ...}`. Because the platform never sends it, that guard is always false and those dots have never appeared. Whether that is a missing feature or an abandoned one is the question to settle.

So there are two coherent outcomes and this branch is the wrong place to choose between them: either the platform starts projecting per-pipe status onto `RunPublic` (and the webapp's dots light up), or the field is retired from all three clients together.

**Filed:** `../wip/inbox/2026-08-25-workspace-pipe-statuses-dead-field-in-three-clients.md` (`to: workspace`, naming `pipelex-server/platform`, `pipelex-sdk-js` and `pipelex-app`). Whichever way the decision goes, this SDK follows the JS SDK; it should not move first.
