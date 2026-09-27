---
status: active
item: L-260927-424b11
---

# PR #50 — deferred review findings

These are findings from the `/rev` passes on [PR #50](https://github.com/Pipelex/pipelex-sdk-python/pull/50) (`feature/Refused-start-raises-bare`). Round 1 reviewed `88805ea` and round 2 reviewed `9d397e3`. The findings that were acted on are in `CHANGELOG.md` under `[Unreleased]`. The findings that belong to another repo are ledger items: the copied message-reason helpers are L-260927-9d2df3 (mthds-python), and the JS twin's missing-route 404 is L-260927-578c32 (pipelex-sdk-js). This note keeps the one finding this repo owns and did not act on.

---

## 1. A `200` from `/v1/version` whose body is not JSON escapes the handshake

**Status:** Unverified (raised by cubic in round 2 and not put to a verifier). It predates this branch and is deferred at the round-2 bar as a defect that does not matter.

`_supports_run_lifecycle` catches `(ApiResponseError, httpx.HTTPError, ValidationError)` around `self.version()`, and its comment says a body that is no version makes the client assume hosted. The inherited `version()` calls `response.json()` before it validates, though. A `200` whose body is not JSON at all, such as an HTML page from a proxy or a mistyped base URL, raises `json.JSONDecodeError`, which none of the three catches, so `start_and_wait` raises that instead of assuming hosted. `pydantic.ValidationError` and `json.JSONDecodeError` both subclass `ValueError`, so catching `ValueError` in place of `ValidationError` would make the comment true.

It does not matter much in practice. Assuming hosted leads straight to `start`, which reads the same non-JSON `200` through `response.json()` and fails the same way, so the caller would see the same decode error one request later. The change is worth making if the handshake is ever touched again.
