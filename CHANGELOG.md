# Changelog

All notable changes to `smb-platform-kernel`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## [1.0.0] — unreleased

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
  `tests/unit/test_provider_structured_output.py` pins it. The fix is planned for 1.0.1.
