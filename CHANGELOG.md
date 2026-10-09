# Changelog

All notable changes to `smb-platform-kernel`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## [1.2.0] — Unreleased

Production hardening for requirement-portal (its `docs/slices/production-hardening.md`, Phase 0).
Everything is additive except the defaults marked **Behaviour change**, which both applications
receive when they bump.

### Added
- `PooledPostgresConnector(configure=…)`: runs on each new pooled connection before it is lent,
  for session settings such as `statement_timeout`; a transaction it leaves open is committed.
- `PooledPostgresConnector.stats()` returns `PoolStats`: connections lent out and idle, the
  ceiling, and borrowers waiting.
- `OidcIdentityProvider` checks:
  - `access_token_types` (default `("Bearer",)`): a token whose `typ` claim names another kind,
    such as a Keycloak ID or refresh token, is refused. A token with no `typ` claim is accepted.
  - `authorized_parties` (default none): when set, the token's `azp` must be one of them, so a
    token issued to another client for the same audience is refused.
- `OidcSigningKeys(retry_seconds=…)`, and `OidcIdentityProvider(jwks_retry_seconds=…)`: how long
  to wait before trying a failed reload again (30s).
- `OpenAIStructuredOutputClient(max_output_tokens=…)`: caps each reply, sent as
  `max_completion_tokens`. Unset, nothing is sent, as before.
- `smb_kernel.http.client.CircuitBreaker`, and `InternalHttpClient(breaker=…)` to share one
  between clients for the same peer.
- Metrics:
  - the Prometheus process, platform and garbage-collector collectors on every `Metrics`
    registry (`process_*`, `python_info`, `python_gc_*`);
  - `smb_build_info` (`set_build_info(service, version)`);
  - `smb_ready` (`set_ready`);
  - `smb_ai_jobs_queued` by operation and `smb_ai_job_oldest_queued_age_seconds`
    (`set_ai_job_queue`; an operation missing from a later sample reads 0);
  - `smb_db_pool_connections` by state (`in_use`, `idle`), `smb_db_pool_max_connections` and
    `smb_db_pool_requests_waiting`, read on every scrape (`watch_db_pool(connector.stats)`);
  - `smb_ingestion_failures_total`, `smb_client_errors_total` by kind and
    `smb_provider_spend_blocked_total` by action.

### Changed
- **Behaviour change.** `run_migrations` serialises runners on a session advisory lock, so two
  started together apply each file once; the second waits, then finds nothing left to do.
- **Behaviour change.** Each migration file waits at most `lock_timeout_seconds` (default 10s;
  None for no limit) for a lock, so one stuck behind live traffic fails instead of queueing every
  later query behind it. The run is still one transaction, so it then leaves the database as it
  was. A file may still set its own `lock_timeout`.
- **Behaviour change.** `OidcIdentityProvider` and `ServiceJwtVerifier` allow 60s of clock
  difference with the issuer on `exp`, `nbf` and `iat` (`leeway_seconds`). Before, the
  provider allowed none, and so did the verifier by default.
- **Behaviour change.** `OidcSigningKeys` fetches outside its lock. While one caller reloads the
  keys, the others are answered from the keys already held, and a failed scheduled reload keeps
  the last good keys in service. Before, every request waited behind a reload, and a failed
  reload failed every request with "identity unavailable". Callers on a cold cache, or with a
  token naming a key the cache lacks, still wait for the fetch, and share its outcome. A key the
  cache lacks while the issuer is unreachable is still "identity unavailable", not an invalid
  token.
- **Behaviour change.** Every `InternalHttpClient` has a circuit breaker: after 5 calls in a row
  fail as unavailable, including failed client-credentials grants, calls fail at once for 30s
  without contacting the peer or the issuer, then one test call decides. A 4xx answer counts as
  the peer being up. This reaches knowledge-portal's clients for requirement-portal too.

## [1.1.0] — 2026-10-09

Service credentials per direction (requirement-portal ADR-0099, ADR-0104): each service can hold
only its own client secret, and the receiving service holds none.

### Added
- `smb_kernel.http.client_credentials.ClientCredentialsTokenSource`: grants a service an access
  token through the issuer's client-credentials grant (the token endpoint comes from discovery and
  must use HTTPS), caches it, and renews it before it expires. Calling it returns the current
  token; `invalidate()` drops it. Any failure raises `ServiceUnavailableError`.
- `InternalHttpClient` takes a callable in place of the token, such as the source above, and asks
  it for the token on each attempt. When the token has `invalidate()` and the peer answers 401,
  the client renews it and sends the request once more; the guard refused it before any route
  ran, so this is safe for a non-idempotent POST too.
- `smb_kernel.http.service_auth.ServiceJwtVerifier`: accepts a service access token signed by the
  issuer with an asymmetric algorithm, for the configured audience, and maps the client it was
  granted to (`azp`, else `client_id`) to a caller name. A person's token never names a service
  client, so it is refused.
- `ServiceVerifierChain`: accepts a token any of its verifiers accepts, so a deployment can take
  shared secrets and granted tokens together while it moves over, or keep shared secrets for
  offline runs.
- `ServiceCallerVerifier`, the protocol `InternalRouteGuard` now takes (`ServiceTokenVerifier`
  still satisfies it).
- `smb_kernel.identity.oidc.OidcSigningKeys` and `discover_oidc`: the discovery and signing-key
  cache `OidcIdentityProvider` used privately, now shared with `ServiceJwtVerifier`.
  `OidcIdentityProvider` behaves as before.

### Changed
- `InternalRouteGuard` answers 503 instead of failing when a token cannot be checked because the
  issuer is unreachable, and runs the verifier in a worker thread, since checking a granted token
  may fetch the issuer's keys.

## [1.0.2] — 2026-10-03

### Fixed
- `OpenAIStructuredOutputClient.parse` keeps `invalid_output` on the cause chain when the SDK
  raises a pydantic `ValidationError`. The error is raised from a classified
  `ModelTransportError("invalid_output")` that carries the pydantic error, so public error
  translation can classify it again, and the details stay for logs. This is the known issue
  carried since 1.0.0.
- `PooledPostgresConnector` no longer names every pool `smb-requirement-agent`. It takes an
  optional `name`, which the application passes. Left unset, the pool takes psycopg_pool's own
  numbered name (`pool-1`). Existing calls keep working.

## [1.0.1] — 2026-10-03

### Security
- PyJWT is raised from 2.13 to `>=2.15.1,<2.16`. 2.13.0 carries thirteen advisories published on
  2026-10-03 (one critical), including forged tokens accepted through HMAC key confusion and
  JWKS fetches that follow redirects. `OidcIdentityProvider` uses only `get_unverified_header`,
  `decode` and `PyJWKSet.from_dict`, which keep their behaviour. A key set with no usable keys
  now raises `PyJWKSetError`, a `PyJWTError`, so it is still reported as identity unavailable.

### Known issues
- The `OpenAIStructuredOutputClient.parse` cause chain described under 1.0.0 is unchanged; its
  fix moved to 1.0.2 so this release carries only the security update.

## [1.0.0] — 2026-10-02

First release, extracted from `smb-ai-requirement-agent@d5cfb57` (ADR-0100).

### Added
- `smb_kernel.errors`: the infrastructure errors the mechanisms raise, plus
  `ServiceUnavailableError`, `ServiceResponseError` and `ServiceAuthenticationError`.
- `smb_kernel.identity`: actor primitives, the identity-provider port, `OidcIdentityProvider`,
  and `FakeIdentityProvider`, which now takes its personas from the application and ships none.
- `smb_kernel.documents`: the extraction model and ports (including the new `DocumentScannerPort`),
  the bounded subprocess extractor, the format extractors, OCR, the Office renderer, and the
  ClamAV and offline scanners.
- `smb_kernel.llm`: the structured-output transports and model profiles. `smb_kernel.embeddings`
  holds the `Embedding` value.
- `smb_kernel.persistence`: the direct and pooled connectors. `run_migrations` and
  `latest_packaged_migration` now take the migrations directory, and legacy names, as
  parameters.
- `smb_kernel.observability`: correlation, logging (`LogFormat` now lives here) and metrics.
  `smb_kernel.time` holds the clock port and clocks; `smb_kernel.diagnostics` holds the debug trace.
- `smb_kernel.http`:
  - `RequestBodyLimit`, which reads `BodyLimits` from a callable instead of application
    settings;
  - the new `InternalHttpClient`;
  - the new `InternalRouteGuard` with `ServiceTokenVerifier`.

### Known issues
- In `OpenAIStructuredOutputClient.parse`, a pydantic `ValidationError` from the SDK is re-raised
  with `raise response_validation_error(exc, "") from exc`. The `from exc` replaces the
  `ModelTransportError("invalid_output")` cause that `StructuredResponseValidationError` sets on
  itself, so public error translation cannot classify it as invalid output. The original
  `smb-ai-requirement-agent` behaves the same way, and 1.0.0 keeps that behaviour unchanged.
  `tests/unit/test_provider_structured_output.py` pins it. Fixed in 1.0.2.
