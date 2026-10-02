# AGENTS.md — platform-kernel

Standing engineering rules for any coding agent working in this repository.

## 1. Purpose

`smb_kernel` holds the mechanisms that `requirement-portal` and `knowledge-portal` share. The
decisions are recorded in requirement-portal's ADR-0098 and ADR-0100.

## 2. The rule

**Mechanisms, never meaning.** Before adding anything, ask whether it encodes a business
decision: a role name, a prompt, a schema, a request or response model, a workflow, or a
domain error. If it does, it belongs in an application, and the change is refused here. The
table in `README.md` is the reference.

## 3. Boundaries enforced in CI

- **No application imports.** The kernel never imports `smb_requirement_agent` or
  `knowledge_portal`.
- **Pure contract modules.** The contract modules import no framework, driver or provider SDK.
  The list is in `.importlinter`.
- **Strict typing.** `mypy --strict` passes on `src` and `tests`.

## 4. Compatibility

- **The public API is every name not prefixed with `_`.** Changing or removing one is a major
  release.
- **Shared types keep their identity.** Applications re-export kernel classes as their own (errors,
  actor and extraction models). Keep those classes stable: a rename breaks every `except` clause
  downstream.
- **Behaviour changes need a changelog entry and a minor release, even when they are fixes.** Each
  application must be able to see what changed when Dependabot bumps it.

## 5. Adapters

An adapter returns content its caller can accept, or raises a kernel error
(`smb_kernel.errors`):
- it never leaks a `KeyError`, `IndexError` or transport exception;
- it never returns an empty success for an unusable response.

## 6. Tests and gates

Run all of these before proposing a change:

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
```

- Integration tests need `TEST_DATABASE_URL`, a disposable PostgreSQL. Never point it at a
  database that holds data.
- New mechanisms come with unit tests in `tests/unit`.

## 7. Origin and porting

This repository was imported fresh from `smb-ai-requirement-agent@d5cfb57`. The original stays
maintained in parallel. A fix there to a module that now lives here is ported by the procedure in
`UPSTREAM.md`, and released.
