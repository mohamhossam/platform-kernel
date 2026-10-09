"""Migrations and pooled connections against a real PostgreSQL.

Skipped unless TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from smb_kernel.errors import PersistenceError
from smb_kernel.persistence.connector import DbConnection, PooledPostgresConnector
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


def test_two_runners_started_together_apply_each_file_once(tmp_path: Path, table: str) -> None:
    assert DATABASE_URL is not None
    # Not idempotent: a second application of the file would fail.
    (tmp_path / f"001_{table}.sql").write_text(
        f"CREATE TABLE {table} (id int); SELECT pg_sleep(0.5);"
    )
    failures: list[BaseException] = []

    def run() -> None:
        try:
            run_migrations(DATABASE_URL, tmp_path)
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    runners = [threading.Thread(target=run) for _ in range(2)]
    for runner in runners:
        runner.start()
    for runner in runners:
        runner.join(30)

    assert failures == []


def test_a_migration_waiting_too_long_for_a_lock_fails_and_leaves_no_trace(
    tmp_path: Path, table: str
) -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"CREATE TABLE {table} (id int)")
    (tmp_path / f"001_{table}_column.sql").write_text(f"ALTER TABLE {table} ADD COLUMN note text;")

    with psycopg.connect(DATABASE_URL) as holder:
        holder.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
        started = time.monotonic()
        with pytest.raises(PersistenceError, match="lock timeout"):
            run_migrations(DATABASE_URL, tmp_path, lock_timeout_seconds=0.2)
        assert time.monotonic() - started < 5
        holder.rollback()

    with psycopg.connect(DATABASE_URL) as connection:
        recorded = connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = %s",
            (f"001_{table}_column.sql",),
        ).fetchone()
    assert recorded == (0,)
    run_migrations(DATABASE_URL, tmp_path, lock_timeout_seconds=0.2)


def test_a_failing_file_leaves_the_files_before_it_unapplied(tmp_path: Path, table: str) -> None:
    assert DATABASE_URL is not None
    (tmp_path / f"001_{table}.sql").write_text(f"CREATE TABLE {table} (id int);")
    (tmp_path / f"002_{table}_broken.sql").write_text("SELECT broken syntax here;")

    with pytest.raises(PersistenceError):
        run_migrations(DATABASE_URL, tmp_path, lock_timeout_seconds=1)

    with psycopg.connect(DATABASE_URL) as connection:
        created = connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
    assert created == (None,)
    assert f"001_{table}.sql" not in versions


def test_a_negative_lock_timeout_is_refused(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    with pytest.raises(ValueError):
        run_migrations(DATABASE_URL, tmp_path, lock_timeout_seconds=0)


def test_configure_sets_up_each_pooled_session_and_stats_count_it() -> None:
    assert DATABASE_URL is not None
    configured: list[int] = []

    def configure(connection: DbConnection) -> None:
        connection.execute("SET statement_timeout = '1500ms'")
        configured.append(1)

    pool = PooledPostgresConnector(
        DATABASE_URL,
        min_size=1,
        max_size=2,
        acquire_timeout_seconds=5,
        max_idle_seconds=60,
        configure=configure,
    )
    pool.open()
    try:
        with pool.connection() as connection:
            setting = connection.execute("SHOW statement_timeout").fetchone()
            busy = pool.stats()
        borrowed = pool.acquire()
        assert pool.stats().in_use == 1
        pool.release(borrowed)
        assert setting == ("1500ms",)
        assert busy.in_use == 1
        assert busy.max_size == 2
        assert configured
        assert pool.stats().in_use == 0
    finally:
        pool.close()
