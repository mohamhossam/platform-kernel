"""The pool carries the name its application gives it, and no application's name by default."""

from __future__ import annotations

from smb_kernel.persistence.connector import PooledPostgresConnector

DATABASE_URL = "postgresql://nobody@localhost:1/none"


def _pool(**kwargs: str) -> PooledPostgresConnector:
    # Never opened, so nothing connects.
    return PooledPostgresConnector(
        DATABASE_URL,
        min_size=1,
        max_size=1,
        acquire_timeout_seconds=1,
        max_idle_seconds=60,
        **kwargs,
    )


def test_the_application_names_its_pool() -> None:
    assert _pool(name="knowledge-portal")._pool.name == "knowledge-portal"


def test_an_unnamed_pool_takes_a_neutral_name() -> None:
    assert _pool()._pool.name.startswith("pool-")
