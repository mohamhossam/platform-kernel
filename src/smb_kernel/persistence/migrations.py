"""Minimal ordered PostgreSQL migration runner.

Each application owns its schema: it passes the directory holding its packaged
`*.sql` files, and any legacy names an earlier release recorded for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import psycopg

from smb_kernel.errors import PersistenceError


def latest_packaged_migration(migrations: Path) -> str:
    """The migration a fully upgraded database has applied last.

    Readiness compares against this rather than a hard-coded name, so adding a
    migration cannot leave probes accepting a schema that lacks it.
    """
    names = sorted(path.name for path in migrations.glob("*.sql"))
    if not names:
        raise PersistenceError("No packaged PostgreSQL migrations were found.")
    return names[-1]


def run_migrations(
    database_url: str,
    migrations: Path,
    legacy_names: Mapping[str, str] | None = None,
) -> None:
    """Apply each packaged SQL migration exactly once."""
    legacy = legacy_names or {}
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for path in sorted(migrations.glob("*.sql")):
                if path.name in applied:
                    continue
                legacy_name = legacy.get(path.name)
                if legacy_name in applied:
                    connection.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                    )
                    applied.add(path.name)
                    continue
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                )
    except psycopg.Error as exc:
        raise PersistenceError(f"PostgreSQL migration failed: {exc}") from exc
