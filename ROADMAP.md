# ROADMAP.md — platform-kernel

| Release | Scope | Status |
|---|---|---|
| 1.0.0 | Extraction from `smb-ai-requirement-agent@d5cfb57`, plus the internal HTTP client and service-token guard (Stage 1 of requirement-portal's `docs/slices/enhancement-platform-split.md`) | Released 2026-10-02 |
| 1.0.1 | PyJWT 2.15.1 for the 2026-10-03 advisories | Released 2026-10-03 |
| 1.0.2 | `OpenAIStructuredOutputClient.parse` keeps the invalid-output cause | Planned |
| 1.1.0 | Keycloak client-credentials tokens for `InternalRouteGuard` and `InternalHttpClient`, as an alternative to shared secrets (ADR-0099) | Planned |

Each later release is driven by a need from one of the applications, and recorded in
`CHANGELOG.md`.
