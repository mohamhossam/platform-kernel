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

These rows decide the 14 commits on the original's `smb-product-flow-architecture` branch (the Product Architecture Explorer, requirement-portal ADR-0101), which are not on its `main` yet. "Synced to" stays `d5cfb57` until they land there.

| Original commit | Kernel release | Pull request |
|---|---|---|
| `02992a2` enterprise solution-architecture explorer with DOCX generation | Not applicable: no kernel path touched | — |
| `7199861` public MVP tab, solution-flow hero, calm theme | Not applicable: no kernel path touched | — |
| `ca4a802` impacted-architecture first tab, leaner tab set | Not applicable: no kernel path touched | — |
| `1d71fec` Architecture explorer in the left sidebar | Not applicable: no kernel path touched | — |
| `9411896` calm borders instead of side-tab accents | Not applicable: no kernel path touched | — |
| `e755684` business change requests, phase 1 | Not applicable: no kernel path touched | — |
| `e629c77` dark-theme text on brand | Not applicable: no kernel path touched | — |
| `8833e34` brand dot instead of a side stripe | Not applicable: no kernel path touched | — |
| `dde1451` change requests from Requirement AI, phase 2 | Not applicable: no kernel path touched | — |
| `acc1b3a` CR-20261004-Business_Pro_Plus applied to the model | Not applicable: no kernel path touched | — |
| `e7b05f3` product profile page | Not applicable: no kernel path touched | — |
| `8f19708` visual product page | Not applicable: no kernel path touched | — |
| `94b35aa` one scroll, not two; lifecycle board for journeys | Not applicable: no kernel path touched | — |
| `a1c19b3` product header and offering hero | Not applicable: no kernel path touched | — |
