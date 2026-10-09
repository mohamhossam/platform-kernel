"""Minimal ordered PostgreSQL migration runner.

Each application owns its schema: it passes the directory holding its packaged
`*.sql` files, and any legacy names an earlier release recorded for them.

A run is one transaction: a failing file leaves the database as it was.
Runners serialise on an advisory lock, so two processes started together (two
replicas' migration jobs, say) apply each file once. Each file waits at most
`lock_timeout_seconds` for a lock: a migration stuck behind live traffic fails
instead of queueing every later query behind it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import psycopg

from smb_kernel.errors import PersistenceError

# One lock per database: every application's runner uses the same key, and
# advisory locks are scoped to the database they are taken in.
_RUNNER_LOCK = "SELECT pg_advisory_lock(hashtextextended('smb_kernel.run_migrations', 0))"
DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS = 10.0


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
    *,
    lock_timeout_seconds: float | None = DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Apply each packaged SQL migration exactly once, all in one transaction.

    `lock_timeout_seconds` bounds how long each file may wait for a lock; None
    lets it wait indefinitely. A file may still set its own `lock_timeout`.
    Waiting for another runner to finish is not bounded.
    """
    if lock_timeout_seconds is not None and lock_timeout_seconds <= 0:
        raise ValueError("The migration lock timeout must be positive, or None.")
    legacy = legacy_names or {}
    try:
        with psycopg.connect(database_url) as connection:
            # Session-scoped, so it is held until the connection closes, after
            # the run's transaction has committed or rolled back.
            connection.execute(_RUNNER_LOCK)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # Read once the lock is held, so a runner that waited sees what the
            # one before it applied.
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for path in sorted(migrations.glob("*.sql")):
                if path.name in applied:
                    continue
                if legacy.get(path.name) not in applied:
                    if lock_timeout_seconds is not None:
                        # Set before each file, since a file may change it.
                        connection.execute(
                            "SELECT set_config('lock_timeout', %s, true)",
                            (f"{max(1, round(lock_timeout_seconds * 1000))}ms",),
                        )
                    connection.execute(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                )
                applied.add(path.name)
    except psycopg.Error as exc:
        raise PersistenceError(f"PostgreSQL migration failed: {exc}") from exc
