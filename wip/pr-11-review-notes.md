# PR #11 — deferred review-agent findings

Follow-up notes from triaging the SWE-bot review comments on [PR #11](https://github.com/Pipelex/pipelex-sdk-python/pull/11) (Release v0.5.0). Each item below was verified read-only against the code and against the two repos this SDK is contractually bound to — `../pipelex-sdk-js/` (`@pipelex/sdk`, the parity counterpart) and `../pipelex/` (the runtime whose `input_normalizer` the walk mirrors).

The other flagged comment on the PR was a false positive and needed no follow-up (recorded under "Dismissed" below).

---

## 1. Nested asset under a structured `url` field is not uploaded (confirmed, deferred)

**Reported by:** codex — thread on `pipelex_sdk/prepare_inputs.py:153`.

**Status:** Confirmed latent bug. Deferred because a correct fix is a cross-repo contract decision, not a local patch, and the SDK is at exact parity with `@pipelex/sdk`.

### The bug

`_resolve_node` classifies a template node as file content purely by shape:

```python
def _is_file_content(node: Any) -> bool:
    return isinstance(node, dict) and "url" in node
```

For a structured concept that has a top-level field literally named `url` **and** a sibling file-bearing field — e.g. `Article { url: str, cover: Image }` — the explicit template renders as:

```json
{"url": "https://mock.invalid/url", "cover": {"url": "https://mock.invalid/url"}}
```

(The template generator special-cases any field named `url` or `*_url` at `../pipelex/pipelex/core/concepts/concept_representation_generator.py:314-320`, so `url` is not reserved to Image/Document.)

`_is_file_content` then fires on the **top-level** `url` (`prepare_inputs.py:153`), so `_resolve_file_position` resolves only the article's text URL and returns early. The walk never recurses into `cover`, so a caller value like `{"url": "https://example.com/a", "cover": <bytes>}` leaves the `cover` bytes **unuploaded** — the hosted run receives raw bytes at a nested Image position.

### Why the runtime gets this right and the SDK does not

The runtime `../pipelex/pipelex/pipeline/input_normalizer.py:61-93` dispatches on the Python **type** of the value: `isinstance(value, (ImageContent, DocumentContent))` vs `isinstance(value, StructuredContent)` (which recurses every field). It never keys on the presence of a `url` dict key. The SDK's shape-only heuristic is a documented approximation of that type-based classifier (`prepare_inputs.py:7-12`, `docs/input-preparation.md`); the two coincide for the common case and diverge exactly on this shape.

### Why it is not cleanly fixable at the SDK layer

- **No type info at nested positions.** Only the top-level template envelope carries a `"concept"` key, and it is dropped when the walk reads `entry["content"]` (`prepare_inputs.py:205`). At a nested `{"url": ...}` the SDK has nothing but shape to go on — it cannot tell an `ImageContent` from a structured concept whose single field is `url: str`.
- **Shape refinement is ambiguous.** A single-key `{"url": str}` structured concept is indistinguishable from single-key image content; and multi-key canonical image content is deliberately supported (the existing test `tests/unit/test_prepare_inputs.py:136` feeds a two-key `{"url", "mime_type"}` cover). So neither "single-key only" nor "keys ⊆ image/document vocabulary" is reliable without hardcoding the runtime's field vocabulary into the SDK — fragile and still collision-prone.
- **Parity constraint.** The Python code is a faithful port of `../pipelex-sdk-js/src/prepare-inputs.ts:67-69,172-174` (identical `isFileContent` + identical short-circuit). Any behavioral change must land in both SDKs in the same coordinated change; a Python-only fix would break the parity invariant.

### Recommended real fix (upstream, coordinated)

Thread concept/type information into the **nested** positions of the explicit inputs template in `../pipelex/` (so a nested node self-identifies as Image/Document vs structured), then update both SDKs to classify by that tag instead of by the `url` key. That is a template-contract change and should be decided with the runtime + JS SDK owners together.

### Fragile interim mitigation (only if forced, must be mirrored in JS)

Make `_is_file_content` treat a dict as file content only when `"url" in node` **and** every other key is drawn from the known Image/Document optional-field set (`public_url, mime_type, filename, title, snippet, caption, width, height, source_prompt, source_negative_prompt`). This recovers the `{url, cover}` case while preserving the multi-key image case in the current tests. It does **not** fix the single-key `{url: str}` structured concept (still shape-indistinguishable), so it is a partial mitigation, not a fix — which is why the honest call is to defer and raise the contract question upstream.

### Repro (documentation only — not added to the suite)

A failing test would break release CI, so this is recorded here rather than committed. In `tests/unit/test_prepare_inputs.py` style (`_FakePrepareClient`, `asyncio.run`, single `TestPrepareInputs` class):

- template entry: `_entry("demo.Article", {"url": "https://mock.invalid/url", "cover": {"url": "https://mock/c.png"}})`
- inputs: `{"article": {"url": "https://example.com/a", "cover": bytes([7, 7])}}`
- expected once fixed: `cover` rewritten to a `pipelex-storage://` url, top-level `url` passed through, `len(uploads) == 1`.

Under today's code this asserts 0 uploads and the cover bytes leak — cleanly documenting the gap.

---

## 2. Oversized upload surfaces as `UploadTransportError`, not `RejectedAssetError` (needs-judgment, server-side)

**Reported by:** greptile (P1) — thread on `pipelex_sdk/upload.py:101-103`. The literal comment ("400/422 should be `RejectedAssetError`") is a **false positive for this PR** (see below), but verification surfaced a real cross-repo seam worth a decision.

### Why the literal comment is a false positive

`_map_upload_error` (`upload.py:90-105`) maps `413 → RejectedAssetError`, `401|403 → UploadAuthenticationError`, `404 → UnsupportedUploadCapabilityError`, and everything else → `UploadTransportError`. `../pipelex-sdk-js/src/upload.ts:196-241` is byte-for-byte identical (only 413 maps to a rejected asset). Mapping 400/422 → `RejectedAssetError` in Python alone would diverge from `@pipelex/sdk`, which is the repo's controlling invariant. So the flagged line is correct-by-design.

### The real seam

`pipelex-api` rejects an oversized upload with **422**, not 413: the base64 `data` field has a Pydantic `max_length=MAX_UPLOAD_BASE64_CHARS` constraint (`pipelex-api/api/routes/uploader.py`), which FastAPI turns into a 422 request-validation error (asserted by `pipelex-api/tests/unit/test_uploader.py`). The explicit `len(data) > MAX_UPLOAD_BYTES` → 413 path is only reachable in the narrow band where the char count passes but decoded bytes marginally exceed the cap. A base64-decode failure returns 400.

Consequence: the documented "asset too big → `RejectedAssetError`" category is effectively **unreachable in the common case**, in *both* SDKs — the most common oversized rejection comes back as a transport error.

### Options (pick one, coordinated)

- **Preferred:** make `pipelex-api`'s size rejection surface as **413** (align the Pydantic-`max_length` rejection with the explicit 413 check) so the existing "413 == rejected asset" contract holds end-to-end. No SDK change; parity preserved.
- **Alternative:** treat 422 as a rejection at the SDK layer — extend `case 413:` → `case 413 | 422:` — but only if landed in **both** `pipelex_sdk/upload.py` and `../pipelex-sdk-js/src/upload.ts` together, with matching tests. `RejectedAssetError` carries `status`, so callers could still tell 413 from 422.

---

## Dismissed (no follow-up needed)

**Empty-list template skips uploads** — greptile (P2), `prepare_inputs.py:155-160`. Can't-happen: the explicit template never emits an empty list. Top-level multiplicity wraps the content in a one-element exemplar (`../pipelex/pipelex/core/concepts/concept.py:225-226`) and nested `list[T]` fields render one example item (`../pipelex/pipelex/core/concepts/concept_representation_generator.py:210-236`). Line 156's `template_node[0]` already relies on non-empty, and JS carries the identical `> 0` guard (`prepare-inputs.ts:175`). Recorded here so it is not re-flagged.
