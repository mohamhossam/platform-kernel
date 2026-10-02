"""Migrations and pooled connections against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from smb_kernel.errors import PersistenceError
from smb_kernel.persistence.connector import PooledPostgresConnector
from smb_kernel.persistence.migrations import latest_packaged_migration, run_migrations

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.fixture
def table() -> Iterator[str]:
    name = f"kernel_probe_{uuid.uuid4().hex[:8]}"
    yield name
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {name}")
        connection.execute("DELETE FROM schema_migrations WHERE version LIKE %s", (f"%{name}%",))


def test_migrations_apply_once_and_honour_legacy_names(tmp_path: Path, table: str) -> None:
    assert DATABASE_URL is not None
    (tmp_path / f"001_{table}.sql").write_text(f"CREATE TABLE {table} (id int);")
    (tmp_path / f"002_{table}_legacy.sql").write_text("SELECT 1/0;")
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)", (f"old_{table}.sql",)
        )

    legacy = {f"002_{table}_legacy.sql": f"old_{table}.sql"}
    run_migrations(DATABASE_URL, tmp_path, legacy)
    run_migrations(DATABASE_URL, tmp_path, legacy)

    assert latest_packaged_migration(tmp_path) == f"002_{table}_legacy.sql"
    with psycopg.connect(DATABASE_URL) as connection:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
    assert {f"001_{table}.sql", f"002_{table}_legacy.sql"} <= versions


def test_a_failing_migration_is_a_persistence_error(tmp_path: Path, table: str) -> None:
    assert DATABASE_URL is not None
    (tmp_path / f"001_{table}.sql").write_text("SELECT broken syntax here;")
    with pytest.raises(PersistenceError, match="migration failed"):
        run_migrations(DATABASE_URL, tmp_path)


def test_no_packaged_migrations_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PersistenceError):
        latest_packaged_migration(tmp_path)


def test_a_one_connection_pool_returns_clean_sessions() -> None:
    assert DATABASE_URL is not None
    pool = PooledPostgresConnector(
        DATABASE_URL, min_size=1, max_size=1, acquire_timeout_seconds=1, max_idle_seconds=60
    )
    pool.open()
    try:
        with pool.connection() as connection:
            first = connection.execute("SELECT pg_backend_pid()").fetchone()
        with pool.connection() as connection:
            second = connection.execute("SELECT pg_backend_pid()").fetchone()
            assert connection.info.transaction_status in (
                TransactionStatus.IDLE,
                TransactionStatus.INTRANS,
            )
        assert first == second
    finally:
        pool.close()
