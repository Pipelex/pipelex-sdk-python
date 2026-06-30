# pipelex-sdk-python

This file guides Claude Code when working in this repo. It is self-contained: the repo overview below, then the Python coding standards (mirroring `../mthds-python/CLAUDE.md`, the relevant standard for this package). The workspace-root `CLAUDE.md` and `.claude/rules/python-standards.md` also apply.

## What this repo is

`pipelex-sdk` (import package `pipelex_sdk`) — the Python client for the Pipelex hosted API. It is the Python counterpart of the TypeScript `@pipelex/sdk` (`PipelexApiClient`), built on `mthds` (the `mthds-python` package) exactly as `@pipelex/sdk` is built on the `mthds` npm package.

It is the **hosted superset**: the five normative MTHDS Protocol routes (inherited from `mthds`) **plus** the durable run lifecycle **plus** the Pipelex product surface (methods, organizations, billing, API keys, onboarding, storage, run records). See `docs/architecture.md`.

## Architecture invariants (do not violate)

- **One-way dependency: `pipelex-sdk → mthds`.** This package depends on `mthds` and never the reverse.
- **Inheritance, not re-implementation.** `class PipelexAPIClient(MthdsAPIClient)`. Reuse the base transport (`_send`, `_url`), body-builders, the reusable protocol methods, `runner_type`, and the async context-manager. Add lifecycle/product/health on top. The base's single-underscore transport methods are treated as a documented **protected extension surface** — do not rename or fork them.
- **Brand boundary (MTHDS vs Pipelex).** MTHDS = the open standard's brand; Pipelex = the runtime/product brand. Protocol routes and their models belong to `mthds` and keep neutral names; Pipelex-specific surfaces (lifecycle, product routes, implementation envelopes) live here. Name by which brand owns the concept.
- **Credentials.** Resolve `PIPELEX_API_KEY` / `PIPELEX_API_URL` first, then fall back to the `mthds` resolver (`MTHDS_API_KEY` / `MTHDS_API_URL`, `~/.mthds/config`). Token is **optional** (anonymous allowed). Default base URL `https://api.pipelex.com`.
- **Async-only.** httpx `AsyncClient`, `async def` throughout. No sync facade in v0.1.
- **No barrel.** `__init__.py` files stay empty — no re-exports, no docstrings. Import via full paths (`from pipelex_sdk.client import PipelexAPIClient`).

## Workflow

- Use Make targets: `make install`, `make agent-check`, `make agent-test`, `make check`.
- Always run `make agent-check` and `make agent-test` before considering a change done or pushing.
- Test-first where practical. `pytest-mock` only (never `unittest.mock`). One `TestClass` per test module. No `__init__.py` in test directories.
- Document every iteration: update `docs/` and `CHANGELOG.md` alongside code.
- No hardcoded counts in code/docs/commits. Pre-1.0 breaking changes → minor version bump.

---

# Python Coding Best Practices

## Python Version Compatibility

- Target Python 3.10+. Never use features introduced after Python 3.10 without a compatibility fallback.
- Common pitfalls:
  - `datetime.UTC` was added in Python 3.11. Use `datetime.timezone.utc` instead.
  - `StrEnum` was added in Python 3.11. Import it from the local `pipelex_sdk._compat` shim.
  - `type` statement (PEP 695) was added in Python 3.12. Use `TypeAlias` from `typing` instead.
  - `ExceptionGroup` / `except*` was added in Python 3.11. Avoid unless using the `exceptiongroup` backport.

## Variables, Loops and Indexes

- Variable names should have a minimum length of 3 characters. No exceptions: name your `for` loop indexes like `index_item`, your exceptions `exc` or more specific like `validation_error` when there are several layers of exceptions, and use `for key, value in ...` for key/value pairs.
- When looping on the keys of a dict, use `for key in the_dict` rather than `for key in the_dict.keys()`.
- Avoid inline for loops, unless it's ultra-simple and holds on one line.
- If you have a variable that will get its value differently through different code paths, declare it first with a type, e.g. `result: str` but DO NOT give it a default value like `result: str = ""` unless it's really justified. We want the variable to be unbound until all paths are covered, and the linters will help us avoid bugs this way.

## Enums

- When defining enums related to string values, always inherit from `StrEnum` (from `pipelex_sdk._compat`).
- When you need the enum value as a string, don't use `str(enum_var)` or `enum_var.value`, just use `enum_var` itself — that is the point of using StrEnum.
- Never test equality to an enum value: use match/case, even to single out 1 case out of 10 cases. To avoid heavy match/case code in awkward places, add `@property` methods to the enum class such as `is_foobar()`. This prevents bugs: when new enum values are added the linter will complain about non-exhaustive matches. Use the `|` operator to group cases.
- Match/case constructs over enums should always be exhaustive. NEVER add a default `case _: ...`.

## Optionals

- Don't write things like `a = b if b else c`, write `a = b or c` instead.

## Imports

- Import all necessary libraries at the top of the file. Do not import inside functions/classes unless a `# noqa` is genuinely required.
- Do not bother ordering or removing unused imports — let Ruff handle it (`make fui`).
- `if TYPE_CHECKING:` blocks must be the **last** block in the imports section.
- No re-exports in `__init__.py`. Always use direct full-path imports.

## Typing

- Every function parameter and return must be typed. Type all fields and non-obvious variables.
- Use lowercase generics: `dict[]`, `list[]`, `tuple[]`. Use `Field(default_factory=...)` for mutable defaults.
- Use `# pyright: ignore[specificError]` / `# type: ignore` only as a last resort; prefer `cast()` or a new typed variable.

### BaseModel / Pydantic Standards

- Use `BaseModel` and respect Pydantic v2 standards. Use `ConfigDict` when needed (e.g. `model_config = ConfigDict(extra="forbid", strict=True)`). Wire models that must tolerate unknown server fields use `extra="allow"`.
- Keep models focused and single-purpose. For list fields with non-string items, use a typed `default_factory`.

## Error Handling

- Catch exceptions where you can add useful context. Use specific exceptions. Convert third-party exceptions to custom ones (except in pydantic validators, where `ValueError`/`TypeError` are fine).
- NEVER catch the generic `Exception` except at a top-level entry point (with a one-line comment naming why).
- Always `raise NewError(msg) from exc`. Write the message into a variable before raising.
- Put custom error classes in `exceptions.py` / `errors.py` modules (this package uses `pipelex_sdk/errors.py`).

```python
try:
    await client.start(...)
except RunLifecycleUnavailableError as exc:
    msg = "This runner does not support the durable run lifecycle"
    raise SomeError(msg) from exc
```

## Writing Tests

- NEVER use `unittest.mock`. Always use pytest-mock: `from pytest_mock import MockerFixture`.
- NEVER put more than one TestClass into a test module.
- Name test files `test_*.py`. Place them under `tests/unit/`, `tests/integration/`, or `tests/e2e/`. No `__init__.py` in test directories.
- Fixtures go in `conftest.py`; test data constants go in `test_data.py` grouped in classes.
- Use strong asserts (test value, not just type/presence). Use `parametrize` for multiple cases. Test success and failure paths. Mock at the httpx boundary.

## Test-Driven Development

1. Write a test first.
2. Write the minimum code to pass it.
3. Run linting and type checking (`make agent-check`).
4. Validate tests (`make agent-test`).

## Post-Coding Checklist

After finishing any change, run `make agent-check` && `make agent-test`. Do not consider a task done until both pass.
