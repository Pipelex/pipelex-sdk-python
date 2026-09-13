---
status: active
item: L-260907-28adb9
---

# Deferred review findings — `method_source_to_contents` and the catalog decoder

What review rounds on `pipelex-sdk-python#31` confirmed but did not fix, with enough detail to pick each up cold. Everything here was verified against the code; nothing rests on a reviewer's word alone. Findings owned by another repo are not here — they are ledger items (`L-260913-6ec559`, `L-260913-37ca47`).

## The `RecursionError` conversion diagnoses the wrong cause when the caller's stack is deep

`pipelex_sdk/product_models.py`, `_decode_method_source`.

`json.loads` raises `RecursionError` for two different reasons that are indistinguishable at the point of the catch: the *source* is nested past what the decoder can descend, or the *caller* was already near the recursion limit when it called. The conversion reports both as "Method file source is nested too deeply to decode", which is a false statement about the data in the second case, and `method_source_to_contents` then reads a perfectly valid catalog array as a raw bundle and returns it without an error.

Reproduced at the default recursion limit of 1000: a recursive caller that reaches depth 995 and then passes the valid catalog string `[{"name": "a.py", "content": "x = 1"}]` gets `ValueError("Method file source is nested too deeply to decode…")` from `parse_method_files`, and through `method_source_to_contents` gets `['[{"name": "a.py", "content": "x = 1"}]']` instead of `['x = 1']`. At depth 990 both behave correctly; at 998 a bare `RecursionError` escapes from the frame setup before `json.loads` is even reached, so the "never raises" contract has a floor no function in Python can lift.

Deferred because the precondition is a program already within a handful of frames of the recursion limit, where essentially nothing behaves. It is recorded rather than fixed because the two causes genuinely cannot be told apart from inside the handler, so any "fix" is either a guard against an untestable state or a reworded message. If it is ever picked up, the honest change is the message — say what was observed ("the decoder ran out of stack") rather than what was inferred about the payload.

## The integer digit-cap `ValueError` bypasses both curated handlers

`pipelex_sdk/product_models.py`, `_decode_method_source`.

`json.loads` on an integer literal past CPython's 4300-digit conversion cap raises a bare `ValueError` ("Exceeds the limit (4300 digits) for integer string conversion") which is neither a `JSONDecodeError` nor a `RecursionError`, so it passes through both `except` clauses untouched and reaches the caller with CPython's message and no `__cause__` chain instead of one naming the expected shape.

The type contract still holds — it *is* a `ValueError`, which is what the docstring promises and what pydantic converts — and behaviour matches the platform, whose own `except ValueError` catches it identically, so `method_source_to_contents` reads it as a bundle on both sides. Deferred as message quality rather than correctness. Worth noting that its `RecursionError` sibling got an owner in the same commit while this one did not, which is the only reason it looks like an oversight.

## The tests monkeypatch stdlib `json.loads` process-wide

`tests/unit/test_method_files.py` and `tests/unit/test_method_source.py`, the deep-nesting tests.

`product_models.json` *is* the stdlib `json` module object, so `mocker.patch.object(product_models.json, "loads", …)` replaces the attribute on the shared module rather than on a seam local to the module under test. Anything else running in the same process during those tests would get the fake. It is harmless here — pydantic-core is Rust and never routes through `json.loads`, and pytest-mock restores the attribute at teardown — and it is the only way to make the real decoder fail without pinning a nesting depth that is an interpreter build constant.

Deferred as test hygiene. A module-local seam is available if it is ever wanted: replace the module reference in the module's own namespace (`mocker.patch.object(product_models, "json", …)` with a stub exposing `loads` and `JSONDecodeError`) rather than the attribute on the shared module.
