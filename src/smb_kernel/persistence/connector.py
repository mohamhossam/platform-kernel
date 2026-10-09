"""Where PostgreSQL adapters obtain connections.

Long-running processes (the API and its workers) share one bounded, health-checked
pool so each unit of work reuses an established session instead of paying a new
TCP/TLS/authentication handshake. One-shot commands (migrations, backfills,
projection rebuilds) open a single direct connection per use and need no pool.
Both hand out connections with the same transaction semantics, so adapters are
unaware which one they were given.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

import psycopg
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool

DbConnection = Connection[tuple[object, ...]]

# Idle pooled sessions above min_size are closed after this long (psycopg's default).
POOL_MAX_IDLE_SECONDS = 600.0


class PostgresConnector(Protocol):
    def acquire(self) -> DbConnection:
        """Borrow a connection whose transaction the caller will end."""
        ...

    def release(self, connection: DbConnection) -> None:
        """Return a borrowed connection; uncommitted work is rolled back."""
        ...

    def connection(
        self, timeout_seconds: float | None = None
    ) -> AbstractContextManager[DbConnection]:
        """One transaction: committed on success, rolled back on error."""
        ...

    def close(self) -> None: ...


class DirectPostgresConnector:
    """A new connection per use, for one-shot commands with no process to pool for."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def acquire(self) -> DbConnection:
        return psycopg.connect(self._database_url)

    def release(self, connection: DbConnection) -> None:
        # Closing discards any uncommitted transaction.
        connection.close()

    @contextmanager
    def connection(self, timeout_seconds: float | None = None) -> Iterator[DbConnection]:
        if timeout_seconds is None:
            connection = psycopg.connect(self._database_url)
        else:
            connection = psycopg.connect(
                self._database_url, connect_timeout=max(1, int(timeout_seconds))
            )
        with connection:
            yield connection

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class PoolStats:
    """A pool at one moment: connections lent out and idle, its ceiling, and borrowers waiting."""

    in_use: int
    idle: int
    max_size: int
    waiting: int


class PooledPostgresConnector:
    """A bounded pool shared by every adapter in one long-running process.

    `configure` runs once on each new connection before the pool hands it out,
    for session settings such as `statement_timeout`. A transaction it leaves
    open is committed, so the connection joins the pool idle.
    """

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int,
        max_size: int,
        acquire_timeout_seconds: float,
        max_idle_seconds: float,
        name: str | None = None,
        configure: Callable[[DbConnection], None] | None = None,
    ) -> None:
        # The application names its pool, for psycopg_pool's logs and stats;
        # left unset, the pool takes psycopg_pool's own numbered name.
        self._pool: ConnectionPool[DbConnection] = ConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=acquire_timeout_seconds,
            max_idle=max_idle_seconds,
            # Validate idle connections before handing them out, so a database
            # restart surfaces as a reconnect rather than a failed request.
            check=ConnectionPool.check_connection,
            name=name,
            configure=None if configure is None else _settled(configure),
            open=False,
        )
        # Counted here: the pool's own size also counts connections still opening.
        self._lent = 0
        self._lent_lock = Lock()

    def open(self) -> None:
        """Start filling the pool without blocking boot on database availability.

        An unreachable database is reported by `/ready` and by the first
        request that needs it, not by a crash loop before probes can run.
        """
        self._pool.open(wait=False)

    def stats(self) -> PoolStats:
        """The pool's current use; cheap enough to read on every metrics scrape."""
        raw = self._pool.get_stats()
        with self._lent_lock:
            in_use = self._lent
        return PoolStats(
            in_use=in_use,
            idle=raw.get("pool_available", 0),
            max_size=self._pool.max_size,
            waiting=raw.get("requests_waiting", 0),
        )

    def acquire(self) -> DbConnection:
        connection = self._pool.getconn()
        self._count_lent(1)
        return connection

    def release(self, connection: DbConnection) -> None:
        # End an abandoned transaction here: the pool would do the same, but
        # logs every such return as a warning, and error paths are expected.
        if not connection.closed and connection.info.transaction_status in (
            TransactionStatus.INTRANS,
            TransactionStatus.INERROR,
        ):
            try:
                connection.rollback()
            except psycopg.Error:
                pass  # A broken connection is discarded by putconn below.
        try:
            self._pool.putconn(connection)
        finally:
            self._count_lent(-1)

    @contextmanager
    def connection(self, timeout_seconds: float | None = None) -> Iterator[DbConnection]:
        with self._pool.connection(timeout=timeout_seconds) as connection:
            self._count_lent(1)
            try:
                yield connection
            finally:
                self._count_lent(-1)

    def _count_lent(self, change: int) -> None:
        with self._lent_lock:
            self._lent += change

    def close(self) -> None:
        self._pool.close()


def _settled(configure: Callable[[DbConnection], None]) -> Callable[[DbConnection], None]:
    """Run `configure`, then commit what it began: the pool accepts only idle connections."""

    def run(connection: DbConnection) -> None:
        configure(connection)
        if connection.info.transaction_status == TransactionStatus.INTRANS:
            connection.commit()

    return run
