# platform-kernel

Shared **mechanisms** for the requirement and knowledge portals: the Python package `smb_kernel`.

It is one of three repositories:
- [`requirement-portal`](https://github.com/mohamhossam/requirement-portal), which owns the
  requirements service, its UI and the platform deployment;
- [`knowledge-portal`](https://github.com/mohamhossam/knowledge-portal), which owns the shared
  library and the architecture and squad catalogues;
- this kernel.

The decisions are recorded in requirement-portal's ADR-0098 (three repositories) and ADR-0100
(this kernel).

## The rule: mechanisms, never meaning

| Area | In the kernel | Stays in each application |
|---|---|---|
| Identity (`smb_kernel.identity`) | OIDC/JWKS validation, the identity-provider port, actor primitives, the fake-provider mechanism | Authorization and business roles, fake personas |
| Documents (`smb_kernel.documents`) | Bounded subprocess extraction, format extractors, OCR, the Office renderer, ClamAV and offline scanners, the extractor/storage/scanner ports | Ingestion workflows |
| LLM (`smb_kernel.llm`) | Structured-output transports (compatible, OpenAI, OpenRouter, local), model profiles | Prompts, schemas, and output interpretation |
| Embeddings (`smb_kernel.embeddings`, `llm.compatible_transport`) | The `Embedding` value and the configured embedding adapter | Retrieval behaviour |
| Persistence (`smb_kernel.persistence`) | Direct and pooled connectors, `run_migrations(url, migrations_dir, legacy_names)` | Schemas, migrations, repositories |
| Operations (`smb_kernel.observability`, `smb_kernel.time`, `smb_kernel.diagnostics`) | Correlation, logging, Prometheus metrics and provider metering, clocks, the opt-in debug trace | Domain metrics and errors |
| Internal HTTP (`smb_kernel.http`) | `InternalHttpClient`, `InternalRouteGuard` with `ServiceTokenVerifier`, `RequestBodyLimit` | API models and contracts |

If a change needs a role name, a prompt, a schema or a request model, it belongs in an
application. The kernel's import-linter forbids importing either application, and keeps the
contract modules (`errors`, `embeddings`, `identity.actor`, `identity.ports`, `documents.model`,
`documents.ports`, `time.clock`) free of frameworks and drivers.

**Applications re-export rather than redefine.** An application's error and model modules import
the kernel's classes (`DocumentExtractionError`, `InvalidDocumentError`, `ActorProfile`, and so
on). A handler written against the application's name then catches exactly what the kernel
raises.

## Using it

```toml
# pyproject.toml of an application
dependencies = ["smb-platform-kernel"]

[tool.uv.sources]
smb-platform-kernel = { git = "https://github.com/mohamhossam/platform-kernel", tag = "v1.0.0" }
```

To develop against a local checkout before a release, point the source at the folder instead:
`smb-platform-kernel = { path = "../platform-kernel", editable = true }`. Never commit that.

## Developing

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports
```

The PostgreSQL integration tests run when `TEST_DATABASE_URL` points at a disposable database.

## Releasing

Releases follow semantic versioning. A breaking change to any public name is a major release.
1. Update `CHANGELOG.md` and `version` in `pyproject.toml` and `src/smb_kernel/__init__.py`.
2. Merge to `main` with green CI.
3. Push the tag `vX.Y.Z`. The release workflow re-runs every gate before publishing the
   GitHub release.

Dependabot then raises a bump in each application.

## Origin

Imported fresh from `smb-ai-requirement-agent@d5cfb57` on 2026-10-02; see `UPSTREAM.md`.
