# UPSTREAM.md

This repository was imported fresh, with no history, from
[`mohamhossam/smb-ai-requirement-agent`](https://github.com/mohamhossam/smb-ai-requirement-agent)
(requirement-portal ADR-0098). The original stays maintained in parallel.

## Synced to

| Field | Value |
|---|---|
| Original commit | `d5cfb57` |
| Imported | 2026-10-02 |

## Where the kernel's modules came from

| Kernel module | Original path under `src/smb_requirement_agent/` |
|---|---|
| `identity/actor.py` | `domain/identity/entities.py` (actor part), `domain/identity/errors.py` (`InvalidIdentityError`) |
| `identity/ports.py`, `oidc.py`, `fake.py` | `application/ports/identity_provider.py`, `infrastructure/identity/*` |
| `documents/model.py` | `domain/document/entities.py` (evidence, asset and warning), `value_objects.py`, `errors.py` |
| `documents/ports.py` | `application/ports/document_extractor.py`, `document_storage.py` |
| `documents/scanner.py` | `infrastructure/documents/library_worker.py` (scanners only) |
| `documents/*` (extractors) | `infrastructure/documents/*` |
| `llm/*` | `infrastructure/llm/{structured_output,compatible_transport,openai_structured_output,openrouter_structured_output,local_structured_output}.py`, `infrastructure/config/llm_profiles.py` |
| `diagnostics.py` | `infrastructure/diagnostics/debug_trace.py` |
| `persistence/*` | `infrastructure/persistence/{postgres_connector,migration_runner}.py` |
| `observability/*`, `time/*` | `infrastructure/observability/*`, `infrastructure/time/*`, `application/ports/clock.py` |
| `http/body_limit.py` | `interfaces/api/body_limit.py` |
| `errors.py` | the matching classes in `application/errors.py` |

## How to port a fix

1. List the original commits after "Synced to" that touch any path in the table above.
2. Port each one in its own pull request, titled `port: <original subject> (<original sha>)`, and
   release it.
3. Add a row below.

## Ported changes

| Original commit | Kernel release | Pull request |
|---|---|---|
| — | — | — |
